#include "target_validation.h"

#if defined(__CC_ARM)
#pragma Otime
#endif

#include <float.h>
#include <stddef.h>
#include <math.h>
#include "main.h"
#include "tim.h"
#include "protection_probe.h"
#include "voltage_open_loop.h"

#define CYCLES_PER_US (170U)
#define GUARD_PERIOD_US (50U)
#define CURRENT_MAX_GAP_US (125U)
#define CURRENT_MIN_GAP_US (75U)
#define CURRENT_PERIOD_US (100U)
#define CAPTURE_MARGIN_US (5000U)
#define BUS_MAX_AGE_TICKS (100U) /* 在 G431 的 20 kHz ControlTime 时域中为 5 ms。 */
#define OPEN_LOOP_NOMINAL_CURRENT_TRIP_A (0.25f)

volatile PlatformBringupDebug g_platform_bringup;
volatile TargetValidationMailbox g_target_validation_request;
volatile TargetValidationBusFailure g_target_validation_bus_failure;
volatile TargetValidationSnapshot g_target_validation;
volatile TargetValidationCurrentCapture g_target_validation_capture;
volatile TargetValidationOpenLoopConfig g_target_validation_open_loop_config;
volatile TargetValidationOpenLoopSnapshot g_target_validation_open_loop;
volatile TargetValidationAngleSurveyConfig g_target_validation_angle_survey_config;
volatile TargetValidationAngleSurveySnapshot g_target_validation_angle_survey;
volatile TargetValidationCurrentSurveyConfig g_target_validation_current_survey_config;
volatile TargetValidationCurrentSurveySnapshot g_target_validation_current_survey;
volatile CurrentHoldConfig g_target_validation_current_hold_config;
volatile CurrentHoldSnapshot g_target_validation_current_hold;
volatile RotorCurrentConfig g_target_validation_rotor_current_config;
volatile RotorCurrentSnapshot g_target_validation_rotor_current;
volatile RotorSpeedConfig g_target_validation_rotor_speed_config;
volatile RotorSpeedSnapshot g_target_validation_rotor_speed;
volatile uint32_t g_target_validation_stop_reason;
volatile uint32_t g_target_validation_timing_recovery_ticks;

static RealtimeMotorIoFacts s_facts;
static ControlTime s_last_position_time;
static ControlTime s_last_bus_time;
static volatile bool s_trip_rescan_pending;
static uint32_t s_last_bringup_poll_ms;
static bool s_started;
static bool s_power_requested;
static bool s_powered;
static volatile bool s_capturing;
static bool s_previous_sample;
static uint32_t s_previous_sample_cycles;
static uint32_t s_last_command_sequence;
static uint32_t s_staging_ticks;
static ProtectionProbeSource s_probe_source;
static uint32_t s_probe_powered_ticks;
static uint32_t s_observation_divider;
static uint32_t s_timing_case;
static PhaseDutyRequest s_duty;
static TargetValidationCurrentStatistics s_statistics;
static float s_m2_b;
static float s_m2_c;
static volatile bool s_open_loop_active; /* ADC 管理事务，TIM6 仅据此选择独立超时原因。 */
static VoltageOpenLoop s_open_loop;
static TargetValidationOpenLoopSnapshot s_open_loop_report;
static ControlTime s_open_loop_sample_time;
static ControlTime s_open_loop_position_time;
static float s_open_loop_position;
static bool s_angle_survey_active;
static bool s_current_survey_active;
static bool s_rotor_current_active;
static RotorCurrentSnapshot s_rotor_current_report;
static bool s_rotor_speed_active;
static RotorSpeedSnapshot s_rotor_speed_report;
static TargetValidationAngleSurveySnapshot s_angle_survey_report;
static float s_angle_window_anchor[3];
static float s_angle_window_min[3];
static float s_angle_window_max[3];
static float s_angle_window_mean[3];
static volatile bool s_current_hold_active; /* TIM6读取它选择失败超时，ADC拥有其余状态。 */
static CurrentHold s_current_hold;
static CurrentHoldSnapshot s_current_hold_report;
static ControlTime s_current_hold_started;
static ControlTime s_current_hold_last_sample;

/* 事务状态仅由 ADC 更新；其他中断只能关断并发布原因。 */
static volatile bool s_operation_active;
static volatile bool s_deadline_active;
static volatile uint32_t s_deadline_cycles;
static volatile uint32_t s_last_sample_cycles;
static volatile uint32_t s_previous_guard_cycles;
static volatile uint32_t s_guard_irq_count;

static void restore_interrupts(uint32_t primask)
{
    if (primask == 0U) {
        __enable_irq();
    }
}

__STATIC_FORCEINLINE bool finite_float(float value)
{
    return (value >= -FLT_MAX) && (value <= FLT_MAX);
}

__STATIC_FORCEINLINE bool current_valid(const PhaseCurrentObservation *current)
{
    return current->acquisition_valid
        && (current->measured_phases == (PHASE_MASK_B | PHASE_MASK_C))
        && finite_float(current->phase_b_a) && finite_float(current->phase_c_a);
}

static void guard_timer_stop(void)
{
    CLEAR_BIT(TIM6->DIER, TIM_DIER_UIE);
    CLEAR_BIT(TIM6->CR1, TIM_CR1_CEN);
    __HAL_TIM_CLEAR_FLAG(&htim6, TIM_FLAG_UPDATE);
}

static void stop_operation(TargetValidationResult reason)
{
    uint32_t primask = __get_PRIMASK();

    __disable_irq();
    realtime_motor_io_safe_off();
    guard_timer_stop();
    s_deadline_active = false;
    if ((g_target_validation_stop_reason == TARGET_VALIDATION_RESULT_NONE)
        || !s_operation_active) {
        g_target_validation_stop_reason = (uint32_t)reason;
    }
    restore_interrupts(primask);
}

static void publish_capture(void)
{
    g_target_validation_capture.sequence++;
    __DMB();
    g_target_validation_capture.statistics = s_statistics;
    __DMB();
    g_target_validation_capture.sequence++;
}

static void publish_open_loop(void)
{
    uint32_t sequence = g_target_validation_open_loop.sequence;

    g_target_validation_open_loop.sequence = sequence + 1U;
    __DMB();
    s_open_loop_report.sequence = sequence + 1U;
    g_target_validation_open_loop = s_open_loop_report;
    __DMB();
    g_target_validation_open_loop.sequence = sequence + 2U;
}

static void finish_operation(void)
{
    if (s_current_hold_active) {
        uint32_t sequence = g_target_validation_current_hold.sequence;

        current_controller_reset(&s_current_hold.controller);
        if (s_current_hold.speed_mode) { speed_controller_reset(&s_current_hold.speed_controller); }
        s_current_hold_report.active = 0U;
        s_current_hold_report.result = g_target_validation_stop_reason;
        if (s_rotor_current_active) {
            RotorCurrentSnapshot *r = &s_rotor_current_report;
            const CurrentHoldSnapshot *h = &s_current_hold_report;
            sequence = g_target_validation_rotor_current.sequence;
            r->active = 0U; r->result = h->result;
            r->sample_count = h->sample_count; r->enabled_samples = h->enabled_samples;
            r->window_samples = h->window_samples; r->voltage_limited_samples = h->voltage_limited_samples;
            r->mean_id_a = h->mean_alpha_a; r->mean_iq_a = h->mean_beta_a;
            r->peak_tracking_error_a = h->peak_tracking_error_a; r->max_voltage_v = h->max_voltage_v;
            r->peak_abs_current_a = h->peak_abs_current_a; r->min_duty = h->min_duty; r->max_duty = h->max_duty;
            r->displacement_turns = s_current_hold.rotor_displacement;
            r->max_position_age_s = s_current_hold.rotor_max_age_s;
            if (s_rotor_speed_active) {
                RotorSpeedSnapshot *v = &s_rotor_speed_report;
                sequence = g_target_validation_rotor_speed.sequence;
                v->request_sequence = r->request_sequence; v->active = r->active; v->result = r->result;
                v->sample_count = r->sample_count; v->enabled_samples = r->enabled_samples;
                v->window_samples = r->window_samples; v->voltage_limited_samples = r->voltage_limited_samples;
                v->mean_id_a = r->mean_id_a; v->mean_iq_a = r->mean_iq_a;
                v->peak_current_tracking_error_a = r->peak_tracking_error_a;
                v->max_voltage_v = r->max_voltage_v; v->peak_abs_current_a = r->peak_abs_current_a;
                v->min_duty = r->min_duty; v->max_duty = r->max_duty;
                v->start_position_turns = r->start_position_turns; v->end_position_turns = r->end_position_turns;
                v->displacement_turns = r->displacement_turns; v->max_abs_velocity_rpm = r->max_abs_velocity_rpm;
                v->last_velocity_rpm = r->last_velocity_rpm; v->mean_velocity_rpm = r->mean_velocity_rpm;
                v->max_position_age_s = r->max_position_age_s;
                v->last_iq_reference_a = s_current_hold.active_iq_reference_a;
                v->speed_update_count = s_current_hold.speed_update_count;
                v->speed_current_limited_updates = s_current_hold.speed_current_limited_updates;
                v->current_integral_limited_samples = s_current_hold.current_integral_limited_samples;
                g_target_validation_rotor_speed.sequence = sequence + 1U;
                __DMB();
                v->sequence = sequence + 1U;
                g_target_validation_rotor_speed = *v;
                __DMB();
                g_target_validation_rotor_speed.sequence = sequence + 2U;
                s_rotor_speed_active = false;
            } else {
                g_target_validation_rotor_current.sequence = sequence + 1U;
                __DMB();
                r->sequence = sequence + 1U;
                g_target_validation_rotor_current = *r;
                __DMB();
                g_target_validation_rotor_current.sequence = sequence + 2U;
            }
            s_rotor_current_active = false;
        } else {
            g_target_validation_current_hold.sequence = sequence + 1U;
            __DMB();
            s_current_hold_report.sequence = sequence + 1U;
            g_target_validation_current_hold = s_current_hold_report;
            __DMB();
            g_target_validation_current_hold.sequence = sequence + 2U;
        }
        s_current_hold_active = false;
    }
    if (s_open_loop_active) {
        if (s_open_loop.state != VOLTAGE_OPEN_LOOP_COMPLETE
            && s_open_loop.state != VOLTAGE_OPEN_LOOP_FAILED) {
            voltage_open_loop_stop(&s_open_loop);
        }
        s_open_loop_report.active = 0U;
        s_open_loop_report.result = g_target_validation_stop_reason;
        s_open_loop_report.module_state = (uint32_t)s_open_loop.state;
        s_open_loop_report.module_reason = (uint32_t)s_open_loop.reason;
        s_open_loop_active = false;
        publish_open_loop();
    }
    if (s_angle_survey_active) {
        uint32_t i;
        uint32_t sequence = g_target_validation_angle_survey.sequence;

        s_angle_survey_report.result = g_target_validation_stop_reason;
        s_angle_survey_report.stable_window_mask = 0U;
        for (i = 0U; i < 3U; i++) {
            const TargetValidationAngleWindow *w = &s_angle_survey_report.windows[i];

            /* 只在正常完成且每个窗口自身满足观察条件时标记；不授予标定或 RUN。 */
            if (g_target_validation_stop_reason == TARGET_VALIDATION_RESULT_COMPLETE &&
                w->sample_count >= 200U && w->last_position_time >= w->first_position_time + 4500U &&
                w->span_turns <= 0.002f) {
                s_angle_survey_report.stable_window_mask |= 1U << i;
            }
        }
        if (s_current_survey_active) {
            TargetValidationCurrentSurveySnapshot result = {0};
            sequence = g_target_validation_current_survey.sequence;
            result.request_sequence = s_angle_survey_report.request_sequence;
            result.result = s_angle_survey_report.result;
            result.stable_window_mask = s_angle_survey_report.stable_window_mask;
            result.excursion_turns = 1.0f;
            result.current_reference_a = s_current_hold_report.alpha_reference_a;
            for (i = 0U; i < 3U; i++) { result.windows[i] = s_angle_survey_report.windows[i]; }
            g_target_validation_current_survey.sequence = sequence + 1U;
            __DMB();
            result.sequence = sequence + 1U;
            g_target_validation_current_survey = result;
            __DMB();
            g_target_validation_current_survey.sequence = sequence + 2U;
            s_current_survey_active = false;
        } else {
            g_target_validation_angle_survey.sequence = sequence + 1U;
            __DMB();
            s_angle_survey_report.sequence = sequence + 1U;
            g_target_validation_angle_survey = s_angle_survey_report;
            __DMB();
            g_target_validation_angle_survey.sequence = sequence + 2U;
        }
        s_angle_survey_active = false;
    }
    if (s_capturing) {
        s_statistics.result = g_target_validation_stop_reason;
        if (s_statistics.sample_count > 1U) {
            s_statistics.phase_b.variance_a2 = s_m2_b / (float)(s_statistics.sample_count - 1U);
            s_statistics.phase_c.variance_a2 = s_m2_c / (float)(s_statistics.sample_count - 1U);
        }
        publish_capture();
    }
    s_operation_active = false;
    s_capturing = false;
    s_powered = false;
    s_probe_source = PROTECTION_PROBE_NONE;
    s_timing_case = 0U;
    g_target_validation.state = TARGET_VALIDATION_STATE_OBSERVE;
    g_target_validation.result = g_target_validation_stop_reason;
}

