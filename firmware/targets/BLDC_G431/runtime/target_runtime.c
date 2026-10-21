#include "target_runtime.h"

#include <float.h>
#include <stddef.h>
#include <string.h>

#include "main.h"
#include "tim.h"
#include "motor.h"
#include "control_runtime.h"
#include "target_vofa.h"
#include "realtime_motor_io.h"
#include "hardware_trip.h"
#include "position_observation.h"
#include "bus_observation.h"
#include "cordic_math.h"
#if defined(BLDC_G431_DIAGNOSTICS)
#include "irq_timing.h"
#include "controller_profile.h"
#include "../platform/realtime_motor_io_diagnostic.h"
#endif

#define CYCLES_PER_US (170U)
#define SAMPLE_MIN_US (75U)
#define SAMPLE_MAX_US (125U)
#define PUBLICATION_DIVIDER (100U)
#define FINITE_RUN_MAX_CYCLES (1500000U * CYCLES_PER_US)
#define FINITE_RUN_MAX_DISPLACEMENT (2.0f)

/* 同一实时边界生成的发布内容，供后台一致性读取。 */
typedef struct {
    MotorSnapshot snapshot; /* 与协议无关的完整业务快照及其有效性。 */
    uint32_t incorporated_guard_faults; /* 已纳入 Motor 快照的故障位，用于识别尚未合并的监视原因。 */
    bool has_sample; /* 是否已有真实采样；启动阶段的初始快照为 false。 */
#if defined(BLDC_G431_DIAGNOSTICS)
    bool compute_probe_permitted; /* ADC 同拍确认 IDLE/OFF、控制配置关闭且无故障；后台仍须复核心跳和 MOE。 */
    bool stage_probe_permitted; /* 当拍只允许隔离试具的无功率计时，不授予真实控制许可。 */
#endif
} RuntimePublication;

volatile uint32_t g_target_runtime_guard_faults;
#if defined(BLDC_G431_DIAGNOSTICS)
volatile uint32_t g_target_runtime_irq_entries[8];
volatile uint32_t g_target_runtime_current_request;
volatile uint32_t g_target_runtime_current_sequence;
volatile ObserveCurrentCaptureResult g_target_runtime_current_capture;
volatile uint32_t g_target_runtime_compute_request;
volatile uint32_t g_target_runtime_compute_sequence;
volatile ObserveComputeComparisonResult g_target_runtime_compute_result;
volatile uint32_t g_target_runtime_stage_request;
#endif
volatile TargetRuntimeRunStatus g_target_runtime_run_status;

/* 启动阶段在时基开启前写入；运行后仅由 sample_ready 更新 Motor。 */
static Motor s_motor;
static ControlRuntime s_control_runtime;
static G431CordicMath s_cordic_math;
#if defined(BLDC_G431_FINITE_RUN)
#if !defined(BLDC_G431_RUNTIME_ENTRY)
#error BLDC_G431_FINITE_RUN requires the runtime Core entry
#endif
/* 仅本板固定空载包络：名义电流换算、符号与定向候选承接已存实物记录。
 * 静态1.236V/0.394名义A约3.14Ω，与独立绕组Rs约3.1Ω相符；不声称精度标定。
 * 时序沿用已验证slot与同源控制链，当前候选仍须完成本构建有限验收。 */
