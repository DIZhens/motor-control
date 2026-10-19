#include "protection_probe.h"

#include <stddef.h>

#include "comp.h"
#include "dac.h"
#include "tim.h"

#define PROBE_WAIT_CYCLES (10U * 170U)
#define PROBE_MAX_POLLS (128U) /* CYCCNT 停止递增时，仍以轮询次数限制等待。 */
#define BREAK_FLAGS (TIM_SR_BIF | TIM_SR_B2IF)
#define COMP_CONFIG_MASK (COMP_CSR_EN | COMP_CSR_INMSEL | COMP_CSR_INPSEL \
    | COMP_CSR_POLARITY | COMP_CSR_BLANKING)

volatile ProtectionProbeCapture g_target_validation_probe;
static ProtectionProbeCapture s_capture;
static volatile uint32_t *s_threshold;
static bool s_held;

static void publish(void)
{
    uint32_t sequence = g_target_validation_probe.sequence;

    g_target_validation_probe.sequence = sequence + 1U;
    __DMB();
    s_capture.sequence = sequence + 1U;
    g_target_validation_probe = s_capture;
    __DMB();
    g_target_validation_probe.sequence = sequence + 2U;
}

static uint32_t comparator_mask(void)
{
    return (((COMP1->CSR & COMP_CSR_VALUE) != 0U) ? 1U : 0U)
        | (((COMP2->CSR & COMP_CSR_VALUE) != 0U) ? 2U : 0U)
        | (((COMP3->CSR & COMP_CSR_VALUE) != 0U) ? 4U : 0U)
        | (((COMP4->CSR & COMP_CSR_VALUE) != 0U) ? 8U : 0U);
}

static bool configuration_valid(void)
{
    uint32_t bdtr = TIM1->BDTR;
    uint32_t bdtr_mask = TIM_BDTR_BKE | TIM_BDTR_BKP | TIM_BDTR_BKF
        | TIM_BDTR_BK2E | TIM_BDTR_BK2P | TIM_BDTR_BK2F | TIM_BDTR_AOE;
    uint32_t af1_mask = TIM1_AF1_BKCMP3E | TIM1_AF1_BKCMP3P;
    uint32_t af2_mask = TIM1_AF2_BK2CMP1E | TIM1_AF2_BK2CMP2E | TIM1_AF2_BK2CMP4E
        | TIM1_AF2_BK2CMP1P | TIM1_AF2_BK2CMP2P | TIM1_AF2_BK2CMP4P;

    return ((bdtr & bdtr_mask) == (TIM_BDTR_BKE | TIM_BDTR_BKP | TIM_BDTR_BK2E | TIM_BDTR_BK2P))
        && ((TIM1->DIER & TIM_DIER_BIE) != 0U)
        && ((TIM1->AF1 & af1_mask) == TIM1_AF1_BKCMP3E)
        && ((TIM1->AF2 & af2_mask) == (TIM1_AF2_BK2CMP1E | TIM1_AF2_BK2CMP2E | TIM1_AF2_BK2CMP4E))
        && ((COMP1->CSR & COMP_CONFIG_MASK) == (COMP_CSR_EN | COMP_INPUT_MINUS_DAC3_CH1))
        && ((COMP2->CSR & COMP_CONFIG_MASK) == (COMP_CSR_EN | COMP_INPUT_MINUS_DAC3_CH2))
        && ((COMP3->CSR & COMP_CONFIG_MASK) == (COMP_CSR_EN | COMP_INPUT_MINUS_DAC1_CH1))
        && ((COMP4->CSR & COMP_CONFIG_MASK) == (COMP_CSR_EN | COMP_INPUT_MINUS_DAC3_CH2))
        && ((DAC1->CR & (DAC_CR_EN1 | DAC_CR_TEN1)) == DAC_CR_EN1)
        && ((DAC3->CR & (DAC_CR_EN1 | DAC_CR_TEN1 | DAC_CR_EN2 | DAC_CR_TEN2)) == (DAC_CR_EN1 | DAC_CR_EN2))
        && ((DAC1->MCR & DAC_MCR_MODE1) == (DAC_MCR_MODE1_0 | DAC_MCR_MODE1_1))
        && ((DAC3->MCR & (DAC_MCR_MODE1 | DAC_MCR_MODE2))
            == (DAC_MCR_MODE1_0 | DAC_MCR_MODE1_1 | DAC_MCR_MODE2_0 | DAC_MCR_MODE2_1));
}