/* CubeMX 已将 TIM6 配置为 1 MHz 计数，并与 DAC 共用中断。
 * 只有验证事务在运行时修改它的周期并启动计数。
 * DWT 经过时间不依赖 ADC 回调或 ControlTime 递增。 */
static void guard_timer_start(uint32_t now_cycles, uint32_t duration_us)
{
    uint32_t primask = __get_PRIMASK();

    __disable_irq();
    guard_timer_stop();
    s_deadline_cycles = now_cycles + duration_us * CYCLES_PER_US;
    s_last_sample_cycles = now_cycles;
    s_previous_guard_cycles = now_cycles;
    g_target_validation_stop_reason = TARGET_VALIDATION_RESULT_NONE;
    TIM6->ARR = GUARD_PERIOD_US - 1U;
    TIM6->CNT = 0U;
    TIM6->EGR = TIM_EGR_UG;
    __HAL_TIM_CLEAR_FLAG(&htim6, TIM_FLAG_UPDATE);
    s_deadline_active = true;
    __DMB();
    SET_BIT(TIM6->DIER, TIM_DIER_UIE);
    SET_BIT(TIM6->CR1, TIM_CR1_CEN);
    restore_interrupts(primask);
}

static void check_deadline(uint32_t now_cycles)
{
    if (!s_deadline_active) {
        return;
    }
    if ((uint32_t)(now_cycles - s_last_sample_cycles) > CURRENT_MAX_GAP_US * CYCLES_PER_US) {
        stop_operation(TARGET_VALIDATION_RESULT_BAD_CURRENT);
    } else if ((int32_t)(now_cycles - s_deadline_cycles) >= 0) {
        stop_operation(s_capturing ? TARGET_VALIDATION_RESULT_BAD_CURRENT
            : ((s_open_loop_active || s_current_hold_active) ? TARGET_VALIDATION_RESULT_OPEN_LOOP_TIMEOUT
                : TARGET_VALIDATION_RESULT_COMPLETE));
    }
}

void target_validation_guard_irq(void)
{
    uint32_t now_cycles;

    if (((TIM6->SR & TIM_SR_UIF) == 0U) || ((TIM6->DIER & TIM_DIER_UIE) == 0U)) {
        return; /* 共用的 DAC 中断仍交给生成代码中的处理函数。 */
    }
    __HAL_TIM_CLEAR_FLAG(&htim6, TIM_FLAG_UPDATE);
    s_guard_irq_count++;
    now_cycles = DWT->CYCCNT;
    if (s_deadline_active && (now_cycles == s_previous_guard_cycles)) {
        stop_operation(TARGET_VALIDATION_RESULT_BAD_CLOCK);
    } else {
        check_deadline(now_cycles);
    }
    s_previous_guard_cycles = now_cycles;
}

static void trip_notify(void *context)
{
    (void)context;
    g_platform_bringup.trip_notifications++;
    s_trip_rescan_pending = true;
    if (s_operation_active) {
        stop_operation(TARGET_VALIDATION_RESULT_HARDWARE_TRIP);
    }
}

static bool rescan_trip(HardwareTripSnapshot *out)
{
    if (!g_platform_bringup.trip_armed || !hardware_trip_get_snapshot(out)) {
        g_platform_bringup.trip_snapshot_valid = false;
        realtime_motor_io_safe_off();
        return false;
    }
    g_platform_bringup.trip_condition_active = out->condition_active;
    g_platform_bringup.trip_causes = out->causes;
    g_platform_bringup.trip_snapshot_valid = true;
    g_platform_bringup.trip_rescans++;
    return true;
}

static bool trip_healthy(void)
{
    HardwareTripSnapshot trip;
    bool valid = rescan_trip(&trip);

    g_target_validation.trip_valid = valid ? 1U : 0U;
    if (valid) {
        g_target_validation.trip = trip;
    }
    return valid && !trip.condition_active && (trip.causes == HARDWARE_TRIP_CAUSE_NONE);
}

static bool read_power_bus(ControlTime now, BusObservation *bus)
{
    bool available = bus_observation_get_latest(bus);
    uint32_t mask = 0U;

    if (!available) { mask = TARGET_BUS_FAILURE_UNAVAILABLE; }
    else {
        if (!bus->acquisition_valid) { mask |= TARGET_BUS_FAILURE_INVALID; }
        if (!isfinite(bus->voltage_v)) { mask |= TARGET_BUS_FAILURE_NONFINITE; }
        else if (bus->voltage_v < 10.0f) { mask |= TARGET_BUS_FAILURE_UNDERVOLTAGE; }
        else if (bus->voltage_v > 14.0f) { mask |= TARGET_BUS_FAILURE_OVERVOLTAGE; }
        if (bus->observation_time > now) { mask |= TARGET_BUS_FAILURE_FUTURE; }
        else if (now - bus->observation_time > BUS_MAX_AGE_TICKS) { mask |= TARGET_BUS_FAILURE_STALE; }
    }
    if (mask != 0U) {
        /* 诊断复制不占用关断前的时间；同一ADC写者只锁存启动后的首次失败。 */
        stop_operation(TARGET_VALIDATION_RESULT_BAD_BUS);
        if (g_target_validation_bus_failure.sequence == 0U) {
            TargetValidationBusFailure failure = {0};
            failure.request_sequence = s_operation_active ? g_target_validation.active_request_sequence
                : g_target_validation.command_sequence;
            failure.failure_mask = mask;
            failure.publication_available = available ? 1U : 0U;
            failure.sample_time = now;
            if (available) {
                failure.observation_time = bus->observation_time;
                failure.voltage_v = bus->voltage_v;
                failure.acquisition_valid = bus->acquisition_valid ? 1U : 0U;
            }
            failure.diagnostic_available = g431_bus_acquisition_get_diagnostic(&failure.acquisition) ? 1U : 0U;
            g_target_validation_bus_failure.sequence = 1U;
            __DMB();
            failure.sequence = 1U;
            g_target_validation_bus_failure = failure;
            __DMB();
            g_target_validation_bus_failure.sequence = 2U;
        }
        return false;
    }
    return true;
}

static bool burst_bus_valid(ControlTime now)
{
    BusObservation bus;
    return read_power_bus(now, &bus);
}

__STATIC_FORCEINLINE bool timing_bus_valid(ControlTime now)
{
    BusObservation bus;

    return bus_observation_get_latest(&bus) && bus.acquisition_valid
        && (bus.observation_time <= now) && ((now - bus.observation_time) <= BUS_MAX_AGE_TICKS)
        && (bus.voltage_v >= 0.0f) && (bus.voltage_v < 4.0f);
}

