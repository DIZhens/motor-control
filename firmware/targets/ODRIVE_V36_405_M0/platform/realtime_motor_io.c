#include "realtime_motor_io.h"

#include <stddef.h>

#include "adc.h"
#include "tim.h"
#include "bus_observation_internal.h"
#include "control_time_source.h"
#include "position_observation_internal.h"
#include "realtime_motor_io_config.h"
#include "realtime_motor_io_internal.h"

#define POWER_CHANNEL_ENABLE_MASK (TIM_CCER_CC1E | TIM_CCER_CC1NE \
    | TIM_CCER_CC2E | TIM_CCER_CC2NE | TIM_CCER_CC3E | TIM_CCER_CC3NE)

static PhaseCurrentSampleReadyHandler s_handler;
static void *s_context;
static bool s_bound;
static volatile bool s_armed;
static volatile bool s_started;
static volatile bool s_stage_window;
static volatile bool s_staged_this_tick;
static volatile bool s_pending_boundary;
static volatile bool s_committed;
static volatile bool s_power_enabled;
static volatile uint32_t s_enable_epoch;
static volatile uint32_t s_callback_epoch;
static volatile uint32_t s_pending_epoch;
static volatile uint64_t s_boundary_count;
static uint64_t s_last_sample_cycle;
static bool s_sample_seen;
static volatile ControlTime s_last_sample_time;

volatile RealtimeMotorIoEnableDiagnostics g_realtime_motor_io_enable_diagnostics;

static void restore_interrupts(uint32_t primask)
{
    if (primask == 0U) {
        __enable_irq();
    }
}

static void stage_neutral(void)
{
    TIM1->CCR1 = REALTIME_MOTOR_IO_NEUTRAL_COMPARE_COUNTS;
    TIM1->CCR2 = REALTIME_MOTOR_IO_NEUTRAL_COMPARE_COUNTS;
    TIM1->CCR3 = REALTIME_MOTOR_IO_NEUTRAL_COMPARE_COUNTS;
}

static bool duty_valid(float duty)
{
    /* NaN / Inf 同时被有界比较拒绝，不隐式 clamp。 */
    return (duty >= REALTIME_MOTOR_IO_DUTY_MIN)
        && (duty <= REALTIME_MOTOR_IO_DUTY_MAX);
}

static bool staging_time_available(void)
{
    /* 只检查提交 deadline；采样频率由 TRGO 上升沿决定，不用 DIR 筛选事件。 */
    return ((TIM1->CR1 & TIM_CR1_DIR) != 0U)
        && (TIM1->CNT >= REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS);
}

static float raw_to_current_a(uint32_t raw)
{
    /* 外部前端为负向 20 mV/A；offset/gain 标定留给 portable Measurement。 */
    return (1.65f - ((float)raw * 3.3f / 4095.0f)) / 0.020f;
}

bool realtime_motor_io_get_facts(RealtimeMotorIoFacts *out_facts)
{
    if ((out_facts == NULL) || (SystemCoreClock != 168000000U)
        || (htim1.Init.Period != REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS)) {
        return false;
    }
    out_facts->normal_voltage_utilization = REALTIME_MOTOR_IO_NORMAL_VOLTAGE_UTILIZATION;
    out_facts->control_time_ticks_per_second = REALTIME_MOTOR_IO_CONTROL_TIME_TICKS_PER_SECOND;
    out_facts->fast_tick_period_ticks = REALTIME_MOTOR_IO_FAST_TICK_PERIOD_TICKS;
    return true;
}

bool realtime_motor_io_bind_sample_ready(PhaseCurrentSampleReadyHandler handler, void *context)
{
    if ((handler == NULL) || s_bound || s_armed || s_started) {
        return false;
    }
    s_handler = handler;
    s_context = context;
    s_bound = true;
    return true;
}

bool realtime_motor_io_arm(void)
{
    if (!s_bound || s_armed || s_started || (TIM1->CR1 & TIM_CR1_CEN) != 0U) {
        return false;
    }

    realtime_motor_io_safe_off();
    /* F405 没有 G4 ADC calibration / OPAMP。两路独立 ADC 使用同一 TRGO。 */
    if (HAL_ADCEx_InjectedStart(&hadc2) != HAL_OK) {
        return false;
    }
    if (HAL_ADCEx_InjectedStart_IT(&hadc3) != HAL_OK) {
        (void)HAL_ADCEx_InjectedStop(&hadc2);
        return false;
    }
    __HAL_ADC_CLEAR_FLAG(&hadc2, ADC_FLAG_JEOC | ADC_FLAG_JSTRT);
    __HAL_ADC_CLEAR_FLAG(&hadc3, ADC_FLAG_JEOC | ADC_FLAG_JSTRT);
    s_armed = true;
    return true;
}

