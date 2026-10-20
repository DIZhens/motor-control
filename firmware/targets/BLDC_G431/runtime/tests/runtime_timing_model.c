/* 同源ARM控制链的端口替身；合成cycle只验证计时布局，不代表实机性能。 */
#include <stddef.h>
#include <math.h>

#include "runtime_timing.h"
#include "control_runtime.h"
#include "realtime_motor_io.h"
#include "position_observation.h"
#include "bus_observation.h"
#include "stm32g4xx.h"

volatile uint32_t model_cycles = 0xFFFFF000U;
uint32_t model_clock_step = 20U;
uint32_t model_stage_cost = 600U;
uint32_t model_compute_cost = 5000U;
uint32_t model_tail_cost = 400U;
uint32_t model_stamp_extra_cost;
uint32_t model_stage_ok = 1U;
uint32_t model_stamp_fail_read;
uint32_t model_stamp_fail_early;
uint32_t model_bad_anchor_read;
uint32_t model_bad_deadline_read;
uint32_t model_moe_read;
uint32_t model_force_enable;
uint32_t model_sink_override;
uint32_t model_sink_component;
uint32_t model_sink_bits;
uint32_t model_power_moe;
uint32_t model_stamp_reads;
uint32_t model_stage_count;
uint32_t model_real_enable_count;
uint32_t model_safe_off_count;
uint32_t model_physical_position_reads;
uint32_t model_physical_bus_reads;
uint32_t model_facts_reads;
uint32_t model_position_available = 1U;
uint32_t model_bus_available = 1U;
uint32_t model_observation_valid = 1U;
uint32_t model_control_time = 1000U;
uint32_t model_time_step = 2U;
uint32_t model_permitted = 1U;
uint32_t model_request = 6U;
uint32_t model_anchor;
uint32_t model_entry_count = 4100U;
uint32_t model_returned;
PhaseDutyRequest model_last_duty;

const uint32_t model_result_size = sizeof(G431RuntimeTimingResult);
const uint32_t model_current_dt_offset = offsetof(CurrentControllerInput, interval_s);
const uint32_t model_speed_dt_offset = offsetof(SpeedControllerInput, interval_s);
const uint32_t model_actuation_prediction_offset = offsetof(ControlRuntime, config.control.actuation_prediction_time_s);
const uint32_t model_runtime_phase_offset = offsetof(ControlRuntime, phase);
const uint32_t model_motor_state_offset = offsetof(Motor, state);

bool g431_runtime_timing_enable(void);

bool position_observation_get_latest(PositionObservation *observation)
{
    model_physical_position_reads++;
    observation->mechanical_position_turns = 0.75f;
    observation->observation_time = model_control_time;
    observation->referenced = true;
    observation->acquisition_valid = model_observation_valid != 0U;
    return model_position_available != 0U;
}

bool bus_observation_get_latest(BusObservation *observation)
{
    model_physical_bus_reads++;
    observation->voltage_v = 2.64f;
    observation->observation_time = model_control_time;
    observation->acquisition_valid = model_observation_valid != 0U;
    return model_bus_available != 0U;
}

bool realtime_motor_io_get_facts(RealtimeMotorIoFacts *facts)
{
    model_facts_reads++;
    facts->normal_voltage_utilization = 0.45f;
    facts->control_time_ticks_per_second = 20000U;
    facts->fast_tick_period_ticks = 2U;
    return true;
}

bool realtime_motor_io_stage_phase_duty(const PhaseDutyRequest *duty)
{
    model_stage_count++;
    model_last_duty = *duty;
    model_cycles += model_stage_cost;
    if (model_force_enable != 0U) {
        (void)g431_runtime_timing_enable();
    }
    return model_stage_ok != 0U && model_power_moe == 0U
        && isfinite(duty->phase_a_duty) && isfinite(duty->phase_b_duty)
        && isfinite(duty->phase_c_duty)
        && duty->phase_a_duty >= 0.10f && duty->phase_a_duty <= 0.90f
        && duty->phase_b_duty >= 0.10f && duty->phase_b_duty <= 0.90f
        && duty->phase_c_duty >= 0.10f && duty->phase_c_duty <= 0.90f;
}

