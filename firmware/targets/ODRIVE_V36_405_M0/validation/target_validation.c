#include "target_validation.h"

#include <stddef.h>
#include <float.h>
#include "tim.h"
#include "control_time_source.h"
#include "realtime_motor_io_internal.h"

#define OBSERVATION_DIVIDER (20U)
#define BUS_MAX_AGE_US (5000U)
#define BURST_BUS_MIN_V (10.0f)
#define BURST_BUS_MAX_V (14.0f)
#define CURRENT_CAPTURE_MARGIN_US (5000U)

volatile TargetValidationMailbox g_target_validation_request;
volatile TargetValidationSnapshot g_target_validation;
volatile uint32_t g_target_validation_stop_reason;

static bool s_started;
static bool s_running;
static bool s_power_requested;
static bool s_powered;
static bool s_previous_current_valid;
static uint32_t s_last_command_sequence;
static uint32_t s_staging_ticks;
static uint32_t s_observation_divider;
static ControlTime s_previous_current_time;
static PhaseDutyRequest s_duty;
static volatile bool s_deadline_active;
static volatile ControlTime s_deadline;
static volatile bool s_capturing_current;
/* Welford 平方差累积，单位 A^2；不使用 sum(x^2)-mean^2 以避免大零偏下相消。 */
static float s_current_m2_b;
static float s_current_m2_c;

static void restore_interrupts(uint32_t primask)
{
    if (primask == 0U) {
        __enable_irq();
    }
}

static void stop_test(TargetValidationResult reason)
{
    uint32_t primask = __get_PRIMASK();

    __disable_irq();
    realtime_motor_io_safe_off();
    s_deadline_active = false;
    if ((g_target_validation_stop_reason == TARGET_VALIDATION_RESULT_NONE) || !s_running) {
        g_target_validation_stop_reason = (uint32_t)reason;
    }
    restore_interrupts(primask);
}

static void finish_test(void)
{
    s_running = false;
    s_powered = false;
    g_target_validation.state = TARGET_VALIDATION_STATE_OBSERVE;
    g_target_validation.result = g_target_validation_stop_reason;
}

static void finish_current_capture(TargetValidationResult reason)
{
    stop_test(reason);
    s_capturing_current = false;
    g_target_validation.state = TARGET_VALIDATION_STATE_OBSERVE;
    g_target_validation.result = g_target_validation_stop_reason;
}

static void accumulate_phase(volatile TargetValidationPhaseStatistics *statistics,
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
}

static void record_current_capture(const PhaseCurrentObservation *current)
{
    volatile TargetValidationCurrentStatistics *statistics =
        &g_target_validation.current_statistics;
    uint32_t count;

    if (!s_capturing_current) {
        return;
    }
    if (g_target_validation_stop_reason != TARGET_VALIDATION_RESULT_NONE) {
        finish_current_capture((TargetValidationResult)g_target_validation_stop_reason);
        return;
    }
    if (!current->acquisition_valid
        || (current->measured_phases != (PHASE_MASK_B | PHASE_MASK_C))
        || !(current->phase_b_a >= -FLT_MAX && current->phase_b_a <= FLT_MAX)
        || !(current->phase_c_a >= -FLT_MAX && current->phase_c_a <= FLT_MAX)
        || ((statistics->sample_count != 0U)
            && ((current->observation_time - statistics->last_sample_time)
                != g_target_validation.facts.fast_tick_period_ticks))) {
        finish_current_capture(TARGET_VALIDATION_RESULT_BAD_CURRENT);
        return;
    }
    count = statistics->sample_count + 1U;
    if (count == 1U) {
        statistics->first_sample_time = current->observation_time;
    }
    statistics->last_sample_time = current->observation_time;
    accumulate_phase(&statistics->phase_b, &s_current_m2_b, current->phase_b_a, count);
    accumulate_phase(&statistics->phase_c, &s_current_m2_c, current->phase_c_a, count);
    statistics->sample_count = count;
    if (count == TARGET_VALIDATION_CURRENT_SAMPLE_COUNT) {
        statistics->phase_b.variance_a2 = s_current_m2_b / (float)(count - 1U);
        statistics->phase_c.variance_a2 = s_current_m2_c / (float)(count - 1U);
        finish_current_capture(TARGET_VALIDATION_RESULT_COMPLETE);
    }
}

