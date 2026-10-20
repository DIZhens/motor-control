#include "timing_entry.h"

#include <float.h>
#include <stddef.h>
#include <string.h>

#include "main.h"
#include "tim.h"
#include "bus_observation.h"
#include "position_observation.h"
#include "hardware_trip.h"
#include "realtime_motor_io.h"
#include "cordic_math.h"
#include "runtime_timing.h"
#include "../platform/realtime_motor_io_diagnostic.h"
#include "../platform/realtime_motor_io_internal.h"

#define CYCLES_PER_US (170U)
#define SAMPLE_MIN_CYCLES (75U * CYCLES_PER_US)
#define SAMPLE_MAX_CYCLES (125U * CYCLES_PER_US)
#define OBSERVATION_MAX_AGE (100U)

volatile uint32_t g_target_runtime_timing_request;
volatile uint32_t g_target_runtime_timing_guard_faults;
volatile uint32_t g_target_runtime_timing_health_sequence;
volatile G431TimingHealth g_target_runtime_timing_health;

static G431CordicMath s_cordic;
static G431TimingHealth s_health;
static uint32_t s_request_seen;
static volatile uint32_t s_last_sample_cycles;
static uint32_t s_last_guard_cycles;
static bool s_attempted;
static bool s_has_sample;
static bool s_cordic_ready;
static bool s_permitted;
static uint32_t s_publication_divider;
static volatile bool s_trip_notified;

static bool finite_float(float value)
{
    return value >= -FLT_MAX && value <= FLT_MAX;
}

static void restore_interrupts(uint32_t primask)
{
    if (primask == 0U) {
        __enable_irq();
    }
}

static bool clock_valid(void)
{
    return SystemCoreClock == 170000000U
        && (CoreDebug->DEMCR & CoreDebug_DEMCR_TRCENA_Msk) != 0U
        && (DWT->CTRL & DWT_CTRL_CYCCNTENA_Msk) != 0U;
}

static bool read_stamp(void *context, G431RuntimeTimingStamp *stamp)
{
    (void)context;
    if (stamp == NULL || !clock_valid()) {
        return false;
    }
    realtime_motor_io_stage_anchor(&stamp->callback_start_cycles, &stamp->callback_entry_count);
    realtime_motor_io_stage_deadline(&stamp->stage_deadline_cycles);
    stamp->timer_cr1 = TIM1->CR1;
    stamp->timer_count = TIM1->CNT;
    stamp->timer_bdtr = TIM1->BDTR;
    stamp->cycles = DWT->CYCCNT;
    return true;
}

static void publish_health(void)
{
    g_target_runtime_timing_health_sequence++;
    __DMB();
    g_target_runtime_timing_health = s_health;
    __DMB();
    g_target_runtime_timing_health_sequence++;
}

bool timing_entry_permitted(void)
{
    HardwareTripSnapshot trip;

    return s_permitted && g_target_runtime_timing_guard_faults == 0U
        && !s_trip_notified && (TIM1->BDTR & TIM_BDTR_MOE) == 0U && clock_valid()
        && hardware_trip_get_snapshot(&trip) && !trip.condition_active
        && trip.causes == HARDWARE_TRIP_CAUSE_NONE;
}

static void sample_ready(void *context, const PhaseCurrentObservation *current)
{
    HardwareTripSnapshot trip;
    BusObservation bus;
    PositionObservation position;
    uint32_t now_cycles = DWT->CYCCNT;
    uint32_t interval = now_cycles - s_last_sample_cycles;
    uint32_t request;
    bool prepared = false;
    bool bus_valid;
    bool position_valid;
    uint32_t old_faults = s_health.faults;
    uint32_t old_permitted = s_health.permitted;
    uint32_t old_guard = s_health.guard_trips;

    (void)context;
    if (s_has_sample && (interval < SAMPLE_MIN_CYCLES || interval > SAMPLE_MAX_CYCLES)) {
        s_health.faults |= G431_TIMING_FAULT_CADENCE;
    }
    s_last_sample_cycles = now_cycles;
    s_has_sample = true;
    s_health.sample_count++;
    s_health.time_low = (uint32_t)current->observation_time;
    s_health.time_high = (uint32_t)(current->observation_time >> 32);
    s_health.last_sample_cycles = now_cycles;
    s_health.last_sample_interval_cycles = interval;
    s_health.current_valid = current->acquisition_valid
        && current->measured_phases == (PHASE_MASK_B | PHASE_MASK_C)
        && finite_float(current->phase_b_a) && finite_float(current->phase_c_a);
    if (s_health.current_valid == 0U) {
        s_health.faults |= G431_TIMING_FAULT_CURRENT;
    }
    if (!clock_valid()) {
        s_health.faults |= G431_TIMING_FAULT_CLOCK;
    }
    if (s_trip_notified || !hardware_trip_get_snapshot(&trip) || trip.condition_active
        || trip.causes != HARDWARE_TRIP_CAUSE_NONE) {
        s_health.faults |= G431_TIMING_FAULT_TRIP;
    }
    s_health.power_disabled = (TIM1->BDTR & TIM_BDTR_MOE) == 0U;
    if (s_health.power_disabled == 0U) {
        s_health.faults |= G431_TIMING_FAULT_UNEXPECTED_POWER;
    }
    bus_valid = bus_observation_get_latest(&bus) && bus.acquisition_valid
        && finite_float(bus.voltage_v) && current->observation_time >= bus.observation_time
        && current->observation_time - bus.observation_time <= OBSERVATION_MAX_AGE;
    position_valid = position_observation_get_latest(&position) && position.acquisition_valid
        && finite_float(position.mechanical_position_turns)
        && position.referenced && position.mechanical_position_turns >= 0.0f
        && position.mechanical_position_turns < 1.0f
        && current->observation_time >= position.observation_time
        && current->observation_time - position.observation_time <= OBSERVATION_MAX_AGE;
    s_health.bus_valid = bus_valid;
    s_health.bus_voltage_v = bus_valid ? bus.voltage_v : 0.0f;
    s_health.position_valid = position_valid;
    s_health.guard_trips = g_target_runtime_timing_guard_faults;
    s_permitted = s_cordic_ready && s_health.drive_started != 0U
        && s_health.faults == 0U && s_health.guard_trips == 0U
        && s_health.power_disabled != 0U && bus_valid && position_valid
        && bus.voltage_v >= 0.0f && bus.voltage_v <= 3.6f;
    if (s_health.faults != 0U || s_health.guard_trips != 0U) {
        realtime_motor_io_safe_off();
    }

    request = g_target_runtime_timing_request;
    if (request != 0U && request != s_request_seen) {
        s_request_seen = request;
        if (g_target_runtime_timing_active != 0U) {
            g431_runtime_timing_cancel(G431_RUNTIME_TIMING_FAILURE_BUSY);
        } else {
            (void)g431_runtime_timing_prepare(request, timing_entry_permitted());
        }
        prepared = true;
    }
    if (!prepared && g_target_runtime_timing_active != 0U) {
        (void)g431_runtime_timing_step(current->observation_time, timing_entry_permitted());
    }
    /* 快照、Platform SSI/母线尾部及HAL返回均处于IRQ首末标记之间。
     * 这里不再执行Observe的OFF Runtime，也不发布伪造的业务状态。 */
    s_health.permitted = timing_entry_permitted();
    s_publication_divider++;
    if (s_publication_divider >= 100U || old_faults != s_health.faults
        || old_guard != s_health.guard_trips || old_permitted != s_health.permitted) {
        s_publication_divider = 0U;
        publish_health();
    }
}

