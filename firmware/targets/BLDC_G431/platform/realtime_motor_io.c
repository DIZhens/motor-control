#include "realtime_motor_io.h"

#include <stddef.h>

#include "adc.h"
#include "opamp.h"
#include "tim.h"

#include "control_time_source.h"

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

bool realtime_motor_io_get_facts(RealtimeMotorIoFacts *out_facts)
{
    /*
     * 当前固定低边采样策略还没有完成 normal modulation envelope 资格化。
     * 在能够给出真实 normal_voltage_utilization 前，整个 facts 对象不发布。
     */
    (void)out_facts;
    return false;
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

    __HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_1, 0U);
    __HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_2, 0U);
    __HAL_TIM_SET_COMPARE(&htim1, TIM_CHANNEL_3, 0U);

    output_enable_mask = TIM_CCER_CC1E | TIM_CCER_CC1NE
        | TIM_CCER_CC2E | TIM_CCER_CC2NE
        | TIM_CCER_CC3E | TIM_CCER_CC3NE
        | TIM_CCER_CC4E;
    SET_BIT(htim1.Instance->CCER, output_enable_mask);

    control_time_source_reset();
    s_timing_started = true;
    __HAL_TIM_ENABLE(&htim1);

    return true;
}

bool realtime_motor_io_stage_phase_duty(const PhaseDutyRequest *request)
{
    /*
     * 当前 Target 还没有冻结能够同时保证 PWM realization 与可靠低边采样的
     * normal modulation envelope。不得在资格化前把任意 [0, 1] duty 当作
     * Hardware Capability Contract 下的正常成功。
     */
    (void)request;
    return false;
}

bool realtime_motor_io_power_enable(void)
{
    /*
     * 与 stage_phase_duty() 同一冻结边界：normal envelope 未资格化前，
     * public Power Enable 必须 fail-closed。
     */
    return false;
}

void realtime_motor_io_safe_off(void)
{
    CLEAR_BIT(htim1.Instance->BDTR, TIM_BDTR_MOE);
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

    s_sample_ready_handler(s_sample_ready_context, &observation);
}
