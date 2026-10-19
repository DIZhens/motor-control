#include "target_runtime.h"

#include <float.h>
#include <stddef.h>
#include <string.h>

#include "main.h"
#include "tim.h"
#include "usart.h"
#include "motor.h"
#include "control_runtime.h"
#include "vofa.h"
#include "motor_vofa.h"
#include "realtime_motor_io.h"
#include "hardware_trip.h"
#include "position_observation.h"
#include "bus_observation.h"

#define CYCLES_PER_US (170U)
#define SAMPLE_MIN_US (25U)
#define SAMPLE_MAX_US (75U)
#define PUBLICATION_DIVIDER (200U)
#define TELEMETRY_PERIOD_MS (10U)
#define RX_FRAGMENT_TIMEOUT_MS (100U)

/* 同一实时边界生成的发布内容，供后台一致性读取。 */
typedef struct {
    MotorSnapshot snapshot; /* 与协议无关的完整业务快照及其有效性。 */
    uint32_t incorporated_guard_faults; /* 已纳入 Motor 快照的故障位，用于识别尚未合并的监视原因。 */
    bool has_sample; /* 是否已有真实采样；启动阶段的初始快照为 false。 */
} RuntimePublication;

volatile uint32_t g_target_runtime_guard_faults;
volatile uint32_t g_target_runtime_current_request;
volatile uint32_t g_target_runtime_current_sequence;
volatile ObserveCurrentCaptureResult g_target_runtime_current_capture;

/* 启动阶段在时基开启前写入；运行后仅由 sample_ready 更新 Motor。 */
static Motor s_motor;
static ControlRuntime s_control_runtime;
/* 3000 rpm 只用于位置差分排歧，不构成电机运行许可；当前没有使能路径。 */
static const ControlRuntimeConfig s_control_config = {
    20000U, 1U, 20U, 100U, 100U, { 3000.0f, 0.01f },
    /* 零偏/噪声限是本次保守工程候选，不代表增益、极性或精度已经标定。 */
    { CURRENT_MEASUREMENT_PHASE_BC, 4096U, 0.10f, 0.05f }, 200U, 5.0f,
    /* 仅此无功率观察构建：启动校零前须断开电池、三相及外部注入源。 */
    true,
    /* 极对数、方向和电角度偏置尚未实测，保持电角度未配置、未确认。 */
    { 0U, 0, 0.0f, 0.0f, 0.0f }, false,
    /* 实机增益、角度与时序尚未验收；当前装配保持控制链及功率许可关闭。 */
    { 0 }
};
static volatile RuntimePublication s_publication;
static volatile uint32_t s_publication_sequence;
static volatile MotorCommand s_pending_command;
static volatile bool s_command_pending;
static volatile bool s_stop_pending;
static volatile bool s_reject_pending;
static volatile uint32_t s_last_sample_cycles;
static uint32_t s_last_guard_cycles;
static uint32_t s_publication_divider;
static bool s_attempted;
static bool s_has_sample;
/* 诊断采集不参与 Motor 生命周期；ADC 是其唯一运行期写者。 */
static ObserveCurrentCapture s_current_capture;
static uint32_t s_current_request_seen;

/* 解析器和正在发送的整帧仅由后台维护；发送完成前不改写帧内容。 */
static VofaParser s_parser;
static uint8_t s_tx_frame[VOFA_TELEMETRY_FRAME_SIZE];
static size_t s_tx_cursor;
static size_t s_tx_length;
static uint32_t s_last_telemetry_ms;
static uint32_t s_last_rx_ms;

static void restore_interrupts(uint32_t primask)
{
    if (primask == 0U) {
        __enable_irq();
    }
}

static bool finite_float(float value)
{
    return (value >= -FLT_MAX) && (value <= FLT_MAX);
}

static void publish(ControlTime now, bool has_sample)
{
    RuntimePublication next;

    memset(&next, 0, sizeof(next));
    control_runtime_snapshot(&s_control_runtime, now, has_sample, &next.snapshot);
    next.incorporated_guard_faults = s_motor.faults;
    next.has_sample = has_sample;
    s_publication_sequence++;
    __DMB();
    s_publication = next;
    __DMB();
    s_publication_sequence++;
}

/* 最多尝试三次读取；后台复制遥测时不屏蔽 ADC 中断。 */
static bool read_publication(RuntimePublication *out)
{
    uint32_t attempt;

    for (attempt = 0U; attempt < 3U; attempt++) {
        uint32_t sequence = s_publication_sequence;

        if ((sequence & 1U) != 0U) {
            continue;
        }
        __DMB();
        *out = s_publication;
        __DMB();
        if (sequence == s_publication_sequence) {
            return true;
        }
    }
    return false;
}

