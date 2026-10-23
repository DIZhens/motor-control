#include "target_runtime.h"

#include <float.h>
#include <stddef.h>
#include <string.h>

#include "main.h"
#include "tim.h"
#include "motor.h"
#include "control_runtime.h"
#include "realtime_motor_io.h"
#include "hardware_trip.h"
#include "position_observation.h"
#include "bus_observation.h"
#include "../platform/cordic_math.h"
#include "g431_control_profile.h"

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
    RuntimeParameters parameters; /* 与状态同一ADC边界发布，不暴露实时私有对象。 */
} RuntimePublication;

volatile uint32_t g_target_runtime_guard_faults;
volatile TargetRuntimeRunStatus g_target_runtime_run_status;

/* 启动阶段在时基开启前写入；运行后仅由 sample_ready 更新 Motor。 */
static Motor s_motor;
static ControlRuntime s_control_runtime;
static RuntimeExchange s_exchange;
static RuntimeParameters s_parameters;
static volatile RuntimeInternalFaultStatus s_internal_fault;
static volatile SpeedCommissioningSample s_commissioning;
static volatile uint32_t s_commissioning_version;
static volatile bool s_commissioning_available;
static uint32_t s_commissioning_count, s_commissioning_divider;

void runtime_exchange_barrier(void) { __DMB(); }

void target_runtime_fail_closed(RuntimeInternalFaultReason reason)
{
    uint32_t primask = __get_PRIMASK();
    __disable_irq();
    realtime_motor_io_safe_off();
    if (reason <= RUNTIME_INTERNAL_NONE || reason > RUNTIME_INTERNAL_TIMEOUT) {
        reason = RUNTIME_INTERNAL_TRANSACTION_CORRUPT;
    }
    if (s_internal_fault.first_reason == RUNTIME_INTERNAL_NONE) { s_internal_fault.first_reason = reason; }
    s_internal_fault.count++;
    g_target_runtime_guard_faults |= TARGET_RUNTIME_FAULT_INTERNAL;
    __DMB();
    if (primask == 0U) { __enable_irq(); }
}

bool target_runtime_session_permitted(void)
{
    return s_internal_fault.first_reason == RUNTIME_INTERNAL_NONE;
}

bool target_runtime_read_internal_fault(RuntimeInternalFaultStatus *status)
{
    uint32_t primask;
    RuntimeInternalFaultStatus next;
    if (status == NULL) { return false; }
    primask = __get_PRIMASK(); __disable_irq(); next = s_internal_fault;
    if (primask == 0U) { __enable_irq(); }
    *status = next;
    return true;
}

uint32_t target_runtime_completion_high_water(void) { return s_exchange.completion_high_water; }

bool target_runtime_pop_completion(RuntimeCommandCompletion *completion)
{
    if ((uint32_t)(s_exchange.head - s_exchange.tail) > RUNTIME_COMPLETION_CAPACITY) {
        target_runtime_fail_closed(RUNTIME_INTERNAL_TRANSACTION_CORRUPT); return false;
    }
    return runtime_exchange_pop(&s_exchange, completion);
}
#if !defined(BLDC_G431_RUNTIME_ENTRY)
#error BLDC_G431_FINITE_RUN requires the runtime Core entry
#endif
static volatile RuntimePublication s_publication;
static volatile uint32_t s_publication_sequence;
static volatile uint32_t s_last_sample_cycles;
static uint32_t s_last_guard_cycles;
static uint32_t s_publication_divider;
static bool s_attempted;
static bool s_has_sample;
static bool s_cordic_ready;

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
    next.parameters = s_parameters;
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
    if (!target_runtime_session_permitted()) { faults |= TARGET_RUNTIME_FAULT_INTERNAL; }
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

bool target_runtime_read_parameters(RuntimeParameters *parameters)
{
    RuntimePublication publication;
    if (parameters == NULL || !read_publication(&publication)) { return false; }
    if (!target_runtime_session_permitted()
        || g_target_runtime_guard_faults != 0U
        || (uint32_t)(DWT->CYCCNT - s_last_sample_cycles) > SAMPLE_MAX_US * CYCLES_PER_US) {
        publication.parameters.speed_active.in_use = false;
    }
    *parameters = publication.parameters;
    return true;
}

