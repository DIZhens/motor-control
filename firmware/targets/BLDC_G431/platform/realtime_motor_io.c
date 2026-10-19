#include "realtime_motor_io.h"

#include <stddef.h>

#include "adc.h"
#include "opamp.h"
#include "tim.h"

#include "bus_observation_internal.h"
#include "control_time_source.h"
#include "position_observation_internal.h"
#include "realtime_motor_io_config.h"
#if defined(BLDC_G431_OBSERVE_ENTRY)
#include "irq_timing.h"
#endif
#if defined(BLDC_G431_OBSERVE_ENTRY) || defined(BLDC_G431_TIMING_ENTRY)
#include "realtime_motor_io_diagnostic.h"
#endif

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
static volatile bool s_stage_window_open;
static volatile bool s_staged_this_tick;
static volatile bool s_stage_pending_boundary;
static volatile bool s_stage_committed;
static volatile bool s_power_enabled;
static volatile uint32_t s_actuation_epoch;
static volatile uint32_t s_callback_epoch;
static volatile uint32_t s_pending_epoch;
static uint32_t s_pending_start_cycles;
static uint32_t s_pending_count;
static uint32_t s_callback_start_cycles;
static uint32_t s_callback_entry_count;

#if defined(BLDC_G431_OBSERVE_ENTRY) || defined(BLDC_G431_TIMING_ENTRY)
void realtime_motor_io_stage_anchor(uint32_t *cycles, uint32_t *entry_count)
{
    *cycles = s_callback_start_cycles;
    *entry_count = s_callback_entry_count;
}
#endif

static void restore_interrupts(uint32_t primask)
{
    if (primask == 0U) {
        __enable_irq();
    }
}

static bool timing_configuration_valid(void)
{
    /* 下述 cycle/count 比值与 UEV 语义仅适用于当前固定配置。 */
    return (SystemCoreClock == REALTIME_MOTOR_IO_TIMER_CLOCK_HZ)
        && ((RCC->CFGR & RCC_CFGR_PPRE2) == 0U)
        && (htim1.Instance->PSC == 0U)
        && (htim1.Instance->ARR == REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS)
        && (htim1.Instance->RCR == 0U)
        && ((htim1.Instance->CR1 & (TIM_CR1_CMS | TIM_CR1_CKD
                | TIM_CR1_OPM | TIM_CR1_UDIS)) == TIM_COUNTERMODE_CENTERALIGNED1)
        && ((htim1.Instance->CCMR1 & (TIM_CCMR1_OC1PE | TIM_CCMR1_OC2PE))
            == (TIM_CCMR1_OC1PE | TIM_CCMR1_OC2PE))
        && ((htim1.Instance->CCMR2 & TIM_CCMR2_OC3PE) != 0U)
        && ((htim1.Instance->DIER & TIM_DIER_UIE) == 0U);
}

__STATIC_FORCEINLINE bool staging_time_available(void)
{
    uint32_t before = htim1.Instance->CR1;
    uint32_t count = htim1.Instance->CNT;
    uint32_t after = htim1.Instance->CR1;
    uint32_t elapsed = DWT->CYCCNT - s_callback_start_cycles;
    uint32_t required = TIM_CR1_CEN | TIM_CR1_DIR | TIM_COUNTERMODE_CENTERALIGNED1;
    uint32_t mask = TIM_CR1_CEN | TIM_CR1_DIR | TIM_CR1_CMS;

    /* DIR/CNT 单独检查会把耗时一整个 PWM 周期的旧 callback 误认作准时。
     * 入口 cycle/count 锚点把窗口限定在本次 callback 所属的同一谷底之前。
     * 先读 cycle 再读入口 CNT，使预算保守；无等待、无计数器清零。 */
    return ((before & mask) == required) && ((after & mask) == required)
        && ((DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) != 0U)
        && ((CoreDebug->DEMCR & CoreDebug_DEMCR_TRCENA_Msk) != 0U)
        && (s_callback_entry_count > REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS)
        && (count >= REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS)
        && (count <= s_callback_entry_count)
        && (elapsed < (s_callback_entry_count - REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS));
}

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

/* 调用者已屏蔽可屏蔽中断；MOE 先关，旧 epoch 不允许随后恢复功率。 */
static void safe_off_locked(void)
{
    CLEAR_BIT(htim1.Instance->BDTR, TIM_BDTR_MOE);
    __DSB();
    stage_neutral_duty();
    s_actuation_epoch++;
    s_stage_window_open = false;
    s_staged_this_tick = false;
    s_stage_pending_boundary = false;
    s_stage_committed = false;
    s_power_enabled = false;
}

