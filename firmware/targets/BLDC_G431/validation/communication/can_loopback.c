#include "target_communication_config.h"
#if BLDC_G431_CAN_LOOPBACK_VALIDATION
#include "can_loopback.h"
#if defined(BLDC_G431_FINITE_RUN)
#include "target_runtime.h"
#else
#include "target_observe_runtime.h"
#endif
#include "can_codec.h"
#include "fdcan.h"
#include "main.h"
#include "system_runtime.h"
#include <string.h>

#define TRACE_CAPACITY 128U
typedef struct {
    uint32_t id, metadata, data0, data1, milliseconds, sequence;
} CanLoopbackTrace;
typedef struct {
    uint32_t version, command_token, acknowledged_token, request_result;
    uint32_t request_id, request_flags, request_dlc, request_data0, request_data1;
    uint32_t ready, milliseconds, service_count, accepted_tx, completed_tx, rx_frames, rx_lost;
    uint32_t trace_count, motion_count, heartbeat_count, mode_register, test_register;
    uint32_t motor_state, motor_faults, guard_faults, internal_reason, target_bits, bus_bits;
    uint32_t moe, six_pin_levels, system_phase, communication_stack, telemetry_stack;
    uint32_t session, highest_sequence, ordinary_ticket, stop_ticket, rx_irq, tx_irq;
    CanLoopbackTrace trace[TRACE_CAPACITY];
} CanLoopbackProbe;

/* 所有字段u32；Host先写请求字段，最后提交非零token，任务只执行新token一次。 */
volatile CanLoopbackProbe g_can_loopback_probe = {.version = 0x43414E31U};

void g431_can_loopback_server_progress(const CanServer *server)
{
    g_can_loopback_probe.session = server->session;
    g_can_loopback_probe.highest_sequence = server->highest_sequence;
    if (server->ordinary.active) { g_can_loopback_probe.ordinary_ticket = server->ordinary.ticket; }
    if (server->stop.active) { g_can_loopback_probe.stop_ticket = server->stop.ticket; }
}
void g431_can_loopback_irq(uint32_t events)
{
    if ((events & CAN_FRAME_RX_AVAILABLE) != 0U) { g_can_loopback_probe.rx_irq++; }
    if ((events & CAN_FRAME_TX_COMPLETE) != 0U) { g_can_loopback_probe.tx_irq++; }
}

void g431_can_loopback_configure(void)
{
    if ((TIM1->BDTR & TIM_BDTR_MOE) != 0U) { Error_Handler(); }
    /* 正式IOC仍为Normal；此专项在控制器尚未Start时重设内部回环，禁止物理TX。 */
    hfdcan1.Init.Mode = FDCAN_MODE_INTERNAL_LOOPBACK;
    if (HAL_FDCAN_Init(&hfdcan1) != HAL_OK
        || (FDCAN1->CCCR & (FDCAN_CCCR_TEST | FDCAN_CCCR_MON)) != (FDCAN_CCCR_TEST | FDCAN_CCCR_MON)
        || (FDCAN1->TEST & FDCAN_TEST_LBCK) == 0U) { Error_Handler(); }
}

void g431_can_loopback_tx_accepted(const CanFrame *frame)
{
    volatile CanLoopbackProbe *p = &g_can_loopback_probe;
    volatile CanLoopbackTrace *entry;
    const volatile uint32_t *ram;
    uint32_t sequence, buffer = HAL_FDCAN_GetLatestTxFifoQRequestBuffer(&hfdcan1);
    p->accepted_tx++;
    /* 250Hz遥测仍真实发送；诊断只保存前8个Motion/4个Heartbeat，避免SWD读取速度改变工作点。 */
    if (frame->id == CAN_V1_MOTION_BASE + BLDC_G431_CAN_NODE_ID && ++p->motion_count > 8U) { return; }
    if (frame->id == CAN_V1_STATE_BASE + BLDC_G431_CAN_NODE_ID && ++p->heartbeat_count > 4U) { return; }
    sequence = p->trace_count + 1U; entry = &p->trace[(sequence - 1U) % TRACE_CAPACITY];
    /* 记录实际TX Message RAM；单在途/唯一任务使其在此处不会被下一帧覆盖。 */
    ram = (const volatile uint32_t *)(hfdcan1.msgRam.TxFIFOQSA + (buffer == 4U ? 2U : buffer == 2U ? 1U : 0U) * 72U);
    entry->id = (ram[0] & (1UL << 30U)) != 0U ? ram[0] & 0x1FFFFFFFU : (ram[0] >> 18U) & 0x7FFU;
    entry->metadata = ((ram[1] >> 16U) & 15U) | ((ram[0] & (1UL << 30U)) != 0U ? 0x100U : 0U)
        | ((ram[0] & (1UL << 29U)) != 0U ? 0x200U : 0U);
    entry->data0 = ram[2]; entry->data1 = ram[3];
    entry->milliseconds = HAL_GetTick(); entry->sequence = sequence;
    __DMB(); p->trace_count = sequence;
}