bool target_runtime_read_commissioning(SpeedCommissioningSample *sample)
{
    SpeedCommissioningSample next;
    uint32_t attempt, version;
    if (sample == NULL || !s_commissioning_available) { return false; }
    for (attempt = 0U; attempt < 3U; ++attempt) {
        version = s_commissioning_version;
        if ((version & 1U) != 0U) { continue; }
        __DMB(); next = s_commissioning; __DMB();
        if (version != s_commissioning_version) { continue; }
        if (!target_runtime_session_permitted() || g_target_runtime_guard_faults != 0U
            || (uint32_t)(DWT->CYCCNT - s_last_sample_cycles) > SAMPLE_MAX_US * CYCLES_PER_US) {
            next.valid_flags = 0U;
            next.limit_flags = 0U;
        }
        *sample = next; return true;
    }
    return false;
}

/* 仅复制已有状态，不调用控制计算、格式编码或内核。RUN按实际速度更新边界发布，
 * OFF/启动过程按10个fast tick发布；序号在此推进，不取决于后台是否读取或发送。
 */
static void publish_commissioning(ControlTime now)
{
    MotorSnapshot snapshot;
    SpeedCommissioningSample next;
    uint32_t valid = 0U;
    bool control_valid = s_motor.state == MOTOR_STATE_RUN && s_motor.faults == 0U
        && s_control_runtime.phase == CONTROL_RUNTIME_RUNNING && target_runtime_session_permitted();
    memset(&next, 0, sizeof(next));
    control_runtime_snapshot(&s_control_runtime, now, true, &snapshot);
    next.sample_sequence = ++s_commissioning_count;
    next.control_time_ticks = now;
    if (snapshot.control_time_valid) { valid |= 1U; }
    if (s_control_runtime.position_time_available && now >= s_control_runtime.last_position_time
        && now - s_control_runtime.last_position_time <= g431_control_config.position_max_age_ticks) {
        next.position_observation_time = s_control_runtime.last_position_time;
        next.mechanical_position_turns = s_control_runtime.mechanical_position_turns
            * (float)g431_control_config.electrical_angle.encoder_direction;
        valid |= 2U;
    }
    next.target_velocity_rpm = snapshot.target_velocity_rpm; next.velocity_rpm = snapshot.velocity_rpm;
    next.iq_feedback_a = snapshot.iq_a; next.id_feedback_a = snapshot.id_a;
    next.bus_voltage_v = snapshot.bus_voltage_v;
    next.motor_state = snapshot.state; next.fault_flags = snapshot.faults;
    if (snapshot.status_valid) { valid |= 4U | 256U; }
    if (snapshot.velocity_valid) { valid |= 8U; }
    if (snapshot.iq_valid) { valid |= 32U; }
    if (snapshot.id_valid) { valid |= 64U; }
    if (snapshot.bus_voltage_valid) { valid |= 128U; }
    if (control_valid && s_control_runtime.speed_output.valid) {
        next.iq_reference_a = s_control_runtime.speed_output.iq_reference_a; valid |= 16U;
        if (s_control_runtime.speed_output.current_limited) { next.limit_flags |= 1U; }
    }
    if (control_valid && s_control_runtime.current_output.valid) {
        if (s_control_runtime.current_output.d_integral_limited) { next.limit_flags |= 2U; }
        if (s_control_runtime.current_output.q_integral_limited) { next.limit_flags |= 4U; }
        if (s_control_runtime.current_output.voltage_limited) { next.limit_flags |= 8U; }
    }
    next.active_profile_generation = s_parameters.speed_active.generation;
    next.telemetry_config_generation = s_parameters.telemetry.generation;
    next.valid_flags = valid | 512U;
    s_commissioning_version++; __DMB(); s_commissioning = next; __DMB(); s_commissioning_version++;
    s_commissioning_available = true;
}

static void complete_request(const RuntimeCommandCompletion *completion)
{
    if (!runtime_exchange_complete(&s_exchange, completion)) {
        target_runtime_fail_closed(RUNTIME_INTERNAL_COMPLETION_OVERFLOW);
    }
}

