#include "position_observation.h"

#include <stddef.h>
#include <stdint.h>

#include "main.h"
#include "spi.h"

#include "position_observation_internal.h"
#include "control_time_source.h"

/* 当前 10 kHz 电流采样每 10 拍发起一次 SSI 采集，即 1 kHz。 */
#define POSITION_ACQUISITION_DIVIDER (10U)
#define POSITION_TRANSFER_TIMEOUT_TICKS (4U) /* ControlTime 每 tick 50 us，共 200 us。 */
#define POSITION_SPI_INTERRUPTS (SPI_CR2_TXEIE | SPI_CR2_RXNEIE | SPI_CR2_ERRIE)
#define POSITION_SPI_ERRORS (SPI_SR_OVR | SPI_SR_MODF | SPI_SR_FRE | SPI_SR_CRCERR)
#define POSITION_STATE_RECEIVING (0U)
#define POSITION_STATE_WAIT_IDLE (1U)
#define POSITION_STATE_PUBLISHING (2U)

#define MT6701_TRANSFER_SIZE_BYTES (4U)
#define MT6701_FRAME_TRAILING_BITS (7U)
#define MT6701_FRAME_MASK (0x00FFFFFFU)
#define MT6701_ANGLE_SHIFT (10U)
#define MT6701_STATUS_SHIFT (6U)
#define MT6701_STATUS_MASK (0x0FU)
#define MT6701_FIELD_STATUS_MASK (0x03U)
#define MT6701_LOSS_OF_TRACK_MASK (0x08U)
#define MT6701_CRC_MASK (0x3FU)
#define MT6701_CRC_DATA_BITS (18U)
#define MT6701_CRC_POLYNOMIAL_LOW (0x03U)
#define MT6701_ANGLE_COUNTS_PER_TURN (16384.0f)

typedef struct
{
    uint32_t transfer;
    uint32_t epoch;
    ControlTime observation_time;
} PositionCompletion;

static uint8_t s_rx_frame[MT6701_TRANSFER_SIZE_BYTES];
static PositionObservation s_publications[2];

static volatile uint32_t s_active_publication_index;
static volatile bool s_publication_available;
static volatile bool s_armed;
static volatile bool s_transfer_active;
static volatile bool s_referenced;
static volatile ControlTime s_transfer_observation_time;

static uint32_t s_acquisition_divider_count;
static uint32_t s_saved_cr1;
static uint32_t s_saved_cr2;
static uint32_t s_tx_count;
static uint32_t s_rx_count;
static volatile uint32_t s_transfer_epoch;
static volatile uint32_t s_transfer_state;
static volatile bool s_transport_faulted;

static void publish_invalid_observation(ControlTime observation_time);

static void restore_interrupts(uint32_t primask)
{
    if (primask == 0U) {
        __enable_irq();
    }
}

/* 此 Target 的运行期 SPI 完全由下述有界 backend 独占，HAL 只负责 CubeMX 初始化。
 * full-duplex 只写四个 dummy byte，SCK 由有限 TX 数据驱动；MOSI 不映射到引脚。 */
static void spi_reset_locked(void)
{
    HAL_GPIO_WritePin(SPI1_CSN_GPIO_Port, SPI1_CSN_Pin, GPIO_PIN_SET);
    hspi1.Instance->CR2 = s_saved_cr2;
    CLEAR_BIT(hspi1.Instance->CR1, SPI_CR1_SPE);
    __DSB();
    __HAL_RCC_SPI1_FORCE_RESET();
    __DSB();
    __HAL_RCC_SPI1_RELEASE_RESET();
    hspi1.Instance->CR1 = s_saved_cr1;
    hspi1.Instance->CR2 = s_saved_cr2;
    HAL_NVIC_ClearPendingIRQ(SPI1_IRQn);
}

/* transport 失败隔离至板复位；不会自动重试、恢复电机或修改 Motor 状态。 */
static void transport_fail_locked(void)
{
    ControlTime time = s_transfer_observation_time;

    spi_reset_locked();
    s_transport_faulted = true;
    s_transfer_epoch++;
    publish_invalid_observation(time);
    s_transfer_active = false;
}

/*
 * MT6701 SSI CRC 覆盖 angle + status 共 18 bit，按 MSB first 输入。
 * 生成多项式为 x^6 + x + 1；宽度固定为 6 bit。
 */
