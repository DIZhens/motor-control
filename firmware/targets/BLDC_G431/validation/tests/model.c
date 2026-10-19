/* 用 HCC 与时间替身执行正式 ARM 验证代码；不包含电气模型。 */
#include "target_validation.h"
#include "tim.h"
#include "comp.h"
#include "dac.h"
#include "protection_probe.h"
#include <stddef.h>

uint32_t SystemCoreClock = 170000000U;
TIM_HandleTypeDef htim1 = { .Instance = TIM1 };
TIM_HandleTypeDef htim6 = { .Instance = TIM6 };
uint64_t model_time;
uint32_t model_step_ticks = 1U;
uint32_t model_step_us = 50U;
uint32_t model_current_valid = 1U;
uint32_t model_current_mask = PHASE_MASK_B | PHASE_MASK_C;
float model_current_b_a;
float model_current_c_a;
uint32_t model_bus_valid = 1U;
uint32_t model_bus_age_ticks;
uint32_t model_bus_future_ticks;
float model_bus_voltage = 12.0f;
uint32_t model_trip_valid = 1U;
uint32_t model_trip_active;
uint32_t model_trip_causes;
uint32_t model_facts_ok = 1U;
uint32_t model_bind_ok = 1U;
uint32_t model_arm_ok = 1U;
uint32_t model_trip_arm_ok = 1U;
uint32_t model_start_ok = 1U;
uint32_t model_stage_ok = 1U;
uint32_t model_enable_ok = 1U;
uint32_t model_stage_calls;
uint32_t model_enable_calls;
uint32_t model_off_calls;
uint32_t model_common_duty_failures;
PhaseDutyRequest model_last_duty;
uint32_t model_clock_ok = 1U;
uint32_t model_rearm_ok = 1U;
uint32_t model_rearm_calls;
static PhaseCurrentSampleReadyHandler s_handler;
static void *s_context;
static HardwareTripHandler s_trip_handler;
static void *s_trip_context;
static bool s_window;
static bool s_staged;
static bool s_pending;
static bool s_committed;

const uint32_t model_offsets[] = {
    offsetof(TargetValidationSnapshot, sequence),
    offsetof(TargetValidationSnapshot, started),
    offsetof(TargetValidationSnapshot, state),
    offsetof(TargetValidationSnapshot, result),
    offsetof(TargetValidationSnapshot, command_sequence),
    offsetof(TargetValidationSnapshot, active_request_sequence),
    offsetof(TargetValidationSnapshot, fast_tick_count),
    offsetof(TargetValidationSnapshot, invalid_current_count),
    offsetof(TargetValidationSnapshot, stage_attempts),
    offsetof(TargetValidationSnapshot, stage_successes),
    offsetof(TargetValidationSnapshot, enable_attempts),
    offsetof(TargetValidationSnapshot, enable_successes),
    offsetof(TargetValidationSnapshot, capture_samples),
    offsetof(TargetValidationSnapshot, guard_irq_count),
    offsetof(TargetValidationCurrentCapture, sequence),
    offsetof(TargetValidationCurrentCapture, statistics),
    offsetof(TargetValidationCurrentStatistics, request_sequence),
    offsetof(TargetValidationCurrentStatistics, result),
    offsetof(TargetValidationCurrentStatistics, sample_count),
    offsetof(TargetValidationCurrentStatistics, interval_min_cycles),
    offsetof(TargetValidationCurrentStatistics, interval_max_cycles),
    offsetof(TargetValidationCurrentStatistics, first_sample_time),
    offsetof(TargetValidationCurrentStatistics, last_sample_time),
    offsetof(TargetValidationCurrentStatistics, phase_b),
    offsetof(TargetValidationCurrentStatistics, phase_c),
    sizeof(TargetValidationMailbox),
    sizeof(PlatformBringupDebug),
    offsetof(TargetValidationSnapshot, interval_min_cycles),
    offsetof(TargetValidationSnapshot, interval_max_cycles)
};

const uint32_t model_probe_size = sizeof(ProtectionProbeCapture);

