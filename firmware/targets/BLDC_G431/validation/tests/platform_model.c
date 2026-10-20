/* 为正式 G431 Realtime Motor I/O 提供外设与服务替身。 */
#include "realtime_motor_io.h"
#include "realtime_motor_io_config.h"
#include "realtime_motor_io_internal.h"
#include "control_time_source.h"
#include "adc.h"
#include "opamp.h"
#include "tim.h"
#ifdef PLATFORM_MODEL_TIMING_PROBE
#include "timing_probe.h"
#endif

uint32_t SystemCoreClock = 170000000U;
TIM_HandleTypeDef htim1 = { .Instance = TIM1 };
ADC_HandleTypeDef hadc1 = { .Instance = ADC1 };
ADC_HandleTypeDef hadc2 = { .Instance = ADC2 };
OPAMP_HandleTypeDef hopamp1 = { .Instance = OPAMP1 };
OPAMP_HandleTypeDef hopamp2 = { .Instance = OPAMP2 };

volatile uint32_t model_actions;
volatile uint32_t model_marker;
volatile uint32_t model_callbacks;
volatile uint32_t model_stage_result;
volatile uint32_t model_second_stage_result;
volatile uint32_t model_enable_result;
volatile uint32_t model_early_enable_result;
volatile uint32_t model_bind_result;
volatile uint32_t model_arm_result;
volatile uint32_t model_start_result;
volatile uint32_t model_observation_valid;
volatile uint32_t model_observation_mask;
volatile float model_observation_b_a;
volatile float model_observation_c_a;
volatile uint32_t model_position_ticks;
volatile uint32_t model_bus_ticks;
volatile uint32_t model_hal_ok = 1U;
volatile uint32_t model_auto_start = 1U;
volatile uint32_t model_adc_started;
volatile uint32_t model_ug_after_adc;
volatile uint32_t model_observation_time;
PhaseDutyRequest model_request = { 0.2f, 0.5f, 0.8f };
static ControlTime model_control_time;
#ifdef PLATFORM_MODEL_TIMING_PROBE
volatile uint32_t model_timing_case = 10U;
volatile uint32_t model_timing_request_sequence = 77U;
volatile uint32_t model_timing_eligible = 1U;
volatile uint32_t model_timing_result;
const uint32_t model_timing_capture_size = sizeof(g_target_validation_timing);
#endif

/* 用位标志选择采样消费者的动作；所有操作仅调用 Platform 公共 API。 */
static void sample_ready(void *context, const PhaseCurrentObservation *observation)
{
    (void)context;
    model_callbacks++;
    model_observation_valid = observation->acquisition_valid;
    model_observation_mask = observation->measured_phases;
    model_observation_b_a = observation->phase_b_a;
    model_observation_c_a = observation->phase_c_a;
    model_observation_time = (uint32_t)observation->observation_time;
    model_marker = 1U;
#ifdef PLATFORM_MODEL_TIMING_PROBE
    if ((model_actions & 128U) != 0U) {
        model_timing_result = timing_probe_run(model_timing_request_sequence,
            model_timing_case, model_timing_eligible != 0U);
    }
#endif
    if ((model_actions & 1U) != 0U) {
        model_early_enable_result = realtime_motor_io_power_enable();
    }
    if ((model_actions & 2U) != 0U) {
        model_stage_result = realtime_motor_io_stage_phase_duty(
            ((model_actions & 4U) != 0U) ? 0 : &model_request);
    }
    model_marker = 2U;
    if ((model_actions & 8U) != 0U) {
        model_second_stage_result = realtime_motor_io_stage_phase_duty(&model_request);
    }
    if ((model_actions & 16U) != 0U) {
        realtime_motor_io_safe_off();
    }
    if ((model_actions & 32U) != 0U) {
        model_enable_result = realtime_motor_io_power_enable();
    }
    if ((model_actions & 64U) != 0U) {
        model_second_stage_result = realtime_motor_io_stage_phase_duty(&model_request);
    }
    model_marker = 3U;
}

void platform_model_init(void)
{
    htim1.Init.Period = REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS;
    htim1.Init.Prescaler = 0U;
    htim1.Init.CounterMode = TIM_COUNTERMODE_CENTERALIGNED1;
    htim1.Init.RepetitionCounter = 3U;
    htim1.Init.AutoReloadPreload = TIM_AUTORELOAD_PRELOAD_ENABLE;
    TIM1->CR1 = TIM_CR1_ARPE | TIM_COUNTERMODE_CENTERALIGNED1;
    TIM1->PSC = 0U;
    TIM1->ARR = REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS;
    TIM1->RCR = 3U;
    TIM1->CCR4 = 4240U;
    TIM1->CCMR1 = TIM_CCMR1_OC1PE | TIM_CCMR1_OC2PE;
    TIM1->CCMR2 = TIM_CCMR2_OC3PE | TIM_CCMR2_OC4PE | (TIM_OCMODE_PWM2 << 8U);
    TIM1->BDTR = TIM_BDTR_OSSI | TIM_BDTR_BKE | TIM_BDTR_BK2E;
    ADC1->JDR1 = 2048U;
    ADC2->JDR1 = 2048U;
    model_bind_result = realtime_motor_io_bind_sample_ready(sample_ready, 0);
    model_arm_result = realtime_motor_io_arm();
    if (model_auto_start != 0U) {
        model_start_result = realtime_motor_io_start_drive_timing();
    }
}

