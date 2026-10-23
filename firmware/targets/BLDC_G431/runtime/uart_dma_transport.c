#include "target_communication_config.h"
#if BLDC_G431_COMMUNICATION == BLDC_G431_COMMUNICATION_UART_V1
#include "byte_stream_transport.h"
#include "uart_dma_transport.h"
#include "main.h"
#include "usart.h"

#include <string.h>

#define RX_SIZE 512U
#define TX_SIZE 130U
#define RX_FLAGS (DMA_ISR_HTIF2 | DMA_ISR_TCIF2 | DMA_ISR_TEIF2)
#define UART_ERRORS (USART_ISR_PE | USART_ISR_FE | USART_ISR_NE | USART_ISR_ORE)
#define PROGRESS_MAX_CYCLES (8U * 170000U)
#define TX_MAX_CYCLES (10U * 170000U)
enum { TX_IDLE, TX_COPYING, TX_DMA, TX_UART };

static uint8_t s_rx[RX_SIZE], s_tx[TX_SIZE];
static ByteStreamStats s_stats;
static ByteStreamEventSink s_sink;
static void *s_context;
static bool s_initialized, s_failed, s_discontinuous;
static uint32_t s_last_position, s_last_cycles, s_last_ms, s_origin;
static uint32_t s_tx_state, s_tx_length, s_tx_cycles;

/* 只屏蔽UART/后台优先级；0..4的控制与保护仍可抢占，复制在临界区外。 */
static uint32_t lock(void)
{
    uint32_t saved = __get_BASEPRI();
    __set_BASEPRI_MAX(0x50U); __DSB(); __ISB();
    return saved;
}
static void unlock(uint32_t saved) { __DMB(); __set_BASEPRI(saved); __ISB(); }
static void emit(uint32_t events, bool irq)
{
    if (events != 0U && s_sink != NULL) { s_sink(s_context, events, irq); }
}

static uint32_t fail_locked(bool timeout)
{
    if (s_failed) { return 0U; }
    s_failed = true;
    /* 内部失败不再接收；同时撤掉IRQ源，防止锁存后HT/TC/IDLE不断重入。 */
    USART1->CR3 &= ~(USART_CR3_DMAT | USART_CR3_DMAR | USART_CR3_EIE);
    USART1->CR1 &= ~(USART_CR1_TCIE | USART_CR1_IDLEIE | USART_CR1_PEIE);
    DMA1_Channel2->CCR &= ~(DMA_CCR_EN | DMA_CCR_HTIE | DMA_CCR_TCIE | DMA_CCR_TEIE);
    DMA1_Channel3->CCR &= ~(DMA_CCR_EN | DMA_CCR_HTIE | DMA_CCR_TCIE | DMA_CCR_TEIE);
    DMA1->IFCR = DMA_IFCR_CGIF2 | DMA_IFCR_CGIF3;
    s_tx_state = TX_IDLE; s_stats.tx_in_flight = false; s_stats.tx_errors++;
    g431_uart_transport_fail(timeout);
    return BYTE_STREAM_TX_ERROR;
}

static uint32_t discontinuity_locked(void)
{
    s_stats.continuity_epoch++;
    s_stats.rx_discontinuities++;
    s_stats.consumed_bytes = s_stats.produced_bytes;
    s_discontinuous = true;
    return BYTE_STREAM_RX_DISCONTINUITY;
}

static uint32_t crossed_flags(uint32_t previous, uint32_t delta)
{
    uint32_t end = previous + delta, flags = 0U;
    if ((previous < 256U && end >= 256U) || end >= 768U) { flags |= DMA_ISR_HTIF2; }
    if (end >= 512U) { flags |= DMA_ISR_TCIF2; }
    return flags;
}

/* HT/TC只为边界证据，不按IRQ次数累计字节。8ms小于512字节填满时间；
 * 超时或旗标不一致时不能排除整圈覆盖，退役未知段并提升epoch，不交付猜测数据。
 * 两个独立时钟共同核对间隔；计数只表示可观察到的DMA推进，不伪造未知丢字节数。 */