static bool take_command(MotorCommand *command, bool *rejected)
{
    *rejected = false;
    if (s_stop_pending) {
        s_stop_pending = false;
        s_command_pending = false;
        s_reject_pending = false;
        command->type = MOTOR_COMMAND_STOP;
        command->target_velocity_rpm = 0.0f;
        return true;
    }
    if (s_command_pending) {
        __DMB();
        *command = s_pending_command;
        s_command_pending = false;
        return true;
    }
    if (s_reject_pending) {
        s_reject_pending = false;
        *rejected = true;
        return true;
    }
    return false;
}

static void publish_current_capture(void)
{
    g_target_runtime_current_sequence++;
    __DMB();
    g_target_runtime_current_capture = s_current_capture.result;
    __DMB();
    g_target_runtime_current_sequence++;
}

static bool capture_current(const PhaseCurrentObservation *current,
    bool power_disabled, bool stop_requested)
{
    const CurrentMeasurement *measurement = &s_control_runtime.current_measurement;
    uint32_t request = g_target_runtime_current_request;
    bool changed = false;
    bool permitted = !stop_requested && power_disabled
        && (s_control_runtime.phase == CONTROL_RUNTIME_OFF)
        && (s_motor.state == MOTOR_STATE_IDLE) && (s_motor.faults == 0U)
        && (measurement->state == CURRENT_MEASUREMENT_READY)
        && (measurement->failure == CURRENT_MEASUREMENT_FAILURE_NONE)
        && measurement->output.valid
        && s_control_runtime.position_time_available
        && s_control_runtime.position_estimator.velocity_valid
        && (s_control_runtime.position_estimator.velocity_rpm
            <= s_control_config.current_zero_velocity_limit_rpm)
        && (s_control_runtime.position_estimator.velocity_rpm
            >= -s_control_config.current_zero_velocity_limit_rpm);

    /* Stop 撤销正在采集的窗口；同拍的新诊断请求也不得开始。 */
    if (stop_requested && observe_current_capture_abort(&s_current_capture,
        OBSERVE_CURRENT_CAPTURE_FAILURE_ABORTED)) {
        publish_current_capture();
        changed = true;
    }
    if ((request != 0U) && (request != s_current_request_seen)) {
        s_current_request_seen = request;
        (void)observe_current_capture_start(&s_current_capture, request,
            s_control_config.fast_tick_period_ticks,
            measurement->offset_a[1], measurement->offset_a[2], permitted);
        publish_current_capture();
        changed = true;
    }
    if (s_current_capture.result.state != OBSERVE_CURRENT_CAPTURE_ACTIVE) {
        return changed;
    }
    if (!permitted) {
        if (observe_current_capture_abort(&s_current_capture,
            OBSERVE_CURRENT_CAPTURE_FAILURE_ABORTED)) {
            publish_current_capture();
            changed = true;
        }
        return changed;
    }
    /* 使用当拍名义电流；窗口只记录冻结零偏，不学习或改写校准值。 */
    if (observe_current_capture_update(&s_current_capture, current->observation_time,
        current->phase_b_a, current->phase_c_a,
        current->acquisition_valid
            && (current->measured_phases == (PHASE_MASK_B | PHASE_MASK_C)))) {
        publish_current_capture();
        changed = true;
    }
    return changed;
}