bool realtime_motor_io_start_drive_timing(void)
{
    if (!s_armed || s_started || ((TIM1->CR1 & TIM_CR1_CEN) != 0U)) {
        return false;
    }

    CLEAR_BIT(TIM1->BDTR, TIM_BDTR_MOE);
    stage_neutral();
    TIM1->CNT = 0U;
    TIM1->EGR = TIM_EGR_UG;
    __HAL_TIM_CLEAR_FLAG(&htim1, TIM_FLAG_UPDATE);
    /* 保持 CCxE/CCxNE，OSSI + OIS=LOW 在 MOE=0 时主动驱动六路 inactive。 */
    SET_BIT(TIM1->CCER, POWER_CHANNEL_ENABLE_MASK);
    if (!control_time_source_start()) {
        return false;
    }

    s_boundary_count = 0U;
    s_sample_seen = false;
    s_last_sample_time = control_time_source_now();
    s_started = true;
    SET_BIT(TIM1->DIER, TIM_DIER_UIE);
    SET_BIT(TIM1->CR1, TIM_CR1_CEN);
    return true;
}

bool realtime_motor_io_stage_phase_duty(const PhaseDutyRequest *request)
{
    uint32_t a;
    uint32_t b;
    uint32_t c;
    uint32_t primask;
    bool accepted = false;

    if ((request == NULL) || !duty_valid(request->phase_a_duty)
        || !duty_valid(request->phase_b_duty) || !duty_valid(request->phase_c_duty)) {
        return false;
    }
    a = (uint32_t)(request->phase_a_duty * REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS + 0.5f);
    b = (uint32_t)(request->phase_b_duty * REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS + 0.5f);
    c = (uint32_t)(request->phase_c_duty * REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS + 0.5f);

    primask = __get_PRIMASK();
    __disable_irq();
    if (s_armed && s_started && s_stage_window && !s_staged_this_tick
        && (s_callback_epoch == s_enable_epoch) && staging_time_available()) {
        /* 三个写入在短临界区完成；10 us 余量保护硬件 preload update 边界。 */
        TIM1->CCR1 = a;
        TIM1->CCR2 = b;
        TIM1->CCR3 = c;
        __DMB();
        if (s_stage_window && (s_callback_epoch == s_enable_epoch)) {
            s_pending_epoch = s_enable_epoch;
            s_pending_boundary = true;
            s_staged_this_tick = true;
            accepted = true;
        } else {
            stage_neutral();
        }
    }
    restore_interrupts(primask);
    return accepted;
}

void realtime_motor_io_capture_enable_diagnostics(void)
{
    uint32_t primask = __get_PRIMASK();
    uint32_t flags;
    uint32_t cr1;
    uint32_t counter;

    __disable_irq();
    cr1 = TIM1->CR1;
    counter = TIM1->CNT;
    flags = (s_armed ? 1U : 0U) | (s_started ? 2U : 0U)
        | (s_stage_window ? 4U : 0U) | (s_staged_this_tick ? 8U : 0U)
        | (s_committed ? 16U : 0U)
        | ((s_callback_epoch == s_enable_epoch) ? 32U : 0U)
        | ((((cr1 & TIM_CR1_DIR) != 0U)
            && (counter >= REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS)) ? 64U : 0U);
    g_realtime_motor_io_enable_diagnostics.flags = flags;
    g_realtime_motor_io_enable_diagnostics.tim1_cr1 = cr1;
    g_realtime_motor_io_enable_diagnostics.tim1_cnt = counter;
    g_realtime_motor_io_enable_diagnostics.tim1_bdtr = TIM1->BDTR;
    g_realtime_motor_io_enable_diagnostics.enable_epoch = s_enable_epoch;
    g_realtime_motor_io_enable_diagnostics.callback_epoch = s_callback_epoch;
    g_realtime_motor_io_enable_diagnostics.boundary_count = s_boundary_count;
    __DMB();
    g_realtime_motor_io_enable_diagnostics.capture_count++;
    restore_interrupts(primask);
}

bool realtime_motor_io_power_enable(void)
{
    uint32_t primask = __get_PRIMASK();
    bool enabled = false;

    __disable_irq();
    if (s_armed && s_started && s_stage_window && s_staged_this_tick && s_committed
        && (s_callback_epoch == s_enable_epoch) && staging_time_available()) {
        SET_BIT(TIM1->BDTR, TIM_BDTR_MOE);
        __DSB();
        enabled = ((TIM1->BDTR & TIM_BDTR_MOE) != 0U);
        s_power_enabled = enabled;
    }
    if (!enabled) {
        /* 只在拒绝后记录，不给成功使能路径增加前置诊断开销。 */
        realtime_motor_io_capture_enable_diagnostics();
    }
    restore_interrupts(primask);
    return enabled;
}