static uint32_t progress_locked(void)
{
    uint32_t before = 0U, after = 0U, remaining = 0U, cycles = 0U, ms = 0U;
    uint32_t position, delta, attempt, events = 0U;
    bool coherent = false;
    if (!s_stats.rx_started || s_failed) { return 0U; }
    for (attempt = 0U; attempt < 3U; ++attempt) {
        before = DMA1->ISR & RX_FLAGS;
        remaining = DMA1_Channel2->CNDTR;
        cycles = DWT->CYCCNT; ms = HAL_GetTick();
        after = DMA1->ISR & RX_FLAGS;
        if ((after & DMA_ISR_TEIF2) != 0U || remaining > RX_SIZE
            || (DMA1_Channel2->CCR & DMA_CCR_EN) == 0U) { return fail_locked(false); }
        if (before != after || remaining == 0U) { continue; }
        position = RX_SIZE - remaining;
        delta = (position - s_last_position) & (RX_SIZE - 1U);
        if (crossed_flags(s_last_position, delta) == (after & (DMA_ISR_HTIF2 | DMA_ISR_TCIF2))) {
            coherent = true; break;
        }
    }
    position = (RX_SIZE - remaining) & (RX_SIZE - 1U);
    delta = (position - s_last_position) & (RX_SIZE - 1U);
    if ((uint32_t)(cycles - s_last_cycles) >= PROGRESS_MAX_CYCLES
        || (uint32_t)(ms - s_last_ms) > 8U
        /* 约束异常停钟/调试暂停后恢复；允许寄存器采样被控制IRQ抢占的8字节余量。 */
        || (delta > 8U && (uint32_t)(cycles - s_last_cycles) < (delta - 8U) * 3300U)) {
        coherent = false;
    }
    DMA1->IFCR = after & (DMA_IFCR_CHTIF2 | DMA_IFCR_CTCIF2);
    s_last_position = position; s_last_cycles = cycles; s_last_ms = ms;
    s_stats.produced_bytes += delta; s_stats.rx_bytes += delta;
    if (!coherent) { return discontinuity_locked(); }
    if ((uint32_t)(s_stats.produced_bytes - s_stats.consumed_bytes) > RX_SIZE) {
        s_stats.buffer_overruns++;
        events |= discontinuity_locked();
    } else if (delta != 0U) { events |= BYTE_STREAM_RX_AVAILABLE; }
    return events;
}

static uint32_t tx_poll_locked(void)
{
    if ((s_tx_state == TX_DMA || s_tx_state == TX_UART)
        && (uint32_t)(DWT->CYCCNT - s_tx_cycles) >= TX_MAX_CYCLES) {
        return fail_locked(true);
    }
    return 0U;
}