bool realtime_motor_io_power_enable(void)
{
    model_real_enable_count++;
    model_power_moe = TIM_BDTR_MOE;
    return true;
}

void realtime_motor_io_safe_off(void)
{
    model_safe_off_count++;
    model_power_moe = 0U;
}

static bool timing_model_stamp(void *context, G431RuntimeTimingStamp *stamp)
{
    uint32_t elapsed;
    uint32_t remainder;
    (void)context;
    model_stamp_reads++;
    if (model_stamp_reads == model_stamp_fail_early) {
        return false;
    }
    stamp->cycles = DWT->CYCCNT;
    elapsed = stamp->cycles - model_anchor;
    stamp->timer_cr1 = TIM_CR1_CEN | TIM_CR1_CMS_0;
    if (elapsed <= model_entry_count) {
        stamp->timer_count = model_entry_count - elapsed;
        stamp->timer_cr1 |= TIM_CR1_DIR;
    } else {
        remainder = (elapsed - model_entry_count) % 8500U;
        stamp->timer_count = remainder <= 4250U ? remainder : 8500U - remainder;
        if (remainder > 4250U) {
            stamp->timer_cr1 |= TIM_CR1_DIR;
        }
    }
    stamp->timer_bdtr = model_power_moe;
    stamp->callback_start_cycles = model_anchor;
    stamp->callback_entry_count = model_entry_count;
    stamp->stage_deadline_cycles = model_anchor + model_entry_count + 8500U - 1700U;
    if (model_stamp_reads == model_moe_read) {
        stamp->timer_bdtr |= TIM_BDTR_MOE;
    }
    if (model_stamp_reads == model_bad_anchor_read) {
        stamp->callback_start_cycles++;
    }
    if (model_stamp_reads == model_bad_deadline_read) {
        stamp->stage_deadline_cycles++;
    }
    model_cycles += model_stamp_extra_cost;
    return model_stamp_reads != model_stamp_fail_read;
}

void model_init(void)
{
    g431_runtime_timing_init(NULL, NULL, timing_model_stamp, NULL);
}

void model_prepare_only(void)
{
    model_returned = g431_runtime_timing_prepare(model_request, model_permitted != 0U) ? 1U : 0U;
}

void model_prepare(void)
{
    model_prepare_only();
    g431_runtime_timing_irq_exit(DWT->CYCCNT, model_permitted != 0U);
}

void model_begin_work(void)
{
    g431_runtime_timing_irq_enter(DWT->CYCCNT);
    model_cycles += 80U;
    model_anchor = DWT->CYCCNT;
    model_cycles += 100U;
    model_stamp_reads = 0U;
    model_returned = g431_runtime_timing_step(model_control_time, model_permitted != 0U) ? 1U : 0U;
    model_control_time += model_time_step;
}

void model_finish_irq(void)
{
    model_cycles += model_tail_cost;
    g431_runtime_timing_irq_exit(DWT->CYCCNT, model_permitted != 0U);
    model_cycles = model_anchor + 17000U;
}

void model_step(void)
{
    model_begin_work();
    model_finish_irq();
}

void model_regular_irq(void)
{
    g431_runtime_timing_irq_enter(DWT->CYCCNT);
    model_cycles += 300U;
    g431_runtime_timing_irq_exit(DWT->CYCCNT, model_permitted != 0U);
}

void model_cancel(void)
{
    g431_runtime_timing_cancel(G431_RUNTIME_TIMING_FAILURE_ABORTED);
}

void model_busy(void)
{
    g431_runtime_timing_cancel(G431_RUNTIME_TIMING_FAILURE_BUSY);
}

void model_without_irq(void)
{
    model_returned = g431_runtime_timing_step(model_control_time, model_permitted != 0U) ? 1U : 0U;
}