bool protection_probe_is_held(void)
{
    return s_held;
}

bool protection_probe_prepare(ProtectionProbeSource source, uint32_t request_sequence)
{
    ProtectionProbeCapture empty = {0};

    if (s_held || ((TIM1->BDTR & TIM_BDTR_MOE) != 0U)
        || !configuration_valid() || (comparator_mask() != 0U)
        || ((TIM1->SR & BREAK_FLAGS) != 0U)) {
        return false;
    }
    switch (source) {
    case PROTECTION_PROBE_OVP:
        s_threshold = &DAC1->DHR12R1;
        empty.expected_comp_mask = 4U;
        empty.expected_break_flag = TIM_SR_BIF;
        break;
    case PROTECTION_PROBE_OCP_C:
        s_threshold = &DAC3->DHR12R1;
        empty.expected_comp_mask = 1U;
        empty.expected_break_flag = TIM_SR_B2IF;
        break;
    case PROTECTION_PROBE_OCP_AB:
        s_threshold = &DAC3->DHR12R2;
        empty.expected_comp_mask = 10U; /* 两个比较器共用阈值，必须同时观察到两路输出。 */
        empty.expected_break_flag = TIM_SR_B2IF;
        break;
    default:
        return false;
    }
    empty.original_code = *s_threshold;
    if ((empty.original_code == 0U) || (empty.original_code > 4095U)) {
        return false;
    }
    empty.request_sequence = request_sequence;
    empty.source = (uint32_t)source;
    empty.outcome = PROTECTION_PROBE_PREPARED;
    s_capture = empty;
    publish();
    return true;
}

ProtectionProbeOutcome protection_probe_fire(void)
{
    s_capture.bdtr_before = TIM1->BDTR;
    s_capture.sr_before = TIM1->SR;
    s_capture.comp_before = comparator_mask();
    if ((__get_PRIMASK() == 0U) || s_held || (s_threshold == NULL)
        || (s_capture.outcome != PROTECTION_PROBE_PREPARED)
        || ((s_capture.bdtr_before & TIM_BDTR_MOE) == 0U)
        || ((s_capture.sr_before & BREAK_FLAGS) != 0U) || (s_capture.comp_before != 0U)
        || !configuration_valid() || (*s_threshold != s_capture.original_code)) {
        s_capture.outcome = PROTECTION_PROBE_REJECTED;
        publish();
        return PROTECTION_PROBE_REJECTED;
    }

    s_held = true;
    s_capture.held = 1U;
    s_capture.outcome = PROTECTION_PROBE_NOT_OBSERVED;
    s_capture.start_cycles = DWT->CYCCNT;
    *s_threshold = 0U;
    __DSB();
    do {
        s_capture.poll_count++;
        s_capture.comp_after = comparator_mask();
        s_capture.sr_after = TIM1->SR;
        s_capture.bdtr_after = TIM1->BDTR;
        s_capture.elapsed_cycles = DWT->CYCCNT - s_capture.start_cycles;
        if ((s_capture.comp_after == s_capture.expected_comp_mask)
            && ((s_capture.sr_after & BREAK_FLAGS) == s_capture.expected_break_flag)
            && ((s_capture.bdtr_after & TIM_BDTR_MOE) == 0U)) {
            s_capture.outcome = PROTECTION_PROBE_OBSERVED;
            break;
        }
    } while ((s_capture.elapsed_cycles < PROBE_WAIT_CYCLES)
        && (s_capture.poll_count < PROBE_MAX_POLLS));
    publish();
    return (ProtectionProbeOutcome)s_capture.outcome;
}

bool protection_probe_clear(void)
{
    if ((TIM1->BDTR & TIM_BDTR_MOE) != 0U) {
        return false;
    }
    if (s_held) {
        if (*s_threshold != 0U) {
            return false; /* 阈值被外部意外改动时，不覆盖该值。 */
        }
        *s_threshold = s_capture.original_code;
        __DSB();
        s_held = false;
        s_capture.held = 0U;
        publish();
    }
    return true;
}