static bool burst_bus_valid(ControlTime now)
{
    BusObservation bus;

    return bus_observation_get_latest(&bus) && bus.acquisition_valid
        && (bus.observation_time <= now)
        && ((now - bus.observation_time) <= BUS_MAX_AGE_US)
        && (bus.voltage_v >= BURST_BUS_MIN_V) && (bus.voltage_v <= BURST_BUS_MAX_V);
}

static void consume_command(const PhaseCurrentObservation *current, ControlTime now)
{
    TargetValidationMailbox request;
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
    /* 防止违背提交顺序的并发 debugger 写入被作为完整 payload 消费。 */
    if (request.sequence != g_target_validation_request.sequence) {
        restore_interrupts(primask);
        return;
    }
    g_target_validation_request.power_key = 0U;
    s_last_command_sequence = request.sequence;
    restore_interrupts(primask);
    g_target_validation.command_sequence = request.sequence;

    if (request.command == TARGET_VALIDATION_COMMAND_SAFE_OFF) {
        if (s_capturing_current) {
            finish_current_capture(TARGET_VALIDATION_RESULT_OPERATOR_STOP);
            return;
        }
        stop_test(TARGET_VALIDATION_RESULT_OPERATOR_STOP);
        g_target_validation.result = TARGET_VALIDATION_RESULT_OPERATOR_STOP;
        return;
    }
    if (s_running || s_capturing_current) {
        g_target_validation.result = TARGET_VALIDATION_RESULT_BUSY;
        return;
    }
    if (request.command == TARGET_VALIDATION_COMMAND_CAPTURE_CURRENT) {
        TargetValidationCurrentStatistics empty = {0};

        if ((TIM1->BDTR & TIM_BDTR_MOE) != 0U) {
            stop_test(TARGET_VALIDATION_RESULT_PLATFORM_OFF);
            g_target_validation.result = TARGET_VALIDATION_RESULT_PLATFORM_OFF;
            return;
        }
        empty.request_sequence = request.sequence;
        g_target_validation.current_statistics = empty;
        s_current_m2_b = 0.0f;
        s_current_m2_c = 0.0f;
        s_capturing_current = true;
        g_target_validation.state = TARGET_VALIDATION_STATE_CAPTURING_CURRENT;
        g_target_validation.result = TARGET_VALIDATION_RESULT_NONE;
        primask = __get_PRIMASK();
        __disable_irq();
        g_target_validation_stop_reason = TARGET_VALIDATION_RESULT_NONE;
        /* 本 Target 时间 tick 为 1 us；丢失 ADC 时由更新中断终止等待。 */
        s_deadline = now + TARGET_VALIDATION_CURRENT_SAMPLE_COUNT
            * g_target_validation.facts.fast_tick_period_ticks + CURRENT_CAPTURE_MARGIN_US;
        __DMB();
        s_deadline_active = true;
        restore_interrupts(primask);
        return;
    }
    if ((request.command != TARGET_VALIDATION_COMMAND_STAGE_ONLY)
        && (request.command != TARGET_VALIDATION_COMMAND_PWM_BURST)) {
        g_target_validation.result = TARGET_VALIDATION_RESULT_BAD_REQUEST;
        return;
    }
    if (!(request.common_duty >= 0.10f && request.common_duty <= 0.90f)
        || (request.duration_us < 1000U)
        || (request.duration_us > TARGET_VALIDATION_MAX_BURST_US)) {
        g_target_validation.result = TARGET_VALIDATION_RESULT_BAD_REQUEST;
        return;
    }
    if (!current->acquisition_valid
        || (current->measured_phases != (PHASE_MASK_B | PHASE_MASK_C))) {
        g_target_validation.result = TARGET_VALIDATION_RESULT_BAD_CURRENT;
        return;
    }
    s_power_requested = (request.command == TARGET_VALIDATION_COMMAND_PWM_BURST);
    if (s_power_requested && (request.power_key != TARGET_VALIDATION_POWER_KEY)) {
        g_target_validation.result = TARGET_VALIDATION_RESULT_LOCKED;
        return;
    }
    if (s_power_requested && !burst_bus_valid(now)) {
        g_target_validation.result = TARGET_VALIDATION_RESULT_BAD_BUS;
        return;
    }

    /*
     * 当前接受的 sample-ready window 必须保留，不能在这里 safe_off 后继续 stage。
     * 上次结束已经取消旧 epoch；若观察模式意外出现 MOE，则关断并拒绝本次动作。
     */
    if ((TIM1->BDTR & TIM_BDTR_MOE) != 0U) {
        stop_test(TARGET_VALIDATION_RESULT_PLATFORM_OFF);
        g_target_validation.result = TARGET_VALIDATION_RESULT_PLATFORM_OFF;
        return;
    }
    s_duty.phase_a_duty = request.common_duty;
    s_duty.phase_b_duty = request.common_duty;
    s_duty.phase_c_duty = request.common_duty;
    s_staging_ticks = 0U;
    s_powered = false;
    s_running = true;
    g_target_validation.state = TARGET_VALIDATION_STATE_STAGING;
    g_target_validation.result = TARGET_VALIDATION_RESULT_NONE;
    primask = __get_PRIMASK();
    __disable_irq();
    g_target_validation_stop_reason = TARGET_VALIDATION_RESULT_NONE;
    s_deadline = now + request.duration_us;
    __DMB();
    s_deadline_active = true;
    restore_interrupts(primask);
}

