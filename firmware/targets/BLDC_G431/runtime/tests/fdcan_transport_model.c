#include "can_frame_port.h"
#include "fdcan.h"
#include "main.h"
#include <stddef.h>

/* 适配器与HAL均为正式源；只替代硬件寄存器、时钟与C库复制。 */
void __aeabi_memcpy(void *to, const void *from, size_t n)
{ volatile uint8_t *d = to; const volatile uint8_t *s = from; while (n-- != 0U) { *d++ = *s++; } }
void __aeabi_memcpy4(void *to, const void *from, size_t n) { __aeabi_memcpy(to, from, n); }
void __aeabi_memclr(void *to, size_t n)
{ volatile uint8_t *d = to; while (n-- != 0U) { *d++ = 0U; } }
void __aeabi_memclr4(void *to, size_t n) { __aeabi_memclr(to, n); }
void __aeabi_memset(void *to, size_t n, int value)
{ volatile uint8_t *d = to; while (n-- != 0U) { *d++ = (uint8_t)value; } }
void __aeabi_memset4(void *to, size_t n, int value) { __aeabi_memset(to, n, value); }

FDCAN_HandleTypeDef hfdcan1;
uint32_t SystemCoreClock = 170000000U;
volatile uint32_t model_ms, model_events, model_irq_events, model_asserts;
CanFrame model_rx, model_tx;
CanFrameStatus model_status;
uint32_t HAL_GetTick(void) { return model_ms; }
void assert_failed(uint8_t *file, uint32_t line) { (void)file; (void)line; model_asserts++; }
static void event(void *context, uint32_t events, bool irq)
{ (void)context; model_events |= events; if (irq) { model_irq_events++; } }
void model_prepare(void)
{
    hfdcan1.Instance = FDCAN1; hfdcan1.State = HAL_FDCAN_STATE_READY;
    hfdcan1.Init.ClockDivider = FDCAN_CLOCK_DIV1; hfdcan1.Init.FrameFormat = FDCAN_FRAME_CLASSIC;
    hfdcan1.Init.Mode = FDCAN_MODE_NORMAL; hfdcan1.Init.AutoRetransmission = ENABLE;
    hfdcan1.Init.NominalPrescaler = 17U; hfdcan1.Init.NominalSyncJumpWidth = 1U;
    hfdcan1.Init.NominalTimeSeg1 = 15U; hfdcan1.Init.NominalTimeSeg2 = 4U;
    hfdcan1.Init.StdFiltersNbr = 3U; hfdcan1.Init.ExtFiltersNbr = 0U;
    hfdcan1.msgRam.StandardFilterSA = SRAMCAN_BASE;
    hfdcan1.msgRam.RxFIFO0SA = SRAMCAN_BASE + 176U;
    hfdcan1.msgRam.TxFIFOQSA = SRAMCAN_BASE + 632U;
    FDCAN1->CCCR = FDCAN_CCCR_INIT | FDCAN_CCCR_CCE;
    FDCAN1->RXGFC = 3U << FDCAN_RXGFC_LSS_Pos;
    FDCAN1->TXFQS = 3U;
    model_tx.id = 0x181U; model_tx.dlc = 8U;
}
uint32_t model_init(void) { return can_frame_init(); }
uint32_t model_bind(void) { return can_frame_bind_event_sink(event, NULL); }
uint32_t model_start(void) { return can_frame_start(); }
uint32_t model_pop(void) { return can_frame_pop(&model_rx); }
uint32_t model_send(void) { return can_frame_try_send(&model_tx); }
uint32_t model_status_read(void) { return can_frame_read_status(&model_status); }
void model_irq(void) { HAL_FDCAN_IRQHandler(&hfdcan1); }
uint32_t model_recover(void) { return can_frame_request_recovery(); }
void model_bad_init(uint32_t kind)
{
    switch (kind) {
    case 0: hfdcan1.Init.NominalPrescaler = 16U; break;
    case 1: hfdcan1.Init.NominalTimeSeg1 = 14U; break;
    case 2: hfdcan1.Init.Mode = FDCAN_MODE_INTERNAL_LOOPBACK; break;
    case 3: hfdcan1.Init.AutoRetransmission = DISABLE; break;
    case 4: hfdcan1.Init.StdFiltersNbr = 0U; break;
    default: hfdcan1.Init.FrameFormat = FDCAN_FRAME_FD_NO_BRS; break;
    }
}
/* 用实际CMSIS/ARM布局导出寄存器与bool偏移；Python不复制HAL结构体布局。 */
const uint32_t model_layout[] = {
    FDCAN1_BASE, SRAMCAN_BASE, FDCAN1_IT0_IRQn,
    offsetof(FDCAN_GlobalTypeDef, CCCR), offsetof(FDCAN_GlobalTypeDef, PSR),
    offsetof(FDCAN_GlobalTypeDef, IR), offsetof(FDCAN_GlobalTypeDef, IE), offsetof(FDCAN_GlobalTypeDef, ILS),
    offsetof(FDCAN_GlobalTypeDef, ILE), offsetof(FDCAN_GlobalTypeDef, RXGFC),
    offsetof(FDCAN_GlobalTypeDef, RXF0S), offsetof(FDCAN_GlobalTypeDef, RXF0A),
    offsetof(FDCAN_GlobalTypeDef, TXFQS), offsetof(FDCAN_GlobalTypeDef, TXBRP),
    offsetof(FDCAN_GlobalTypeDef, TXBAR), offsetof(FDCAN_GlobalTypeDef, TXBCR), offsetof(FDCAN_GlobalTypeDef, TXBTO),
    FDCAN_IR_RF0N, FDCAN_IR_RF0L, FDCAN_IR_TC, FDCAN_IR_BO, FDCAN_IR_EW, FDCAN_IR_EP, FDCAN_IR_MRAF,
    sizeof(CanFrame), offsetof(CanFrame, data),
    offsetof(CanFrameStatus, rx_frames), offsetof(CanFrameStatus, tx_frames), offsetof(CanFrameStatus, rx_lost),
    offsetof(CanFrameStatus, tx_errors), offsetof(CanFrameStatus, bus_off_count),
    offsetof(CanFrameStatus, started), offsetof(CanFrameStatus, tx_in_flight),
    offsetof(CanFrameStatus, error_warning), offsetof(CanFrameStatus, error_passive),
    offsetof(CanFrameStatus, bus_off), offsetof(CanFrameStatus, recovering)
};
