#include "realtime_motor_io.h"

#include <stddef.h>

#include "adc.h"
#include "opamp.h"
#include "tim.h"

#include "bus_observation_internal.h"
#include "control_time_source.h"
#include "position_observation_internal.h"
#include "realtime_motor_io_config.h"

#define CURRENT_ADC_FULL_SCALE_COUNTS (4095.0f)
#define CURRENT_ADC_VREF_NOMINAL_V (3.3f)
#define CURRENT_SENSE_BIAS_NOMINAL_V (1.623f)
#define CURRENT_SENSE_GAIN_NOMINAL_V_PER_A (0.7049f)

static PhaseCurrentSampleReadyHandler s_sample_ready_handler;
static void *s_sample_ready_context;
static bool s_sample_ready_bound;
static bool s_armed;
static bool s_timing_started;
static bool s_current_pair_synchronized;
static bool s_stage_window_open;
static bool s_staged_this_tick;
static bool s_stage_pending_boundary;
static bool s_stage_committed;
static bool s_power_enabled;

static float adc_raw_to_phase_current_a(uint32_t raw)
{
    float voltage_v;

    voltage_v = ((float)raw * CURRENT_ADC_VREF_NOMINAL_V)
        / CURRENT_ADC_FULL_SCALE_COUNTS;

    return (voltage_v - CURRENT_SENSE_BIAS_NOMINAL_V)
        / CURRENT_SENSE_GAIN_NOMINAL_V_PER_A;
}

static void stop_current_acquisition(void)
{
    (void)HAL_ADCEx_InjectedStop_IT(&hadc1);
    (void)HAL_ADCEx_InjectedStop(&hadc2);
    (void)HAL_OPAMP_Stop(&hopamp2);
    (void)HAL_OPAMP_Stop(&hopamp1);
}

static bool duty_is_valid(float duty)
{
    return (duty >= REALTIME_MOTOR_IO_DUTY_MIN)
        && (duty <= REALTIME_MOTOR_IO_DUTY_MAX);
}

static uint32_t duty_to_compare(float duty)
{
    float compare = duty * (float)REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS;

    return (uint32_t)(compare + 0.5f);
}

static void stage_neutral_duty(void)
{
    __HAL_TIM_SET_COMPARE(
        &htim1,
        TIM_CHANNEL_1,
        REALTIME_MOTOR_IO_NEUTRAL_COMPARE_COUNTS);
    __HAL_TIM_SET_COMPARE(
        &htim1,
        TIM_CHANNEL_2,
        REALTIME_MOTOR_IO_NEUTRAL_COMPARE_COUNTS);
    __HAL_TIM_SET_COMPARE(
        &htim1,
        TIM_CHANNEL_3,
        REALTIME_MOTOR_IO_NEUTRAL_COMPARE_COUNTS);
}

bool realtime_motor_io_get_facts(RealtimeMotorIoFacts *out_facts)
{
    if (out_facts == NULL) {
        return false;
    }

    if (htim1.Init.Period != REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS) {
        return false;
    }

    out_facts->normal_voltage_utilization =
        REALTIME_MOTOR_IO_NORMAL_VOLTAGE_UTILIZATION;
    out_facts->control_time_ticks_per_second =
        REALTIME_MOTOR_IO_CONTROL_TIME_TICKS_PER_SECOND;
    out_facts->fast_tick_period_ticks =
        REALTIME_MOTOR_IO_FAST_TICK_PERIOD_TICKS;

    return true;
}

bool realtime_motor_io_bind_sample_ready(
    PhaseCurrentSampleReadyHandler handler,
    void *context)
{
    if ((handler == NULL) || s_sample_ready_bound || s_armed || s_timing_started) {
        return false;
    }

    s_sample_ready_handler = handler;
    s_sample_ready_context = context;
    s_sample_ready_bound = true;

    return true;
}