static bool is_timing_command(uint32_t command)
{
    return (command >= TARGET_VALIDATION_COMMAND_TIMING_IMMEDIATE)
        && (command <= TARGET_VALIDATION_COMMAND_TIMING_CANCEL);
}

static bool open_loop_zero_valid(void)
{
    const TargetValidationPhaseStatistics *b = &s_statistics.phase_b;
    const TargetValidationPhaseStatistics *c = &s_statistics.phase_c;

    return s_statistics.result == TARGET_VALIDATION_RESULT_COMPLETE
        && s_statistics.sample_count == TARGET_VALIDATION_CURRENT_SAMPLE_COUNT
        && s_statistics.request_sequence != 0U
        && finite_float(b->mean_a) && finite_float(c->mean_a)
        && finite_float(b->min_a) && finite_float(b->max_a)
        && finite_float(c->min_a) && finite_float(c->max_a)
        && b->mean_a >= -0.10f && b->mean_a <= 0.10f
        && c->mean_a >= -0.10f && c->mean_a <= 0.10f
        && b->max_a >= b->min_a && b->max_a - b->min_a <= 0.05f
        && c->max_a >= c->min_a && c->max_a - c->min_a <= 0.05f;
}

static bool open_loop_position_valid(ControlTime now, PositionObservation *position)
{
    return position_observation_get_latest(position) && position->acquisition_valid
        && position->referenced && finite_float(position->mechanical_position_turns)
        && position->mechanical_position_turns >= 0.0f
        && position->mechanical_position_turns < 1.0f
        && position->observation_time <= now
        && now - position->observation_time <= BUS_MAX_AGE_TICKS;
}

static bool open_loop_input(const PhaseCurrentObservation *current, VoltageOpenLoopInput *input)
{
    BusObservation bus;
    float b = current->phase_b_a - s_open_loop_report.offset_b_a;
    float c = current->phase_c_a - s_open_loop_report.offset_c_a;
    uint32_t index;

    input->now = current->observation_time;
    input->current_time = current->observation_time;
    input->phase_current_a[0] = -(b + c);
    input->phase_current_a[1] = b;
    input->phase_current_a[2] = c;
    input->permitted = true; /* 已接受的一次性台架请求，任何关断原因会先终止事务。 */
    input->current_valid = current_valid(current);
    input->fault_active = false;
    input->stop_requested = false;
    if (!bus_observation_get_latest(&bus)) {
        return false;
    }
    input->bus_time = bus.observation_time;
    input->bus_voltage_v = bus.voltage_v;
    input->bus_valid = bus.acquisition_valid;
    for (index = 0U; index < 3U; index++) {
        float value = input->phase_current_a[index];
        float absolute = value < 0.0f ? -value : value;

        if (!finite_float(value)) {
            return false;
        }
        if (absolute > s_open_loop_report.peak_abs_current_a) {
            s_open_loop_report.peak_abs_current_a = absolute;
        }
    }
    return true;
}

static TargetValidationResult open_loop_module_result(void)
{
    if (s_open_loop.state == VOLTAGE_OPEN_LOOP_COMPLETE) {
        return TARGET_VALIDATION_RESULT_COMPLETE;
    }
    if (s_open_loop.reason == VOLTAGE_OPEN_LOOP_REASON_NOMINAL_CURRENT) {
        return TARGET_VALIDATION_RESULT_NOMINAL_CURRENT;
    }
    if (s_open_loop.reason == VOLTAGE_OPEN_LOOP_REASON_TIMEOUT) {
        return TARGET_VALIDATION_RESULT_OPEN_LOOP_TIMEOUT;
    }
    return TARGET_VALIDATION_RESULT_OPEN_LOOP_REJECTED;
}

static bool prepare_open_loop(const TargetValidationMailbox *request,
    const TargetValidationOpenLoopConfig *requested, const PhaseCurrentObservation *current,
    uint32_t now_cycles, bool survey)
{
    TargetValidationOpenLoopSnapshot empty = {0};
    VoltageOpenLoopConfig config = {0};
    PositionObservation position;
    TargetValidationResult failure = TARGET_VALIDATION_RESULT_NONE;

    s_open_loop_report = empty;
    s_open_loop_report.request_sequence = request->sequence;
    s_open_loop_report.voltage_v = requested->voltage_v;
    s_open_loop_report.electrical_frequency_hz = survey ? 0.0f : requested->electrical_frequency_hz;
    s_open_loop_report.min_duty = 0.5f;
    s_open_loop_report.max_duty = 0.5f;
    if (request->power_key != TARGET_VALIDATION_POWER_KEY) {
        failure = TARGET_VALIDATION_RESULT_LOCKED;
    } else if (request->duration_us != (survey ? TARGET_VALIDATION_ANGLE_SURVEY_DURATION_US
            : TARGET_VALIDATION_OPEN_LOOP_DURATION_US)
        || request->common_duty != 0.5f || !finite_float(requested->voltage_v)
        || requested->voltage_v <= 0.0f || requested->voltage_v > 0.5f
        || !finite_float(requested->electrical_frequency_hz)
        || (survey ? (requested->electrical_frequency_hz <= 0.0f || requested->electrical_frequency_hz > 1.0f)
            : (requested->electrical_frequency_hz == 0.0f || requested->electrical_frequency_hz < -5.0f
                || requested->electrical_frequency_hz > 5.0f))) {
        failure = TARGET_VALIDATION_RESULT_BAD_REQUEST;
    } else if (!open_loop_zero_valid()) {
        failure = TARGET_VALIDATION_RESULT_BAD_ZERO;
    } else if (!burst_bus_valid(current->observation_time)) {
        failure = TARGET_VALIDATION_RESULT_BAD_BUS;
    } else if (!open_loop_position_valid(current->observation_time, &position)) {
        failure = TARGET_VALIDATION_RESULT_BAD_POSITION;
    }
    if (failure != TARGET_VALIDATION_RESULT_NONE) {
        s_open_loop_report.result = (uint32_t)failure;
        g_target_validation.result = (uint32_t)failure;
        publish_open_loop();
        return false;
    }
    config.ticks_per_second = s_facts.control_time_ticks_per_second;
    config.voltage_v = requested->voltage_v;
    config.target_electrical_hz = survey ? 0.0f : requested->electrical_frequency_hz;
    config.max_voltage_ratio = s_facts.normal_voltage_utilization;
    config.min_bus_voltage_v = 10.0f;
    config.max_bus_voltage_v = 14.0f;
    config.nominal_current_trip_a = OPEN_LOOP_NOMINAL_CURRENT_TRIP_A;
    config.voltage_ramp_ticks = 10000U;
    config.alignment_hold_ticks = survey ? 20000U : 10000U;
    config.frequency_ramp_ticks = 10000U;
    config.rotation_ticks = 20000U;
    config.voltage_ramp_down_ticks = 10000U;
    config.timeout_ticks = survey ? 110000U : 70000U;
    config.max_step_ticks = s_facts.fast_tick_period_ticks;
    config.max_current_age_ticks = 0U;
    config.max_bus_age_ticks = BUS_MAX_AGE_TICKS;
    config.profile = survey ? VOLTAGE_OPEN_LOOP_STATIC_SURVEY : VOLTAGE_OPEN_LOOP_ROTATING;
    config.excursion_turns = survey ? requested->electrical_frequency_hz : 0.0f;
    if (!voltage_open_loop_init(&s_open_loop, &config)) {
        s_open_loop_report.result = TARGET_VALIDATION_RESULT_OPEN_LOOP_REJECTED;
        g_target_validation.result = TARGET_VALIDATION_RESULT_OPEN_LOOP_REJECTED;
        publish_open_loop();
        return false;
    }
    s_open_loop_report.offset_b_a = s_statistics.phase_b.mean_a;
    s_open_loop_report.offset_c_a = s_statistics.phase_c.mean_a;
    s_open_loop_report.start_position_turns = position.mechanical_position_turns;
    s_open_loop_report.end_position_turns = position.mechanical_position_turns;
    s_open_loop_position = position.mechanical_position_turns;
    s_open_loop_position_time = position.observation_time;
    s_open_loop_sample_time = current->observation_time;
    s_open_loop_report.active = 1U;
    s_angle_survey_active = survey;
    if (survey) {
        TargetValidationAngleSurveySnapshot empty_survey = {0};

        s_angle_survey_report = empty_survey;
        s_angle_survey_report.request_sequence = request->sequence;
        s_angle_survey_report.excursion_turns = config.excursion_turns;
        s_angle_survey_report.voltage_v = config.voltage_v;
        /* 不清版本字；新请求开始即撤销上一份窗口，结束时才发布本次窗口。 */
        g_target_validation_angle_survey.sequence++;
        __DMB();
        g_target_validation_angle_survey.request_sequence = request->sequence;
        g_target_validation_angle_survey.result = TARGET_VALIDATION_RESULT_NONE;
        g_target_validation_angle_survey.stable_window_mask = 0U;
        __DMB();
        g_target_validation_angle_survey.sequence++;
    }
    s_open_loop_active = true;
    s_power_requested = true;
    s_powered = false;
    s_capturing = false;
    s_probe_source = PROTECTION_PROBE_NONE;
    s_staging_ticks = 0U;
    s_operation_active = true;
    g_target_validation.active_request_sequence = request->sequence;
    g_target_validation.state = TARGET_VALIDATION_STATE_STAGING;
    g_target_validation.result = TARGET_VALIDATION_RESULT_NONE;
    guard_timer_start(now_cycles, request->duration_us);
    publish_open_loop();
    return true;
}