static void sample_ready(void *context, const PhaseCurrentObservation *current)
{
    HardwareTripSnapshot trip;
    uint32_t now_cycles = DWT->CYCCNT;
    uint32_t faults = g_target_runtime_guard_faults;
    MotorCommand command;
    bool rejected;
    bool command_available;
    bool business_changed;
    bool power_disabled = (TIM1->BDTR & TIM_BDTR_MOE) == 0U;

    (void)context;
    if (s_has_sample && (((uint32_t)(now_cycles - s_last_sample_cycles)
        < SAMPLE_MIN_US * CYCLES_PER_US)
        || ((uint32_t)(now_cycles - s_last_sample_cycles)
        > SAMPLE_MAX_US * CYCLES_PER_US))) {
        faults |= TARGET_RUNTIME_FAULT_CADENCE;
    }
    s_last_sample_cycles = now_cycles;
    s_has_sample = true;
    if (!current->acquisition_valid
        || (current->measured_phases != (PHASE_MASK_B | PHASE_MASK_C))
        || !finite_float(current->phase_b_a) || !finite_float(current->phase_c_a)) {
        faults |= TARGET_RUNTIME_FAULT_CURRENT;
    }
    /* 通知本身不是故障证据，需要重新读取权威保护快照。 */
    if (!hardware_trip_get_snapshot(&trip)
        || trip.condition_active || (trip.causes != HARDWARE_TRIP_CAUSE_NONE)) {
        faults |= TARGET_RUNTIME_FAULT_TRIP;
    }
    if (!power_disabled && (!s_control_runtime.config.control.enabled
        || s_control_runtime.phase != CONTROL_RUNTIME_RUNNING
        || s_motor.state != MOTOR_STATE_RUN || s_motor.faults != 0U)) {
        faults |= TARGET_RUNTIME_FAULT_UNEXPECTED_POWER;
    }
    command_available = take_command(&command, &rejected);
    business_changed = control_runtime_step(&s_control_runtime, current->observation_time,
        current, power_disabled,
        faults, rejected ? NULL : &command, command_available);
    /* 同拍更新健康快照，主机复核完整窗口时不沿用此前的100Hz旧状态。 */
    business_changed |= capture_current(current, power_disabled,
        command_available && !rejected && (command.type == MOTOR_COMMAND_STOP));
    s_publication_divider++;
    if (business_changed || (s_publication_divider >= PUBLICATION_DIVIDER)) {
        if (s_publication_divider >= PUBLICATION_DIVIDER) {
            s_publication_divider = 0U;
        }
        publish(current->observation_time, true);
    }
}

static void trip_notify(void *context)
{
    (void)context;
    /* Platform 已关断功率；下一次采样读取其锁存的保护快照。 */
}

void target_runtime_guard_irq(void)
{
    uint32_t primask;
    uint32_t now_cycles;
    uint32_t last_sample_cycles;

    if (((TIM6->SR & TIM_SR_UIF) == 0U) || ((TIM6->DIER & TIM_DIER_UIE) == 0U)) {
        return;
    }
    __HAL_TIM_CLEAR_FLAG(&htim6, TIM_FLAG_UPDATE);
    /* ADC 优先级更高：用临界区保证两个时间读数不会被新采样更新隔开。 */
    primask = __get_PRIMASK();
    __disable_irq();
    last_sample_cycles = s_last_sample_cycles;
    now_cycles = DWT->CYCCNT;
    restore_interrupts(primask);
    if (now_cycles == s_last_guard_cycles) {
        g_target_runtime_guard_faults |= TARGET_RUNTIME_FAULT_CLOCK;
    }
    if ((uint32_t)(now_cycles - last_sample_cycles) > SAMPLE_MAX_US * CYCLES_PER_US) {
        g_target_runtime_guard_faults |= TARGET_RUNTIME_FAULT_CADENCE;
    }
    s_last_guard_cycles = now_cycles;
    if (g_target_runtime_guard_faults != 0U) {
        realtime_motor_io_safe_off();
    }
}

void target_runtime_start(void)
{
    RealtimeMotorIoFacts facts;
    HardwareTripSnapshot trip;
    uint32_t primask;
    bool success;

    if (s_attempted) {
        return;
    }
    s_attempted = true;
    observe_current_capture_init(&s_current_capture);
    g_target_runtime_current_request = 0U;
    g_target_runtime_current_sequence = 0U;
    s_current_request_seen = 0U;
    publish_current_capture();
    motor_init(&s_motor);
    vofa_parser_init(&s_parser);
    realtime_motor_io_safe_off();
    if (!control_runtime_init(&s_control_runtime, &s_motor, &s_control_config)) {
        motor_latch_fault(&s_motor, TARGET_RUNTIME_FAULT_STARTUP);
        publish(0U, false);
        return;
    }
    if ((SystemCoreClock != 170000000U) || (TIM6->PSC != 169U)) {
        motor_latch_fault(&s_motor, TARGET_RUNTIME_FAULT_CLOCK);
        publish(0U, false);
        return;
    }
    if (!realtime_motor_io_get_facts(&facts)
        || (facts.control_time_ticks_per_second != 20000U)
        || (facts.fast_tick_period_ticks != 1U)
        || !realtime_motor_io_bind_sample_ready(sample_ready, NULL)
        || !hardware_trip_bind(trip_notify, NULL)
        || !position_observation_arm() || !bus_observation_arm()
        || !realtime_motor_io_arm() || !hardware_trip_arm()
        || !hardware_trip_get_snapshot(&trip)
        || trip.condition_active || (trip.causes != HARDWARE_TRIP_CAUSE_NONE)) {
        realtime_motor_io_safe_off();
        motor_latch_fault(&s_motor, TARGET_RUNTIME_FAULT_STARTUP);
        publish(0U, false);
        return;
    }

    /* 开放首次 ADC 回调前，完成启动阶段对 Motor 的状态写入。 */
    primask = __get_PRIMASK();
    __disable_irq();
    motor_finish_boot(&s_motor);
    success = realtime_motor_io_start_drive_timing();
    if (!success) {
        realtime_motor_io_safe_off();
        motor_latch_fault(&s_motor, TARGET_RUNTIME_FAULT_STARTUP);
    }
    publish(0U, false);
    if (success) {
        s_last_sample_cycles = DWT->CYCCNT;
        s_last_guard_cycles = s_last_sample_cycles;
        TIM6->ARR = 49U;
        TIM6->CNT = 0U;
        TIM6->EGR = TIM_EGR_UG;
        __HAL_TIM_CLEAR_FLAG(&htim6, TIM_FLAG_UPDATE);
        SET_BIT(TIM6->DIER, TIM_DIER_UIE);
        SET_BIT(TIM6->CR1, TIM_CR1_CEN);
    }
    restore_interrupts(primask);
}