static uint8_t mt6701_crc6(uint32_t data)
{
    uint8_t crc = 0U;
    uint32_t bit_index;

    for (bit_index = MT6701_CRC_DATA_BITS; bit_index > 0U; bit_index--) {
        uint32_t data_bit = (data >> (bit_index - 1U)) & 1U;
        uint32_t crc_msb = (crc >> 5U) & 1U;
        uint32_t feedback = data_bit ^ crc_msb;

        crc = (uint8_t)((crc << 1U) & MT6701_CRC_MASK);
        if (feedback != 0U) {
            crc ^= MT6701_CRC_POLYNOMIAL_LOW;
        }
    }

    return crc;
}

/*
 * SPI 完成、ADC 尾部和 transport 失败的发布均在短临界区串行提交。
 * 先写 inactive slot，再切换 index；后台读取不会看到半写事务。
 */
static void publish_observation(const PositionObservation *observation)
{
    uint32_t next_index = s_active_publication_index ^ 1U;

    s_publications[next_index] = *observation;
    __DMB();

    s_active_publication_index = next_index;
    s_publication_available = true;
}

static void publish_invalid_observation(ControlTime observation_time)
{
    PositionObservation observation;

    observation.mechanical_position_turns = 0.0f;
    observation.observation_time = observation_time;
    observation.referenced = s_referenced;
    observation.acquisition_valid = false;

    publish_observation(&observation);
}

static bool frame_is_valid(uint32_t frame)
{
    uint8_t status = (uint8_t)((frame >> MT6701_STATUS_SHIFT) & MT6701_STATUS_MASK);
    uint8_t received_crc = (uint8_t)(frame & MT6701_CRC_MASK);
    uint8_t calculated_crc = mt6701_crc6(frame >> MT6701_STATUS_SHIFT);

    if (received_crc != calculated_crc) {
        return false;
    }

    if ((status & MT6701_FIELD_STATUS_MASK) != 0U) {
        return false;
    }

    if ((status & MT6701_LOSS_OF_TRACK_MASK) != 0U) {
        return false;
    }

    return true;
}

static uint32_t assemble_transfer(void)
{
    uint32_t transfer = ((uint32_t)s_rx_frame[0] << 24U)
        | ((uint32_t)s_rx_frame[1] << 16U)
        | ((uint32_t)s_rx_frame[2] << 8U)
        | (uint32_t)s_rx_frame[3];

    /*
     * SPI1 空闲高、首边沿采样：首个下降沿先采到一位空闲 DO，
     * 随后的上升沿才开始移出 D13。4 字节传输覆盖这一个前导位、
     * 完整 24 位 SSI 帧和 7 位尾部数据；CRC 只校验有效帧。
     */
    return transfer;
}

static bool transfer_timed_out(ControlTime now)
{
    return (now - s_transfer_observation_time) >= POSITION_TRANSFER_TIMEOUT_TICKS;
}

/* 只有确认四个 RX byte 和 BSY=0 后才调用。捕获完整事务，再在锁外校验 CRC。 */
static void capture_completion_locked(PositionCompletion *completion)
{
    hspi1.Instance->CR2 = s_saved_cr2;
    CLEAR_BIT(hspi1.Instance->CR1, SPI_CR1_SPE);
    __DSB();
    HAL_GPIO_WritePin(SPI1_CSN_GPIO_Port, SPI1_CSN_Pin, GPIO_PIN_SET);
    completion->transfer = assemble_transfer();
    completion->observation_time = s_transfer_observation_time;
    completion->epoch = s_transfer_epoch;
    s_transfer_state = POSITION_STATE_PUBLISHING;
}

