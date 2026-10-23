#include "target_communication_config.h"
#if BLDC_G431_COMMUNICATION == BLDC_G431_COMMUNICATION_CAN_V1
#include "can_frame_port.h"
#include "can_codec.h"
#include "main.h"
#include "fdcan.h"
#include <string.h>
#if BLDC_G431_CAN_LOOPBACK_VALIDATION
#include "../validation/communication/can_loopback.h"
#define EXPECTED_CAN_MODE FDCAN_MODE_INTERNAL_LOOPBACK
#else
#define EXPECTED_CAN_MODE FDCAN_MODE_NORMAL
#endif

#define CAN_TX_TIMEOUT_MS 100U
#define CAN_RECOVERY_TIMEOUT_MS 200U
#define CAN_FATAL_FLAGS (FDCAN_IR_MRAF | FDCAN_IR_WDI | FDCAN_IR_ARA)
#define CAN_NOTIFICATIONS (FDCAN_IT_RX_FIFO0_NEW_MESSAGE | FDCAN_IT_RX_FIFO0_MESSAGE_LOST \
    | FDCAN_IT_TX_COMPLETE | FDCAN_IT_ERROR_WARNING | FDCAN_IT_ERROR_PASSIVE \
    | FDCAN_IT_BUS_OFF | FDCAN_IT_RAM_ACCESS_FAILURE | FDCAN_IT_RAM_WATCHDOG \
    | FDCAN_IT_RESERVED_ADDRESS_ACCESS)

static volatile CanFrameStatus s_status;
static CanFrameEventSink s_sink;
static void *s_context;
static bool s_initialized, s_failed, s_recovery_started;
static uint32_t s_tx_buffer, s_tx_ms, s_recovery_ms;

/* 仅屏蔽通信/后台，0..4的控制和保护继续抢占。 */
static uint32_t lock(void)
{
    uint32_t saved = __get_BASEPRI();
    __set_BASEPRI_MAX(0x50U); __DSB(); __ISB(); return saved;
}
static void unlock(uint32_t saved) { __DMB(); __set_BASEPRI(saved); __ISB(); }
static void emit(uint32_t events, bool irq)
{ if (events != 0U && s_sink != NULL) { s_sink(s_context, events, irq); } }
static void fail_locked(void)
{
    if (!s_failed) { s_status.tx_errors++; }
    s_failed = true;
    FDCAN1->IE = 0U; FDCAN1->ILE = 0U;
    SET_BIT(FDCAN1->CCCR, FDCAN_CCCR_INIT);
}
static void tx_complete_locked(void)
{
    s_status.tx_frames++; s_status.tx_in_flight = false; s_tx_buffer = 0U;
}
static void refresh_locked(void)
{
    uint32_t psr = FDCAN1->PSR;
    s_status.error_warning = (psr & FDCAN_PSR_EW) != 0U;
    s_status.error_passive = (psr & FDCAN_PSR_EP) != 0U;
    /* Bus-off按事实锁存，只有明确的任务恢复流程能撤销；不因下一次读数消失而漏报。 */
    if ((psr & FDCAN_PSR_BO) != 0U && !s_status.bus_off) {
        s_status.bus_off = true; s_status.bus_off_count++;
    }
    if ((FDCAN1->IR & FDCAN_IR_RF0L) != 0U) {
        __HAL_FDCAN_CLEAR_FLAG(&hfdcan1, FDCAN_IR_RF0L); s_status.rx_lost++;
    }
    if ((FDCAN1->IR & CAN_FATAL_FLAGS) != 0U) { fail_locked(); }
    if (s_status.tx_in_flight && !s_status.bus_off && !s_status.recovering) {
        if ((FDCAN1->TXBRP & s_tx_buffer) == 0U && (FDCAN1->TXBTO & s_tx_buffer) != 0U) {
            tx_complete_locked();
        } else if ((uint32_t)(HAL_GetTick() - s_tx_ms) >= CAN_TX_TIMEOUT_MS) { fail_locked(); }
    }
}