static void prepare_current_hold(const TargetValidationMailbox *request,
    const CurrentHoldConfig *config, const PhaseCurrentObservation *current, uint32_t now_cycles,
    bool survey, float excursion, const RotorCurrentConfig *rotor, const RotorSpeedConfig *speed)
{
    CurrentHoldSnapshot empty = {0};
    PositionObservation position;
    TargetValidationResult failure = TARGET_VALIDATION_RESULT_NONE;

    if (request->power_key != TARGET_VALIDATION_POWER_KEY) {
        failure = TARGET_VALIDATION_RESULT_LOCKED;
    } else if (request->duration_us != (speed != NULL ? ROTOR_SPEED_DURATION_US : (rotor != NULL ? ROTOR_CURRENT_DURATION_US : (survey ? CURRENT_SURVEY_DURATION_US : CURRENT_HOLD_DURATION_US))) || request->common_duty != 0.5f
        || s_facts.control_time_ticks_per_second != 20000U || s_facts.fast_tick_period_ticks != 2U
        || (survey && excursion != 1.0f)
        || !(speed != NULL ? current_hold_speed_init(&s_current_hold, speed) : (rotor != NULL ? current_hold_rotor_init(&s_current_hold, rotor)
            : (survey ? current_hold_survey_init(&s_current_hold, config->alpha_reference_a)
            : current_hold_init(&s_current_hold, config))))) {
        failure = TARGET_VALIDATION_RESULT_BAD_REQUEST;
    } else if (!open_loop_zero_valid()) {
        failure = TARGET_VALIDATION_RESULT_BAD_ZERO;
    } else if (!burst_bus_valid(current->observation_time)) {
        failure = TARGET_VALIDATION_RESULT_BAD_BUS;
    } else if (!open_loop_position_valid(current->observation_time, &position)) {
        failure = TARGET_VALIDATION_RESULT_BAD_POSITION;
    }
    s_current_hold_report = empty;
    s_current_hold_report.request_sequence = request->sequence;
    s_current_hold_report.alpha_reference_a = config->alpha_reference_a;
    s_current_hold_report.beta_reference_a = config->beta_reference_a;
    s_current_hold_report.min_duty = 0.5f;
    s_current_hold_report.max_duty = 0.5f;
    s_current_hold_report.offset_b_a = s_statistics.phase_b.mean_a;
    s_current_hold_report.offset_c_a = s_statistics.phase_c.mean_a;
    s_current_hold_active = true;
    s_rotor_current_active = rotor != NULL;
    s_rotor_speed_active = speed != NULL;
    s_current_survey_active = survey;
    s_angle_survey_active = survey;
    if (rotor != NULL) {
        RotorCurrentSnapshot empty_rotor = {0};
        s_rotor_current_report = empty_rotor;
        s_rotor_current_report.request_sequence = request->sequence;
        s_rotor_current_report.iq_reference_a = rotor->iq_reference_a;
        s_rotor_current_report.electrical_offset_turns = rotor->electrical_offset_turns;
        if (failure == TARGET_VALIDATION_RESULT_NONE) {
            s_rotor_current_report.start_position_turns = position.mechanical_position_turns;
            s_rotor_current_report.end_position_turns = position.mechanical_position_turns;
        }
        if (speed != NULL) {
            RotorSpeedSnapshot empty_speed = {0};
            s_rotor_speed_report = empty_speed;
            s_rotor_speed_report.target_velocity_rpm = speed->target_velocity_rpm;
            s_rotor_speed_report.electrical_offset_turns = speed->electrical_offset_turns;
            g_target_validation_rotor_speed.sequence++;
            __DMB();
            g_target_validation_rotor_speed.request_sequence = request->sequence;
            g_target_validation_rotor_speed.active = 1U;
            g_target_validation_rotor_speed.result = TARGET_VALIDATION_RESULT_NONE;
            __DMB();
            g_target_validation_rotor_speed.sequence++;
        } else {
            g_target_validation_rotor_current.sequence++;
            __DMB();
            g_target_validation_rotor_current.request_sequence = request->sequence;
            g_target_validation_rotor_current.active = 1U;
            g_target_validation_rotor_current.result = TARGET_VALIDATION_RESULT_NONE;
            __DMB();
            g_target_validation_rotor_current.sequence++;
        }
    }
    if (survey) {
        TargetValidationAngleSurveySnapshot empty_survey = {0};
        uint32_t i;
        s_angle_survey_report = empty_survey;
        s_angle_survey_report.request_sequence = request->sequence;
        s_angle_survey_report.excursion_turns = 1.0f;
        for (i = 0U; i < 3U; i++) {
            s_angle_window_anchor[i] = s_angle_window_min[i] = 0.0f;
            s_angle_window_max[i] = s_angle_window_mean[i] = 0.0f;
        }
        g_target_validation_current_survey.sequence++;
        __DMB();
        g_target_validation_current_survey.request_sequence = request->sequence;
        g_target_validation_current_survey.result = TARGET_VALIDATION_RESULT_NONE;
        g_target_validation_current_survey.stable_window_mask = 0U;
        __DMB();
        g_target_validation_current_survey.sequence++;
    }
    if (failure != TARGET_VALIDATION_RESULT_NONE) {
        stop_operation(failure);
        finish_operation();
        return;
    }
    s_operation_active = true;
    s_current_hold_report.active = 1U;
    if (rotor == NULL) {
        g_target_validation_current_hold.sequence++;
        __DMB();
        s_current_hold_report.sequence = g_target_validation_current_hold.sequence;
        g_target_validation_current_hold = s_current_hold_report;
        __DMB();
        g_target_validation_current_hold.sequence++;
    }
    s_current_hold_last_sample = current->observation_time;
    s_power_requested = true;
    s_powered = false;
    s_capturing = false;
    s_probe_source = PROTECTION_PROBE_NONE;
    s_staging_ticks = 0U;
    g_target_validation.active_request_sequence = request->sequence;
    g_target_validation.state = TARGET_VALIDATION_STATE_STAGING;
    g_target_validation.result = TARGET_VALIDATION_RESULT_NONE;
    guard_timer_start(now_cycles, request->duration_us);
}