bool realtime_motor_io_get_facts(RealtimeMotorIoFacts *facts)
{
    facts->normal_voltage_utilization = 0.45f;
    facts->control_time_ticks_per_second = 20000U;
    facts->fast_tick_period_ticks = 1U;
    return model_facts_ok != 0U;
}
bool realtime_motor_io_bind_sample_ready(PhaseCurrentSampleReadyHandler fn, void *ctx)
{
    s_handler = fn;
    s_context = ctx;
    return model_bind_ok != 0U;
}
bool realtime_motor_io_arm(void)
{
    realtime_motor_io_safe_off();
    return model_arm_ok != 0U;
}
bool realtime_motor_io_start_drive_timing(void)
{
    if (!model_start_ok) {
        return false;
    }
    TIM1->CR1 = TIM_CR1_CEN;
    return true;
}
bool realtime_motor_io_stage_phase_duty(const PhaseDutyRequest *duty)
{
    model_stage_calls++;
    model_last_duty = *duty;
    if ((duty->phase_a_duty != duty->phase_b_duty)
        || (duty->phase_b_duty != duty->phase_c_duty)) {
        model_common_duty_failures++;
    }
    if (!model_stage_ok || !s_window || s_staged) {
        return false;
    }
    s_staged = true;
    s_pending = true;
    TIM1->CCR1 = (uint32_t)(duty->phase_a_duty * 4250.0f + 0.5f);
    TIM1->CCR2 = (uint32_t)(duty->phase_b_duty * 4250.0f + 0.5f);
    TIM1->CCR3 = (uint32_t)(duty->phase_c_duty * 4250.0f + 0.5f);
    return true;
}
bool realtime_motor_io_power_enable(void)
{
    model_enable_calls++;
    if (!model_enable_ok || !s_window || !s_staged || !s_committed) {
        return false;
    }
    TIM1->BDTR |= TIM_BDTR_MOE;
    return true;
}
void realtime_motor_io_safe_off(void)
{
    model_off_calls++;
    TIM1->BDTR &= ~TIM_BDTR_MOE;
    TIM1->CCR1 = 2125U;
    TIM1->CCR2 = 2125U;
    TIM1->CCR3 = 2125U;
    s_window = false;
    s_staged = false;
    s_pending = false;
    s_committed = false;
}
bool hardware_trip_bind(HardwareTripHandler fn, void *ctx)
{
    s_trip_handler = fn;
    s_trip_context = ctx;
    return true;
}
bool hardware_trip_arm(void)
{
    TIM1->BDTR = TIM_BDTR_BKE | TIM_BDTR_BKP | TIM_BDTR_BK2E | TIM_BDTR_BK2P;
    TIM1->DIER |= TIM_DIER_BIE;
    TIM1->AF1 = TIM1_AF1_BKCMP3E;
    TIM1->AF2 = TIM1_AF2_BK2CMP1E | TIM1_AF2_BK2CMP2E | TIM1_AF2_BK2CMP4E;
    COMP1->CSR = COMP_CSR_EN | COMP_INPUT_MINUS_DAC3_CH1;
    COMP2->CSR = COMP_CSR_EN | COMP_INPUT_MINUS_DAC3_CH2;
    COMP3->CSR = COMP_CSR_EN | COMP_INPUT_MINUS_DAC1_CH1;
    COMP4->CSR = COMP_CSR_EN | COMP_INPUT_MINUS_DAC3_CH2;
    DAC1->CR = DAC_CR_EN1;
    DAC3->CR = DAC_CR_EN1 | DAC_CR_EN2;
    DAC1->MCR = DAC_MCR_MODE1_0 | DAC_MCR_MODE1_1;
    DAC3->MCR = DAC_MCR_MODE1_0 | DAC_MCR_MODE1_1 | DAC_MCR_MODE2_0 | DAC_MCR_MODE2_1;
    DAC1->DHR12R1 = 2065U;
    DAC3->DHR12R1 = 345U;
    DAC3->DHR12R2 = 345U;
    return model_trip_arm_ok != 0U;
}
bool hardware_trip_get_snapshot(HardwareTripSnapshot *out)
{
    uint32_t comp = (COMP1->CSR | COMP2->CSR | COMP4->CSR) & COMP_CSR_VALUE;
    bool ovp = (COMP3->CSR & COMP_CSR_VALUE) != 0U;

    if (comp || ((TIM1->SR & TIM_SR_B2IF) != 0U)) {
        model_trip_causes |= HARDWARE_TRIP_CAUSE_OVERCURRENT;
    }
    if (ovp || ((TIM1->SR & TIM_SR_BIF) != 0U)) {
        model_trip_causes |= HARDWARE_TRIP_CAUSE_BUS_OVERVOLTAGE;
    }
    out->condition_active = (model_trip_active != 0U) || (comp != 0U) || ovp;
    out->causes = model_trip_causes;
    return model_trip_valid != 0U;
}
bool hardware_trip_rearm(void)
{
    HardwareTripSnapshot trip;

    model_rearm_calls++;
    realtime_motor_io_safe_off();
    (void)hardware_trip_get_snapshot(&trip);
    if (!model_rearm_ok || trip.condition_active) {
        return false;
    }
    TIM1->SR &= ~(TIM_SR_BIF | TIM_SR_B2IF);
    model_trip_causes = 0U;
    return true;
}
bool position_observation_arm(void) { return true; }
bool bus_observation_arm(void) { return true; }
bool position_observation_get_latest(PositionObservation *out)
{
    out->observation_time = model_time;
    out->mechanical_position_turns = 0.25f;
    out->referenced = true;
    out->acquisition_valid = true;
    return true;
}
bool bus_observation_get_latest(BusObservation *out)
{
    out->observation_time = model_time - model_bus_age_ticks + model_bus_future_ticks;
    out->voltage_v = model_bus_voltage;
    out->acquisition_valid = model_bus_valid != 0U;
    return true;
}
uint32_t HAL_GetTick(void) { return DWT->CYCCNT / 170000U; }

void model_init(void)
{
    TIM6->PSC = model_clock_ok ? 169U : 168U;
    target_validation_start();
}
void model_tick(void)
{
    PhaseCurrentObservation current = {0};
    DWT->CYCCNT += model_step_us * 170U;
    model_time += model_step_ticks;
    if (s_pending) {
        s_pending = false;
        s_committed = true;
    }
    s_window = true;
    s_staged = false;
    current.observation_time = model_time;
    current.phase_b_a = model_current_b_a;
    current.phase_c_a = model_current_c_a;
    current.measured_phases = (PhaseMask)model_current_mask;
    current.acquisition_valid = model_current_valid != 0U;
    if (s_handler) {
        s_handler(s_context, &current);
    }
    s_window = false;
}
void model_guard(void)
{
    TIM6->SR |= TIM_SR_UIF;
    target_validation_guard_irq();
}
void model_trip_event(void)
{
    model_trip_active = 1U;
    model_trip_causes = HARDWARE_TRIP_CAUSE_BUS_OVERVOLTAGE;
    TIM1->BDTR &= ~TIM_BDTR_MOE;
    if (s_trip_handler) {
        s_trip_handler(s_trip_context);
    }
}
