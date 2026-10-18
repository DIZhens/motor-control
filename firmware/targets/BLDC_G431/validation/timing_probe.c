#include "timing_probe.h"

/* 采集自身必须计入真实截止预算；与正式实时路径采用相同的速度优化。 */
#if defined(__CC_ARM)
#pragma Otime
#endif

#include <string.h>

#include "tim.h"
#include "realtime_motor_io.h"
#include "../platform/realtime_motor_io_config.h"

#define TIMING_WAIT_CYCLES (17000U)
#define TIMING_MAX_POLLS (4096U) /* CYCCNT 停止时仍有独立的有限退出路径。 */
#define TIMING_FULL_CYCLE (2U * REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS)
#define TIMING_BEFORE_COUNT (3000U)
#define TIMING_BEFORE_MIN_COUNT (2200U)
#define TIMING_AFTER_COUNT (1600U)
#define TIMING_BREAK_FLAGS (TIM_SR_BIF | TIM_SR_B2IF)

volatile TimingProbeCapture g_target_validation_timing;

__STATIC_FORCEINLINE void observe(TimingProbeObservation *value)
{
    value->cycles = DWT->CYCCNT;
    value->cnt = TIM1->CNT;
    value->cr1 = TIM1->CR1;
    value->sr = TIM1->SR;
    value->bdtr = TIM1->BDTR;
    value->ccr1 = TIM1->CCR1;
    value->ccr2 = TIM1->CCR2;
    value->ccr3 = TIM1->CCR3;
}

__STATIC_FORCEINLINE bool live_valid(const TimingProbeObservation *value)
{
    return ((value->bdtr & TIM_BDTR_MOE) == 0U)
        && ((value->sr & TIMING_BREAK_FLAGS) == 0U)
        && ((value->cr1 & (TIM_CR1_CEN | TIM_CR1_CMS | TIM_CR1_UDIS | TIM_CR1_OPM))
            == (TIM_CR1_CEN | TIM_COUNTERMODE_CENTERALIGNED1))
        && ((DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) != 0U)
        && ((CoreDebug->DEMCR & CoreDebug_DEMCR_TRCENA_Msk) != 0U)
        && (value->cnt <= REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS);
}

__STATIC_FORCEINLINE bool baseline_valid(const TimingProbeObservation *value)
{
    return live_valid(value)
        && (SystemCoreClock == REALTIME_MOTOR_IO_TIMER_CLOCK_HZ)
        && ((RCC->CFGR & RCC_CFGR_PPRE2) == 0U)
        && (TIM1->PSC == 0U) && (TIM1->RCR == 0U)
        && (TIM1->ARR == REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS)
        && ((TIM1->DIER & TIM_DIER_UIE) == 0U)
        && ((TIM1->CCMR1 & (TIM_CCMR1_OC1PE | TIM_CCMR1_OC2PE))
            == (TIM_CCMR1_OC1PE | TIM_CCMR1_OC2PE))
        && ((TIM1->CCMR2 & TIM_CCMR2_OC3PE) != 0U)
        && (value->cnt <= REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS);
}

__STATIC_FORCEINLINE bool is_neutral(const TimingProbeObservation *value)
{
    return (value->ccr1 == REALTIME_MOTOR_IO_NEUTRAL_COMPARE_COUNTS)
        && (value->ccr2 == REALTIME_MOTOR_IO_NEUTRAL_COMPARE_COUNTS)
        && (value->ccr3 == REALTIME_MOTOR_IO_NEUTRAL_COMPARE_COUNTS);
}

__STATIC_FORCEINLINE bool is_requested(const TimingProbeObservation *value)
{
    return (value->ccr1 == 1275U) && (value->ccr2 == 1700U) && (value->ccr3 == 2975U);
}

__STATIC_FORCEINLINE bool same_compare(const TimingProbeObservation *left, const TimingProbeObservation *right)
{
    return (left->ccr1 == right->ccr1) && (left->ccr2 == right->ccr2) && (left->ccr3 == right->ccr3);
}