bool realtime_motor_io_get_facts(RealtimeMotorIoFacts *out_facts)
{
    if (out_facts == NULL) {
        return false;
    }

    if ((htim1.Init.Period != REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS)
        || !timing_configuration_valid()) {
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

    if (((htim1.Instance->CR1 & TIM_CR1_CEN) != 0U)
        || !timing_configuration_valid()) {
        return false;
    }

    /* 共享只增的 CPU cycle clock；不依赖台架先启用，也不重置其他使用者。 */
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
    if ((DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) == 0U) {
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
    if (HAL_TIM_GenerateEvent(&htim1, TIM_EVENTSOURCE_UPDATE) != HAL_OK) {
        return false;
    }
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

/* 从 ADC 到下一谷底有有限预算；只对运行期热路径启用速度优化。 */
#if defined(__CC_ARM)
#pragma push
#pragma Otime
#endif

bool realtime_motor_io_stage_phase_duty(const PhaseDutyRequest *request)
{
    uint32_t phase_a;
    uint32_t phase_b;
    uint32_t phase_c;
    uint32_t primask;
    bool accepted = false;

    /* 已取消/关闭的回调无需做浮点转换；有效入口仍在临界区内完整复核。 */
    if (!s_stage_window_open) {
        return false;
    }
    if ((request == NULL) || !duty_is_valid(request->phase_a_duty)
        || !duty_is_valid(request->phase_b_duty)
        || !duty_is_valid(request->phase_c_duty)) {
        return false;
    }

    /* 浮点转换留在临界区外；进去以后重新检查所有可被 safe-off 改变的状态。 */
    phase_a = duty_to_compare(request->phase_a_duty);
    phase_b = duty_to_compare(request->phase_b_duty);
    phase_c = duty_to_compare(request->phase_c_duty);
    primask = __get_PRIMASK();
    __disable_irq();

    if (s_armed && s_timing_started && s_stage_window_open && !s_staged_this_tick
        && (s_callback_epoch == s_actuation_epoch)
        && ((htim1.Instance->CR1 & TIM_CR1_UDIS) == 0U)
        && ((htim1.Instance->SR & (TIM_FLAG_BREAK | TIM_FLAG_BREAK2)) == 0U)
        && staging_time_available()) {
        /* PRIMASK 只阻止软件抢占；UDIS 阻止硬件在三个 preload 写入之间更新。
         * OC4REF 触发与计数器继续运行。成功路径必须在同一谷底前释放 UDIS。 */
        SET_BIT(htim1.Instance->CR1, TIM_CR1_UDIS);
        __DSB();
        if (staging_time_available()) {
            htim1.Instance->CCR1 = phase_a;
            htim1.Instance->CCR2 = phase_b;
            htim1.Instance->CCR3 = phase_c;
            __DSB();
            if (staging_time_available() && s_stage_window_open
                && (s_callback_epoch == s_actuation_epoch)
                && ((htim1.Instance->SR & (TIM_FLAG_BREAK | TIM_FLAG_BREAK2)) == 0U)) {
                /* 本 Platform 独占 UIF（UIE 关闭），下一次 UEV 才能证明提交生效。 */
                s_pending_start_cycles = DWT->CYCCNT;
                s_pending_count = htim1.Instance->CNT;
                __HAL_TIM_CLEAR_FLAG(&htim1, TIM_FLAG_UPDATE);
                s_pending_epoch = s_actuation_epoch;
                s_stage_pending_boundary = true;
                s_staged_this_tick = true;
                accepted = true;
            }
        }
        if (!accepted) {
            /* 未完成的请求不能作为部分三相或旧请求在下一次 enable 中出现。 */
            safe_off_locked();
        }
        __DSB();
        CLEAR_BIT(htim1.Instance->CR1, TIM_CR1_UDIS);
        __DSB();
        if (accepted && (!staging_time_available()
            || ((htim1.Instance->SR & (TIM_FLAG_BREAK | TIM_FLAG_BREAK2)) != 0U))) {
            safe_off_locked();
            accepted = false;
        }
    }
    restore_interrupts(primask);
    return accepted;
}

bool realtime_motor_io_power_enable(void)
{
    uint32_t primask;
    bool enabled = false;

    primask = __get_PRIMASK();
    __disable_irq();

    if (s_armed && s_timing_started && s_stage_window_open
        && s_stage_committed && s_staged_this_tick
        && (s_callback_epoch == s_actuation_epoch)
        && ((htim1.Instance->CR1 & TIM_CR1_UDIS) == 0U)
        && staging_time_available()
        && ((htim1.Instance->SR & (TIM_FLAG_BREAK | TIM_FLAG_BREAK2)) == 0U)) {
        SET_BIT(htim1.Instance->BDTR, TIM_BDTR_MOE);
        __DSB();

        enabled = ((htim1.Instance->BDTR & TIM_BDTR_MOE) != 0U)
            && ((htim1.Instance->SR & (TIM_FLAG_BREAK | TIM_FLAG_BREAK2)) == 0U)
            && staging_time_available();
    }
    if (enabled) {
        s_power_enabled = true;
    } else {
        safe_off_locked();
    }
    restore_interrupts(primask);
    return enabled;
}

void realtime_motor_io_safe_off(void)
{
    uint32_t primask;

    primask = __get_PRIMASK();
    __disable_irq();

    safe_off_locked();
    restore_interrupts(primask);
}

void HAL_ADCEx_InjectedConvCpltCallback(ADC_HandleTypeDef *hadc)
{
    PhaseCurrentObservation observation;
    ControlTime observation_time;
    uint32_t raw_b;
    uint32_t raw_c;
    uint32_t primask;
    uint32_t entry_cr1;
    bool adc2_complete;

    if ((hadc != &hadc1) || !s_armed || !s_timing_started) {
        return;
    }

    primask = __get_PRIMASK();
    __disable_irq();
    s_callback_start_cycles = DWT->CYCCNT;
    entry_cr1 = htim1.Instance->CR1;
    s_callback_entry_count = htim1.Instance->CNT;
    s_callback_epoch = s_actuation_epoch;
    s_stage_window_open = false;

    /*
     * stage 在 UDIS 内清过 UIF；只凭后续硬件 UEV 认定已生效。
     * 重复/过早的 callback 不再被误当成已经跨过更新边界。
     */
    if (s_stage_pending_boundary && (s_pending_epoch == s_actuation_epoch)
        && ((htim1.Instance->SR & TIM_FLAG_UPDATE) != 0U)
        && ((entry_cr1 & TIM_CR1_UDIS) == 0U)
        && ((s_callback_start_cycles - s_pending_start_cycles) >= s_pending_count)) {
        s_stage_pending_boundary = false;
        s_stage_committed = true;
    }

    s_staged_this_tick = false;
    /* 入口在向上半周或翻转附近时，本次 consumer 不获得 stage 权限。 */
    if (((entry_cr1 & TIM_CR1_DIR) == 0U)
        || ((htim1.Instance->CR1 & TIM_CR1_DIR) == 0U)) {
        s_callback_entry_count = 0U;
    }
    restore_interrupts(primask);

#if defined(BLDC_G431_OBSERVE_ENTRY)
    if (g_target_irq_timing_active != 0U) {
        g431_irq_timing_platform_enter(s_callback_start_cycles);
    }
#endif
    observation_time = control_time_source_advance_fast_tick();

    /* 两路均固定单 injected rank；直接读取 JDR1，免去通用 rank 分派。 */
    raw_c = hadc1.Instance->JDR1;

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
        raw_b = hadc2.Instance->JDR1;
        __HAL_ADC_CLEAR_FLAG(&hadc2, ADC_FLAG_JEOC | ADC_FLAG_JEOS);

        observation.phase_b_a = adc_raw_to_phase_current_a(raw_b);
        observation.measured_phases = PHASE_MASK_B | PHASE_MASK_C;
        observation.acquisition_valid = true;
    }

    /*
     * G431 只在 sample-ready consumer 的同步 callback 内开放 staging window。
     * callback 返回后立刻关闭，避免后台代码在不可证明的时刻改写下一周期 PWM。
     */
    primask = __get_PRIMASK();
    __disable_irq();
    s_stage_window_open = (s_callback_epoch == s_actuation_epoch)
        && staging_time_available();
    restore_interrupts(primask);
#if defined(BLDC_G431_OBSERVE_ENTRY)
    if (g_target_irq_timing_active != 0U) {
        g431_irq_timing_consumer_enter(DWT->CYCCNT);
    }
#endif
    s_sample_ready_handler(s_sample_ready_context, &observation);
#if defined(BLDC_G431_OBSERVE_ENTRY)
    if (g_target_irq_timing_active != 0U) {
        g431_irq_timing_consumer_exit(DWT->CYCCNT);
    }
#endif
    s_stage_window_open = false;

    /*
     * RUN 中每个 fast tick 都必须提交 fresh actuation。
     * 无成功 stage 或 consumer 用尽整个半周期时立刻 fail-safe；HAL 在
     * callback 返回后才清 JEOS，不能让过长 consumer 吞掉下一次采样。
     */
    if (s_power_enabled && (!s_staged_this_tick
        || ((DWT->CYCCNT - s_callback_start_cycles)
            >= REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS))) {
        realtime_motor_io_safe_off();
    }

    position_observation_fast_tick(observation_time);
    bus_observation_fast_tick(observation_time);
}

#if defined(__CC_ARM)
#pragma pop
#endif