/* ADC为业务写者；TIM6只在短临界区执行有限时长撤销和Stop交接。 */
static void monitor_finite_run(const MotorCommand *command, bool has_command)
{
    volatile TargetRuntimeRunStatus *run = &g_target_runtime_run_status;
    if (!g431_control_config.control.enabled) { return; }
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


static void sample_ready(void *context, const PhaseCurrentObservation *current)
{
    HardwareTripSnapshot trip;
    uint32_t now_cycles = DWT->CYCCNT;
    uint32_t faults = g_target_runtime_guard_faults;
    MotorCommand command;
    bool command_available = false;
    bool business_changed;
    RuntimeCommandEnvelope envelope;
    RuntimeCommandCompletion completion;
    uint32_t cancelled_ticket = 0U;
    bool has_envelope;
    bool parameter_request = false;
    ControlTime previous_speed_time = s_control_runtime.last_speed_time;
    ControlRuntimePhase previous_control_phase = s_control_runtime.phase;
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
    if (!target_runtime_session_permitted()) { faults |= TARGET_RUNTIME_FAULT_INTERNAL; }
    /* 外部请求与内部守卫Stop只从同一交接消费；外部ticket由Exchange保留。 */
    has_envelope = runtime_exchange_take(&s_exchange, &envelope, &cancelled_ticket);
    if (has_envelope && ((unsigned int)envelope.request.kind > RUNTIME_REQUEST_TELEMETRY
        || (envelope.request.kind == RUNTIME_REQUEST_MOTOR
            && (unsigned int)envelope.request.value.motor.type > MOTOR_COMMAND_CLEAR_FAULT)
        || (envelope.ticket == 0U && (envelope.request.kind != RUNTIME_REQUEST_MOTOR
            || envelope.request.value.motor.type != MOTOR_COMMAND_STOP)))) {
        target_runtime_fail_closed(RUNTIME_INTERNAL_TRANSACTION_CORRUPT);
        faults |= TARGET_RUNTIME_FAULT_INTERNAL; has_envelope = false;
    }
    if (has_envelope) {
        memset(&completion, 0, sizeof(completion));
        completion.ticket = envelope.ticket; completion.kind = envelope.request.kind;
        if (envelope.request.kind == RUNTIME_REQUEST_MOTOR) {
            command = envelope.request.value.motor; command_available = true;
            if (command.type == MOTOR_COMMAND_START_VELOCITY && s_parameters.telemetry.rate_hz == 500U) {
                /* 参数为500Hz时不进入Motor；正常协议Server同样在提交前返回PRECONDITION_FAILED。 */
                completion.outcome = RUNTIME_PRECONDITION_FAILED;
                command_available = false;
            }
        } else {
            bool idle = faults == 0U && power_disabled && s_motor.state == MOTOR_STATE_IDLE
                && s_motor.faults == 0U && s_control_runtime.phase == CONTROL_RUNTIME_OFF;
            parameter_request = true; command_available = false;
            if (envelope.request.kind == RUNTIME_REQUEST_SPEED_PI) {
                completion.parameter_result = runtime_parameters_write_pi(&s_parameters,
                    &envelope.request.value.speed_pi, idle);
                completion.value.speed_pi = s_parameters.speed_staged;
            } else {
                completion.parameter_result = runtime_parameters_write_telemetry(&s_parameters,
                    envelope.request.value.telemetry.enabled, envelope.request.value.telemetry.rate_hz, idle);
                completion.value.telemetry = s_parameters.telemetry;
            }
        }
    }
    business_changed = control_runtime_step(&s_control_runtime, current->observation_time,
        current, power_disabled,
        faults, command_available ? &command : NULL, command_available);
    if (command_available && command.type == MOTOR_COMMAND_START_VELOCITY
        && s_motor.command_result == MOTOR_COMMAND_RESULT_ACCEPTED
        && s_control_runtime.phase == CONTROL_RUNTIME_START_QUIET) {
        if (control_runtime_install_start_speed_pi(&s_control_runtime,
            s_parameters.speed_staged.value.kp, s_parameters.speed_staged.value.ki)) {
            runtime_parameters_activate(&s_parameters);
        } else { target_runtime_fail_closed(RUNTIME_INTERNAL_TRANSACTION_CORRUPT); }
    }
    monitor_finite_run(command_available ? &command : NULL, command_available);
    if (s_control_runtime.phase == CONTROL_RUNTIME_OFF || s_motor.faults != 0U
        || !target_runtime_session_permitted()) { runtime_parameters_deactivate(&s_parameters); }
    if (cancelled_ticket != 0U) {
        RuntimeCommandCompletion cancelled;
        memset(&cancelled, 0, sizeof(cancelled));
        cancelled.ticket = cancelled_ticket; cancelled.outcome = RUNTIME_CANCELLED_BY_STOP;
        complete_request(&cancelled);
    }
    if (has_envelope && envelope.ticket != 0U) {
        if (!parameter_request && command_available) { completion.motor_result = s_motor.command_result; }
        complete_request(&completion);
        business_changed = true;
    }
    s_commissioning_divider++;
    if ((s_control_runtime.phase == CONTROL_RUNTIME_RUNNING && previous_control_phase == CONTROL_RUNTIME_RUNNING
            && s_control_runtime.last_speed_time != previous_speed_time)
        || (s_control_runtime.phase != CONTROL_RUNTIME_RUNNING && s_commissioning_divider >= 10U)) {
        s_commissioning_divider = 0U;
        publish_commissioning(current->observation_time);
    }
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
    if (g431_control_config.control.enabled && g_target_runtime_run_status.active != 0U
        && (uint32_t)(now_cycles - g_target_runtime_run_status.started_cycles) >= FINITE_RUN_MAX_CYCLES) {
        /* 时长包括跨拍启动，Update不续期。先撤销功率，再交接普通Stop语义。
         * 队列中的旧运动意图同时退休；ADC停滞时仍由心跳故障保持关断。 */
        realtime_motor_io_safe_off();
        g_target_runtime_run_status.active = 0U;
        g_target_runtime_run_status.limit_reason = 1U;
        runtime_exchange_request_internal_stop(&s_exchange);
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
    s_cordic_ready = g431_control_profile_initialize_math();
    motor_init(&s_motor);
    runtime_exchange_init(&s_exchange);
    runtime_parameters_init(&s_parameters, g431_control_config.control.speed.kp_a_per_rpm,
        g431_control_config.control.speed.ki_a_per_rpm_s);
    realtime_motor_io_safe_off();
    /* 先建立快照所需的实例，即使后端装配失败也能发布明确FAULT。 */
    if (!control_runtime_init(&s_control_runtime, &s_motor, &g431_control_config)
        || (g431_control_config.control.enabled && !s_cordic_ready)) {
        motor_latch_fault(&s_motor, TARGET_RUNTIME_FAULT_STARTUP);
        if (!target_runtime_session_permitted()) { motor_latch_fault(&s_motor, TARGET_RUNTIME_FAULT_INTERNAL); }
        publish(0U, false);
        return;
    }
    if (!target_runtime_session_permitted()) {
        realtime_motor_io_safe_off(); motor_latch_fault(&s_motor, TARGET_RUNTIME_FAULT_INTERNAL);
        publish(0U, false); return;
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


RuntimeExchangeResult target_runtime_submit_request(const RuntimeRequest *request,
    uint32_t *ticket, uint32_t *cancelled_ticket)
{
    uint32_t primask;
    RuntimeExchangeResult result;
    if (request == NULL || ticket == NULL || cancelled_ticket == NULL) { return RUNTIME_EXCHANGE_BAD_REQUEST; }
    primask = __get_PRIMASK(); __disable_irq();
    if (!target_runtime_session_permitted() && (request->kind != RUNTIME_REQUEST_MOTOR
        || request->value.motor.type == MOTOR_COMMAND_START_VELOCITY
        || request->value.motor.type == MOTOR_COMMAND_UPDATE_VELOCITY)) {
        restore_interrupts(primask); return RUNTIME_EXCHANGE_FAILED;
    }
    if (!s_attempted) { restore_interrupts(primask); return RUNTIME_EXCHANGE_FAILED; }
    if (request->kind == RUNTIME_REQUEST_MOTOR && request->value.motor.type == MOTOR_COMMAND_START_VELOCITY
        && s_parameters.telemetry.rate_hz == 500U) {
        restore_interrupts(primask); return RUNTIME_EXCHANGE_PRECONDITION_FAILED;
    }
    result = runtime_exchange_submit(&s_exchange, request, ticket, cancelled_ticket);
    if (result == RUNTIME_EXCHANGE_TICKET_EXHAUSTED) {
        target_runtime_fail_closed(RUNTIME_INTERNAL_TRANSACTION_CORRUPT);
    }
    restore_interrupts(primask);
    return result;
}

void target_runtime_service(void)
{
}