__STATIC_FORCEINLINE bool same_half_cycle(const TimingProbeObservation *value, const TimingProbeObservation *entry)
{
    return ((value->cr1 & TIM_CR1_DIR) != 0U)
        && (entry->cnt > REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS)
        && (value->cnt > REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS)
        && ((value->cycles - entry->cycles)
            < entry->cnt - REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS);
}

__STATIC_FORCEINLINE bool desired_phase(uint32_t case_id, const TimingProbeObservation *value,
    const TimingProbeObservation *entry)
{
    uint32_t elapsed = value->cycles - entry->cycles;

    /* 连 IMMEDIATE 也要求观察到 CYCCNT 前进，不能仅检查 enable 位。 */
    if ((elapsed == 0U) || ((value->cr1 & TIM_CR1_DIR) == 0U)) {
        return false;
    }
    switch (case_id) {
    case TIMING_PROBE_CASE_IMMEDIATE:
    case TIMING_PROBE_CASE_CANCEL:
        return same_half_cycle(value, entry);
    case TIMING_PROBE_CASE_BEFORE:
        return same_half_cycle(value, entry) && (value->cnt <= TIMING_BEFORE_COUNT)
            && (value->cnt >= TIMING_BEFORE_MIN_COUNT);
    case TIMING_PROBE_CASE_AFTER:
        return (elapsed < entry->cnt) && (value->cnt > 0U) && (value->cnt <= TIMING_AFTER_COUNT);
    case TIMING_PROBE_CASE_CROSS_CYCLE:
        return (elapsed >= TIMING_FULL_CYCLE) && (value->cnt > REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS)
            && (value->cnt <= entry->cnt);
    default:
        return false;
    }
}

static void publish(TimingProbeCapture *capture)
{
    uint32_t sequence = g_target_validation_timing.sequence;

    g_target_validation_timing.sequence = sequence + 1U;
    __DMB();
    capture->sequence = sequence + 1U;
    g_target_validation_timing = *capture;
    __DMB();
    g_target_validation_timing.sequence = sequence + 2U;
}

