#include "realtime_motor_io.h"

#include <stddef.h>

#include "adc.h"
#include "opamp.h"
#include "tim.h"

#include "bus_observation_internal.h"
#include "control_time_source.h"
#include "position_observation_internal.h"
#include "realtime_motor_io_config.h"
#include "realtime_motor_io_internal.h"
#include "raw_adc_capture.h"
#if defined(BLDC_G431_DIAGNOSTICS)
#include "irq_timing.h"
#endif
#include "realtime_motor_io_diagnostic.h"

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
static volatile uint32_t s_slot_sequence;
static uint32_t s_pending_slot;
static uint32_t s_callback_slot;
static uint32_t s_adc_slot;
static uint32_t s_adc_epoch;
static uint32_t s_committed_slot;
static volatile uint32_t s_slot_start_cycles;
static uint32_t s_expected_update_cycles;
static uint32_t s_callback_deadline_cycles;
static volatile ControlTime s_slot_time;
static volatile bool s_adc_busy;
static bool s_adc_injected_irq;
static volatile bool s_adc_eligible;
static volatile bool s_slot_valid;
static volatile bool s_sample_taken;
static volatile bool s_timing_faulted;
static uint32_t s_callback_start_cycles;
static uint32_t s_callback_entry_count;
static uint32_t s_irq_entry_cycles;
static uint32_t s_consumer_entry_cycles;
static uint32_t s_consumer_exit_cycles;
static uint32_t s_auxiliary_exit_cycles;
volatile G431TimingFailure g_g431_timing_failure;

#if defined(BLDC_G431_RAW_ADC_CAPTURE)
volatile uint32_t g_g431_raw_adc_request;
volatile G431RawAdcCapture g_g431_raw_adc_capture;

/* 调用者已有 PRIMASK。只复制本拍已经读取的整数码；不重复读取 ADC，
 * 不改变采样/输出/RTOS。正常构建完全排除；终态发布后数组保持冻结。 */
static void raw_adc_capture_sample(uint32_t raw_b, uint32_t raw_c, bool valid,
    ControlTime time)
{
    volatile G431RawAdcCapture *capture = &g_g431_raw_adc_capture;
    uint32_t count, reason = G431_RAW_ADC_OK, terminal, elapsed;
    uint32_t started;

    if (capture->state != G431_RAW_ADC_COLLECTING
        && (capture->state != G431_RAW_ADC_IDLE
            || g_g431_raw_adc_request != G431_RAW_ADC_CAPTURE_REQUEST)) {
        return;
    }
    started = DWT->CYCCNT;
    terminal = capture->state == G431_RAW_ADC_IDLE
        ? G431_RAW_ADC_REJECTED : G431_RAW_ADC_ABORTED;
    g_g431_raw_adc_request = 0U;
    count = capture->count;
    if (s_power_enabled || (htim1.Instance->BDTR & TIM_BDTR_MOE) != 0U) {
        reason = G431_RAW_ADC_POWER;
    } else if (s_timing_faulted || !s_slot_valid || s_callback_slot != s_slot_sequence) {
        reason = G431_RAW_ADC_TIMING;
    } else if (!valid || raw_b > 4095U || raw_c > 4095U) {
        reason = G431_RAW_ADC_INVALID_PAIR;
    } else if (s_callback_epoch != s_actuation_epoch
        || (count != 0U && capture->epoch != s_actuation_epoch)) {
        reason = G431_RAW_ADC_EPOCH;
    } else if (count >= G431_RAW_ADC_CAPTURE_PAIRS) {
        reason = G431_RAW_ADC_BOUNDS;
    } else if (count != 0U && (s_callback_slot != capture->last_slot + 1U
        || time != capture->last_time + REALTIME_MOTOR_IO_FAST_TICK_PERIOD_TICKS)) {
        reason = G431_RAW_ADC_DISCONTINUITY;
    }
    if (reason == G431_RAW_ADC_OK) {
        if (count == 0U) {
            capture->first_slot = s_callback_slot;
            capture->first_time = time;
            capture->first_entry_cycles = s_callback_start_cycles;
            capture->epoch = s_actuation_epoch;
        }
        capture->raw[count][0] = (uint16_t)raw_b;
        capture->raw[count][1] = (uint16_t)raw_c;
        capture->last_slot = s_callback_slot;
        capture->last_time = time;
        capture->last_entry_cycles = s_callback_start_cycles;
        capture->count = count + 1U;
        terminal = count + 1U == G431_RAW_ADC_CAPTURE_PAIRS
            ? G431_RAW_ADC_COMPLETE : G431_RAW_ADC_COLLECTING;
    }
    capture->reason = reason;
    elapsed = DWT->CYCCNT - started;
    if (elapsed > capture->max_copy_cycles) { capture->max_copy_cycles = elapsed; }
    __DMB();
    capture->state = terminal;
}
#endif