void realtime_motor_io_safe_off(void)
{
    uint32_t primask = __get_PRIMASK();

    __disable_irq();
    CLEAR_BIT(TIM1->BDTR, TIM_BDTR_MOE);
    __DSB();
    stage_neutral();
    s_enable_epoch++;
    s_stage_window = false;
    s_staged_this_tick = false;
    s_pending_boundary = false;
    s_committed = false;
    s_power_enabled = false;
    /* CEN、CH4 OC4REF、ADC 和 TIM2 均保持运行；不伪造 Hardware Trip。 */
    restore_interrupts(primask);
}

void realtime_motor_io_update_irq(void)
{
    if ((TIM1->SR & TIM_SR_UIF) == 0U) {
        return;
    }
    __HAL_TIM_CLEAR_FLAG(&htim1, TIM_FLAG_UPDATE);
    if (!s_started) {
        return;
    }
    /* RCR=0 的 peak / valley update 都是真实边界；duty 只在 peak 后提交。 */
    s_boundary_count++;
    if (s_pending_boundary && (s_pending_epoch == s_enable_epoch)) {
        s_committed = true;
        s_pending_boundary = false;
    }
    if (s_power_enabled
        && ((control_time_source_now() - s_last_sample_time)
            > REALTIME_MOTOR_IO_SAMPLE_TIMEOUT_US)) {
        realtime_motor_io_safe_off();
    }
}

void realtime_motor_io_adc_irq(void)
{
    PhaseCurrentObservation observation;
    uint32_t raw_b;
    uint32_t raw_c;
    uint64_t cycle;
    uint32_t counter;
    uint32_t primask;
    uint32_t elapsed_counts;
    ControlTime now;
    bool complete;
    bool on_time;

    /* ADC3 是唯一 pair completion authority；ADC2 不产生 injected IRQ。 */
    if ((ADC3->SR & ADC_SR_JEOC) == 0U) {
        return;
    }
    complete = ((ADC2->SR & ADC_SR_JEOC) != 0U);
    raw_b = ADC2->JDR1;
    raw_c = ADC3->JDR1;
    __HAL_ADC_CLEAR_FLAG(&hadc2, ADC_FLAG_JEOC | ADC_FLAG_JSTRT);
    __HAL_ADC_CLEAR_FLAG(&hadc3, ADC_FLAG_JEOC | ADC_FLAG_JSTRT);
    if (!s_started) {
        return;
    }

    primask = __get_PRIMASK();
    __disable_irq();
    now = control_time_source_now();
    cycle = s_boundary_count / 2U;
    counter = TIM1->CNT;
    on_time = ((TIM1->CR1 & TIM_CR1_DIR) != 0U)
        && (counter >= REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS);
    s_last_sample_time = now;
    restore_interrupts(primask);
    observation.phase_a_a = 0.0f; /* mask 不含 A，此字段没有业务意义。 */
    observation.phase_b_a = complete ? raw_to_current_a(raw_b) : 0.0f;
    observation.phase_c_a = raw_to_current_a(raw_c);
    observation.measured_phases = complete ? (PHASE_MASK_B | PHASE_MASK_C) : PHASE_MASK_C;
    observation.acquisition_valid = complete && on_time
        && (s_sample_seen ? ((cycle - s_last_sample_cycle) == 1U) : (cycle == 0U));
    /* 从 TIM1 phase 回推 TRGO 采集时刻，去掉转换和 IRQ 延迟，精度约 1 us。 */
    elapsed_counts = 2U * REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS
        - REALTIME_MOTOR_IO_SAMPLE_COMPARE_COUNTS - counter;
    observation.observation_time = on_time
        ? now - (elapsed_counts / REALTIME_MOTOR_IO_TIMER_COUNTS_PER_US) : now;
    s_last_sample_cycle = cycle;
    s_sample_seen = true;

    if (!observation.acquisition_valid) {
        realtime_motor_io_safe_off();
    }
    s_staged_this_tick = false;
    s_callback_epoch = s_enable_epoch;
    s_stage_window = observation.acquisition_valid;
    s_handler(s_context, &observation);
    s_stage_window = false;
    if (s_power_enabled && !s_staged_this_tick) {
        realtime_motor_io_safe_off();
    }
    position_observation_fast_tick(now);
    bus_observation_fast_tick(now);
}