TimingProbeOutcome timing_probe_run(uint32_t request_sequence, uint32_t case_id, bool eligible)
{
    const PhaseDutyRequest request = {0.30f, 0.40f, 0.70f};
    /* ADC consumer 单写者、非重入；静态工作区避免占用 264 bytes ISR 栈。 */
    static TimingProbeCapture capture;
    static TimingProbeObservation second_before;
    TimingProbeOutcome result;
    bool expect_accept;

    /* 初值来自 BSS，后续在发布后清零，将大块清零移出提交窗口。 */
    capture.request_sequence = request_sequence;
    capture.case_id = case_id;
    capture.outcome = TIMING_PROBE_PRECONDITION;
    observe(&capture.entry);
    if (!eligible || (__get_PRIMASK() != 0U) || !baseline_valid(&capture.entry)
        || !is_neutral(&capture.entry) || ((capture.entry.cr1 & TIM_CR1_DIR) == 0U)
        || (capture.entry.cnt <= REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS)
        || (case_id < TIMING_PROBE_CASE_IMMEDIATE) || (case_id > TIMING_PROBE_CASE_CANCEL)) {
        goto cleanup;
    }

    capture.outcome = TIMING_PROBE_TIMEOUT;
    if ((case_id == TIMING_PROBE_CASE_IMMEDIATE) || (case_id == TIMING_PROBE_CASE_CANCEL)) {
        /* 入口已做完整前提检查；无等待的用例只确认时钟前进和原半周期预算。
         * 本 ADC 为最高可屏蔽优先级，期间没有合法 MOE 开启者；正式 stage 仍复核
         * 时间与 Break。完整调用前快照的判定留到调用后，避免测量吞掉被测窗口。 */
        do {
            observe(&capture.before);
            capture.wait_iterations++;
            if (capture.before.cycles != capture.entry.cycles) {
                if (!same_half_cycle(&capture.before, &capture.entry)) {
                    goto cleanup;
                }
                goto stage;
            }
        } while (capture.wait_iterations < TIMING_MAX_POLLS);
        goto cleanup;
    }
    while (capture.wait_iterations < TIMING_MAX_POLLS) {
        observe(&capture.before);
        capture.wait_iterations++;
        capture.elapsed_cycles = capture.before.cycles - capture.entry.cycles;
        /* 静态时钟/preload 配置在入口和结果处核对；本 ADC 内没有合法配置写者。
         * 轮询仅复核仍可能变化的运行状态，避免诊断开销耗尽截止裕量。 */
        if (!live_valid(&capture.before)) {
            capture.outcome = TIMING_PROBE_PRECONDITION;
            goto cleanup;
        }
        if (capture.elapsed_cycles >= TIMING_WAIT_CYCLES) {
            goto cleanup;
        }
        if (desired_phase(case_id, &capture.before, &capture.entry)) {
            break;
        }
        /* 原半周期的目标窗口一旦错过就退出；不能等下一圈伪装成同拍。 */
        if (case_id != TIMING_PROBE_CASE_CROSS_CYCLE) {
            uint32_t budget = capture.entry.cnt;
            if (case_id != TIMING_PROBE_CASE_AFTER) {
                budget -= REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS;
            }
            if ((capture.elapsed_cycles >= budget)
                || ((case_id == TIMING_PROBE_CASE_BEFORE)
                    && ((capture.before.cr1 & TIM_CR1_DIR) != 0U)
                    && (capture.before.cnt < TIMING_BEFORE_MIN_COUNT))) {
                goto cleanup;
            }
        }
    }
    if ((capture.wait_iterations == TIMING_MAX_POLLS)
        && !desired_phase(case_id, &capture.before, &capture.entry)) {
        goto cleanup;
    }

stage:
    capture.stage_called = 1U;
    capture.stage_accepted = realtime_motor_io_stage_phase_duty(&request) ? 1U : 0U;
    observe(&capture.after);
    expect_accept = (case_id == TIMING_PROBE_CASE_IMMEDIATE)
        || (case_id == TIMING_PROBE_CASE_BEFORE) || (case_id == TIMING_PROBE_CASE_CANCEL);
    if (capture.stage_accepted != (expect_accept ? 1U : 0U)) {
        capture.outcome = TIMING_PROBE_MISMATCH;
        goto cleanup;
    }
    if (case_id == TIMING_PROBE_CASE_CANCEL) {
        realtime_motor_io_safe_off();
        observe(&second_before);
        /* 先保留两个调用点的原始读数，完整判定放到第二次调用之后。 */
        capture.second_stage_called = 1U;
        capture.second_stage_accepted = realtime_motor_io_stage_phase_duty(&request) ? 1U : 0U;
        observe(&capture.second_after);
        if (capture.second_stage_accepted || !live_valid(&second_before)
            || !is_neutral(&second_before) || !same_half_cycle(&second_before, &capture.entry)
            || !live_valid(&capture.second_after)
            || !same_compare(&second_before, &capture.second_after)
            || !same_half_cycle(&capture.second_after, &capture.entry)) {
            capture.outcome = TIMING_PROBE_MISMATCH;
            goto cleanup;
        }
    }
    if (!live_valid(&capture.before) || !baseline_valid(&capture.after)
        || (expect_accept ? !is_requested(&capture.after) : !same_compare(&capture.before, &capture.after))) {
        capture.outcome = TIMING_PROBE_MISMATCH;
        goto cleanup;
    }
    capture.outcome = TIMING_PROBE_PASS;

cleanup:
    realtime_motor_io_safe_off();
    observe(&capture.cleanup);
    capture.elapsed_cycles = capture.cleanup.cycles - capture.entry.cycles;
    if ((capture.outcome == TIMING_PROBE_PASS) && (capture.elapsed_cycles >= TIMING_WAIT_CYCLES)) {
        capture.outcome = TIMING_PROBE_TIMEOUT;
    }
    if (((capture.cleanup.bdtr & TIM_BDTR_MOE) != 0U) || !is_neutral(&capture.cleanup)) {
        capture.outcome = TIMING_PROBE_MISMATCH;
    }
    result = (TimingProbeOutcome)capture.outcome;
    publish(&capture);
    memset(&capture, 0, sizeof(capture));
    return result;
}