void realtime_motor_io_stage_anchor(uint32_t *cycles, uint32_t *entry_count)
{
    *cycles = s_callback_start_cycles;
    *entry_count = s_callback_entry_count;
}

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
        && (htim1.Instance->RCR == 3U)
        && ((htim1.Instance->CR1 & (TIM_CR1_CMS | TIM_CR1_CKD
                | TIM_CR1_OPM | TIM_CR1_UDIS)) == TIM_COUNTERMODE_CENTERALIGNED1)
        && ((htim1.Instance->CCMR1 & (TIM_CCMR1_OC1PE | TIM_CCMR1_OC2PE))
            == (TIM_CCMR1_OC1PE | TIM_CCMR1_OC2PE))
        && ((htim1.Instance->CCMR2 & TIM_CCMR2_OC3PE) != 0U)
        && (htim1.Instance->CCR4 == 4240U)
        && ((htim1.Instance->CCMR2 & TIM_CCMR2_OC4M) == (TIM_OCMODE_PWM2 << 8U));
}

/* 只关运行期 OC4REF 门；RESET 对 UG 不免疫，因此运行期禁止再发 UG。 */
static void sample_gate_close(void)
{
    MODIFY_REG(htim1.Instance->CR2, TIM_CR2_MMS2, TIM_TRGO2_RESET);
}

__STATIC_FORCEINLINE bool sample_arm_time_available(void)
{
    return ((DWT->CYCCNT - s_slot_start_cycles) < REALTIME_MOTOR_IO_ARM_WINDOW_COUNTS)
        && (htim1.Instance->CNT < REALTIME_MOTOR_IO_ARM_WINDOW_COUNTS)
        && ((htim1.Instance->CR1 & (TIM_CR1_CEN | TIM_CR1_DIR | TIM_CR1_UDIS | TIM_CR1_CMS))
            == (TIM_CR1_CEN | TIM_COUNTERMODE_CENTERALIGNED1))
        && ((htim1.Instance->SR & TIM_FLAG_UPDATE) == 0U);
}

__STATIC_FORCEINLINE bool irq_time_available(void)
{
    uint32_t cr1 = htim1.Instance->CR1;
    uint32_t elapsed = DWT->CYCCNT - s_slot_start_cycles;
    uint32_t mask = TIM_CR1_CEN | TIM_CR1_CMS;

    /* CNT/DIR 每 50 us 重复，不能单独证明当前 sample 的交付身份。
     * cycle、slot、epoch 与尚未处理的 UIF 一起禁止跨目标 UEV 或旧 IRQ 重放。 */
    return ((cr1 & mask) == (TIM_CR1_CEN | TIM_COUNTERMODE_CENTERALIGNED1))
        && ((DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) != 0U)
        && ((CoreDebug->DEMCR & CoreDebug_DEMCR_TRCENA_Msk) != 0U)
        && s_adc_busy && s_adc_eligible && s_slot_valid
        && (s_adc_slot == s_slot_sequence)
        && ((htim1.Instance->SR & TIM_FLAG_UPDATE) == 0U)
        && (elapsed < (REALTIME_MOTOR_IO_CONTROL_PERIOD_CYCLES
            - REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS));
}

__STATIC_FORCEINLINE bool staging_time_available(void)
{
    return !s_timing_faulted && (s_callback_slot == s_slot_sequence)
        && irq_time_available();
}

