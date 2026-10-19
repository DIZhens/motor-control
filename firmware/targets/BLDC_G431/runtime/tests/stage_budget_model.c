/* 正式 ARM 同源链路的受控 HCC 模型；合成 cycle 只校验行为，不代表性能。 */
#include <stddef.h>
#include <math.h>

#include "stage_budget.h"
#include "control_runtime.h"
#include "realtime_motor_io.h"
#include "position_observation.h"
#include "bus_observation.h"
#include "stm32g4xx.h"

uint32_t model_clock_step = 20U;
uint32_t model_stage_cost = 600U;
uint32_t model_stage_ok = 1U;
uint32_t model_stamp_fail_read;
uint32_t model_bad_anchor_read;
uint32_t model_moe_read;
uint32_t model_force_enable;
uint32_t model_power_moe;
uint32_t model_clock_reads;
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
uint32_t model_time_step = 1U;
uint32_t model_permitted = 1U;
uint32_t model_cycles = 0xFFFFF000U;
uint32_t model_anchor;
uint32_t model_entry_count = 4100U;
uint32_t model_returned;
PhaseDutyRequest model_last_duty;

const uint32_t model_phase_offset = offsetof(ControlRuntime, phase);
const uint32_t model_result_size = sizeof(G431StageBudgetResult);

bool g431_stage_budget_enable(void);

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
    facts->fast_tick_period_ticks = 1U;
    return true;
}

bool realtime_motor_io_stage_phase_duty(const PhaseDutyRequest *duty)
{
    model_stage_count++;
    model_last_duty = *duty;
    model_cycles += model_stage_cost;
    if (model_force_enable != 0U) {
        (void)g431_stage_budget_enable();
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

static bool stamp_read(void *context, G431StageBudgetStamp *stamp)
{
    uint32_t elapsed;
    (void)context;
    model_clock_reads++;
    model_cycles += model_clock_step;
    elapsed = model_cycles - model_anchor;
    stamp->cycles = model_cycles;
    stamp->timer_count = model_entry_count - elapsed;
    stamp->timer_cr1 = TIM_CR1_CEN | TIM_CR1_DIR | TIM_CR1_CMS_0;
    stamp->timer_bdtr = model_power_moe;
    stamp->callback_start_cycles = model_anchor;
    stamp->callback_entry_count = model_entry_count;
    if (model_clock_reads == model_moe_read) {
        stamp->timer_bdtr |= TIM_BDTR_MOE;
    }
    if (model_clock_reads == model_bad_anchor_read) {
        stamp->callback_start_cycles++;
    }
    return model_clock_reads != model_stamp_fail_read;
}

void model_init(void)
{
    g431_stage_budget_init(NULL, NULL, stamp_read, NULL);
}

void model_prepare(void)
{
    model_returned = g431_stage_budget_prepare(1U, model_permitted != 0U) ? 1U : 0U;
}

void model_prepare_second(void)
{
    model_returned = g431_stage_budget_prepare(2U, model_permitted != 0U) ? 1U : 0U;
}

void model_step(void)
{
    model_anchor = model_cycles;
    model_cycles += 100U;
    model_clock_reads = 0U;
    model_returned = g431_stage_budget_step(model_control_time,
        model_permitted != 0U) ? 1U : 0U;
    model_control_time += model_time_step;
    model_cycles = model_anchor + 8500U;
}

void model_cancel(void)
{
    g431_stage_budget_cancel(G431_STAGE_BUDGET_FAILURE_ABORTED);
}

void model_busy(void)
{
    g431_stage_budget_cancel(G431_STAGE_BUDGET_FAILURE_BUSY);
}
