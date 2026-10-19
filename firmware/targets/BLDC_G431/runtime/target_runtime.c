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
#include "cordic_math.h"
#include "irq_timing.h"
#include "controller_profile.h"
#include "../platform/realtime_motor_io_diagnostic.h"

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
    bool compute_probe_permitted; /* ADC 同拍确认 IDLE/OFF、控制配置关闭且无故障；后台仍须复核心跳和 MOE。 */
    bool stage_probe_permitted; /* 当拍只允许隔离试具的无功率计时，不授予真实控制许可。 */
} RuntimePublication;

volatile uint32_t g_target_runtime_guard_faults;
volatile uint32_t g_target_runtime_irq_entries[8];
volatile uint32_t g_target_runtime_current_request;
volatile uint32_t g_target_runtime_current_sequence;
volatile ObserveCurrentCaptureResult g_target_runtime_current_capture;
volatile uint32_t g_target_runtime_compute_request;
volatile uint32_t g_target_runtime_compute_sequence;
volatile ObserveComputeComparisonResult g_target_runtime_compute_result;
volatile uint32_t g_target_runtime_stage_request;

/* 启动阶段在时基开启前写入；运行后仅由 sample_ready 更新 Motor。 */
static Motor s_motor;
static ControlRuntime s_control_runtime;
static G431CordicMath s_cordic_math;
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
    { 0 }, g431_cordic_sincos, &s_cordic_math
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

/* 计时诊断只由后台写入，ADC 与 guard 不修改实例或结果。 */
static ObserveComputeComparison s_compute_probe;
static bool s_cordic_ready;
static uint32_t s_stage_request_seen;
static uint32_t s_compute_request_seen;
static bool s_compute_stop_this_service; /* 同轮收到 Stop 后，即使 ADC 已消费也不接受计时请求。 */

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

/* 只由 ADC 所有者读取实时对象；许可变化须立即发布，不能沿用 100 Hz 的旧母线值。 */
static bool compute_probe_adc_permitted(bool has_sample)
{
#if !defined(CONTROL_CURRENT_PROFILE)
    /* 旧工具要求完整内部探针统计；当前无探针候选在写请求前明确拒绝。 */
    (void)has_sample;
    return false;
#else
    return has_sample && g_target_stage_budget_active == 0U
        && g_target_runtime_stage_request == s_stage_request_seen
        && (s_motor.state == MOTOR_STATE_IDLE) && (s_motor.faults == 0U)
        && (s_control_runtime.phase == CONTROL_RUNTIME_OFF)
        && !s_control_runtime.config.control.enabled && s_control_runtime.bus_valid
        && finite_float(s_control_runtime.bus.voltage_v)
        && (s_control_runtime.bus.voltage_v >= 0.0f) && (s_control_runtime.bus.voltage_v <= 3.6f);
#endif
}

/* 原观察对象仍保持OFF；只给隔离试具读取/计算/真实stage的许可。 */
static bool stage_budget_adc_permitted(bool has_sample)
{
    return has_sample && s_cordic_ready && s_motor.state == MOTOR_STATE_IDLE
        && s_motor.faults == 0U && s_control_runtime.phase == CONTROL_RUNTIME_OFF
        && !s_control_runtime.config.control.enabled && s_control_runtime.bus_valid
        && finite_float(s_control_runtime.bus.voltage_v)
        && s_control_runtime.bus.voltage_v >= 0.0f && s_control_runtime.bus.voltage_v <= 3.6f
        && g_target_runtime_guard_faults == 0U && (TIM1->BDTR & TIM_BDTR_MOE) == 0U
        && !s_command_pending && !s_stop_pending && !s_reject_pending
        && s_current_capture.result.state != OBSERVE_CURRENT_CAPTURE_ACTIVE
        && g_target_runtime_current_request == s_current_request_seen
        && s_compute_probe.result.state != OBSERVE_COMPUTE_PROBE_ACTIVE
        && g_target_runtime_compute_request == s_compute_request_seen
        && SystemCoreClock == 170000000U
        && (CoreDebug->DEMCR & CoreDebug_DEMCR_TRCENA_Msk) != 0U
        && (DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) != 0U;
}