void platform_model_start(void)
{
    model_start_result = realtime_motor_io_start_drive_timing();
}

void platform_model_tick(void)
{
    realtime_motor_io_adc_irq_enter();
    HAL_ADCEx_InjectedConvCpltCallback(&hadc1);
    model_marker = 4U; /* 模拟 HAL 清 injected flags 等剩余尾部。 */
    ADC1->ISR = ADC_FLAG_JEOC | ADC_FLAG_JEOS;
    realtime_motor_io_adc_irq_exit();
}

void platform_model_update_irq(void) { realtime_motor_io_update_irq(); }
void platform_model_adc_enter(void) { realtime_motor_io_adc_irq_enter(); }
void platform_model_adc_callback(void) { HAL_ADCEx_InjectedConvCpltCallback(&hadc1); }
void platform_model_adc_exit(void)
{
    ADC1->ISR = ADC_FLAG_JEOC | ADC_FLAG_JEOS;
    realtime_motor_io_adc_irq_exit();
}
void platform_model_regular_exit(void)
{
    /* HAL 的 regular-only ISR 快照只清 EOC/EOS，不消费后来到达的 JEOS。 */
    ADC1->ISR = ADC_FLAG_EOC | ADC_FLAG_EOS;
    realtime_motor_io_adc_irq_exit();
}

#ifdef PLATFORM_MODEL_CORE_IRQ
void TIM1_UP_TIM16_IRQHandler(void);
void TIM1_BRK_TIM15_IRQHandler(void);
void platform_model_core_up(void) { TIM1_UP_TIM16_IRQHandler(); }
void platform_model_core_break(void) { TIM1_BRK_TIM15_IRQHandler(); }
#endif

void platform_model_wrong_adc_tick(void)
{
    HAL_ADCEx_InjectedConvCpltCallback(&hadc2);
}

void platform_model_stage_outside(void)
{
    model_stage_result = realtime_motor_io_stage_phase_duty(&model_request);
}

void platform_model_enable_outside(void)
{
    model_enable_result = realtime_motor_io_power_enable();
}

void control_time_source_reset(void) { model_control_time = 0U; }
ControlTime control_time_source_advance_fast_tick(void) { return control_time_source_advance_slots(1U); }
ControlTime control_time_source_advance_slots(uint32_t slots) { model_control_time += 2U * slots; return model_control_time; }
ControlTime control_time_source_now(void) { return model_control_time; }
void position_observation_fast_tick(ControlTime time) { (void)time; model_position_ticks++; }
void bus_observation_fast_tick(ControlTime time) { (void)time; model_bus_ticks++; }

HAL_StatusTypeDef HAL_ADCEx_Calibration_Start(ADC_HandleTypeDef *adc, uint32_t mode)
{ (void)adc; (void)mode; return model_hal_ok ? HAL_OK : HAL_ERROR; }
HAL_StatusTypeDef HAL_ADCEx_InjectedStart(ADC_HandleTypeDef *adc)
{ (void)adc; model_adc_started++; return model_hal_ok ? HAL_OK : HAL_ERROR; }
HAL_StatusTypeDef HAL_ADCEx_InjectedStart_IT(ADC_HandleTypeDef *adc)
{ (void)adc; model_adc_started++; return model_hal_ok ? HAL_OK : HAL_ERROR; }
HAL_StatusTypeDef HAL_ADCEx_InjectedStop(ADC_HandleTypeDef *adc)
{ (void)adc; return HAL_OK; }
HAL_StatusTypeDef HAL_ADCEx_InjectedStop_IT(ADC_HandleTypeDef *adc)
{ (void)adc; return HAL_OK; }
HAL_StatusTypeDef HAL_OPAMP_Start(OPAMP_HandleTypeDef *opamp)
{ (void)opamp; return model_hal_ok ? HAL_OK : HAL_ERROR; }
HAL_StatusTypeDef HAL_OPAMP_Stop(OPAMP_HandleTypeDef *opamp)
{ (void)opamp; return HAL_OK; }
HAL_StatusTypeDef HAL_TIM_GenerateEvent(TIM_HandleTypeDef *timer, uint32_t event)
{ if (model_adc_started != 0U) { model_ug_after_adc++; } timer->Instance->EGR = event; return HAL_OK; }
void HAL_NVIC_ClearPendingIRQ(IRQn_Type irq) { (void)irq; }
