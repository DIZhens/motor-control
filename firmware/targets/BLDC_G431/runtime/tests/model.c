/* 用受控 HCC 替身执行正式 runtime / Motor / VOFA；无电气模型或 ST-Link。 */
#include "target_runtime.h"
#include "main.h"
#include "tim.h"
#include "motor.h"
#include "realtime_motor_io.h"
#include "hardware_trip.h"
#include "position_observation.h"
#include "bus_observation.h"

#include <stddef.h>

uint32_t SystemCoreClock = 170000000U;
TIM_HandleTypeDef htim6 = { TIM6 };
uint32_t model_ms;
uint32_t model_facts_ok = 1U;
uint32_t model_facts_rate = 20000U;
uint32_t model_facts_period = 1U;
uint32_t model_bind_ok = 1U;
uint32_t model_trip_bind_ok = 1U;
uint32_t model_position_arm_ok = 1U;
uint32_t model_bus_arm_ok = 1U;
uint32_t model_io_arm_ok = 1U;
uint32_t model_trip_arm_ok = 1U;
uint32_t model_start_ok = 1U;
uint32_t model_trip_available = 1U;
uint32_t model_trip_active;
uint32_t model_trip_causes;
uint32_t model_bus_available = 1U;
uint32_t model_bus_valid = 1U;
uint32_t model_bus_age;
uint32_t model_bus_future;
float model_bus_voltage = 24.0f;
uint32_t model_current_valid = 1U;
uint32_t model_current_mask = PHASE_MASK_B | PHASE_MASK_C;
float model_current_b;
float model_current_c;
uint32_t model_tick_cycles = 8500U;
uint32_t model_in_callback;
uint32_t model_callback_count;
uint32_t model_bind_count;
uint32_t model_arm_count;
uint32_t model_start_count;
uint32_t model_safeoff_count;
uint32_t model_stage_count;
uint32_t model_enable_count;
uint32_t model_rearm_count;
uint32_t model_bus_read_count;
uint32_t model_trip_read_count;
uint32_t model_notify_count;
ControlTime model_control_time;

const uint32_t model_motor_offsets[] = {
    offsetof(Motor, state), offsetof(Motor, faults), offsetof(Motor, command_result),
    offsetof(Motor, command_count), offsetof(Motor, target_velocity_rpm)
};
static PhaseCurrentSampleReadyHandler s_sample_handler;
static void *s_sample_context;
static HardwareTripHandler s_trip_handler;
static void *s_trip_context;

uint32_t HAL_GetTick(void)
{
    return model_ms;
}

bool realtime_motor_io_get_facts(RealtimeMotorIoFacts *facts)
{
    if (model_facts_ok == 0U) {
        return false;
    }
    facts->normal_voltage_utilization = 0.45f;
    facts->control_time_ticks_per_second = model_facts_rate;
    facts->fast_tick_period_ticks = model_facts_period;
    return true;
}

bool realtime_motor_io_bind_sample_ready(PhaseCurrentSampleReadyHandler handler, void *context)
{
    model_bind_count++;
    if (model_bind_ok == 0U) {
        return false;
    }
    s_sample_handler = handler;
    s_sample_context = context;
    return true;
}

bool realtime_motor_io_arm(void)
{
    model_arm_count++;
    return model_io_arm_ok != 0U;
}

bool realtime_motor_io_start_drive_timing(void)
{
    model_start_count++;
    return model_start_ok != 0U;
}

void realtime_motor_io_safe_off(void)
{
    model_safeoff_count++;
    TIM1->BDTR &= ~TIM_BDTR_MOE;
}

bool realtime_motor_io_stage_phase_duty(const PhaseDutyRequest *request)
{
    (void)request;
    model_stage_count++;
    return true;
}

bool realtime_motor_io_power_enable(void)
{
    model_enable_count++;
    TIM1->BDTR |= TIM_BDTR_MOE;
    return true;
}

bool hardware_trip_bind(HardwareTripHandler handler, void *context)
{
    if (model_trip_bind_ok == 0U) {
        return false;
    }
    s_trip_handler = handler;
    s_trip_context = context;
    return true;
}

bool hardware_trip_arm(void)
{
    return model_trip_arm_ok != 0U;
}

bool hardware_trip_get_snapshot(HardwareTripSnapshot *snapshot)
{
    model_trip_read_count++;
    if (model_trip_available == 0U) {
        return false;
    }
    snapshot->condition_active = model_trip_active != 0U;
    snapshot->causes = model_trip_causes;
    return true;
}

bool hardware_trip_rearm(void)
{
    model_rearm_count++;
    return true;
}

bool position_observation_arm(void)
{
    return model_position_arm_ok != 0U;
}

bool bus_observation_arm(void)
{
    return model_bus_arm_ok != 0U;
}

bool bus_observation_get_latest(BusObservation *observation)
{
    model_bus_read_count++;
    if (model_bus_available == 0U) {
        return false;
    }
    observation->voltage_v = model_bus_voltage;
    observation->observation_time = model_control_time - model_bus_age + model_bus_future;
    observation->acquisition_valid = model_bus_valid != 0U;
    return true;
}

void model_start(void)
{
    target_runtime_start();
}

void model_sample(void)
{
    PhaseCurrentObservation current = { 0 };

    DWT->CYCCNT += model_tick_cycles;
    model_control_time++;
    current.phase_b_a = model_current_b;
    current.phase_c_a = model_current_c;
    current.measured_phases = (PhaseMask)model_current_mask;
    current.observation_time = model_control_time;
    current.acquisition_valid = model_current_valid != 0U;
    if (s_sample_handler != NULL) {
        model_in_callback = 1U;
        model_callback_count++;
        s_sample_handler(s_sample_context, &current);
        model_in_callback = 0U;
    }
}

void model_notify(void)
{
    if (s_trip_handler != NULL) {
        model_notify_count++;
        s_trip_handler(s_trip_context);
    }
}

void model_service(void)
{
    target_runtime_service();
}

void model_guard(void)
{
    target_runtime_guard_irq();
}