bool realtime_motor_io_arm(void)
{
    if (!s_sample_ready_bound || s_armed || s_timing_started) {
        return false;
    }

    if (HAL_ADCEx_Calibration_Start(&hadc1, ADC_SINGLE_ENDED) != HAL_OK) {
        return false;
    }

    if (HAL_ADCEx_Calibration_Start(&hadc2, ADC_SINGLE_ENDED) != HAL_OK) {
        return false;
    }

    if (HAL_OPAMP_Start(&hopamp1) != HAL_OK) {
        return false;
    }

    if (HAL_OPAMP_Start(&hopamp2) != HAL_OK) {
        (void)HAL_OPAMP_Stop(&hopamp1);
        return false;
    }

    /*
     * ADC2 先进入 external-trigger ready，ADC1 作为唯一 injected IRQ authority。
     * 两路均由同一个 TIM1 TRGO2 触发、单 rank、相同 sampling time。
     */
    if (HAL_ADCEx_InjectedStart(&hadc2) != HAL_OK) {
        stop_current_acquisition();
        return false;
    }

    if (HAL_ADCEx_InjectedStart_IT(&hadc1) != HAL_OK) {
        stop_current_acquisition();
        return false;
    }

    s_current_pair_synchronized = true;
    s_armed = true;

    return true;
}

bool realtime_motor_io_start_drive_timing(void)
{
    uint32_t output_enable_mask;

    if (!s_armed || s_timing_started) {
        return false;
    }

    if ((htim1.Instance->CR1 & TIM_CR1_CEN) != 0U) {
        return false;
    }

    /*
     * 在 Drive Timing 启动前保持 MOE=0。
     * CCxE/CCxNE 可以预先准备，但真实 PWM pin 仍由 MOE 阻断。
     */
    CLEAR_BIT(htim1.Instance->BDTR, TIM_BDTR_MOE);

    stage_neutral_duty();

    /*
     * CH1/2/3 已启用 preload。计数器尚未启动时用一次 UG 把 neutral preload
     * 转入 active register，保证 Drive Timing 从确定的零线电压状态开始。
     */
    __HAL_TIM_GENERATE_EVENT(&htim1, TIM_EVENTSOURCE_UPDATE);
    __HAL_TIM_CLEAR_FLAG(&htim1, TIM_FLAG_UPDATE);

    output_enable_mask = TIM_CCER_CC1E | TIM_CCER_CC1NE
        | TIM_CCER_CC2E | TIM_CCER_CC2NE
        | TIM_CCER_CC3E | TIM_CCER_CC3NE
        | TIM_CCER_CC4E;
    SET_BIT(htim1.Instance->CCER, output_enable_mask);

    control_time_source_reset();

    s_stage_window_open = false;
    s_staged_this_tick = false;
    s_stage_pending_boundary = false;
    s_stage_committed = false;
    s_power_enabled = false;

    s_timing_started = true;
    __HAL_TIM_ENABLE(&htim1);

    return true;
}

bool realtime_motor_io_stage_phase_duty(const PhaseDutyRequest *request)
{
    if ((request == NULL) || !s_armed || !s_timing_started) {
        return false;
    }

    if (!s_stage_window_open || s_staged_this_tick) {
        return false;
    }

    if (!duty_is_valid(request->phase_a_duty)
        || !duty_is_valid(request->phase_b_duty)
        || !duty_is_valid(request->phase_c_duty)) {
        return false;
    }

    __HAL_TIM_SET_COMPARE(
        &htim1,
        TIM_CHANNEL_1,
        duty_to_compare(request->phase_a_duty));
    __HAL_TIM_SET_COMPARE(
        &htim1,
        TIM_CHANNEL_2,
        duty_to_compare(request->phase_b_duty));
    __HAL_TIM_SET_COMPARE(
        &htim1,
        TIM_CHANNEL_3,
        duty_to_compare(request->phase_c_duty));

    s_staged_this_tick = true;
    s_stage_pending_boundary = true;

    return true;
}

