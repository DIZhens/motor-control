/* 复用私有fixture的HCC替身，再绑定真实Timing入口；不模拟电气或性能。 */
#define position_observation_get_latest fixture_position_get_latest
#define bus_observation_get_latest fixture_bus_get_latest
#define realtime_motor_io_get_facts fixture_get_facts
#define realtime_motor_io_safe_off fixture_safe_off
#include "runtime_timing_model.c"
#undef position_observation_get_latest
#undef bus_observation_get_latest
#undef realtime_motor_io_get_facts
#undef realtime_motor_io_safe_off
#undef SET_BIT /* 既有HAL替身以自己的宏形参重新声明。 */

#include "timing_entry.h"
#include "main.h"
#include "tim.h"
#include "hardware_trip.h"
#include "cordic_math.h"

uint32_t SystemCoreClock = 170000000U;
TIM_HandleTypeDef htim6 = {TIM6};
uint32_t entry_facts_ok = 1U;
uint32_t entry_rate = 20000U;
uint32_t entry_period = 2U;
uint32_t entry_bind_ok = 1U;
uint32_t entry_arm_ok = 1U;
uint32_t entry_start_ok = 1U;
uint32_t entry_cordic_ok = 1U;
uint32_t entry_trip_ok = 1U;
uint32_t entry_trip_active;
uint32_t entry_trip_causes;
uint32_t entry_trip_notify_only;
uint32_t entry_trip_on_cordic_call;
uint32_t entry_position_referenced = 1U;
uint32_t entry_position_age;
uint32_t entry_position_future;
float entry_position_turns = 0.75f;
uint32_t entry_bus_age;
uint32_t entry_bus_future;
float entry_bus_voltage = 2.64f;
uint32_t entry_current_valid = 1U;
uint32_t entry_current_mask = PHASE_MASK_B | PHASE_MASK_C;
float entry_current_b;
float entry_current_c;
uint32_t entry_tick_cycles = 17000U;
uint32_t entry_sample_count;
uint32_t entry_bind_count;
uint32_t entry_arm_count;
uint32_t entry_start_count;
uint32_t entry_trip_rearm_count;
uint32_t entry_trip_read_count;
uint32_t entry_cordic_calls;
uint32_t entry_permitted;
static PhaseCurrentSampleReadyHandler entry_handler;
static void *entry_handler_context;
static HardwareTripHandler entry_trip_handler;
static void *entry_trip_context;

bool realtime_motor_io_get_facts(RealtimeMotorIoFacts *facts)
{
    (void)fixture_get_facts(facts);
    facts->control_time_ticks_per_second = entry_rate;
    facts->fast_tick_period_ticks = entry_period;
    return entry_facts_ok != 0U;
}

void realtime_motor_io_safe_off(void)
{
    fixture_safe_off();
    TIM1->BDTR &= ~TIM_BDTR_MOE;
}

bool position_observation_get_latest(PositionObservation *observation)
{
    bool valid = fixture_position_get_latest(observation);
    observation->mechanical_position_turns = entry_position_turns;
    observation->referenced = entry_position_referenced != 0U;
    observation->observation_time = entry_position_future != 0U
        ? model_control_time + entry_position_future : model_control_time - entry_position_age;
    return valid;
}

bool bus_observation_get_latest(BusObservation *observation)
{
    bool valid = fixture_bus_get_latest(observation);
    observation->voltage_v = entry_bus_voltage;
    observation->observation_time = entry_bus_future != 0U
        ? model_control_time + entry_bus_future : model_control_time - entry_bus_age;
    return valid;
}

bool realtime_motor_io_bind_sample_ready(PhaseCurrentSampleReadyHandler handler, void *context)
{
    entry_bind_count++;
    entry_handler = handler;
    entry_handler_context = context;
    return entry_bind_ok != 0U;
}

bool hardware_trip_bind(HardwareTripHandler handler, void *context)
{
    entry_trip_handler = handler;
    entry_trip_context = context;
    return entry_bind_ok != 0U;
}

bool position_observation_arm(void) { entry_arm_count++; return entry_arm_ok != 0U; }
bool bus_observation_arm(void) { entry_arm_count++; return entry_arm_ok != 0U; }
bool realtime_motor_io_arm(void) { entry_arm_count++; return entry_arm_ok != 0U; }
bool hardware_trip_arm(void) { entry_arm_count++; return entry_arm_ok != 0U; }

bool realtime_motor_io_start_drive_timing(void)
{
    entry_start_count++;
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
    TIM1->CR1 = TIM_CR1_CEN | TIM_CR1_DIR | TIM_CR1_CMS_0;
    TIM1->CNT = model_entry_count;
    return entry_start_ok != 0U;
}

bool hardware_trip_get_snapshot(HardwareTripSnapshot *snapshot)
{
    entry_trip_read_count++;
    snapshot->condition_active = entry_trip_active != 0U;
    snapshot->causes = entry_trip_causes;
    return entry_trip_ok != 0U;
}

bool hardware_trip_rearm(void)
{
    entry_trip_rearm_count++;
    return false;
}

bool g431_cordic_math_init(G431CordicMath *context)
{
    context->initialized = entry_cordic_ok != 0U;
    return context->initialized;
}

bool g431_cordic_sincos(void *context, float turns, float *sine, float *cosine)
{
    G431CordicMath *math = context;
    entry_cordic_calls++;
    if (entry_cordic_calls == entry_trip_on_cordic_call) {
        if (entry_trip_notify_only == 0U) {
            entry_trip_causes = HARDWARE_TRIP_CAUSE_OVERCURRENT;
        }
        if (entry_trip_handler != NULL) {
            entry_trip_handler(entry_trip_context);
        }
    }
    return math->initialized && electrical_angle_sincos_software(NULL, turns, sine, cosine);
}

void realtime_motor_io_stage_anchor(uint32_t *cycles, uint32_t *count)
{
    *cycles = model_anchor;
    *count = model_entry_count;
}

void realtime_motor_io_stage_deadline(uint32_t *deadline)
{
    *deadline = model_anchor + model_entry_count + 8500U - 1700U;
}

void entry_model_start(void)
{
    timing_entry_start();
}

void entry_model_tick(void)
{
    PhaseCurrentObservation current;

    g431_runtime_timing_irq_enter(DWT->CYCCNT);
    model_cycles += 80U;
    model_anchor = DWT->CYCCNT;
    model_cycles += 100U;
    TIM1->CR1 = TIM_CR1_CEN | TIM_CR1_DIR | TIM_CR1_CMS_0;
    TIM1->CNT = model_entry_count - 100U;
    current.phase_a_a = 0.0f;
    current.phase_b_a = entry_current_b;
    current.phase_c_a = entry_current_c;
    current.measured_phases = (PhaseMask)entry_current_mask;
    current.acquisition_valid = entry_current_valid != 0U;
    current.observation_time = model_control_time;
    entry_sample_count++;
    if (entry_handler != NULL) {
        entry_handler(entry_handler_context, &current);
    }
    model_cycles += model_tail_cost;
    g431_runtime_timing_irq_exit(DWT->CYCCNT, timing_entry_permitted());
    model_control_time += model_time_step;
    /* 下拍IRQ首/anchor/consumer各读clock共120个模型cycle，保持实际入口间隔。 */
    model_cycles = model_anchor + entry_tick_cycles - 120U;
}

void entry_model_guard(void)
{
    TIM6->SR = TIM_SR_UIF;
    timing_entry_guard_irq();
}

void entry_model_get_permission(void)
{
    entry_permitted = timing_entry_permitted() ? 1U : 0U;
}
