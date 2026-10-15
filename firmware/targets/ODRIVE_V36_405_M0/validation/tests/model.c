/* ARM CPU 模型替身：只验证台架命令行为，不模拟真实 ADC/TIM/SPI。 */
#include "target_validation.h"
#include "control_time_source.h"
#include "tim.h"
#include <stddef.h>

uint64_t model_time;
uint32_t model_current_valid = 1;
uint32_t model_current_mask = PHASE_MASK_B | PHASE_MASK_C;
uint32_t model_bus_valid = 1;
uint32_t model_bus_age_us;
float model_bus_voltage = 12.0f;
uint32_t model_stage_ok = 1;
uint32_t model_enable_ok = 1;
uint32_t model_enable_calls;
uint32_t model_off_calls;
uint32_t model_stage_calls;
uint32_t model_common_duty_failures;
uint32_t model_facts_ok = 1;
uint32_t model_bind_ok = 1;
uint32_t model_arm_ok = 1;
uint32_t model_drive_start_ok = 1;
float model_current_b_a;
float model_current_c_a;
uint32_t model_tick_step_us = 50;
uint32_t model_enable_diagnostic_calls;

static PhaseCurrentSampleReadyHandler model_handler;
static void *model_context;

const uint32_t model_offsets[] = {
    offsetof(TargetValidationSnapshot, sequence),
    offsetof(TargetValidationSnapshot, started),
    offsetof(TargetValidationSnapshot, state),
    offsetof(TargetValidationSnapshot, result),
    offsetof(TargetValidationSnapshot, command_sequence),
    offsetof(TargetValidationSnapshot, fast_tick_count),
    offsetof(TargetValidationSnapshot, invalid_current_count),
    offsetof(TargetValidationSnapshot, current_interval_min_us),
    offsetof(TargetValidationSnapshot, current_interval_max_us),
    offsetof(TargetValidationSnapshot, stage_attempts),
    offsetof(TargetValidationSnapshot, stage_successes),
    offsetof(TargetValidationSnapshot, enable_attempts),
    offsetof(TargetValidationSnapshot, enable_successes),
    offsetof(TargetValidationSnapshot, bus_count),
    sizeof(TargetValidationMailbox),
    offsetof(TargetValidationSnapshot, current_statistics),
    offsetof(TargetValidationCurrentStatistics, request_sequence),
    offsetof(TargetValidationCurrentStatistics, sample_count),
    offsetof(TargetValidationCurrentStatistics, first_sample_time),
    offsetof(TargetValidationCurrentStatistics, last_sample_time),
    offsetof(TargetValidationCurrentStatistics, phase_b),
    offsetof(TargetValidationCurrentStatistics, phase_c)
};

ControlTime control_time_source_now(void)
{
    return model_time;
}
bool realtime_motor_io_get_facts(RealtimeMotorIoFacts *facts)
{
    facts->control_time_ticks_per_second = 1000000;
    facts->fast_tick_period_ticks = 50;
    facts->normal_voltage_utilization = 0.45f;
    return model_facts_ok != 0;
}
bool realtime_motor_io_bind_sample_ready(PhaseCurrentSampleReadyHandler fn, void *ctx)
{
    model_handler = fn;
    model_context = ctx;
    return model_bind_ok != 0;
}
bool realtime_motor_io_arm(void)
{
    realtime_motor_io_safe_off();
    return model_arm_ok != 0;
}
bool realtime_motor_io_start_drive_timing(void)
{
    if (!model_drive_start_ok) {
        return false;
    }
    TIM1->CR1 = TIM_CR1_CEN;
    return true;
}
bool position_observation_arm(void)
{
    return true;
}
bool bus_observation_arm(void)
{
    return true;
}
bool position_observation_get_latest(PositionObservation *out)
{
    out->mechanical_position_turns = 0.25f;
    out->observation_time = model_time;
    out->referenced = true;
    out->acquisition_valid = true;
    return true;
}
bool bus_observation_get_latest(BusObservation *out)
{
    out->voltage_v = model_bus_voltage;
    out->observation_time = model_time - model_bus_age_us;
    out->acquisition_valid = model_bus_valid != 0;
    return true;
}
bool realtime_motor_io_stage_phase_duty(const PhaseDutyRequest *duty)
{
    model_stage_calls++;
    if ((duty->phase_a_duty != duty->phase_b_duty)
        || (duty->phase_a_duty != duty->phase_c_duty)) {
        model_common_duty_failures++;
    }
    return model_stage_ok != 0;
}
void realtime_motor_io_capture_enable_diagnostics(void)
{
    model_enable_diagnostic_calls++;
}
bool realtime_motor_io_power_enable(void)
{
    model_enable_calls++;
    if (model_enable_ok) {
        TIM1->BDTR |= TIM_BDTR_MOE;
        return true;
    }
    return false;
}
void realtime_motor_io_safe_off(void)
{
    model_off_calls++;
    TIM1->BDTR &= ~TIM_BDTR_MOE;
}
void model_init(void)
{
    (void)target_validation_start();
}
void model_tick(void)
{
    PhaseCurrentObservation current = {0};
    model_time += model_tick_step_us;
    current.phase_b_a = model_current_b_a;
    current.phase_c_a = model_current_c_a;
    current.observation_time = model_time;
    current.acquisition_valid = model_current_valid != 0;
    current.measured_phases = (PhaseMask)model_current_mask;
    model_handler(model_context, &current);
}
void model_update(void)
{
    target_validation_update_irq();
}