static void consume_command(const PhaseCurrentObservation *current, uint32_t now_cycles)
{
    TargetValidationMailbox request;
    TargetValidationOpenLoopConfig open_loop_config;
    CurrentHoldConfig current_hold_config;
    TargetValidationCurrentSurveyConfig current_survey_config;
    RotorCurrentConfig rotor_current_config;
    RotorSpeedConfig rotor_speed_config;
    ProtectionProbeSource probe_source = PROTECTION_PROBE_NONE;
    uint32_t primask = __get_PRIMASK();
    uint32_t sequence;

    __disable_irq();
    sequence = g_target_validation_request.sequence;
    if ((sequence == 0U) || (sequence == s_last_command_sequence)) {
        restore_interrupts(primask);
        return;
    }
    request = g_target_validation_request;
    open_loop_config = g_target_validation_open_loop_config;
    current_hold_config = g_target_validation_current_hold_config;
    current_survey_config = g_target_validation_current_survey_config;
    rotor_current_config = g_target_validation_rotor_current_config;
    rotor_speed_config = g_target_validation_rotor_speed_config;
    if (request.command == TARGET_VALIDATION_COMMAND_ANGLE_SURVEY) {
        open_loop_config.voltage_v = g_target_validation_angle_survey_config.voltage_v;
        open_loop_config.electrical_frequency_hz = g_target_validation_angle_survey_config.excursion_turns;
    }
    __DMB();
    if (request.sequence != g_target_validation_request.sequence) {
        restore_interrupts(primask);
        return;
    }
    s_last_command_sequence = request.sequence;
    g_target_validation_request.power_key = 0U;
    restore_interrupts(primask);
    g_target_validation.command_sequence = request.sequence;

    if (request.command == TARGET_VALIDATION_COMMAND_SAFE_OFF) {
        stop_operation(TARGET_VALIDATION_RESULT_OPERATOR_STOP);
        finish_operation();
        return;
    }
    if (s_operation_active) {
        g_target_validation.result = TARGET_VALIDATION_RESULT_BUSY;
        return;
    }
    if ((request.command != TARGET_VALIDATION_COMMAND_STAGE_ONLY)
        && (request.command != TARGET_VALIDATION_COMMAND_PWM_BURST)
        && (request.command != TARGET_VALIDATION_COMMAND_CAPTURE_CURRENT)
        && (request.command != TARGET_VALIDATION_COMMAND_BREAK_OVP)
        && (request.command != TARGET_VALIDATION_COMMAND_BREAK_OCP_C)
        && (request.command != TARGET_VALIDATION_COMMAND_BREAK_OCP_AB)
        && (request.command != TARGET_VALIDATION_COMMAND_CLEAR_INJECTION)
        && (request.command != TARGET_VALIDATION_COMMAND_REARM)
        && (request.command != TARGET_VALIDATION_COMMAND_OPEN_LOOP)
        && (request.command != TARGET_VALIDATION_COMMAND_ANGLE_SURVEY)
        && (request.command != TARGET_VALIDATION_COMMAND_CURRENT_HOLD)
        && (request.command != TARGET_VALIDATION_COMMAND_CURRENT_SURVEY)
        && (request.command != TARGET_VALIDATION_COMMAND_ROTOR_CURRENT)
        && (request.command != TARGET_VALIDATION_COMMAND_ROTOR_SPEED)
        && !is_timing_command(request.command)) {
        g_target_validation.result = TARGET_VALIDATION_RESULT_BAD_REQUEST;
        return;
    }
    if ((request.command == TARGET_VALIDATION_COMMAND_CLEAR_INJECTION)
        || (request.command == TARGET_VALIDATION_COMMAND_REARM)) {
        TargetValidationResult result;

        stop_operation(TARGET_VALIDATION_RESULT_OPERATOR_STOP);
        if (request.command == TARGET_VALIDATION_COMMAND_CLEAR_INJECTION) {
            result = protection_probe_clear() ? TARGET_VALIDATION_RESULT_COMPLETE
                : TARGET_VALIDATION_RESULT_PROBE_REJECTED;
        } else {
            bool rearmed = hardware_trip_rearm();

            result = rearmed ? TARGET_VALIDATION_RESULT_COMPLETE : TARGET_VALIDATION_RESULT_REARM_REJECTED;
            if (rearmed && (protection_probe_is_held() || !trip_healthy())) {
                result = TARGET_VALIDATION_RESULT_PROBE_REJECTED;
            }
        }
        (void)trip_healthy(); /* 空闲控制操作后，同时发布当前保护条件与尚未恢复的锁存原因。 */
        stop_operation(result);
        g_target_validation.result = (uint32_t)result;
        return;
    }
    if ((TIM1->BDTR & TIM_BDTR_MOE) != 0U) {
        stop_operation(TARGET_VALIDATION_RESULT_PLATFORM_OFF);
        g_target_validation.result = TARGET_VALIDATION_RESULT_PLATFORM_OFF;
        return;
    }
    if (!current_valid(current)) {
        g_target_validation.result = TARGET_VALIDATION_RESULT_BAD_CURRENT;
        return;
    }
    if (!trip_healthy()) {
        g_target_validation.result = TARGET_VALIDATION_RESULT_HARDWARE_TRIP;
        return;
    }
    if (protection_probe_is_held()) {
        g_target_validation.result = TARGET_VALIDATION_RESULT_PROBE_REJECTED;
        return;
    }
    if (request.command == TARGET_VALIDATION_COMMAND_CURRENT_SURVEY) {
        CurrentHoldConfig config = {current_survey_config.current_reference_a, 0.0f};
        prepare_current_hold(&request, &config, current, now_cycles, true, current_survey_config.excursion_turns, NULL, NULL);
        return;
    }
    if (request.command == TARGET_VALIDATION_COMMAND_CURRENT_HOLD) {
        prepare_current_hold(&request, &current_hold_config, current, now_cycles, false, 0.0f, NULL, NULL);
        return;
    }
    if (request.command == TARGET_VALIDATION_COMMAND_ROTOR_CURRENT) {
        CurrentHoldConfig config = {0.0f, rotor_current_config.iq_reference_a};
        prepare_current_hold(&request, &config, current, now_cycles, false, 0.0f, &rotor_current_config, NULL);
        return;
    }
    if (request.command == TARGET_VALIDATION_COMMAND_ROTOR_SPEED) {
        CurrentHoldConfig envelope = {0.0f, ROTOR_SPEED_MAX_IQ_A};
        RotorCurrentConfig rotor = {ROTOR_SPEED_MAX_IQ_A, rotor_speed_config.electrical_offset_turns};
        prepare_current_hold(&request, &envelope, current, now_cycles, false, 0.0f, &rotor, &rotor_speed_config);
        return;
    }
    if (request.command == TARGET_VALIDATION_COMMAND_OPEN_LOOP || request.command == TARGET_VALIDATION_COMMAND_ANGLE_SURVEY) {
        (void)prepare_open_loop(&request, &open_loop_config, current, now_cycles,
            request.command == TARGET_VALIDATION_COMMAND_ANGLE_SURVEY);
        return;
    }
    if (is_timing_command(request.command)) {
        /* 旧探针只验证 RCR=0 的原半周窗口，不能用其 PASS 证明新 slot 时序。 */
        if (s_facts.fast_tick_period_ticks != 1U) {
            g_target_validation.result = TARGET_VALIDATION_RESULT_TIMING_REJECTED;
            return;
        }
        if ((request.power_key != 0U) || (request.duration_us != 0U) || (request.common_duty != 0.0f)) {
            g_target_validation.result = TARGET_VALIDATION_RESULT_BAD_REQUEST;
            return;
        }
        if (!timing_bus_valid(current->observation_time)) {
            g_target_validation.result = TARGET_VALIDATION_RESULT_BAD_BUS;
            return;
        }
        if (g_target_validation_timing_recovery_ticks < 2U) {
            g_target_validation.result = TARGET_VALIDATION_RESULT_TIMING_REJECTED;
            return;
        }
        /* safe_off 使本拍窗口失效；故只排队，下一次 callback 才执行探测。 */
        stop_operation(TARGET_VALIDATION_RESULT_NONE);
        s_power_requested = false;
        s_powered = false;
        s_capturing = false;
        s_probe_source = PROTECTION_PROBE_NONE;
        s_timing_case = request.command;
        s_operation_active = true;
        g_target_validation.active_request_sequence = request.sequence;
        g_target_validation.state = TARGET_VALIDATION_STATE_TIMING;
        g_target_validation.result = TARGET_VALIDATION_RESULT_NONE;
        return;
    }
    if (request.command == TARGET_VALIDATION_COMMAND_CAPTURE_CURRENT) {
        TargetValidationCurrentStatistics empty = {0};

        empty.request_sequence = request.sequence;
        empty.interval_min_cycles = UINT32_MAX;
        s_statistics = empty;
        s_m2_b = 0.0f;
        s_m2_c = 0.0f;
        s_capturing = true;
        s_operation_active = true;
        g_target_validation.active_request_sequence = request.sequence;
        g_target_validation.state = TARGET_VALIDATION_STATE_CAPTURING_CURRENT;
        g_target_validation.result = TARGET_VALIDATION_RESULT_NONE;
        g_target_validation.capture_samples = 0U;
        publish_capture();
        guard_timer_start(now_cycles, TARGET_VALIDATION_CURRENT_SAMPLE_COUNT * CURRENT_PERIOD_US
            + CAPTURE_MARGIN_US);
        return;
    }
    if (!(request.common_duty >= 0.10f && request.common_duty <= 0.90f)
        || (request.duration_us < 1000U) || (request.duration_us > TARGET_VALIDATION_MAX_BURST_US)) {
        g_target_validation.result = TARGET_VALIDATION_RESULT_BAD_REQUEST;
        return;
    }
    if ((request.command >= TARGET_VALIDATION_COMMAND_BREAK_OVP)
        && (request.command <= TARGET_VALIDATION_COMMAND_BREAK_OCP_AB)) {
        if ((request.duration_us != TARGET_VALIDATION_PROBE_DURATION_US) || (request.common_duty != 0.5f)) {
            g_target_validation.result = TARGET_VALIDATION_RESULT_BAD_REQUEST;
            return;
        }
        probe_source = (ProtectionProbeSource)(request.command - TARGET_VALIDATION_COMMAND_BREAK_OVP + 1U);
    }
    s_power_requested = (request.command == TARGET_VALIDATION_COMMAND_PWM_BURST)
        || (probe_source != PROTECTION_PROBE_NONE);
    if (s_power_requested && (request.power_key != TARGET_VALIDATION_POWER_KEY)) {
        g_target_validation.result = TARGET_VALIDATION_RESULT_LOCKED;
        return;
    }
    if (s_power_requested && !burst_bus_valid(current->observation_time)) {
        g_target_validation.result = TARGET_VALIDATION_RESULT_BAD_BUS;
        return;
    }
    if ((probe_source != PROTECTION_PROBE_NONE) && !protection_probe_prepare(probe_source, request.sequence)) {
        g_target_validation.result = TARGET_VALIDATION_RESULT_PROBE_REJECTED;
        return;
    }
    s_probe_source = probe_source;
    s_probe_powered_ticks = 0U;
    s_duty.phase_a_duty = request.common_duty;
    s_duty.phase_b_duty = request.common_duty;
    s_duty.phase_c_duty = request.common_duty;
    s_staging_ticks = 0U;
    s_capturing = false;
    s_powered = false;
    s_operation_active = true;
    g_target_validation.active_request_sequence = request.sequence;
    g_target_validation.state = TARGET_VALIDATION_STATE_STAGING;
    g_target_validation.result = TARGET_VALIDATION_RESULT_NONE;
    /* 从接收时计总期限；首次 stage 留给下一采样，避免解析请求耗尽本拍窗口。 */
    guard_timer_start(now_cycles, request.duration_us);
}

static bool accumulate_phase(TargetValidationPhaseStatistics *statistics,
    float *m2, float value, uint32_t count)
{
    float delta = value - statistics->mean_a;

    statistics->mean_a += delta / (float)count;
    *m2 += delta * (value - statistics->mean_a);
    if ((count == 1U) || (value < statistics->min_a)) {
        statistics->min_a = value;
    }
    if ((count == 1U) || (value > statistics->max_a)) {
        statistics->max_a = value;
    }
    return finite_float(statistics->mean_a) && finite_float(*m2);
}

static void record_capture(const PhaseCurrentObservation *current, uint32_t interval_cycles)
{
    uint32_t count = s_statistics.sample_count + 1U;

    if ((count > 1U) && (((current->observation_time - s_statistics.last_sample_time)
            != s_facts.fast_tick_period_ticks)
        || (interval_cycles < CURRENT_MIN_GAP_US * CYCLES_PER_US)
        || (interval_cycles > CURRENT_MAX_GAP_US * CYCLES_PER_US))) {
        stop_operation(TARGET_VALIDATION_RESULT_BAD_CURRENT);
        return;
    }
    if (!accumulate_phase(&s_statistics.phase_b, &s_m2_b, current->phase_b_a, count)
        || !accumulate_phase(&s_statistics.phase_c, &s_m2_c, current->phase_c_a, count)) {
        stop_operation(TARGET_VALIDATION_RESULT_BAD_CURRENT);
        return;
    }
    if (count == 1U) {
        s_statistics.first_sample_time = current->observation_time;
    } else {
        if (interval_cycles < s_statistics.interval_min_cycles) {
            s_statistics.interval_min_cycles = interval_cycles;
        }
        if (interval_cycles > s_statistics.interval_max_cycles) {
            s_statistics.interval_max_cycles = interval_cycles;
        }
    }
    s_statistics.sample_count = count;
    s_statistics.last_sample_time = current->observation_time;
    g_target_validation.capture_samples = count;
    if (count == TARGET_VALIDATION_CURRENT_SAMPLE_COUNT) {
        stop_operation(TARGET_VALIDATION_RESULT_COMPLETE);
    }
}