bool realtime_motor_io_power_enable(void)
{
    uint32_t primask;
    bool enabled;

    if (!s_armed || !s_timing_started || !s_stage_window_open) {
        return false;
    }

    if (!s_stage_committed || !s_staged_this_tick) {
        return false;
    }

    primask = __get_PRIMASK();
    __disable_irq();

    if ((htim1.Instance->SR & (TIM_FLAG_BREAK | TIM_FLAG_BREAK2)) != 0U) {
        enabled = false;
    } else {
        SET_BIT(htim1.Instance->BDTR, TIM_BDTR_MOE);
        __DSB();

        enabled = ((htim1.Instance->BDTR & TIM_BDTR_MOE) != 0U)
            && ((htim1.Instance->SR & (TIM_FLAG_BREAK | TIM_FLAG_BREAK2)) == 0U);

        if (!enabled) {
            CLEAR_BIT(htim1.Instance->BDTR, TIM_BDTR_MOE);
        }
    }

    s_power_enabled = enabled;

    if (primask == 0U) {
        __enable_irq();
    }

    return enabled;
}

void realtime_motor_io_safe_off(void)
{
    uint32_t primask;

    primask = __get_PRIMASK();
    __disable_irq();

    CLEAR_BIT(htim1.Instance->BDTR, TIM_BDTR_MOE);
    stage_neutral_duty();

    s_stage_window_open = false;
    s_staged_this_tick = false;
    s_stage_pending_boundary = false;
    s_stage_committed = false;
    s_power_enabled = false;

    if (primask == 0U) {
        __enable_irq();
    }
}

void HAL_ADCEx_InjectedConvCpltCallback(ADC_HandleTypeDef *hadc)
{
    PhaseCurrentObservation observation;
    ControlTime observation_time;
    uint32_t raw_b;
    uint32_t raw_c;
    bool adc2_complete;

    if ((hadc != &hadc1) || !s_armed || !s_timing_started) {
        return;
    }

    observation_time = control_time_source_advance_fast_tick();

    /*
     * 上一 fast tick 在 callback 内写入的 CCR preload 至少已经跨过一次
     * center-aligned update boundary；现在才把它视为可用于 Power Enable
     * 的 committed fresh actuation。
     */
    if (s_stage_pending_boundary) {
        s_stage_pending_boundary = false;
        s_stage_committed = true;
    }

    s_staged_this_tick = false;

    raw_c = HAL_ADCEx_InjectedGetValue(&hadc1, ADC_INJECTED_RANK_1);

    adc2_complete = (__HAL_ADC_GET_FLAG(&hadc2, ADC_FLAG_JEOS) != 0U);
    if (!adc2_complete) {
        s_current_pair_synchronized = false;
    }

    observation.phase_a_a = 0.0f;
    observation.phase_b_a = 0.0f;
    observation.phase_c_a = adc_raw_to_phase_current_a(raw_c);
    observation.measured_phases = PHASE_MASK_C;
    observation.observation_time = observation_time;
    observation.acquisition_valid = false;

    if (adc2_complete && s_current_pair_synchronized) {
        raw_b = HAL_ADCEx_InjectedGetValue(&hadc2, ADC_INJECTED_RANK_1);
        __HAL_ADC_CLEAR_FLAG(&hadc2, ADC_FLAG_JEOC | ADC_FLAG_JEOS);

        observation.phase_b_a = adc_raw_to_phase_current_a(raw_b);
        observation.measured_phases = PHASE_MASK_B | PHASE_MASK_C;
        observation.acquisition_valid = true;
    }

    /*
     * G431 只在 sample-ready consumer 的同步 callback 内开放 staging window。
     * callback 返回后立刻关闭，避免后台代码在不可证明的时刻改写下一周期 PWM。
     */
    s_stage_window_open = true;
    s_sample_ready_handler(s_sample_ready_context, &observation);
    s_stage_window_open = false;

    /*
     * RUN 中每个 fast tick 都必须提交 fresh actuation。
     * 如果功率已经打开而本 tick 没有成功 stage，请求立刻 fail-safe。
     */
    if (s_power_enabled && !s_staged_this_tick) {
        realtime_motor_io_safe_off();
    }

    position_observation_fast_tick(observation_time);
    bus_observation_fast_tick(observation_time);
}