static const ControlRuntimeConfig s_control_config = {
    /* 采集仍1kHz；每个fast tick读取已发布缓存，避免再分频引入近1ms消费延迟。 */
    20000U, 2U, 1U, 30U, 100U, {300.0f, 0.005f},
    {CURRENT_MEASUREMENT_PHASE_BC, 4096U, 0.10f, 0.05f,
        true, {1.0f, -1.0f, -1.0f}}, 100U, 5.0f, true,
    {7U, 1, 0.02444341778755188f, 0.002f, 300.0f}, true,
    {true, true, true,
        {0.5f, 0.5f, 300.0f, 300.0f, 50.0f, 1.5f, 0.2f, 1.8f/14.0f, 0.000125f},
        {0.001f, 0.004f, 20.0f, 0.1f, 0.1f, 80.0f, 0.00125f},
        10U, 10.0f, 14.0f, 0.65f, 0.000125f},
    g431_cordic_sincos, &s_cordic_math
};
#else
/* Observe的3000 rpm仅用于位置差分排歧，不构成电机运行许可。 */
static const ControlRuntimeConfig s_control_config = {
    20000U, 2U, 10U, 100U, 100U, { 3000.0f, 0.01f },
    /* 名义值正向为 Shunt+→GND；电机相电流以桥→电机为正，B/C 均取反。
     * 网表和正α/正β静态对照已确认符号；幅值仍用名义增益，精度尚未标定。 */
    { CURRENT_MEASUREMENT_PHASE_BC, 2048U, 0.10f, 0.05f,
        true, { 1.0f, -1.0f, -1.0f } }, 100U, 5.0f,
    /* 仅此无功率观察构建：启动校零前须断开电池、三相及外部注入源。 */
    true,
    /* 此 Observe 构建不装配已验证的电角映射，保持电角度未配置、未确认。 */
    { 0U, 0, 0.0f, 0.0f, 0.0f }, false,
    /* Observe 仅提供无功率观察，不承接 Run 的控制参数或功率许可。 */
    { 0 }, g431_cordic_sincos, &s_cordic_math
};
#endif
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
static bool s_cordic_ready;
#if defined(BLDC_G431_DIAGNOSTICS)
/* 诊断采集不参与 Motor 生命周期；ADC 是其唯一运行期写者。 */
static ObserveCurrentCapture s_current_capture;
static uint32_t s_current_request_seen;

/* 计时诊断只由后台写入，ADC 与 guard 不修改实例或结果。 */
static ObserveComputeComparison s_compute_probe;
static uint32_t s_stage_request_seen;
static uint32_t s_compute_request_seen;
static bool s_compute_stop_this_service; /* 同轮收到 Stop 后，即使 ADC 已消费也不接受计时请求。 */
#endif

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