static void trip_notify(void *context)
{
    (void)context;
    /* 单独置位提示，避免与ADC的fault位RMW互相覆盖；本拍末端立即撤销许可。
     * Platform已经关断；真实原因在下一个ADC边界重新读取并锁存。 */
    s_trip_notified = true;
}

void timing_entry_guard_irq(void)
{
    uint32_t primask;
    uint32_t last_sample;
    uint32_t now;

    if ((TIM6->SR & TIM_SR_UIF) == 0U || (TIM6->DIER & TIM_DIER_UIE) == 0U) {
        return;
    }
    __HAL_TIM_CLEAR_FLAG(&htim6, TIM_FLAG_UPDATE);
    primask = __get_PRIMASK();
    __disable_irq();
    last_sample = s_last_sample_cycles;
    now = DWT->CYCCNT;
    restore_interrupts(primask);
    if (now == s_last_guard_cycles) {
        g_target_runtime_timing_guard_faults |= G431_TIMING_FAULT_CLOCK;
    }
    if (now - last_sample > SAMPLE_MAX_CYCLES) {
        g_target_runtime_timing_guard_faults |= G431_TIMING_FAULT_CADENCE;
    }
    s_last_guard_cycles = now;
    if (g_target_runtime_timing_guard_faults != 0U) {
        realtime_motor_io_safe_off();
    }
}

void timing_entry_start(void)
{
    RealtimeMotorIoFacts facts;
    HardwareTripSnapshot trip;
    uint32_t primask;
    bool success;

    if (s_attempted) {
        return;
    }
    s_attempted = true;
    memset(&s_health, 0, sizeof(s_health));
    s_health.schema_version = 2U;
    s_health.power_disabled = 1U;
    realtime_motor_io_safe_off();
    s_cordic_ready = g431_cordic_math_init(&s_cordic);
    g431_runtime_timing_init(g431_cordic_sincos, &s_cordic, read_stamp, NULL);
    if (!s_cordic_ready || SystemCoreClock != 170000000U || TIM6->PSC != 169U
        || !realtime_motor_io_get_facts(&facts)
        || facts.control_time_ticks_per_second != 20000U || facts.fast_tick_period_ticks != 2U
        || !realtime_motor_io_bind_sample_ready(sample_ready, NULL)
        || !hardware_trip_bind(trip_notify, NULL)
        || !position_observation_arm() || !bus_observation_arm()
        || !realtime_motor_io_arm() || !hardware_trip_arm()
        || !hardware_trip_get_snapshot(&trip) || trip.condition_active
        || trip.causes != HARDWARE_TRIP_CAUSE_NONE) {
        realtime_motor_io_safe_off();
        s_health.faults |= G431_TIMING_FAULT_STARTUP;
        publish_health();
        return;
    }
    primask = __get_PRIMASK();
    __disable_irq();
    success = realtime_motor_io_start_drive_timing();
    s_health.drive_started = success;
    if (!success) {
        realtime_motor_io_safe_off();
        s_health.faults |= G431_TIMING_FAULT_STARTUP;
    } else {
        s_last_sample_cycles = DWT->CYCCNT;
        s_last_guard_cycles = s_last_sample_cycles;
        TIM6->ARR = 49U;
        TIM6->CNT = 0U;
        TIM6->EGR = TIM_EGR_UG;
        __HAL_TIM_CLEAR_FLAG(&htim6, TIM_FLAG_UPDATE);
        SET_BIT(TIM6->DIER, TIM_DIER_UIE);
        SET_BIT(TIM6->CR1, TIM_CR1_CEN);
    }
    publish_health();
    restore_interrupts(primask);
}