static void run_timing_operation(const PhaseCurrentObservation *current, uint32_t interval_cycles)
{
    TimingProbeOutcome outcome;
    HardwareTripSnapshot trip;
    bool eligible;

    eligible = (g_target_validation_stop_reason == TARGET_VALIDATION_RESULT_NONE)
        && current_valid(current)
        && (interval_cycles >= CURRENT_MIN_GAP_US * CYCLES_PER_US)
        && (interval_cycles <= CURRENT_MAX_GAP_US * CYCLES_PER_US);
    eligible = eligible && g_platform_bringup.trip_armed && hardware_trip_get_snapshot(&trip)
        && !trip.condition_active && (trip.causes == HARDWARE_TRIP_CAUSE_NONE)
        && !protection_probe_is_held();
    eligible = eligible && timing_bus_valid(current->observation_time);
    outcome = timing_probe_run(g_target_validation.active_request_sequence, s_timing_case, eligible);
    g_target_validation.stage_attempts += g_target_validation_timing.stage_called
        + g_target_validation_timing.second_stage_called;
    g_target_validation.stage_successes += g_target_validation_timing.stage_accepted
        + g_target_validation_timing.second_stage_accepted;
    if ((g_target_validation_timing.entry.bdtr & TIM_BDTR_MOE) != 0U) {
        g_platform_bringup.moe_violations++;
        g_platform_bringup.status = 111U;
    }
    g_target_validation_timing_recovery_ticks = 0U;
    stop_operation(outcome == TIMING_PROBE_PASS ? TARGET_VALIDATION_RESULT_COMPLETE
        : (outcome == TIMING_PROBE_TIMEOUT ? TARGET_VALIDATION_RESULT_TIMING_TIMEOUT
            : TARGET_VALIDATION_RESULT_TIMING_REJECTED));
    finish_operation();
    return;
}

static void record_open_loop_duty(const PhaseDutyRequest *duty)
{
    const float values[3] = {duty->phase_a_duty, duty->phase_b_duty, duty->phase_c_duty};
    uint32_t index;

    for (index = 0U; index < 3U; index++) {
        if (values[index] < s_open_loop_report.min_duty) {
            s_open_loop_report.min_duty = values[index];
        }
        if (values[index] > s_open_loop_report.max_duty) {
            s_open_loop_report.max_duty = values[index];
        }
    }
}

static bool record_angle_survey(const PositionObservation *position, float nominal_b, float nominal_c)
{
    uint64_t elapsed;
    uint64_t ends[3];
    ControlTime started = s_current_survey_active ? s_current_hold_started : s_open_loop.started_at;
    uint32_t i;

    if (!s_angle_survey_active || position->observation_time < started) {
        return true;
    }
    elapsed = position->observation_time - started;
    ends[0] = (uint64_t)s_open_loop.config.voltage_ramp_ticks + s_open_loop.config.alignment_hold_ticks;
    ends[1] = ends[0] + s_open_loop.config.frequency_ramp_ticks + s_open_loop.config.alignment_hold_ticks;
    ends[2] = ends[1] + s_open_loop.config.frequency_ramp_ticks + s_open_loop.config.alignment_hold_ticks;
    if (s_current_survey_active) { ends[0] = 30000U; ends[1] = 60000U; ends[2] = 90000U; }
    for (i = 0U; i < 3U; i++) {
        TargetValidationAngleWindow *w = &s_angle_survey_report.windows[i];
        float delta, count, mean;

        if (elapsed < ends[i] - 5000U || elapsed >= ends[i] ||
            (w->sample_count != 0U && position->observation_time == w->last_position_time)) {
            continue;
        }
        if (w->sample_count == 0U) {
            s_angle_window_anchor[i] = position->mechanical_position_turns;
            s_angle_window_min[i] = 0.0f;
            s_angle_window_max[i] = 0.0f;
            s_angle_window_mean[i] = 0.0f;
            w->first_position_time = position->observation_time;
            w->first_position_turns = position->mechanical_position_turns;
        }
        delta = position->mechanical_position_turns - s_angle_window_anchor[i];
        if (delta > 0.5f) {
            delta -= 1.0f;
        } else if (delta < -0.5f) {
            delta += 1.0f;
        }
        /* 固定保持段不能跨半圈；异常观测立即撤销，不进行模糊展开。 */
        if (delta <= -0.25f || delta >= 0.25f) {
            return false;
        }
        if (delta < s_angle_window_min[i]) {
            s_angle_window_min[i] = delta;
        }
        if (delta > s_angle_window_max[i]) {
            s_angle_window_max[i] = delta;
        }
        count = (float)(++w->sample_count);
        s_angle_window_mean[i] += (delta - s_angle_window_mean[i]) / count;
        mean = s_angle_window_anchor[i] + s_angle_window_mean[i];
        if (mean < 0.0f) {
            mean += 1.0f;
        } else if (mean >= 1.0f) {
            mean -= 1.0f;
        }
        w->mean_position_turns = mean;
        w->span_turns = s_angle_window_max[i] - s_angle_window_min[i];
        w->last_position_time = position->observation_time;
        w->last_position_turns = position->mechanical_position_turns;
        w->mean_nominal_b_a += (nominal_b - w->mean_nominal_b_a) / count;
        w->mean_nominal_c_a += (nominal_c - w->mean_nominal_c_a) / count;
    }
    return true;
}

static void run_open_loop(const PhaseCurrentObservation *current, uint32_t interval_cycles)
{
    VoltageOpenLoopInput input;
    PositionObservation position;
    PhaseDutyRequest duty = {0.5f, 0.5f, 0.5f};

    /* 事务独占本 Target 的 sample consumer，不与旧 common_duty 路径并写。 */
    if (interval_cycles < CURRENT_MIN_GAP_US * CYCLES_PER_US
        || interval_cycles > CURRENT_MAX_GAP_US * CYCLES_PER_US
        || current->observation_time <= s_open_loop_sample_time
        || current->observation_time - s_open_loop_sample_time != s_facts.fast_tick_period_ticks) {
        stop_operation(TARGET_VALIDATION_RESULT_BAD_CURRENT);
        return;
    }
    s_open_loop_sample_time = current->observation_time;
    if (!open_loop_position_valid(current->observation_time, &position)
        || position.observation_time < s_open_loop_position_time
        || (position.observation_time == s_open_loop_position_time
            && position.mechanical_position_turns != s_open_loop_position)) {
        stop_operation(TARGET_VALIDATION_RESULT_BAD_POSITION);
        return;
    }
    if (position.observation_time != s_open_loop_position_time) {
        float delta = position.mechanical_position_turns - s_open_loop_position;

        if (delta > 0.5f) {
            delta -= 1.0f;
        } else if (delta < -0.5f) {
            delta += 1.0f;
        } else if (delta == 0.5f || delta == -0.5f) {
            stop_operation(TARGET_VALIDATION_RESULT_BAD_POSITION);
            return;
        }
        s_open_loop_report.displacement_turns += delta;
        s_open_loop_report.end_position_turns = position.mechanical_position_turns;
        s_open_loop_position = position.mechanical_position_turns;
        s_open_loop_position_time = position.observation_time;
    }
    if (!open_loop_input(current, &input)) {
        stop_operation(TARGET_VALIDATION_RESULT_BAD_CURRENT);
        return;
    }
    s_open_loop_report.sample_count++;
    if (s_open_loop_report.peak_abs_current_a >= OPEN_LOOP_NOMINAL_CURRENT_TRIP_A) {
        stop_operation(TARGET_VALIDATION_RESULT_NOMINAL_CURRENT);
        return;
    }
    if (!s_powered) {
        /* 首拍中性 preload，在下一目标 UEV 装载；第二拍再 stage 中性并 enable。
         * 电压模块从实际 enable 成功后的时刻起计时，不把预热算成定向输出。 */
        g_target_validation.stage_attempts++;
        if (!realtime_motor_io_stage_phase_duty(&duty)) {
            stop_operation(TARGET_VALIDATION_RESULT_STAGE_REJECTED);
            return;
        }
        g_target_validation.stage_successes++;
        s_staging_ticks++;
        if (s_staging_ticks >= 2U) {
            if (!voltage_open_loop_start(&s_open_loop, &input)) {
                stop_operation(open_loop_module_result());
                return;
            }
            g_target_validation.enable_attempts++;
            if (!realtime_motor_io_power_enable()) {
                stop_operation(TARGET_VALIDATION_RESULT_ENABLE_REJECTED);
                return;
            }
            s_powered = true;
            s_open_loop_report.enabled_samples++;
            g_target_validation.enable_successes++;
            g_target_validation.state = TARGET_VALIDATION_STATE_POWERED;
        }
    } else {
        s_open_loop_report.enabled_samples++;
        if (!voltage_open_loop_update(&s_open_loop, &input)) {
            stop_operation(open_loop_module_result());
            return;
        }
        if (!record_angle_survey(&position, input.phase_current_a[1], input.phase_current_a[2])) {
            stop_operation(TARGET_VALIDATION_RESULT_BAD_POSITION);
            return;
        }
        duty.phase_a_duty = s_open_loop.output.duty_a;
        duty.phase_b_duty = s_open_loop.output.duty_b;
        duty.phase_c_duty = s_open_loop.output.duty_c;
        g_target_validation.stage_attempts++;
        if (!s_open_loop.output.output_requested || !realtime_motor_io_stage_phase_duty(&duty)) {
            stop_operation(TARGET_VALIDATION_RESULT_STAGE_REJECTED);
            return;
        }
        g_target_validation.stage_successes++;
        record_open_loop_duty(&duty);
    }
    s_open_loop_report.module_state = (uint32_t)s_open_loop.state;
    s_open_loop_report.module_reason = (uint32_t)s_open_loop.reason;
    publish_open_loop();
}