ByteStreamResult byte_stream_init(void)
{
    uint32_t saved = lock();
    if (s_initialized) { unlock(saved); return BYTE_STREAM_BUSY; }
    if (SystemCoreClock != 170000000U || (DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) == 0U
        || huart1.Init.BaudRate != 460800U || USART1->BRR != 369U
        || (USART1->CR1 & (USART_CR1_UE | USART_CR1_RE | USART_CR1_TE | USART_CR1_PCE
            | USART_CR1_M0 | USART_CR1_M1 | USART_CR1_OVER8 | USART_CR1_FIFOEN))
            != (USART_CR1_UE | USART_CR1_RE | USART_CR1_TE)
        || (USART1->CR2 & USART_CR2_STOP) != 0U
        || (USART1->CR3 & (USART_CR3_RTSE | USART_CR3_CTSE)) != 0U
        || NVIC_GetPriority(USART1_IRQn) != 5U || NVIC_GetPriority(DMA1_Channel2_IRQn) != 5U
        || NVIC_GetPriority(DMA1_Channel3_IRQn) != 5U
        || NVIC_GetEnableIRQ(USART1_IRQn) == 0U || NVIC_GetEnableIRQ(DMA1_Channel2_IRQn) == 0U
        || NVIC_GetEnableIRQ(DMA1_Channel3_IRQn) == 0U
        || (DMA1_Channel2->CCR & (DMA_CCR_EN | DMA_CCR_DIR | DMA_CCR_PINC | DMA_CCR_PSIZE
            | DMA_CCR_MSIZE | DMA_CCR_CIRC | DMA_CCR_MINC)) != (DMA_CCR_CIRC | DMA_CCR_MINC)
        || (DMA1_Channel3->CCR & (DMA_CCR_EN | DMA_CCR_DIR | DMA_CCR_PINC | DMA_CCR_PSIZE
            | DMA_CCR_MSIZE | DMA_CCR_CIRC | DMA_CCR_MINC)) != (DMA_CCR_DIR | DMA_CCR_MINC)
        || (DMAMUX1_Channel1->CCR & DMAMUX_CxCR_DMAREQ_ID) != DMA_REQUEST_USART1_RX
        || (DMAMUX1_Channel2->CCR & DMAMUX_CxCR_DMAREQ_ID) != DMA_REQUEST_USART1_TX) {
        unlock(saved); return BYTE_STREAM_HARDWARE_ERROR;
    }
    memset(&s_stats, 0, sizeof(s_stats));
    s_initialized = true;
    USART1->CR1 &= ~(USART_CR1_IDLEIE | USART_CR1_PEIE | USART_CR1_TCIE);
    USART1->CR3 &= ~(USART_CR3_EIE | USART_CR3_DMAR | USART_CR3_DMAT);
    DMA1->IFCR = DMA_IFCR_CGIF2 | DMA_IFCR_CGIF3;
    unlock(saved); return BYTE_STREAM_OK;
}

ByteStreamResult byte_stream_bind_event_sink(ByteStreamEventSink sink, void *context)
{
    uint32_t saved;
    if (sink == NULL) { return BYTE_STREAM_BAD_ARGUMENT; }
    saved = lock();
    if (!s_initialized) { unlock(saved); return BYTE_STREAM_NOT_STARTED; }
    if (s_sink != NULL || s_stats.rx_started) { unlock(saved); return BYTE_STREAM_BUSY; }
    s_sink = sink; s_context = context;
    unlock(saved); return BYTE_STREAM_OK;
}

ByteStreamResult byte_stream_start_rx(void)
{
    uint32_t saved = lock();
    if (!s_initialized || s_sink == NULL) { unlock(saved); return BYTE_STREAM_NOT_STARTED; }
    if (s_failed) { unlock(saved); return BYTE_STREAM_HARDWARE_ERROR; }
    if (s_stats.rx_started) { unlock(saved); return BYTE_STREAM_BUSY; }
    USART1->ICR = USART_ICR_IDLECF | USART_ICR_PECF | USART_ICR_FECF | USART_ICR_NECF | USART_ICR_ORECF;
    USART1->RQR = USART_RQR_RXFRQ;
    DMA1_Channel2->CPAR = (uint32_t)&USART1->RDR;
    DMA1_Channel2->CMAR = (uint32_t)s_rx;
    DMA1_Channel2->CNDTR = RX_SIZE;
    s_last_position = 0U; s_last_cycles = DWT->CYCCNT; s_last_ms = HAL_GetTick();
    s_origin = s_stats.produced_bytes;
    s_stats.rx_started = true;
    DMA1_Channel2->CCR |= DMA_CCR_HTIE | DMA_CCR_TCIE | DMA_CCR_TEIE | DMA_CCR_EN;
    USART1->CR3 |= USART_CR3_DMAR | USART_CR3_EIE;
    USART1->CR1 |= USART_CR1_IDLEIE | USART_CR1_PEIE;
    unlock(saved); return BYTE_STREAM_OK;
}