#if defined(BLDC_G431_DIAGNOSTICS)
/* 只由 ADC 所有者读取实时对象；许可变化须立即发布，不能沿用 100 Hz 的旧母线值。 */
static bool compute_probe_adc_permitted(bool has_sample)
{
#if !defined(CONTROL_CURRENT_PROFILE)
    /* 旧工具要求完整内部探针统计；当前无探针候选在写请求前明确拒绝。 */
    (void)has_sample;
    return false;
#else
    return s_control_config.fast_tick_period_ticks == 1U
        && has_sample && g_target_stage_budget_active == 0U
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
    /* 旧半周期诊断只适用于原20kHz采样；新slot统一使用Timing入口。 */
    return s_control_config.fast_tick_period_ticks == 1U
        && has_sample && s_cordic_ready && s_motor.state == MOTOR_STATE_IDLE
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
#endif

static void publish(ControlTime now, bool has_sample)
{
    RuntimePublication next;

    memset(&next, 0, sizeof(next));
    control_runtime_snapshot(&s_control_runtime, now, has_sample, &next.snapshot);
    next.incorporated_guard_faults = s_motor.faults;
    next.has_sample = has_sample;
#if defined(BLDC_G431_DIAGNOSTICS)
    next.compute_probe_permitted = compute_probe_adc_permitted(has_sample);
    next.stage_probe_permitted = stage_budget_adc_permitted(has_sample);
#endif
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

bool target_runtime_read_snapshot(MotorSnapshot *snapshot)
{
    RuntimePublication publication;
    uint32_t faults;
    uint32_t last_sample_cycles;
    uint32_t now_cycles;

    if (snapshot == NULL || !read_publication(&publication)) {
        return false;
    }
    faults = g_target_runtime_guard_faults;
    last_sample_cycles = s_last_sample_cycles;
    now_cycles = DWT->CYCCNT;
    /* 完整旧快照不等于当前健康；统一在交接边界收紧所有后台消费者的资格。 */
    if ((faults & ~publication.incorporated_guard_faults) != 0U
        || (faults & TARGET_RUNTIME_FAULT_CLOCK) != 0U
        || (publication.has_sample
            && (uint32_t)(now_cycles - last_sample_cycles) > SAMPLE_MAX_US * CYCLES_PER_US)) {
        memset(&publication.snapshot, 0, sizeof(publication.snapshot));
    }
    *snapshot = publication.snapshot;
    return true;
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

/* ADC为业务写者；TIM6只在短临界区执行有限时长撤销和Stop交接。 */
static void monitor_finite_run(const MotorCommand *command, bool has_command)
{
    volatile TargetRuntimeRunStatus *run = &g_target_runtime_run_status;
    if (!s_control_config.control.enabled) { return; }
    if (has_command && command != NULL && command->type == MOTOR_COMMAND_START_VELOCITY
        && s_motor.command_result == MOTOR_COMMAND_RESULT_ACCEPTED
        && s_control_runtime.phase == CONTROL_RUNTIME_START_QUIET) {
        run->accepted_starts++;
        run->limit_reason = 0U;
        run->started_cycles = DWT->CYCCNT;
        run->last_position_time = s_control_runtime.last_position_time;
        run->last_position_turns = s_control_runtime.mechanical_position_turns;
        run->displacement_turns = 0.0f;
        run->active = 1U;
    }
    if (s_control_runtime.phase == CONTROL_RUNTIME_OFF || s_motor.faults != 0U) {
        run->active = 0U;
        return;
    }
    if (run->active && s_control_runtime.position_time_available
        && run->last_position_time != s_control_runtime.last_position_time) {
        float position = s_control_runtime.mechanical_position_turns;
        float delta = position - run->last_position_turns;
        if (delta > 0.5f) { delta -= 1.0f; }
        if (delta < -0.5f) { delta += 1.0f; }
        run->displacement_turns += delta;
        run->last_position_turns = position;
        run->last_position_time = s_control_runtime.last_position_time;
        if (run->displacement_turns >= FINITE_RUN_MAX_DISPLACEMENT
            || run->displacement_turns <= -FINITE_RUN_MAX_DISPLACEMENT) {
            realtime_motor_io_safe_off();
            g_target_runtime_guard_faults |= TARGET_RUNTIME_FAULT_RUN_MOTION;
            motor_latch_fault(&s_motor, TARGET_RUNTIME_FAULT_RUN_MOTION);
            run->limit_reason = 2U;
            run->active = 0U;
        }
    }
}

#if defined(BLDC_G431_DIAGNOSTICS)
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
#endif

static void sample_ready(void *context, const PhaseCurrentObservation *current)
{
    HardwareTripSnapshot trip;
    uint32_t now_cycles = DWT->CYCCNT;
    uint32_t faults = g_target_runtime_guard_faults;
    MotorCommand command;
    bool rejected;
    bool command_available;
    bool business_changed;
#if defined(BLDC_G431_DIAGNOSTICS)
    bool stage_prepared = false;
    uint32_t stage_request;
#endif
    bool power_disabled = (TIM1->BDTR & TIM_BDTR_MOE) == 0U;

    (void)context;
#if defined(BLDC_G431_DIAGNOSTICS)
    if (g_target_irq_timing_active != 0U) {
        g431_irq_timing_observe_enter(now_cycles);
    }
#endif
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
#if defined(BLDC_G431_DIAGNOSTICS)
    if (!s_control_config.control.enabled) {
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
    }
#endif
    /* Observe先执行隔离试具，再推进OFF观察；Run直接推进唯一正式实例。 */
    business_changed = control_runtime_step(&s_control_runtime, current->observation_time,
        current, power_disabled,
        faults, rejected ? NULL : &command, command_available);
    monitor_finite_run(rejected ? NULL : &command, command_available);
#if defined(BLDC_G431_DIAGNOSTICS)
    /* 同拍更新健康快照，主机复核完整窗口时不沿用此前的100Hz旧状态。 */
    if (!s_control_config.control.enabled) {
        business_changed |= capture_current(current, power_disabled,
            command_available && !rejected && (command.type == MOTOR_COMMAND_STOP));
        business_changed |= compute_probe_adc_permitted(true) != s_publication.compute_probe_permitted;
        business_changed |= stage_budget_adc_permitted(true) != s_publication.stage_probe_permitted;
    }
#endif
    s_publication_divider++;
    if (business_changed || (s_publication_divider >= PUBLICATION_DIVIDER)) {
        if (s_publication_divider >= PUBLICATION_DIVIDER) {
            s_publication_divider = 0U;
        }
        publish(current->observation_time, true);
    }
#if defined(BLDC_G431_DIAGNOSTICS)
    if (g_target_irq_timing_active != 0U) {
        g431_irq_timing_observe_exit(DWT->CYCCNT);
    }
#endif
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
    if (s_control_config.control.enabled && g_target_runtime_run_status.active != 0U
        && (uint32_t)(now_cycles - g_target_runtime_run_status.started_cycles) >= FINITE_RUN_MAX_CYCLES) {
        /* 时长包括跨拍启动，Update不续期。先撤销功率，再交接普通Stop语义。
         * 队列中的旧运动意图同时退休；ADC停滞时仍由心跳故障保持关断。 */
        realtime_motor_io_safe_off();
        g_target_runtime_run_status.active = 0U;
        g_target_runtime_run_status.limit_reason = 1U;
        s_command_pending = false;
        s_reject_pending = false;
        s_stop_pending = true;
    }
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
#if defined(BLDC_G431_DIAGNOSTICS)
    g431_irq_timing_init();
    /* 每个异常槽位只有该 handler 写入，嵌套 IRQ 不会覆盖另一路计数。 */
    memset((void *)g_target_runtime_irq_entries, 0, sizeof(g_target_runtime_irq_entries));
#endif
    s_cordic_ready = g431_cordic_math_init(&s_cordic_math);
#if defined(BLDC_G431_DIAGNOSTICS)
    if (!s_control_config.control.enabled) {
        observe_compute_comparison_init(&s_compute_probe);
        observe_controller_profile_init();
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
    }
#endif
    motor_init(&s_motor);
    target_vofa_init();
    realtime_motor_io_safe_off();
    /* 先建立快照所需的实例，即使后端装配失败也能发布明确FAULT。 */
    if (!control_runtime_init(&s_control_runtime, &s_motor, &s_control_config)
        || (s_control_config.control.enabled && !s_cordic_ready)) {
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
        || (facts.fast_tick_period_ticks != 2U)
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

#if defined(BLDC_G431_DIAGNOSTICS)
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
#endif

TargetRuntimeSubmitResult target_runtime_submit_command(const MotorCommand *command)
{
    uint32_t primask = __get_PRIMASK();
    bool rejected = command == NULL;
    bool stop;
    TargetRuntimeSubmitResult result;

    if (!rejected) {
        switch (command->type) {
        case MOTOR_COMMAND_START_VELOCITY:
        case MOTOR_COMMAND_UPDATE_VELOCITY:
        case MOTOR_COMMAND_STOP:
        case MOTOR_COMMAND_CLEAR_FAULT:
            break;
        default:
            rejected = true;
            break;
        }
    }
    stop = !rejected && command->type == MOTOR_COMMAND_STOP;

    __disable_irq();
    if (stop) {
#if defined(BLDC_G431_DIAGNOSTICS)
        s_compute_stop_this_service = true;
#endif
        s_command_pending = false;
        s_stop_pending = true;
        result = TARGET_RUNTIME_SUBMIT_QUEUED;
    } else if (rejected || s_command_pending || s_stop_pending) {
        s_reject_pending = true;
        result = rejected ? TARGET_RUNTIME_SUBMIT_REJECTED : TARGET_RUNTIME_SUBMIT_BUSY;
    } else {
        s_pending_command = *command;
        __DMB();
        s_command_pending = true;
        result = TARGET_RUNTIME_SUBMIT_QUEUED;
    }
    restore_interrupts(primask);
#if defined(BLDC_G431_DIAGNOSTICS)
    /* Stop 可能已被高优先级 ADC 消费并清掉 pending；在同一后台直接取消诊断。 */
    if (stop && observe_compute_comparison_abort(&s_compute_probe,
        OBSERVE_COMPUTE_PROBE_FAILURE_ABORTED)) {
        observe_controller_profile_freeze(false);
        publish_compute_probe();
    }
#endif
    return result;
}

void target_runtime_service(void)
{
#if defined(BLDC_G431_DIAGNOSTICS)
    s_compute_stop_this_service = false;
#endif
    target_vofa_service(HAL_GetTick());
#if defined(BLDC_G431_DIAGNOSTICS)
    if (!s_control_config.control.enabled) { service_compute_probe(); }
#endif
}