void g431_can_loopback_service(void)
{
    volatile CanLoopbackProbe *p = &g_can_loopback_probe;
    MotorSnapshot snapshot;
    RuntimeInternalFaultStatus fault;
    CanFrameStatus status;
    CanFrame frame = {0};
    uint32_t token = p->command_token;
    uint32_t pins = ((GPIOA->IDR >> 8U) & 7U) | (((GPIOB->IDR >> 13U) & 7U) << 3U);
    bool ready = target_runtime_read_snapshot(&snapshot) && snapshot.status_valid && snapshot.bus_voltage_valid;
    p->milliseconds = HAL_GetTick(); p->service_count++;
    p->mode_register = FDCAN1->CCCR; p->test_register = FDCAN1->TEST;
    p->moe = (TIM1->BDTR & TIM_BDTR_MOE) != 0U; p->six_pin_levels = pins;
    p->system_phase = g_system_runtime_status.phase;
    p->communication_stack = g_system_runtime_status.communication_stack_free;
    p->telemetry_stack = g_system_runtime_status.telemetry_stack_free;
    p->guard_faults = g_target_runtime_guard_faults;
    if (target_runtime_read_internal_fault(&fault)) { p->internal_reason = fault.first_reason; }
    else { ready = false; }
    if (can_frame_read_status(&status)) {
        p->completed_tx = status.tx_frames; p->rx_frames = status.rx_frames; p->rx_lost = status.rx_lost;
    } else { ready = false; }
    if (ready) {
        p->motor_state = snapshot.state; p->motor_faults = snapshot.faults;
        memcpy((void *)&p->target_bits, &snapshot.target_velocity_rpm, 4U);
        memcpy((void *)&p->bus_bits, &snapshot.bus_voltage_v, 4U);
        ready = snapshot.state == MOTOR_STATE_IDLE && snapshot.faults == 0U && snapshot.target_velocity_rpm == 0.0F
            && snapshot.bus_voltage_v >= 0.0F && snapshot.bus_voltage_v < 4.0F;
    }
    ready = ready && p->moe == 0U && pins == 0U && p->internal_reason == 0U && p->guard_faults == 0U
        && (FDCAN1->CCCR & (FDCAN_CCCR_TEST | FDCAN_CCCR_MON)) == (FDCAN_CCCR_TEST | FDCAN_CCCR_MON)
        && (FDCAN1->TEST & FDCAN_TEST_LBCK) != 0U;
    p->ready = ready;
    if (token == 0U || token == p->acknowledged_token) { return; }
    if (!ready) { p->request_result = CAN_FRAME_HARDWARE_ERROR; }
    else if (p->request_id > 0x7FFU || p->request_dlc > 8U || (p->request_flags & ~3U) != 0U) {
        p->request_result = CAN_FRAME_BAD_ARGUMENT;
    } else {
        frame.id = (uint16_t)p->request_id; frame.dlc = (uint8_t)p->request_dlc;
        frame.extended = (p->request_flags & 1U) != 0U; frame.remote = (p->request_flags & 2U) != 0U;
        memcpy(frame.data, (const void *)&p->request_data0, 4U); memcpy(frame.data + 4, (const void *)&p->request_data1, 4U);
        __DMB();
        if (token != p->command_token) { return; }
        p->request_result = can_frame_try_send(&frame);
    }
    __DMB(); p->acknowledged_token = token;
}
#endif