ByteStreamResult byte_stream_read(uint8_t *destination, size_t capacity,
    size_t *bytes_read, uint32_t *continuity_epoch)
{
    uint32_t saved, events, epoch, consumed, available, index, count, first;
    ByteStreamResult result;
    if (destination == NULL || bytes_read == NULL || continuity_epoch == NULL || capacity == 0U) {
        return BYTE_STREAM_BAD_ARGUMENT;
    }
    *bytes_read = 0U;
    saved = lock();
    if (!s_stats.rx_started) { unlock(saved); return BYTE_STREAM_NOT_STARTED; }
    events = progress_locked(); events |= tx_poll_locked();
    epoch = s_stats.continuity_epoch; *continuity_epoch = epoch;
    if (s_failed || s_discontinuous) {
        s_discontinuous = false;
        result = s_failed ? BYTE_STREAM_HARDWARE_ERROR : BYTE_STREAM_DISCONTINUITY;
        unlock(saved); emit(events, false); return result;
    }
    consumed = s_stats.consumed_bytes;
    available = s_stats.produced_bytes - consumed;
    count = available < capacity ? available : (uint32_t)capacity;
    index = (consumed - s_origin) & (RX_SIZE - 1U);
    unlock(saved); emit(events, false);
    if (count == 0U) { return BYTE_STREAM_EMPTY; }
    first = count < RX_SIZE - index ? count : RX_SIZE - index;
    __DMB();
    memcpy(destination, s_rx + index, first);
    if (count != first) { memcpy(destination + first, s_rx, count - first); }
    __DMB();
    saved = lock();
    events = progress_locked(); events |= tx_poll_locked();
    *continuity_epoch = s_stats.continuity_epoch;
    if (s_failed || epoch != s_stats.continuity_epoch || s_discontinuous) {
        s_discontinuous = false;
        result = s_failed ? BYTE_STREAM_HARDWARE_ERROR : BYTE_STREAM_DISCONTINUITY;
    } else {
        s_stats.consumed_bytes = consumed + count;
        *bytes_read = count; result = BYTE_STREAM_OK;
    }
    unlock(saved); emit(events, false); return result;
}

ByteStreamResult byte_stream_try_send(const uint8_t *frame, size_t length)
{
    uint32_t saved, events;
    if (frame == NULL || length == 0U) { return BYTE_STREAM_BAD_ARGUMENT; }
    if (length > TX_SIZE) { return BYTE_STREAM_TOO_LARGE; }
    saved = lock();
    if (!s_stats.rx_started) { unlock(saved); return BYTE_STREAM_NOT_STARTED; }
    events = tx_poll_locked();
    if (s_failed) { unlock(saved); emit(events, false); return BYTE_STREAM_HARDWARE_ERROR; }
    if (s_tx_state != TX_IDLE) {
        s_stats.tx_busy_count++; unlock(saved); return BYTE_STREAM_BUSY;
    }
    if ((DMA1_Channel3->CCR & DMA_CCR_EN) != 0U) {
        events |= fail_locked(false); unlock(saved); emit(events, false); return BYTE_STREAM_HARDWARE_ERROR;
    }
    s_tx_state = TX_COPYING; s_stats.tx_in_flight = true;
    unlock(saved);
    memcpy(s_tx, frame, length);
    saved = lock();
    if (s_failed) { unlock(saved); return BYTE_STREAM_HARDWARE_ERROR; }
    s_tx_length = (uint32_t)length; s_tx_cycles = DWT->CYCCNT;
    USART1->ICR = USART_ICR_TCCF;
    DMA1->IFCR = DMA_IFCR_CGIF3;
    DMA1_Channel3->CPAR = (uint32_t)&USART1->TDR;
    DMA1_Channel3->CMAR = (uint32_t)s_tx;
    DMA1_Channel3->CNDTR = (uint32_t)length;
    s_tx_state = TX_DMA;
    __DMB();
    DMA1_Channel3->CCR |= DMA_CCR_TCIE | DMA_CCR_TEIE | DMA_CCR_EN;
    USART1->CR3 |= USART_CR3_DMAT;
    unlock(saved); return BYTE_STREAM_ACCEPTED;
}

bool byte_stream_read_stats(ByteStreamStats *stats)
{
    uint32_t saved;
    if (stats == NULL || !s_initialized) { return false; }
    saved = lock(); *stats = s_stats; unlock(saved); return true;
}