CanFrameResult can_frame_init(void)
{
    FDCAN_FilterTypeDef filter = {0};
    const uint32_t ids[3] = {CAN_V1_BROADCAST_STOP, BLDC_G431_CAN_NODE_ID,
        CAN_V1_REQUEST_BASE + BLDC_G431_CAN_NODE_ID};
    uint32_t i;
    if (s_initialized) { return CAN_FRAME_BUSY; }
    if (SystemCoreClock != 170000000U || hfdcan1.State != HAL_FDCAN_STATE_READY
        || hfdcan1.Init.ClockDivider != FDCAN_CLOCK_DIV1 || hfdcan1.Init.FrameFormat != FDCAN_FRAME_CLASSIC
        || hfdcan1.Init.Mode != EXPECTED_CAN_MODE || hfdcan1.Init.AutoRetransmission != ENABLE
        || hfdcan1.Init.NominalPrescaler != 17U || hfdcan1.Init.NominalSyncJumpWidth != 1U
        || hfdcan1.Init.NominalTimeSeg1 != 15U || hfdcan1.Init.NominalTimeSeg2 != 4U
        || hfdcan1.Init.StdFiltersNbr != 3U || hfdcan1.Init.ExtFiltersNbr != 0U
        || NVIC_GetPriority(FDCAN1_IT0_IRQn) != 5U || NVIC_GetEnableIRQ(FDCAN1_IT0_IRQn) == 0U
        || NVIC_GetEnableIRQ(FDCAN1_IT1_IRQn) != 0U) { return CAN_FRAME_HARDWARE_ERROR; }
    /* G4 HAL固定分配每FIFO 3项、每项72字节；不用其他系列的可变Message RAM参数。 */
    filter.IdType = FDCAN_STANDARD_ID; filter.FilterType = FDCAN_FILTER_MASK;
    filter.FilterConfig = FDCAN_FILTER_TO_RXFIFO0; filter.FilterID2 = 0x7FFU;
    for (i = 0U; i < 3U; ++i) {
        filter.FilterIndex = i; filter.FilterID1 = ids[i];
        if (HAL_FDCAN_ConfigFilter(&hfdcan1, &filter) != HAL_OK) { return CAN_FRAME_HARDWARE_ERROR; }
    }
    if (HAL_FDCAN_ConfigGlobalFilter(&hfdcan1, FDCAN_REJECT, FDCAN_REJECT,
            FDCAN_REJECT_REMOTE, FDCAN_REJECT_REMOTE) != HAL_OK
        || HAL_FDCAN_ConfigRxFifoOverwrite(&hfdcan1, FDCAN_RX_FIFO0, FDCAN_RX_FIFO_BLOCKING) != HAL_OK
        || HAL_FDCAN_ConfigInterruptLines(&hfdcan1, FDCAN_ILS_MASK, FDCAN_INTERRUPT_LINE0) != HAL_OK) {
        return CAN_FRAME_HARDWARE_ERROR;
    }
    memset((void *)&s_status, 0, sizeof(s_status)); s_initialized = true;
    return CAN_FRAME_ACCEPTED;
}