/* 后台只向实时边界提交长度受限且内容完整的命令。 */
static void enqueue(const VofaCommand *request, bool rejected)
{
    uint32_t primask = __get_PRIMASK();

    __disable_irq();
    if (!rejected && (request->type == VOFA_COMMAND_STOP)) {
        s_command_pending = false;
        s_stop_pending = true;
    } else if (rejected || s_command_pending || s_stop_pending) {
        s_reject_pending = true;
    } else {
        MotorCommand command;

        if (motor_vofa_command(request, &command)) {
            s_pending_command = command;
            __DMB();
            s_command_pending = true;
        } else {
            s_reject_pending = true;
        }
    }
    restore_interrupts(primask);
}

void target_runtime_service(void)
{
    uint32_t now_ms = HAL_GetTick();
    uint32_t uart_status = USART1->ISR;
    uint32_t errors = uart_status & (USART_ISR_PE | USART_ISR_FE | USART_ISR_NE | USART_ISR_ORE);

    if ((s_parser.length != 0U) && !s_parser.discarding
        && ((uint32_t)(now_ms - s_last_rx_ms) >= RX_FRAGMENT_TIMEOUT_MS)) {
        s_parser.length = 0U;
        s_parser.discarding = true;
    }
    if (errors != 0U) {
        USART1->ICR = errors; /* PE/FE/NE/ORE 的状态位与清除位位置一一对应。 */
        s_parser.length = 0U;
        s_parser.discarding = true;
    }
    if ((uart_status & USART_ISR_RXNE_RXFNE) != 0U) {
        VofaCommand request;
        VofaParseResult result = vofa_parser_feed(&s_parser, (uint8_t)USART1->RDR, &request);

        s_last_rx_ms = now_ms;
        if (result == VOFA_PARSE_COMMAND) {
            enqueue(&request, false);
        } else if (result == VOFA_PARSE_REJECTED) {
            enqueue(NULL, true);
        }
    }
    if (((uint32_t)(now_ms - s_last_telemetry_ms) >= TELEMETRY_PERIOD_MS)
        && (s_tx_cursor == s_tx_length)) {
        RuntimePublication snapshot;

        s_last_telemetry_ms = now_ms;
        if (read_publication(&snapshot)) {
            VofaTelemetry telemetry;
            uint32_t faults = g_target_runtime_guard_faults;
            uint32_t last_sample_cycles = s_last_sample_cycles;
            uint32_t now_cycles = DWT->CYCCNT;

            /* ADC 停滞后仍可能读到完整旧快照，此时不能发布为当前有效状态。 */
            if (((faults & ~snapshot.incorporated_guard_faults) != 0U)
                || ((faults & TARGET_RUNTIME_FAULT_CLOCK) != 0U)
                || (snapshot.has_sample
                    && ((uint32_t)(now_cycles - last_sample_cycles)
                        > SAMPLE_MAX_US * CYCLES_PER_US))) {
                memset(&snapshot.snapshot, 0, sizeof(snapshot.snapshot));
            }
            motor_vofa_telemetry(&snapshot.snapshot, &telemetry);
            s_tx_length = vofa_encode_telemetry(&telemetry, s_tx_frame, sizeof(s_tx_frame));
            s_tx_cursor = 0U;
        }
    }
    if ((s_tx_cursor < s_tx_length) && ((USART1->ISR & USART_ISR_TXE_TXFNF) != 0U)) {
        USART1->TDR = s_tx_frame[s_tx_cursor++];
    }
}