void g431_uart_rx_dma_irq(void)
{
    uint32_t saved = lock(), events = progress_locked();
    unlock(saved); emit(events, true);
}

void g431_uart_tx_dma_irq(void)
{
    uint32_t saved = lock(), flags = DMA1->ISR, events = tx_poll_locked();
    if (s_failed) { unlock(saved); emit(events, true); return; }
    if ((flags & DMA_ISR_TEIF3) != 0U) { events = fail_locked(false); }
    else if ((flags & DMA_ISR_TCIF3) != 0U) {
        if (s_tx_state != TX_DMA) { events = fail_locked(false); }
        else {
            DMA1_Channel3->CCR &= ~DMA_CCR_EN;
            USART1->CR3 &= ~USART_CR3_DMAT;
            DMA1->IFCR = DMA_IFCR_CTCIF3;
            s_tx_state = TX_UART;
            USART1->CR1 |= USART_CR1_TCIE;
        }
    }
    unlock(saved); emit(events, true);
}

static uint32_t recover_rx_locked(uint32_t errors)
{
    uint32_t attempts, events = progress_locked();
    if ((errors & USART_ISR_PE) != 0U) { s_stats.parity_errors++; }
    if ((errors & USART_ISR_FE) != 0U) { s_stats.framing_errors++; }
    if ((errors & USART_ISR_NE) != 0U) { s_stats.noise_errors++; }
    if ((errors & USART_ISR_ORE) != 0U) { s_stats.hardware_rx_overruns++; }
    USART1->CR3 &= ~USART_CR3_DMAR;
    DMA1_Channel2->CCR &= ~DMA_CCR_EN;
    /* 只等正在进行的总线事务，固定次数；不调用有毫秒等待的HAL Abort。 */
    for (attempts = 0U; attempts < 8U; ++attempts) {
        if ((DMA1_Channel2->CCR & DMA_CCR_EN) == 0U) { break; }
    }
    if (attempts == 8U || s_failed) { return events | fail_locked(false); }
    USART1->ICR = errors;
    USART1->RQR = USART_RQR_RXFRQ;
    DMA1->IFCR = DMA_IFCR_CGIF2;
    DMA1_Channel2->CNDTR = RX_SIZE;
    s_origin = s_stats.produced_bytes;
    s_last_position = 0U; s_last_cycles = DWT->CYCCNT; s_last_ms = HAL_GetTick();
    events |= discontinuity_locked();
    DMA1_Channel2->CCR |= DMA_CCR_EN;
    USART1->CR3 |= USART_CR3_DMAR;
    return events;
}

void g431_uart_irq(void)
{
    uint32_t saved = lock(), flags = USART1->ISR, events = 0U;
    if (!s_stats.rx_started || s_failed) { unlock(saved); return; }
    events |= tx_poll_locked();
    if (s_failed) { unlock(saved); emit(events, true); return; }
    if ((flags & UART_ERRORS) != 0U) { events |= recover_rx_locked(flags & UART_ERRORS); }
    if ((flags & USART_ISR_IDLE) != 0U) {
        USART1->ICR = USART_ICR_IDLECF;
        events |= progress_locked();
    }
    if ((flags & USART_ISR_TC) != 0U && (USART1->CR1 & USART_CR1_TCIE) != 0U) {
        if (s_tx_state != TX_UART) { events |= fail_locked(false); }
        else {
            USART1->CR1 &= ~USART_CR1_TCIE;
            USART1->ICR = USART_ICR_TCCF;
            s_tx_state = TX_IDLE; s_stats.tx_in_flight = false;
            s_stats.tx_bytes += s_tx_length;
            events |= BYTE_STREAM_TX_COMPLETE;
        }
    }
    events |= tx_poll_locked();
    unlock(saved); emit(events, true);
}

/* CubeMX全局DMA配置只开放采样IRQ；UART专属向量随选中链路一起链接。 */
void DMA1_Channel2_IRQHandler(void) { g431_uart_rx_dma_irq(); }
void DMA1_Channel3_IRQHandler(void) { g431_uart_tx_dma_irq(); }
#endif