static void publish_completion(const PositionCompletion *completion)
{
    PositionObservation observation;
    uint32_t frame = (completion->transfer >> MT6701_FRAME_TRAILING_BITS) & MT6701_FRAME_MASK;
    bool valid = frame_is_valid(frame);
    uint32_t primask;

    observation.mechanical_position_turns =
        (float)(frame >> MT6701_ANGLE_SHIFT) / MT6701_ANGLE_COUNTS_PER_TURN;
    observation.observation_time = completion->observation_time;
    observation.acquisition_valid = valid;

    primask = __get_PRIMASK();
    __disable_irq();
    /* ADC 可以在 CRC 计算时抢占并退休超时事务，旧局部快照不能重新发布。 */
    if (s_transfer_active && !s_transport_faulted
        && (completion->epoch == s_transfer_epoch)
        && (s_transfer_state == POSITION_STATE_PUBLISHING)) {
        if (transfer_timed_out(control_time_source_now())) {
            transport_fail_locked();
        } else {
            if (valid) {
                s_referenced = true;
            }
            observation.referenced = s_referenced;
            publish_observation(&observation);
            /* 完整 publication 提交后再释放事务；新请求不得借用旧 buffer/time。 */
            s_transfer_active = false;
        }
    }
    restore_interrupts(primask);
}

PositionTopology position_observation_get_topology(void)
{
    return POSITION_TOPOLOGY_PERIODIC;
}

bool position_observation_arm(void)
{
    if (s_armed || s_transfer_active || (hspi1.Instance != SPI1)
        || (HAL_SPI_GetState(&hspi1) != HAL_SPI_STATE_READY)
        || (hspi1.Init.Direction != SPI_DIRECTION_2LINES_RXONLY)
        || ((hspi1.Instance->CR1 & (SPI_CR1_BIDIMODE | SPI_CR1_CRCEN
            | SPI_CR1_LSBFIRST | SPI_CR1_CPHA | SPI_CR1_SPE)) != 0U)
        || ((hspi1.Instance->CR1 & (SPI_CR1_MSTR | SPI_CR1_SSM | SPI_CR1_SSI | SPI_CR1_CPOL
            | SPI_CR1_RXONLY | SPI_CR1_BR))
            != (SPI_CR1_MSTR | SPI_CR1_SSM | SPI_CR1_SSI | SPI_CR1_CPOL
                | SPI_CR1_RXONLY | SPI_BAUDRATEPRESCALER_16))
        || ((hspi1.Instance->CR2 & (SPI_CR2_DS | SPI_CR2_FRXTH | SPI_CR2_FRF))
            != (SPI_DATASIZE_8BIT | SPI_CR2_FRXTH))) {
        return false;
    }

    /* CubeMX 保留 RXONLY 的 SCK/MISO 引脚配置：full-duplex 静态模式会强制路由
     * MOSI，与现有采样/LED 引脚冲突。这里只在 SPE=0 的 arm 边界取消 RXONLY，
     * 作为有限事务的内部时钟控制；不改变 Init、不路由 MOSI、不修改 GPIO。
     * 随后的四个 dummy TX byte 仅产生 32 个 SCK，避免主机 RXONLY 自动续时钟。 */
    s_saved_cr1 = hspi1.Instance->CR1 & ~SPI_CR1_RXONLY;
    s_saved_cr2 = hspi1.Instance->CR2
        & ~(POSITION_SPI_INTERRUPTS | SPI_CR2_TXDMAEN | SPI_CR2_RXDMAEN);
    spi_reset_locked();
    s_active_publication_index = 0U;
    s_publication_available = false;
    s_transfer_active = false;
    s_transport_faulted = false;
    s_referenced = false;
    s_transfer_observation_time = 0U;
    s_transfer_epoch = 0U;
    s_acquisition_divider_count = 0U;
    s_armed = true;
    return true;
}

/*
 * 后台读取时用极短临界区阻止 SPI writer 抢占；fast ISR 本身优先级高于 SPI IRQ。
 * 与双缓冲配合后，读取不需要自旋或等待 writer 完成。
 */
bool position_observation_get_latest(PositionObservation *out_observation)
{
    uint32_t primask;
    uint32_t index;

    if (out_observation == NULL) {
        return false;
    }

    primask = __get_PRIMASK();
    __disable_irq();

    if (!s_publication_available) {
        if (primask == 0U) {
            __enable_irq();
        }
        return false;
    }

    index = s_active_publication_index;
    *out_observation = s_publications[index];

    if (primask == 0U) {
        __enable_irq();
    }

    return true;
}