static void run_current_hold(const PhaseCurrentObservation *current, uint32_t interval_cycles)
{
    CurrentHoldSnapshot *report = &s_current_hold_report;
    PositionObservation position;
    BusObservation bus;
    CurrentControllerOutput output;
    PhaseDutyRequest duty = {0.5f, 0.5f, 0.5f};
    ControlTime elapsed;
    /* 与Observe一致：先扣校正前零偏，再将Shunt方向转换为桥→电机正方向。 */
    float b = -(current->phase_b_a - report->offset_b_a);
    float c = -(current->phase_c_a - report->offset_c_a);
    float a = -(b + c);
    float alpha = a;
    float beta = (b - c) * 0.5773502691896258f;
    float peak = fmaxf(fabsf(a), fmaxf(fabsf(b), fabsf(c)));
    float voltage, valpha, vbeta;
    bool window;

    if (interval_cycles < CURRENT_MIN_GAP_US * CYCLES_PER_US
        || interval_cycles > CURRENT_MAX_GAP_US * CYCLES_PER_US
        || current->observation_time <= s_current_hold_last_sample
        || current->observation_time - s_current_hold_last_sample != 2U) {
        stop_operation(TARGET_VALIDATION_RESULT_BAD_CURRENT);
        return;
    }
    s_current_hold_last_sample = current->observation_time;
    if (!isfinite(peak) || peak >= ((s_current_survey_active || s_rotor_current_active) ? CURRENT_SURVEY_NOMINAL_TRIP_A : OPEN_LOOP_NOMINAL_CURRENT_TRIP_A)) {
        stop_operation(TARGET_VALIDATION_RESULT_NOMINAL_CURRENT);
        return;
    }
    if (!open_loop_position_valid(current->observation_time, &position)) {
        stop_operation(TARGET_VALIDATION_RESULT_BAD_POSITION);
        return;
    }
    if (s_rotor_current_active) {
        if (!current_hold_rotor_feedback(&s_current_hold, &position, current->observation_time)) {
            stop_operation(s_current_hold.motion_limit ? TARGET_VALIDATION_RESULT_ROTOR_MOTION_LIMIT
                : TARGET_VALIDATION_RESULT_BAD_POSITION);
            return;
        }
        s_rotor_current_report.end_position_turns = position.mechanical_position_turns;
        s_rotor_current_report.last_velocity_rpm = s_current_hold.velocity.velocity_rpm;
        s_rotor_current_report.max_abs_velocity_rpm = fmaxf(s_rotor_current_report.max_abs_velocity_rpm,
            fabsf(s_current_hold.velocity.velocity_rpm));
        if (!s_current_hold.feedback_ready) { return; }
    }
    if (!read_power_bus(current->observation_time, &bus)) {
        stop_operation(TARGET_VALIDATION_RESULT_BAD_BUS);
        return;
    }
    report->sample_count++;
    report->peak_abs_current_a = fmaxf(peak, report->peak_abs_current_a);
    if (!s_powered) {
        g_target_validation.stage_attempts++;
        if (!realtime_motor_io_stage_phase_duty(&duty)) {
            stop_operation(TARGET_VALIDATION_RESULT_STAGE_REJECTED);
            return;
        }
        g_target_validation.stage_successes++;
        if (++s_staging_ticks >= 2U) {
            s_current_hold_started = current->observation_time;
            g_target_validation.enable_attempts++;
            if (!realtime_motor_io_power_enable()) {
                stop_operation(TARGET_VALIDATION_RESULT_ENABLE_REJECTED);
                return;
            }
            s_powered = true;
            report->enabled_samples++;
            g_target_validation.enable_successes++;
            g_target_validation.state = TARGET_VALIDATION_STATE_POWERED;
        }
        return;
    }
    report->enabled_samples++;
    elapsed = current->observation_time - s_current_hold_started;
    if (!current_hold_step(&s_current_hold, elapsed, alpha, beta, bus.voltage_v, &output)) {
        stop_operation(s_current_hold.complete ? TARGET_VALIDATION_RESULT_COMPLETE
            : TARGET_VALIDATION_RESULT_OPEN_LOOP_REJECTED);
        return;
    }
    if (s_current_survey_active && !record_angle_survey(&position,
        current->phase_b_a - report->offset_b_a, current->phase_c_a - report->offset_c_a)) {
        stop_operation(TARGET_VALIDATION_RESULT_BAD_POSITION);
        return;
    }
    valpha = output.applied_vd_v * s_current_hold.stator_cosine - output.applied_vq_v * s_current_hold.stator_sine;
    vbeta = output.applied_vd_v * s_current_hold.stator_sine + output.applied_vq_v * s_current_hold.stator_cosine;
    voltage = sqrtf(output.applied_vd_v * output.applied_vd_v
        + output.applied_vq_v * output.applied_vq_v);
    if (!output.valid || !isfinite(voltage) || voltage > ((s_current_survey_active || s_rotor_current_active) ? CURRENT_SURVEY_MAX_VOLTAGE_V : 0.4f) + 0.00001f) {
        stop_operation(TARGET_VALIDATION_RESULT_OPEN_LOOP_REJECTED);
        return;
    }
    duty.phase_a_duty = output.duty_a;
    duty.phase_b_duty = output.duty_b;
    duty.phase_c_duty = output.duty_c;
    g_target_validation.stage_attempts++;
    if (!realtime_motor_io_stage_phase_duty(&duty)) {
        stop_operation(TARGET_VALIDATION_RESULT_STAGE_REJECTED);
        return;
    }
    g_target_validation.stage_successes++;
    report->min_duty = fminf(report->min_duty, fminf(output.duty_a, fminf(output.duty_b, output.duty_c)));
    report->max_duty = fmaxf(report->max_duty, fmaxf(output.duty_a, fmaxf(output.duty_b, output.duty_c)));
    report->max_voltage_v = fmaxf(report->max_voltage_v, voltage);
    report->voltage_limited_samples += output.voltage_limited ? 1U : 0U;
    window = s_rotor_speed_active ? (elapsed >= 16000U && elapsed < 24000U)
        : (s_rotor_current_active ? (elapsed >= 12000U && elapsed < 16000U)
        : (s_current_survey_active ? ((elapsed >= 25000U && elapsed < 30000U)
        || (elapsed >= 55000U && elapsed < 60000U) || (elapsed >= 85000U && elapsed < 90000U))
        : (elapsed >= 40000U && elapsed < 50000U)));
    if (window) {
        float count = (float)++report->window_samples;
        float measured_d = s_rotor_current_active ? output.id_a : alpha;
        float measured_q = s_rotor_current_active ? output.iq_a : beta;
        float ea = measured_d - report->alpha_reference_a;
        float eb = measured_q - (s_rotor_speed_active ? s_current_hold.active_iq_reference_a : report->beta_reference_a);

        report->mean_alpha_a += (measured_d - report->mean_alpha_a) / count;
        report->mean_beta_a += (measured_q - report->mean_beta_a) / count;
        if (s_rotor_current_active) {
            s_rotor_current_report.mean_velocity_rpm += (s_current_hold.velocity.velocity_rpm
                - s_rotor_current_report.mean_velocity_rpm) / count;
        }
        if (s_rotor_speed_active) {
            float speed_error = s_current_hold.target_velocity_rpm - s_current_hold.velocity.velocity_rpm;
            s_rotor_speed_report.mean_iq_reference_a += (s_current_hold.active_iq_reference_a
                - s_rotor_speed_report.mean_iq_reference_a) / count;
            s_rotor_speed_report.mean_speed_error_rpm += (speed_error - s_rotor_speed_report.mean_speed_error_rpm) / count;
            s_rotor_speed_report.peak_speed_error_rpm = fmaxf(s_rotor_speed_report.peak_speed_error_rpm, fabsf(speed_error));
            if (s_current_hold.last_speed_elapsed == elapsed) { s_rotor_speed_report.speed_window_updates++; }
        }
        report->mean_voltage_alpha_v += (valpha - report->mean_voltage_alpha_v) / count;
        report->mean_voltage_beta_v += (vbeta - report->mean_voltage_beta_v) / count;
        report->peak_tracking_error_a = fmaxf(report->peak_tracking_error_a, sqrtf(ea * ea + eb * eb));
    }
}

static void run_operation(const PhaseCurrentObservation *current,
    uint32_t now_cycles, uint32_t interval_cycles)
{
    if (!s_operation_active) {
        if ((TIM1->BDTR & TIM_BDTR_MOE) != 0U) {
            g_platform_bringup.moe_violations++;
            g_platform_bringup.status = 111U;
            stop_operation(TARGET_VALIDATION_RESULT_PLATFORM_OFF);
            g_target_validation.result = TARGET_VALIDATION_RESULT_PLATFORM_OFF;
        }
        return;
    }
    if (!s_powered && ((TIM1->BDTR & TIM_BDTR_MOE) != 0U)) {
        g_platform_bringup.moe_violations++;
        g_platform_bringup.status = 111U;
        stop_operation(TARGET_VALIDATION_RESULT_PLATFORM_OFF);
    }
    check_deadline(now_cycles);
    if (g_target_validation_stop_reason != TARGET_VALIDATION_RESULT_NONE) {
        finish_operation();
        return;
    }
    if (!current_valid(current)
        || (interval_cycles > CURRENT_MAX_GAP_US * CYCLES_PER_US)) {
        stop_operation(TARGET_VALIDATION_RESULT_BAD_CURRENT);
    } else if (!trip_healthy()) {
        stop_operation(TARGET_VALIDATION_RESULT_HARDWARE_TRIP);
    } else if (s_capturing) {
        if ((TIM1->BDTR & TIM_BDTR_MOE) != 0U) {
            stop_operation(TARGET_VALIDATION_RESULT_PLATFORM_OFF);
        } else {
            record_capture(current, interval_cycles);
        }
    } else if (s_power_requested && !burst_bus_valid(current->observation_time)) {
        stop_operation(TARGET_VALIDATION_RESULT_BAD_BUS);
    } else if (s_powered && ((TIM1->BDTR & TIM_BDTR_MOE) == 0U)) {
        stop_operation(TARGET_VALIDATION_RESULT_PLATFORM_OFF);
    } else if (s_current_hold_active) {
        run_current_hold(current, interval_cycles);
    } else if (s_open_loop_active) {
        run_open_loop(current, interval_cycles);
    } else if (s_powered && (s_probe_source != PROTECTION_PROBE_NONE)
        && (++s_probe_powered_ticks >= 4U)) {
        ProtectionProbeOutcome outcome;
        uint32_t primask = __get_PRIMASK();

        /* 修改阈值至捕获硬件证据之间，不由软件写 MOE，也不执行中断处理。
         * 即使硬件 Break 未生效，也必须先 safe-off，再恢复中断。
         * 10 us 等待不用于测量栅极关断延迟。 */
        __disable_irq();
        outcome = protection_probe_fire();
        stop_operation(outcome == PROTECTION_PROBE_OBSERVED ? TARGET_VALIDATION_RESULT_HARDWARE_TRIP
            : (outcome == PROTECTION_PROBE_REJECTED ? TARGET_VALIDATION_RESULT_PROBE_REJECTED
                : TARGET_VALIDATION_RESULT_BREAK_NOT_OBSERVED));
        restore_interrupts(primask);
    } else {
        g_target_validation.stage_attempts++;
        if (!realtime_motor_io_stage_phase_duty(&s_duty)) {
            stop_operation(TARGET_VALIDATION_RESULT_STAGE_REJECTED);
        } else {
            g_target_validation.stage_successes++;
            s_staging_ticks++;
            if (s_staging_ticks >= 2U) {
                if (!s_power_requested) {
                    stop_operation(TARGET_VALIDATION_RESULT_COMPLETE);
                } else if (!s_powered) {
                    g_target_validation.enable_attempts++;
                    if (!realtime_motor_io_power_enable()) {
                        stop_operation(TARGET_VALIDATION_RESULT_ENABLE_REJECTED);
                    } else {
                        s_powered = true;
                        g_target_validation.enable_successes++;
                        g_target_validation.state = TARGET_VALIDATION_STATE_POWERED;
                    }
                }
            }
        }
    }
    if (g_target_validation_stop_reason != TARGET_VALIDATION_RESULT_NONE) {
        finish_operation();
    }
}