CanFrameResult can_frame_bind_event_sink(CanFrameEventSink sink, void *context)
{
    if (sink == NULL) { return CAN_FRAME_BAD_ARGUMENT; }
    if (!s_initialized) { return CAN_FRAME_NOT_STARTED; }
    if (s_sink != NULL || s_status.started) { return CAN_FRAME_BUSY; }
    s_sink = sink; s_context = context; return CAN_FRAME_ACCEPTED;
}
CanFrameResult can_frame_start(void)
{
    if (!s_initialized || s_sink == NULL) { return CAN_FRAME_NOT_STARTED; }
    if (s_status.started) { return CAN_FRAME_BUSY; }
    if (HAL_FDCAN_ActivateNotification(&hfdcan1, CAN_NOTIFICATIONS, 7U) != HAL_OK
        || HAL_FDCAN_Start(&hfdcan1) != HAL_OK) { return CAN_FRAME_HARDWARE_ERROR; }
    s_status.started = true; return CAN_FRAME_ACCEPTED;
}
CanFrameResult can_frame_pop(CanFrame *frame)
{
    FDCAN_RxHeaderTypeDef header;
    uint8_t data[64] = {0}; /* HAL按DLC复制，先容纳最坏长度，再拒绝非Classic/DLC8。 */
    uint32_t saved;
    CanFrameResult result = CAN_FRAME_EMPTY;
    if (frame == NULL) { return CAN_FRAME_BAD_ARGUMENT; }
    if (!s_status.started) { return CAN_FRAME_NOT_STARTED; }
    saved = lock(); refresh_locked();
    if (s_failed) { result = CAN_FRAME_HARDWARE_ERROR; }
    else if (s_status.bus_off || s_status.recovering) { result = CAN_FRAME_BUSY; }
    else if (HAL_FDCAN_GetRxFifoFillLevel(&hfdcan1, FDCAN_RX_FIFO0) != 0U) {
        if (HAL_FDCAN_GetRxMessage(&hfdcan1, FDCAN_RX_FIFO0, &header, data) != HAL_OK) {
            fail_locked(); result = CAN_FRAME_HARDWARE_ERROR;
        } else {
            memset(frame, 0, sizeof(*frame));
            frame->id = (uint16_t)header.Identifier;
            frame->extended = header.IdType != FDCAN_STANDARD_ID;
            frame->remote = header.RxFrameType != FDCAN_DATA_FRAME;
            frame->dlc = header.FDFormat == FDCAN_CLASSIC_CAN && header.DataLength == FDCAN_DLC_BYTES_8 ? 8U : 0U;
            memcpy(frame->data, data, 8U); s_status.rx_frames++; result = CAN_FRAME_ACCEPTED;
        }
    }
    unlock(saved); return result;
}
CanFrameResult can_frame_try_send(const CanFrame *frame)
{
    FDCAN_TxHeaderTypeDef header = {0};
    CanFrameResult result;
    uint32_t saved;
    if (frame == NULL || frame->id > 0x7FFU || frame->dlc > 8U
#if !BLDC_G431_CAN_LOOPBACK_VALIDATION
        || frame->extended || frame->remote
#endif
        ) {
        return CAN_FRAME_BAD_ARGUMENT;
    }
    if (!s_status.started) { return CAN_FRAME_NOT_STARTED; }
    saved = lock(); refresh_locked();
    if (s_failed) { result = CAN_FRAME_HARDWARE_ERROR; }
    else if (s_status.tx_in_flight || s_status.bus_off || s_status.recovering) { result = CAN_FRAME_BUSY; }
    else {
        header.Identifier = frame->id; header.IdType = FDCAN_STANDARD_ID; header.TxFrameType = FDCAN_DATA_FRAME;
#if BLDC_G431_CAN_LOOPBACK_VALIDATION
        header.IdType = frame->extended ? FDCAN_EXTENDED_ID : FDCAN_STANDARD_ID;
        header.TxFrameType = frame->remote ? FDCAN_REMOTE_FRAME : FDCAN_DATA_FRAME;
#endif
        /* 当前CubeG4 1.6.3 HAL的DLC是0..15原值，不是旧版本的寄存器左移值。 */
        header.DataLength = frame->dlc; header.ErrorStateIndicator = FDCAN_ESI_ACTIVE;
        header.BitRateSwitch = FDCAN_BRS_OFF; header.FDFormat = FDCAN_CLASSIC_CAN;
        header.TxEventFifoControl = FDCAN_NO_TX_EVENTS;
        /* 只允许一个硬件在途，避免提前塞满硬件队列使后来的关键Response排在遥测后。 */
        if (HAL_FDCAN_AddMessageToTxFifoQ(&hfdcan1, &header, frame->data) != HAL_OK) {
            fail_locked(); result = CAN_FRAME_HARDWARE_ERROR;
        } else {
            s_tx_buffer = HAL_FDCAN_GetLatestTxFifoQRequestBuffer(&hfdcan1);
            s_tx_ms = HAL_GetTick(); s_status.tx_in_flight = true; result = CAN_FRAME_ACCEPTED;
        }
    }
    unlock(saved);
#if BLDC_G431_CAN_LOOPBACK_VALIDATION
    if (result == CAN_FRAME_ACCEPTED) { g431_can_loopback_tx_accepted(frame); }
#endif
    return result;
}
bool can_frame_read_status(CanFrameStatus *status)
{
    uint32_t saved;
    bool ok;
    if (status == NULL || !s_initialized) { return false; }
    saved = lock(); refresh_locked(); *status = s_status; ok = !s_failed;
    unlock(saved); return ok;
}
static bool discard_rx_locked(void)
{
    FDCAN_RxHeaderTypeDef header;
    uint8_t data[64];
    uint32_t i;
    for (i = 0U; i < 3U && HAL_FDCAN_GetRxFifoFillLevel(&hfdcan1, FDCAN_RX_FIFO0) != 0U; ++i) {
        if (HAL_FDCAN_GetRxMessage(&hfdcan1, FDCAN_RX_FIFO0, &header, data) != HAL_OK) { return false; }
    }
    return HAL_FDCAN_GetRxFifoFillLevel(&hfdcan1, FDCAN_RX_FIFO0) == 0U;
}
CanFrameResult can_frame_request_recovery(void)
{
    uint32_t saved;
    CanFrameResult result = CAN_FRAME_BUSY;
    if (!s_status.started) { return CAN_FRAME_NOT_STARTED; }
    saved = lock(); refresh_locked();
    if (s_failed || !s_status.bus_off) { result = CAN_FRAME_HARDWARE_ERROR; }
    else {
        if (!s_status.recovering) {
            s_status.recovering = true; s_recovery_ms = HAL_GetTick();
            if (HAL_FDCAN_AbortTxRequest(&hfdcan1, 7U) != HAL_OK) { fail_locked(); }
        }
        if ((uint32_t)(HAL_GetTick() - s_recovery_ms) >= CAN_RECOVERY_TIMEOUT_MS) { fail_locked(); }
        if (!s_failed && FDCAN1->TXBRP == 0U) {
            if (!discard_rx_locked()) { fail_locked(); }
            else if (!s_recovery_started) {
                /* RM0440：任务只请求退出INIT，128×11隐性位恢复由控制器完成。
                 * 不复位错误计数、不循环切INIT、不把一次寄存器写当作恢复成功。 */
                CLEAR_BIT(FDCAN1->CCCR, FDCAN_CCCR_INIT); s_recovery_started = true;
            } else if ((FDCAN1->PSR & FDCAN_PSR_BO) == 0U && (FDCAN1->CCCR & FDCAN_CCCR_INIT) == 0U) {
                s_status.bus_off = false; s_status.recovering = false; s_status.tx_in_flight = false;
                s_tx_buffer = 0U; s_recovery_started = false; result = CAN_FRAME_ACCEPTED;
            }
        }
        if (s_failed) { result = CAN_FRAME_HARDWARE_ERROR; }
    }
    unlock(saved); return result;
}