static bool stage_budget_stamp(void *context, G431StageBudgetStamp *stamp)
{
    (void)context;
    if (stamp == NULL || SystemCoreClock != 170000000U
        || (CoreDebug->DEMCR & CoreDebug_DEMCR_TRCENA_Msk) == 0U
        || (DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) == 0U) {
        return false;
    }
    realtime_motor_io_stage_anchor(&stamp->callback_start_cycles, &stamp->callback_entry_count);
    stamp->timer_cr1 = TIM1->CR1;
    stamp->timer_count = TIM1->CNT;
    stamp->timer_bdtr = TIM1->BDTR;
    stamp->cycles = DWT->CYCCNT;
    return true;
}

bool target_runtime_timing_permitted(void)
{
    return compute_probe_adc_permitted(s_has_sample) &&
        g_target_runtime_guard_faults == 0U && (TIM1->BDTR & TIM_BDTR_MOE) == 0U &&
        !s_command_pending && !s_stop_pending && !s_reject_pending && !s_compute_stop_this_service &&
        SystemCoreClock == 170000000U &&
        (CoreDebug->DEMCR & CoreDebug_DEMCR_TRCENA_Msk) != 0U &&
        (DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) != 0U;
}

static void publish(ControlTime now, bool has_sample)
{
    RuntimePublication next;

    memset(&next, 0, sizeof(next));
    control_runtime_snapshot(&s_control_runtime, now, has_sample, &next.snapshot);
    next.incorporated_guard_faults = s_motor.faults;
    next.has_sample = has_sample;
    next.compute_probe_permitted = compute_probe_adc_permitted(has_sample);
    next.stage_probe_permitted = stage_budget_adc_permitted(has_sample);
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
    bool permitted = !stop_requested && power_disabled && g_target_stage_budget_active == 0U
        && g_target_runtime_stage_request == s_stage_request_seen
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
    bool stage_prepared = false;
    uint32_t stage_request;
    bool power_disabled = (TIM1->BDTR & TIM_BDTR_MOE) == 0U;

    (void)context;
    if (g_target_irq_timing_active != 0U) {
        g431_irq_timing_observe_enter(now_cycles);
    }
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
    stage_request = g_target_runtime_stage_request;
    if (command_available && (g_target_stage_budget_active != 0U
        || (stage_request != 0U && stage_request != s_stage_request_seen))
        && (rejected || command.type != MOTOR_COMMAND_STOP)) {
        /* 诊断期间只接受Stop；普通命令终止窗口后仍按拒绝事件交给真实Motor。 */
        rejected = true;
    }
    if (stage_request != 0U && stage_request != s_stage_request_seen) {
        s_stage_request_seen = stage_request;
        if (g_target_stage_budget_active != 0U) {
            g431_stage_budget_cancel(G431_STAGE_BUDGET_FAILURE_BUSY);
        } else {
            (void)g431_stage_budget_prepare(stage_request,
                faults == 0U && !command_available && stage_budget_adc_permitted(true));
        }
        stage_prepared = true;
    }
    if (!stage_prepared && g_target_stage_budget_active != 0U) {
        if (command_available || faults != 0U || !stage_budget_adc_permitted(true)) {
            g431_stage_budget_cancel(command_available && !rejected
                && command.type == MOTOR_COMMAND_STOP
                ? G431_STAGE_BUDGET_FAILURE_ABORTED : G431_STAGE_BUDGET_FAILURE_NOT_READY);
        } else {
            (void)g431_stage_budget_step(current->observation_time, true);
        }
    }
    /* 隔离试具先到真实stage截止；原OFF观察位于诊断尾部，始终逐拍推进。
     * 该尾部是诊断附加开销，不冒充正式RUN的完整ISR耗时。 */
    business_changed = control_runtime_step(&s_control_runtime, current->observation_time,
        current, power_disabled,
        faults, rejected ? NULL : &command, command_available);
    /* 同拍更新健康快照，主机复核完整窗口时不沿用此前的100Hz旧状态。 */
    business_changed |= capture_current(current, power_disabled,
        command_available && !rejected && (command.type == MOTOR_COMMAND_STOP));
    business_changed |= compute_probe_adc_permitted(true) != s_publication.compute_probe_permitted;
    business_changed |= stage_budget_adc_permitted(true) != s_publication.stage_probe_permitted;
    s_publication_divider++;
    if (business_changed || (s_publication_divider >= PUBLICATION_DIVIDER)) {
        if (s_publication_divider >= PUBLICATION_DIVIDER) {
            s_publication_divider = 0U;
        }
        publish(current->observation_time, true);
    }
    if (g_target_irq_timing_active != 0U) {
        g431_irq_timing_observe_exit(DWT->CYCCNT);
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
    observe_compute_comparison_init(&s_compute_probe);
    g431_irq_timing_init();
    observe_controller_profile_init();
    /* 每个异常槽位只有该 handler 写入，嵌套 IRQ 不会覆盖另一路计数。 */
    memset((void *)g_target_runtime_irq_entries, 0, sizeof(g_target_runtime_irq_entries));
    s_cordic_ready = g431_cordic_math_init(&s_cordic_math);
    g_target_runtime_stage_request = 0U;
    s_stage_request_seen = 0U;
    g431_stage_budget_init(g431_cordic_sincos, &s_cordic_math, stage_budget_stamp, NULL);
    g_target_runtime_compute_request = 0U;
    g_target_runtime_compute_sequence = 0U;
    s_compute_request_seen = 0U;
    g_target_runtime_compute_result = s_compute_probe.result;
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

/* 后台单写计时结果；外部通过版本号读取冻结的一致副本。 */
static void publish_compute_probe(void)
{
    g_target_runtime_compute_sequence++;
    __DMB();
    g_target_runtime_compute_result = s_compute_probe.result;
    __DMB();
    g_target_runtime_compute_sequence++;
}

/* 健康 publication 只是同拍证据；独立心跳、硬件关闭状态和邮箱须额外复核。
 * 临界区只保护采样时间/DWT 读数对，绝不包围控制计算。 */
static bool compute_probe_permitted(void)
{
    RuntimePublication publication;
    uint32_t primask;
    uint32_t last_sample;
    uint32_t now;

    if (!read_publication(&publication) || !publication.has_sample
        || !publication.compute_probe_permitted || !publication.snapshot.status_valid
        || !publication.snapshot.control_time_valid || !publication.snapshot.bus_voltage_valid
        || (publication.snapshot.state != MOTOR_STATE_IDLE) || (publication.snapshot.faults != 0U)
        || !finite_float(publication.snapshot.bus_voltage_v)
        || (publication.snapshot.bus_voltage_v < 0.0f) || (publication.snapshot.bus_voltage_v > 3.6f)
        || s_command_pending || s_stop_pending || s_reject_pending || s_compute_stop_this_service
        || g_target_stage_budget_active != 0U
        || g_target_runtime_stage_request != s_stage_request_seen
        || (g_target_runtime_guard_faults != 0U) || ((TIM1->BDTR & TIM_BDTR_MOE) != 0U)
        || (SystemCoreClock != 170000000U)
        || ((CoreDebug->DEMCR & CoreDebug_DEMCR_TRCENA_Msk) == 0U)
        || ((DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) == 0U)) {
        return false;
    }
    primask = __get_PRIMASK();
    __disable_irq();
    last_sample = s_last_sample_cycles;
    now = DWT->CYCCNT;
    restore_interrupts(primask);
    return (uint32_t)(now - last_sample) <= SAMPLE_MAX_US * CYCLES_PER_US;
}

static uint32_t compute_probe_irq_sum(void)
{
    uint32_t index, sum = 0U;
    for (index = 0U; index < 8U; index++) {
        sum += g_target_runtime_irq_entries[index];
    }
    return sum;
}

static bool compute_probe_stamp(void *context, ObserveComputeProbeStamp *stamp)
{
    uint32_t before, after, cycles, attempt;
    (void)context;
    if (((CoreDebug->DEMCR & CoreDebug_DEMCR_TRCENA_Msk) == 0U)
        || ((DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) == 0U)) {
        return false;
    }
    /* 不关中断。计数夹住 cycle 读取，若被抢占只重取瞬时边界，最多三次。 */
    for (attempt = 0U; attempt < 3U; attempt++) {
        before = compute_probe_irq_sum();
        __DMB();
        cycles = DWT->CYCCNT;
        __DMB();
        after = compute_probe_irq_sum();
        if (before == after) {
            stamp->cycles = cycles;
            stamp->irq_entries = after;
            return true;
        }
    }
    return false;
}

/* 每轮后台至多执行一个固定算例；结果仅发布诊断，不提交 duty 或开放许可。 */
static void service_compute_probe(void)
{
    uint32_t request = g_target_runtime_compute_request;
    bool permitted;

    if ((request != 0U) && (request != s_compute_request_seen)) {
        s_compute_request_seen = request;
        permitted = compute_probe_permitted();
        (void)observe_compute_comparison_start(&s_compute_probe, request,
            SystemCoreClock, 200U * CYCLES_PER_US, permitted && s_cordic_ready,
            g431_cordic_sincos, &s_cordic_math);
        if (s_compute_probe.result.state == OBSERVE_COMPUTE_PROBE_ACTIVE) {
            (void)observe_controller_profile_start(request, compute_probe_stamp, NULL);
        } else {
            observe_controller_profile_freeze(false);
        }
        publish_compute_probe();
    }
    if (s_compute_probe.result.state != OBSERVE_COMPUTE_PROBE_ACTIVE) {
        return;
    }
    permitted = compute_probe_permitted();
    (void)observe_compute_comparison_step(&s_compute_probe, permitted, compute_probe_stamp, NULL);
    if (!compute_probe_permitted()) {
        (void)observe_compute_comparison_revoke(&s_compute_probe);
    }
    /* ACTIVE 中的统计保持私有；开始/终态才发布，避免采集端读到半份统计。 */
    if (s_compute_probe.result.state != OBSERVE_COMPUTE_PROBE_ACTIVE) {
        observe_controller_profile_freeze(s_compute_probe.result.state == OBSERVE_COMPUTE_PROBE_COMPLETE);
        publish_compute_probe();
    }
}

/* 后台只向实时边界提交长度受限且内容完整的命令。 */
static void enqueue(const VofaCommand *request, bool rejected)
{
    uint32_t primask = __get_PRIMASK();

    __disable_irq();
    if (!rejected && (request->type == VOFA_COMMAND_STOP)) {
        s_compute_stop_this_service = true;
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
    /* Stop 可能已被高优先级 ADC 消费并清掉 pending；在同一后台直接取消诊断。 */
    if (!rejected && (request->type == VOFA_COMMAND_STOP)
        && observe_compute_comparison_abort(&s_compute_probe, OBSERVE_COMPUTE_PROBE_FAILURE_ABORTED)) {
        observe_controller_profile_freeze(false);
        publish_compute_probe();
    }
}

void target_runtime_service(void)
{
    uint32_t now_ms = HAL_GetTick();
    uint32_t uart_status = USART1->ISR;
    uint32_t errors = uart_status & (USART_ISR_PE | USART_ISR_FE | USART_ISR_NE | USART_ISR_ORE);

    s_compute_stop_this_service = false;
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
    service_compute_probe();
}