void realtime_motor_io_stage_deadline(uint32_t *deadline_cycles)
{
    *deadline_cycles = s_callback_deadline_cycles;
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

/* 调用者持有PRIMASK。健康路径不复制大结构；只在原失败分支采集首因。
 * 保存清理会改变的少量上下文后，立即走既有safe-off，再写诊断publication。 */
static void fail_timing_locked(G431TimingFailureReason reason)
{
    uint32_t now = DWT->CYCCNT;
    uint32_t count = htim1.Instance->CNT;
    uint32_t status = htim1.Instance->SR;
    uint32_t control = htim1.Instance->CR1;
    uint32_t epoch = s_actuation_epoch;
    uint32_t state = (s_power_enabled ? 1U : 0U) | (s_adc_busy ? 2U : 0U)
        | (s_adc_eligible ? 4U : 0U) | (s_slot_valid ? 8U : 0U)
        | (s_sample_taken ? 16U : 0U) | (s_staged_this_tick ? 32U : 0U)
        | (s_stage_pending_boundary ? 64U : 0U) | (s_stage_committed ? 128U : 0U)
        | ((DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) != 0U ? 256U : 0U)
        | ((CoreDebug->DEMCR & CoreDebug_DEMCR_TRCENA_Msk) != 0U ? 512U : 0U)
        | (s_callback_epoch == s_actuation_epoch ? 1024U : 0U)
        | (s_adc_slot == s_slot_sequence ? 2048U : 0U);

    s_timing_faulted = true;
    safe_off_locked();
    if (g_g431_timing_failure.sequence != 0U) {
        return;
    }
    g_g431_timing_failure.sequence = 1U;
    __DMB();
    g_g431_timing_failure.reason = (uint32_t)reason;
    g_g431_timing_failure.slot_sequence = s_slot_sequence;
    g_g431_timing_failure.callback_slot = s_callback_slot;
    g_g431_timing_failure.observed_cycles = now;
    g_g431_timing_failure.slot_start_cycles = s_slot_start_cycles;
    g_g431_timing_failure.irq_entry_cycles = s_irq_entry_cycles;
    g_g431_timing_failure.callback_start_cycles = s_callback_start_cycles;
    g_g431_timing_failure.consumer_entry_cycles = s_consumer_entry_cycles;
    g_g431_timing_failure.consumer_exit_cycles = s_consumer_exit_cycles;
    g_g431_timing_failure.auxiliary_exit_cycles = s_auxiliary_exit_cycles;
    g_g431_timing_failure.reserved = 0U;
    g_g431_timing_failure.tim_count = count;
    g_g431_timing_failure.tim_status = status;
    g_g431_timing_failure.tim_control = control;
    g_g431_timing_failure.state_bits = state;
    g_g431_timing_failure.adc1_status = hadc1.Instance->ISR;
    g_g431_timing_failure.adc2_status = hadc2.Instance->ISR;
    g_g431_timing_failure.actuation_epoch = epoch;
    g_g431_timing_failure.callback_epoch = s_callback_epoch;
    g_g431_timing_failure.observation_time = s_slot_time;
    __DMB();
    g_g431_timing_failure.sequence = 2U;
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

    /* neutral、所有 UG 和 RCR 启动相位必须先于 injected external-trigger ready。
     * RCR=1 的 UG 预置内部重复计数，恢复 3 后首个自然 UEV 位于谷底。 */
    if (((htim1.Instance->CR1 & TIM_CR1_CEN) != 0U)
        || !timing_configuration_valid()) {
        stop_current_acquisition();
        return false;
    }
    CLEAR_BIT(htim1.Instance->BDTR, TIM_BDTR_MOE);
    sample_gate_close();
    CLEAR_BIT(htim1.Instance->DIER, TIM_DIER_UIE);
    stage_neutral_duty();
    htim1.Instance->RCR = 1U;
    if (HAL_TIM_GenerateEvent(&htim1, TIM_EVENTSOURCE_UPDATE) != HAL_OK) {
        htim1.Instance->RCR = 3U;
        stop_current_acquisition();
        return false;
    }
    htim1.Instance->RCR = 3U;
    __HAL_TIM_CLEAR_FLAG(&htim1, TIM_FLAG_UPDATE);
    HAL_NVIC_ClearPendingIRQ(TIM1_UP_TIM16_IRQn);

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

    /* arm 已完成 neutral 和重复计数预置；ADC 已 ready 后不能再发 UG。 */
    sample_gate_close();
    __HAL_TIM_CLEAR_FLAG(&htim1, TIM_FLAG_UPDATE);
    s_stage_window_open = false;
    HAL_NVIC_ClearPendingIRQ(TIM1_UP_TIM16_IRQn);

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

    s_adc_busy = false;
    s_adc_eligible = false;
    s_slot_valid = false;
    s_sample_taken = false;
    s_slot_sequence = 0U;
    s_timing_faulted = false;
    s_slot_time = 0U;
    /* CEN 之前读 cycle：软件写入延迟只会缩短首个 arming 窗口。 */
    s_expected_update_cycles = DWT->CYCCNT
        + 2U * REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS;
    s_timing_started = true;
    SET_BIT(htim1.Instance->DIER, TIM_DIER_UIE);
    __HAL_TIM_ENABLE(&htim1);

    return true;
}

void realtime_motor_io_update_irq(void)
{
    uint32_t primask = __get_PRIMASK();
    uint32_t now;
    uint32_t late;
    uint32_t slots;
    uint32_t count;
    uint32_t cr1;
    bool valid;
    bool delivery_valid;
    G431TimingFailureReason failure = (G431TimingFailureReason)0;

    __disable_irq();
    if ((htim1.Instance->SR & TIM_FLAG_UPDATE) == 0U) {
        restore_interrupts(primask);
        return;
    }
    now = DWT->CYCCNT;
    count = htim1.Instance->CNT;
    cr1 = htim1.Instance->CR1;
    sample_gate_close();
    __HAL_TIM_CLEAR_FLAG(&htim1, TIM_FLAG_UPDATE);
    s_stage_window_open = false;
    if (!s_timing_started) {
        restore_interrupts(primask);
        return;
    }

    late = now - s_expected_update_cycles;
    /* 在 cycle 上还未到期的 UIF 也不能制造新 slot。差值以有界半回绕解释。 */
    if (late >= 0x80000000U) {
        fail_timing_locked(G431_TIMING_FAILURE_EARLY_UPDATE);
        s_slot_valid = false;
        restore_interrupts(primask);
        return;
    }
    slots = late / REALTIME_MOTOR_IO_CONTROL_PERIOD_CYCLES + 1U;
    s_slot_start_cycles = s_expected_update_cycles
        + (slots - 1U) * REALTIME_MOTOR_IO_CONTROL_PERIOD_CYCLES;
    s_expected_update_cycles = s_slot_start_cycles
        + REALTIME_MOTOR_IO_CONTROL_PERIOD_CYCLES;
    s_slot_sequence += slots;
    s_slot_time = control_time_source_advance_slots(slots);

    /* 只在指定 UEV 后首个上数半周开门。计数相似不代表 cycle 身份相同。
     * ADC busy 覆盖 HAL 收尾，UP 不得让旧 IRQ 与下一次 sample 重叠。 */
    valid = (slots == 1U)
        && (late < REALTIME_MOTOR_IO_ARM_WINDOW_COUNTS)
        && (count < REALTIME_MOTOR_IO_ARM_WINDOW_COUNTS)
        && ((cr1 & (TIM_CR1_CEN | TIM_CR1_DIR | TIM_CR1_UDIS | TIM_CR1_CMS))
            == (TIM_CR1_CEN | TIM_COUNTERMODE_CENTERALIGNED1))
        && ((DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) != 0U)
        && ((CoreDebug->DEMCR & CoreDebug_DEMCR_TRCENA_Msk) != 0U)
        && ((hadc1.Instance->ISR & (ADC_FLAG_JEOC | ADC_FLAG_JEOS)) == 0U)
        && ((hadc2.Instance->ISR & (ADC_FLAG_JEOC | ADC_FLAG_JEOS)) == 0U)
        && !s_adc_busy;
    delivery_valid = true;
    if (s_stage_pending_boundary) {
        delivery_valid = (s_pending_epoch == s_actuation_epoch)
            && (s_pending_slot == s_slot_sequence);
    } else if (s_power_enabled) {
        delivery_valid = false;
    }
    if (!valid || !delivery_valid) {
        failure = !valid ? G431_TIMING_FAILURE_UPDATE_STATE : G431_TIMING_FAILURE_DELIVERY;
    }
    valid = valid && delivery_valid
        && ((htim1.Instance->SR & (TIM_FLAG_BREAK | TIM_FLAG_BREAK2)) == 0U);
    s_stage_committed = false;
    if (!valid) {
        if (failure != (G431TimingFailureReason)0) {
            fail_timing_locked(failure);
        } else {
            safe_off_locked();
        }
        s_slot_valid = false;
        s_adc_eligible = false;
    } else {
        if (s_stage_pending_boundary) {
            s_stage_pending_boundary = false;
            s_stage_committed = true;
            s_committed_slot = s_slot_sequence;
        }
        /* 处理 pending/ControlTime 的成本也属于 arming 窗口，不能只验入口。 */
        if (sample_arm_time_available()) {
            s_slot_valid = true;
            s_sample_taken = false;
            /* PWM2、CNT < CCR4 共同保证 OC4REF 仍低，开门不会重放第二峰。 */
            MODIFY_REG(htim1.Instance->CR2, TIM_CR2_MMS2, TIM_TRGO2_OC4REF);
            __DSB();
            valid = sample_arm_time_available();
        } else {
            valid = false;
        }
        if (!valid) {
            sample_gate_close();
            s_slot_valid = false;
            fail_timing_locked(G431_TIMING_FAILURE_ARM_WINDOW);
        }
    }
    restore_interrupts(primask);
}

__STATIC_FORCEINLINE void claim_adc_irq(void)
{
    uint32_t primask = __get_PRIMASK();
    uint32_t elapsed;
    bool injected;

    __disable_irq();
    injected = ((hadc1.Instance->ISR & (ADC_FLAG_JEOS | ADC_FLAG_JEOC)) != 0U);
    s_adc_injected_irq = injected;
    if (injected) {
        sample_gate_close();
    }
    elapsed = DWT->CYCCNT - s_slot_start_cycles;
    s_adc_eligible = injected && s_timing_started && s_slot_valid && !s_adc_busy
        && !s_sample_taken && (elapsed >= 4240U)
        && (elapsed < (REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS
            + REALTIME_MOTOR_IO_ARM_WINDOW_COUNTS))
        && ((htim1.Instance->CR1 & TIM_CR1_DIR) != 0U)
        && ((htim1.Instance->SR & TIM_FLAG_UPDATE) == 0U);
    /* 只有 injected 事务拥有 sample slot。Bus 的 regular-only IRQ 可以跨过
     * UEV；它未消费同步 sample，不得阻止 UP 为下一首峰开门。 */
    if (injected) {
        s_adc_busy = true;
    }
    if (s_adc_eligible) {
        s_sample_taken = true;
        s_adc_slot = s_slot_sequence;
        s_adc_epoch = s_actuation_epoch;
    } else if (injected && s_timing_started) {
        fail_timing_locked(G431_TIMING_FAILURE_ADC_ENTRY);
    }
    restore_interrupts(primask);
}

void realtime_motor_io_adc_irq_enter(void)
{
    /* 仅vector入口更新锚点；regular IRQ中迟到的JEOS升级不能重置整段IRQ计时。 */
    s_irq_entry_cycles = DWT->CYCCNT;
    claim_adc_irq();
}

void realtime_motor_io_adc_irq_exit(void)
{
    uint32_t primask = __get_PRIMASK();

    __disable_irq();
    /* exit 放在 HAL/诊断尾部之后；10 us 预留还覆盖异常返回与短 UP 延迟。 */
    if (s_adc_eligible && (!irq_time_available()
        || (s_callback_epoch != s_actuation_epoch))) {
        if (!irq_time_available()) {
            fail_timing_locked(G431_TIMING_FAILURE_IRQ_EXIT);
        } else {
            safe_off_locked();
        }
    }
    /* ADC2 不启用 injected IRQ；被拒绝的旧事务也须在 HAL 收尾后排空副 ADC flags。 */
    if (s_adc_injected_irq) {
        __HAL_ADC_CLEAR_FLAG(&hadc2, ADC_FLAG_JEOC | ADC_FLAG_JEOS);
    }
    s_adc_injected_irq = false;
    s_adc_eligible = false;
    s_adc_busy = false;
    restore_interrupts(primask);
}

/* 从首峰 sample 到下一有效谷底有有限预算；仅运行期热路径启用速度优化。 */
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
         * OC4REF 触发与计数器继续运行。成功路径必须在指定有效谷底前释放 UDIS。 */
        SET_BIT(htim1.Instance->CR1, TIM_CR1_UDIS);
        __DSB();
        if (staging_time_available()) {
            /* 原始网表：PA8/PB13(CH1)=C，PA9/PB14(CH2)=B，PA10/PB15(CH3)=A。
             * HCC 的 A/B/C 始终指物理相；仅在此适配寄存器顺序，不互换电流通道。 */
            htim1.Instance->CCR1 = phase_c;
            htim1.Instance->CCR2 = phase_b;
            htim1.Instance->CCR3 = phase_a;
            __DSB();
            if (staging_time_available() && s_stage_window_open
                && (s_callback_epoch == s_actuation_epoch)
                && ((htim1.Instance->SR & (TIM_FLAG_BREAK | TIM_FLAG_BREAK2)) == 0U)) {
                /* UP 独占 UIF；只有该 slot 的下一指定 UEV 能确认成组装载。 */
                s_pending_slot = s_slot_sequence + 1U;
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
        && (s_committed_slot == s_callback_slot)
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
    uint32_t raw_b = 0U;
    uint32_t raw_c;
    uint32_t primask;
    bool adc2_complete;

    /* Core 入口与 HAL 的 ISR 快照之间可能刚好出现 JEOS：该 IRQ 原先只有
     * regular EOC，但 HAL 已决定交付 injected callback。此处用同一严格入口
     * 规则升级 ownership，之后 busy 一直覆盖到整个 HAL/诊断尾部退出。 */
    if ((hadc == &hadc1) && !s_adc_injected_irq
        && ((hadc1.Instance->ISR & (ADC_FLAG_JEOS | ADC_FLAG_JEOC)) != 0U)) {
        claim_adc_irq();
    }
    if ((hadc != &hadc1) || !s_armed || !s_timing_started || !s_adc_eligible) {
        return;
    }

    primask = __get_PRIMASK();
    __disable_irq();
    /* UP 或 Stop 可能在入口判断后抢占；旧 JDR 不得借新 slot/epoch 重新获得资格。 */
    if (!s_adc_eligible || !s_slot_valid || (s_adc_slot != s_slot_sequence)
        || (s_adc_epoch != s_actuation_epoch)) {
        restore_interrupts(primask);
        return;
    }
    s_callback_start_cycles = DWT->CYCCNT;
    s_callback_entry_count = htim1.Instance->CNT;
    s_callback_epoch = s_actuation_epoch;
    s_callback_slot = s_slot_sequence;
    s_callback_deadline_cycles = s_slot_start_cycles
        + REALTIME_MOTOR_IO_CONTROL_PERIOD_CYCLES
        - REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS;
    s_stage_window_open = false;
    s_staged_this_tick = false;
    observation_time = s_slot_time;
    restore_interrupts(primask);

#if defined(BLDC_G431_DIAGNOSTICS)
    if (g_target_irq_timing_active != 0U) {
        g431_irq_timing_platform_enter(s_callback_start_cycles);
    }
#endif
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
        observation.acquisition_valid = !s_timing_faulted;
    }

    /*
     * G431 只在 sample-ready consumer 的同步 callback 内开放 staging window。
     * callback 返回后立刻关闭，避免后台代码在不可证明的时刻改写下一周期 PWM。
     */
    primask = __get_PRIMASK();
    __disable_irq();
#if defined(BLDC_G431_RAW_ADC_CAPTURE)
    raw_adc_capture_sample(raw_b, raw_c, observation.acquisition_valid, observation_time);
#endif
    s_stage_window_open = (s_callback_epoch == s_actuation_epoch)
        && staging_time_available();
    restore_interrupts(primask);
#if defined(BLDC_G431_DIAGNOSTICS)
    if (g_target_irq_timing_active != 0U) {
        g431_irq_timing_consumer_enter(DWT->CYCCNT);
    }
#endif
    s_consumer_entry_cycles = DWT->CYCCNT;
    s_sample_ready_handler(s_sample_ready_context, &observation);
    s_consumer_exit_cycles = DWT->CYCCNT;
#if defined(BLDC_G431_DIAGNOSTICS)
    if (g_target_irq_timing_active != 0U) {
        g431_irq_timing_consumer_exit(DWT->CYCCNT);
    }
#endif
    s_stage_window_open = false;

    /* 绝对交付截止不随 consumer 时长重置；完整 HAL IRQ 尾部另由 exit 检查。 */
    if ((s_power_enabled && !s_staged_this_tick)
        || !staging_time_available()) {
        if (!irq_time_available() || (s_power_enabled && !s_staged_this_tick)) {
            primask = __get_PRIMASK();
            __disable_irq();
            fail_timing_locked(G431_TIMING_FAILURE_CONSUMER_RETURN);
            restore_interrupts(primask);
        } else {
            realtime_motor_io_safe_off();
        }
    }

    position_observation_fast_tick(observation_time);
    bus_observation_fast_tick(observation_time);
    s_auxiliary_exit_cycles = DWT->CYCCNT;
}

#if defined(__CC_ARM)
#pragma pop
#endif