/* IRQ只记录硬件事实并通知；不解析协议，不调用Motor或Runtime提交。 */
void HAL_FDCAN_RxFifo0Callback(FDCAN_HandleTypeDef *handle, uint32_t events)
{
    uint32_t flags = CAN_FRAME_RX_AVAILABLE;
    if (handle != &hfdcan1) { return; }
    if ((events & FDCAN_IT_RX_FIFO0_MESSAGE_LOST) != 0U) { s_status.rx_lost++; flags |= CAN_FRAME_RX_LOST; }
#if BLDC_G431_CAN_LOOPBACK_VALIDATION
    g431_can_loopback_irq(CAN_FRAME_RX_AVAILABLE);
#endif
    emit(flags, true);
}
void HAL_FDCAN_TxBufferCompleteCallback(FDCAN_HandleTypeDef *handle, uint32_t buffers)
{
    if (handle != &hfdcan1) { return; }
    if (s_status.tx_in_flight && (buffers & s_tx_buffer) != 0U) { tx_complete_locked(); }
#if BLDC_G431_CAN_LOOPBACK_VALIDATION
    g431_can_loopback_irq(CAN_FRAME_TX_COMPLETE);
#endif
    emit(CAN_FRAME_TX_COMPLETE, true);
}
void HAL_FDCAN_ErrorStatusCallback(FDCAN_HandleTypeDef *handle, uint32_t events)
{
    if (handle != &hfdcan1) { return; }
    if ((events & FDCAN_IT_BUS_OFF) != 0U && (FDCAN1->PSR & FDCAN_PSR_BO) != 0U && !s_status.bus_off) {
        s_status.bus_off = true; s_status.bus_off_count++;
    }
    emit(CAN_FRAME_ERROR_CHANGED, true);
}
void HAL_FDCAN_ErrorCallback(FDCAN_HandleTypeDef *handle)
{
    if (handle != &hfdcan1) { return; }
    /* 只启用Message RAM/地址等内部硬件错误源，普通总线Warning/Passive单独处理。 */
    fail_locked(); emit(CAN_FRAME_ERROR_CHANGED, true);
}
#endif