static void run_test(const PhaseCurrentObservation *current, ControlTime now)
{
    if (!s_running) {
        if ((TIM1->BDTR & TIM_BDTR_MOE) != 0U) {
            stop_test(TARGET_VALIDATION_RESULT_PLATFORM_OFF);
            g_target_validation.result = TARGET_VALIDATION_RESULT_PLATFORM_OFF;
        }
        return;
    }
    if (g_target_validation_stop_reason != TARGET_VALIDATION_RESULT_NONE) {
        finish_test();
        return;
    }
    if (now >= s_deadline) {
        stop_test(TARGET_VALIDATION_RESULT_COMPLETE);
        finish_test();
        return;
    }
    if (!current->acquisition_valid
        || (current->measured_phases != (PHASE_MASK_B | PHASE_MASK_C))) {
        stop_test(TARGET_VALIDATION_RESULT_BAD_CURRENT);
    } else if (s_power_requested && !burst_bus_valid(now)) {
        stop_test(TARGET_VALIDATION_RESULT_BAD_BUS);
    } else if (s_powered && ((TIM1->BDTR & TIM_BDTR_MOE) == 0U)) {
        /* Platform 已关断后不在同一次动作中重试 enable。 */
        stop_test(TARGET_VALIDATION_RESULT_PLATFORM_OFF);
    } else {
        g_target_validation.stage_attempts++;
        if (!realtime_motor_io_stage_phase_duty(&s_duty)) {
            stop_test(TARGET_VALIDATION_RESULT_STAGE_REJECTED);
        } else {
            g_target_validation.stage_successes++;
            s_staging_ticks++;
            if (s_staging_ticks >= 2U) {
                if (!s_power_requested) {
                    /* 先观察使能前提再关断；STAGE_ONLY 始终不调用 power_enable。 */
                    realtime_motor_io_capture_enable_diagnostics();
                    stop_test(TARGET_VALIDATION_RESULT_COMPLETE);
                } else if (!s_powered) {
                    g_target_validation.enable_attempts++;
                    if (!realtime_motor_io_power_enable()) {
                        stop_test(TARGET_VALIDATION_RESULT_ENABLE_REJECTED);
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
        finish_test();
    }
}

static void record_observations(void)
{
    PositionObservation position;
    BusObservation bus;

    if (++s_observation_divider < OBSERVATION_DIVIDER) {
        return;
    }
    s_observation_divider = 0U;
    if (position_observation_get_latest(&position)) {
        if (!g_target_validation.position_available
            || (position.observation_time != g_target_validation.position.observation_time)) {
            g_target_validation.position_count++;
            if (!position.acquisition_valid) {
                g_target_validation.invalid_position_count++;
            }
        }
        g_target_validation.position = position;
        g_target_validation.position_available = 1U;
    }
    if (bus_observation_get_latest(&bus)) {
        if (!g_target_validation.bus_available
            || (bus.observation_time != g_target_validation.bus.observation_time)) {
            g_target_validation.bus_count++;
            if (!bus.acquisition_valid) {
                g_target_validation.invalid_bus_count++;
            }
        }
        g_target_validation.bus = bus;
        g_target_validation.bus_available = 1U;
    }
}

static void sample_ready(void *context, const PhaseCurrentObservation *current)
{
    uint32_t entry_cycles = DWT->CYCCNT;
    uint32_t cycles;
    ControlTime now = control_time_source_now();
    ControlTime interval;

    (void)context;
    g_target_validation.sequence++;
    __DMB();
    g_target_validation.current = *current;
    g_target_validation.fast_tick_count++;
    if (!current->acquisition_valid) {
        g_target_validation.invalid_current_count++;
    }
    if (s_previous_current_valid && current->acquisition_valid) {
        if (current->observation_time < s_previous_current_time) {
            g_target_validation.current_time_regressions++;
        } else {
            interval = current->observation_time - s_previous_current_time;
            if (interval <= UINT32_MAX) {
                if ((uint32_t)interval < g_target_validation.current_interval_min_us) {
                    g_target_validation.current_interval_min_us = (uint32_t)interval;
                }
                if ((uint32_t)interval > g_target_validation.current_interval_max_us) {
                    g_target_validation.current_interval_max_us = (uint32_t)interval;
                }
            }
        }
    }
    s_previous_current_time = current->observation_time;
    s_previous_current_valid = current->acquisition_valid;
    if (s_running && (g_target_validation_stop_reason != TARGET_VALIDATION_RESULT_NONE)) {
        finish_test();
    }
    consume_command(current, now);
    run_test(current, now);
    record_current_capture(current);
    record_observations();
    g_target_validation.tim1_cr1 = TIM1->CR1;
    g_target_validation.tim1_bdtr = TIM1->BDTR;
    g_target_validation.tim1_ccer = TIM1->CCER;
    g_target_validation.tim1_ccr1 = TIM1->CCR1;
    g_target_validation.tim1_ccr2 = TIM1->CCR2;
    g_target_validation.tim1_ccr3 = TIM1->CCR3;
    cycles = DWT->CYCCNT - entry_cycles;
    if (cycles > g_target_validation.callback_max_cycles) {
        g_target_validation.callback_max_cycles = cycles;
    }
    __DMB();
    g_target_validation.sequence++;
}

bool target_validation_start(void)
{
    RealtimeMotorIoFacts facts;

    if (s_started) {
        return false;
    }
    /* 外部调试器残留值不能跨 reset / start 自动发起动作。 */
    g_target_validation_request.sequence = 0U;
    g_target_validation_request.power_key = 0U;
    g_target_validation_request.common_duty = 0.5f;
    g_target_validation_request.duration_us = 2000U;
    g_target_validation.current_interval_min_us = UINT32_MAX;
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
    if (!realtime_motor_io_get_facts(&facts)
        || !realtime_motor_io_bind_sample_ready(sample_ready, NULL)
        || !position_observation_arm() || !bus_observation_arm()
        || !realtime_motor_io_arm()) {
        realtime_motor_io_safe_off();
        return false;
    }
    g_target_validation.facts = facts;
    if (!realtime_motor_io_start_drive_timing()) {
        realtime_motor_io_safe_off();
        return false;
    }
    s_started = true;
    /* 运行期唯一 writer 是 ADC callback；started 通过序列号发布。 */
    {
        uint32_t primask = __get_PRIMASK();
        __disable_irq();
        g_target_validation.sequence++;
        __DMB();
        g_target_validation.started = 1U;
        __DMB();
        g_target_validation.sequence++;
        restore_interrupts(primask);
    }
    return true;
}

void target_validation_update_irq(void)
{
    if (s_deadline_active && (control_time_source_now() >= s_deadline)) {
        /* 不依赖下一次 ADC callback；TIM1 counter/观测时基继续运行。 */
        stop_test(s_capturing_current ? TARGET_VALIDATION_RESULT_BAD_CURRENT
            : TARGET_VALIDATION_RESULT_COMPLETE);
    }
}