static void poll_legacy_observations(void)
{
    PositionObservation position;
    BusObservation bus;
    HardwareTripSnapshot trip;

    if (!rescan_trip(&trip)) {
        g_platform_bringup.status = 109U;
    }
    if (position_observation_get_latest(&position)) {
        if (position.observation_time != s_last_position_time) {
            s_last_position_time = position.observation_time;
            g_platform_bringup.position_samples++;
        }
        g_platform_bringup.position_turns = position.mechanical_position_turns;
        g_platform_bringup.position_valid = position.acquisition_valid;
    }
    if (bus_observation_get_latest(&bus)) {
        if (bus.observation_time != s_last_bus_time) {
            s_last_bus_time = bus.observation_time;
            g_platform_bringup.bus_samples++;
        }
        g_platform_bringup.bus_voltage_v = bus.voltage_v;
        g_platform_bringup.bus_valid = bus.acquisition_valid;
    }
}

static void sample_ready(void *context, const PhaseCurrentObservation *current)
{
    uint32_t entry_cycles = DWT->CYCCNT;
    uint32_t interval_cycles = s_previous_sample ? entry_cycles - s_previous_sample_cycles : 0U;
    uint32_t cycles;
    bool timing_ran = false;
    bool operation_was_active = s_operation_active;
    bool command_pending = (g_target_validation_request.sequence != 0U)
        && (g_target_validation_request.sequence != s_last_command_sequence);

    (void)context;
    g_target_validation.sequence++;
    __DMB();
    /* SAFE_OFF 先于执行；运行期检查与 stage 先于 Watch 复制/统计。
     * 新 stage 请求只排队，下一采样重新校验 current/trip/bus 后才提交。
     * BUSY 不替换普通活动事务，也不能让已使能输出漏掉本拍 fresh stage。 */
    if (command_pending) {
        consume_command(current, entry_cycles);
    }
    if (s_operation_active && (s_timing_case != 0U)) {
        /* 探测仅走早期入口；新 mailbox 即使返回 BUSY，本拍也不执行探测。 */
        if (!command_pending) {
            run_timing_operation(current, interval_cycles);
            timing_ran = true;
        }
    } else if (operation_was_active || !s_operation_active || s_capturing) {
        run_operation(current, entry_cycles, interval_cycles);
    }
    g_target_validation.fast_tick_count++;
    g_target_validation.current = *current;
    if (!current_valid(current)) {
        g_target_validation.invalid_current_count++;
    }
    if (s_previous_sample) {
        if (interval_cycles < g_target_validation.interval_min_cycles) {
            g_target_validation.interval_min_cycles = interval_cycles;
        }
        if (interval_cycles > g_target_validation.interval_max_cycles) {
            g_target_validation.interval_max_cycles = interval_cycles;
        }
    }
    if (!current_valid(current) || !s_previous_sample
        || (interval_cycles < CURRENT_MIN_GAP_US * CYCLES_PER_US)
        || (interval_cycles > CURRENT_MAX_GAP_US * CYCLES_PER_US)) {
        g_target_validation_timing_recovery_ticks = 0U;
    } else if (!timing_ran && !s_operation_active && (g_target_validation_timing_recovery_ticks < 2U)) {
        g_target_validation_timing_recovery_ticks++;
    }
    s_last_sample_cycles = entry_cycles;
    s_previous_sample_cycles = entry_cycles;
    s_previous_sample = true;

    /* 保留原 Watch ABI；其中的慢观测字段仍是由多个上下文分别更新的诊断数据。 */
    g_platform_bringup.fast_ticks++;
    g_platform_bringup.moe_checks++;
    g_platform_bringup.measured_phases = current->measured_phases;
    if (current->acquisition_valid) {
        g_platform_bringup.current_valid_ticks++;
    } else {
        g_platform_bringup.current_invalid_ticks++;
    }
    if ((current->measured_phases & PHASE_MASK_B) != 0U) {
        g_platform_bringup.phase_b_a = current->phase_b_a;
    }
    if ((current->measured_phases & PHASE_MASK_C) != 0U) {
        g_platform_bringup.phase_c_a = current->phase_c_a;
    }
    if (++s_observation_divider >= 10U) {
        PositionObservation position;
        BusObservation bus;

        s_observation_divider = 0U;
        (void)trip_healthy();
        if (position_observation_get_latest(&position)) {
            g_target_validation.position = position;
        }
        if (bus_observation_get_latest(&bus)) {
            g_target_validation.bus = bus;
        }
    }
    g_target_validation.tim1_bdtr = TIM1->BDTR;
    g_target_validation.tim1_ccer = TIM1->CCER;
    g_target_validation.guard_irq_count = s_guard_irq_count;
    cycles = DWT->CYCCNT - entry_cycles;
    if (cycles > g_target_validation.callback_max_cycles) {
        g_target_validation.callback_max_cycles = cycles;
    }
    __DMB();
    g_target_validation.sequence++;
}

void target_validation_start(void)
{
    HardwareTripSnapshot trip;
    uint32_t primask;

    if (s_started) {
        return;
    }
    realtime_motor_io_safe_off();
    guard_timer_stop();
    g_target_validation_request.sequence = 0U;
    g_target_validation_request.power_key = 0U;
    g_target_validation_request.command = TARGET_VALIDATION_COMMAND_NONE;
    g_target_validation_request.common_duty = 0.5f;
    g_target_validation_request.duration_us = 2000U;
    g_target_validation.interval_min_cycles = UINT32_MAX;
    if ((SystemCoreClock != 170000000U) || (TIM6->PSC != 169U)) {
        g_platform_bringup.status = 112U;
        return;
    }
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
    if ((DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) == 0U) {
        g_platform_bringup.status = 112U;
        return;
    }
    if (!realtime_motor_io_get_facts(&s_facts)
        || (s_facts.control_time_ticks_per_second != 20000U)
        || (s_facts.fast_tick_period_ticks != 2U)) {
        g_platform_bringup.status = 101U;
        return;
    }
    g_target_validation.facts = s_facts;
    if (!realtime_motor_io_bind_sample_ready(sample_ready, NULL)) {
        g_platform_bringup.status = 102U;
        return;
    }
    if (!hardware_trip_bind(trip_notify, NULL)) {
        g_platform_bringup.status = 107U;
        return;
    }
    if (!position_observation_arm()) {
        g_platform_bringup.status = 103U;
        return;
    }
    if (!bus_observation_arm()) {
        g_platform_bringup.status = 104U;
        return;
    }
    if (!realtime_motor_io_arm()) {
        g_platform_bringup.status = 105U;
        realtime_motor_io_safe_off();
        return;
    }
    if (!hardware_trip_arm()) {
        g_platform_bringup.status = 108U;
        realtime_motor_io_safe_off();
        return;
    }
    g_platform_bringup.trip_armed = true;
    if (!rescan_trip(&trip)) {
        g_platform_bringup.status = 109U;
        return;
    }
    if (trip.condition_active || (trip.causes != HARDWARE_TRIP_CAUSE_NONE)) {
        g_platform_bringup.status = 110U;
        realtime_motor_io_safe_off();
        return;
    }
    if (!realtime_motor_io_start_drive_timing()) {
        g_platform_bringup.status = 106U;
        realtime_motor_io_safe_off();
        return;
    }
    primask = __get_PRIMASK();
    __disable_irq();
    s_started = true;
    g_platform_bringup.status = 1U;
    g_target_validation.sequence++;
    __DMB();
    g_target_validation.started = 1U;
    __DMB();
    g_target_validation.sequence++;
    restore_interrupts(primask);
}

void target_validation_service(void)
{
    uint32_t now_ms;

    if (s_trip_rescan_pending) {
        HardwareTripSnapshot trip;

        s_trip_rescan_pending = false;
        if (!rescan_trip(&trip)) {
            g_platform_bringup.status = 109U;
        }
    }
    now_ms = HAL_GetTick();
    if ((now_ms - s_last_bringup_poll_ms) >= 10U) {
        s_last_bringup_poll_ms = now_ms;
        poll_legacy_observations();
    }
}