void position_observation_fast_tick(ControlTime fast_tick_time)
{
    uint32_t primask;
    uint32_t status;
    PositionCompletion completion;
    bool complete = false;

    if (!s_armed) {
        return;
    }

    primask = __get_PRIMASK();
    __disable_irq();
    if (s_transfer_active) {
        status = hspi1.Instance->SR;
        if (transfer_timed_out(fast_tick_time) || ((status & POSITION_SPI_ERRORS) != 0U)) {
            transport_fail_locked();
        } else if ((s_transfer_state == POSITION_STATE_WAIT_IDLE)
            && ((status & SPI_SR_BSY) == 0U)) {
            capture_completion_locked(&completion);
            complete = true;
        }
    }
    s_acquisition_divider_count++;
    if (s_acquisition_divider_count >= POSITION_ACQUISITION_DIVIDER) {
        s_acquisition_divider_count = 0U;
        if (!s_transfer_active && !s_transport_faulted) {
            s_transfer_observation_time = fast_tick_time;
            s_tx_count = 0U;
            s_rx_count = 0U;
            s_transfer_epoch++;
            s_transfer_state = POSITION_STATE_RECEIVING;
            s_transfer_active = true;
            HAL_GPIO_WritePin(SPI1_CSN_GPIO_Port, SPI1_CSN_Pin, GPIO_PIN_RESET);
            SET_BIT(hspi1.Instance->CR1, SPI_CR1_SPE);
            /* 空 FIFO 先由 TXE 请求服务，首个 DR 写发生在 SPI IRQ 获得服务之后。
             * 高优先级 ADC 的剩余工作不会被提前启动的 RX-only 时钟追赶。 */
            hspi1.Instance->CR2 = s_saved_cr2 | POSITION_SPI_INTERRUPTS;
        }
    }
    restore_interrupts(primask);
    if (complete) {
        publish_completion(&completion);
    }
}

void position_observation_irq(void)
{
    uint32_t primask = __get_PRIMASK();
    uint32_t index;
    uint32_t status;
    PositionCompletion completion;
    bool complete = false;

    __disable_irq();
    if (!s_armed || !s_transfer_active || s_transport_faulted) {
        if (s_armed) {
            hspi1.Instance->CR2 = s_saved_cr2;
        }
        restore_interrupts(primask);
        return;
    }
    if (transfer_timed_out(control_time_source_now())) {
        transport_fail_locked();
        restore_interrupts(primask);
        return;
    }
    /* 固定最多四次 RX read / 四次 TX write；没有 FIFO、BSY 或 HAL tick 忙等。 */
    for (index = 0U; index < MT6701_TRANSFER_SIZE_BYTES; index++) {
        status = hspi1.Instance->SR;
        if ((status & POSITION_SPI_ERRORS) != 0U) {
            transport_fail_locked();
            restore_interrupts(primask);
            return;
        }
        if (((status & SPI_SR_RXNE) != 0U) && (s_rx_count < MT6701_TRANSFER_SIZE_BYTES)) {
            s_rx_frame[s_rx_count] = *((__IO uint8_t *)&hspi1.Instance->DR);
            s_rx_count++;
        }
        if (((status & SPI_SR_TXE) != 0U) && (s_tx_count < MT6701_TRANSFER_SIZE_BYTES)) {
            *((__IO uint8_t *)&hspi1.Instance->DR) = 0U;
            s_tx_count++;
        }
    }
    if (s_tx_count == MT6701_TRANSFER_SIZE_BYTES) {
        CLEAR_BIT(hspi1.Instance->CR2, SPI_CR2_TXEIE);
    }
    status = hspi1.Instance->SR;
    if ((status & POSITION_SPI_ERRORS) != 0U) {
        transport_fail_locked();
    } else if (s_rx_count == MT6701_TRANSFER_SIZE_BYTES) {
        /* 最后 RXNE 早于 BSY 释放时不轮询，由下一 fast tick 有界确认。 */
        hspi1.Instance->CR2 = s_saved_cr2;
        if ((s_tx_count != MT6701_TRANSFER_SIZE_BYTES) || ((status & SPI_SR_RXNE) != 0U)) {
            transport_fail_locked();
        } else if ((status & SPI_SR_BSY) != 0U) {
            s_transfer_state = POSITION_STATE_WAIT_IDLE;
        } else {
            capture_completion_locked(&completion);
            complete = true;
        }
    }
    restore_interrupts(primask);
    if (complete) {
        publish_completion(&completion);
    }
}
