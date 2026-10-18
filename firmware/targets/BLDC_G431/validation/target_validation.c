#include "target_validation.h"

#if defined(__CC_ARM)
#pragma Otime
#endif

#include <float.h>
#include <stddef.h>
#include "main.h"
#include "tim.h"
#include "protection_probe.h"

#define CYCLES_PER_US (170U)
#define GUARD_PERIOD_US (50U)
#define CURRENT_MAX_GAP_US (75U)
#define CURRENT_MIN_GAP_US (25U)
#define CAPTURE_MARGIN_US (5000U)
#define BUS_MAX_AGE_TICKS (100U) /* 5 ms in the G431 20 kHz ControlTime domain. */

volatile PlatformBringupDebug g_platform_bringup;
volatile TargetValidationMailbox g_target_validation_request;
volatile TargetValidationSnapshot g_target_validation;
volatile TargetValidationCurrentCapture g_target_validation_capture;
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

/* ADC owns transaction state; other IRQs may only stop it and publish a reason. */
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

static void finish_operation(void)
{
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

/* TIM6 is already configured at 1 MHz with a shared DAC IRQ by CubeMX.
 * Only the validation transaction changes its runtime period and starts it.
 * DWT elapsed time does not depend on ADC callbacks or ControlTime increments. */
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
            : TARGET_VALIDATION_RESULT_COMPLETE);
    }
}

void target_validation_guard_irq(void)
{
    uint32_t now_cycles;

    if (((TIM6->SR & TIM_SR_UIF) == 0U) || ((TIM6->DIER & TIM_DIER_UIE) == 0U)) {
        return; /* Shared DAC interrupt still goes through its generated handler. */
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

static bool burst_bus_valid(ControlTime now)
{
    BusObservation bus;

    return bus_observation_get_latest(&bus) && bus.acquisition_valid
        && (bus.observation_time <= now) && ((now - bus.observation_time) <= BUS_MAX_AGE_TICKS)
        && (bus.voltage_v >= 10.0f) && (bus.voltage_v <= 14.0f);
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

static void consume_command(const PhaseCurrentObservation *current, uint32_t now_cycles)
{
    TargetValidationMailbox request;
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
        (void)trip_healthy(); /* Publish current condition AND sticky episode after idle control. */
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
    if (is_timing_command(request.command)) {
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
        guard_timer_start(now_cycles, TARGET_VALIDATION_CURRENT_SAMPLE_COUNT * 50U + CAPTURE_MARGIN_US);
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
    } else if (s_powered && (s_probe_source != PROTECTION_PROBE_NONE)
        && (++s_probe_powered_ticks >= 4U)) {
        ProtectionProbeOutcome outcome;
        uint32_t primask = __get_PRIMASK();

        /* No software MOE write or interrupt handler between threshold change and
         * captured hardware evidence. Always safe-off BEFORE unmasking, even if
         * hardware Break fails. The 10 us wait is not a Gate latency measurement. */
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

    /* Retain the original watch ABI; its slow fields remain multi-writer diagnostics. */
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
    if (++s_observation_divider >= 20U) {
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
        || (s_facts.fast_tick_period_ticks != 1U)) {
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
