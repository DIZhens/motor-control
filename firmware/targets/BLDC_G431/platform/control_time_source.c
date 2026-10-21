#include "control_time_source.h"

#include "stm32g4xx.h"
#include "realtime_motor_io_config.h"

static volatile ControlTime s_control_time;

void control_time_source_reset(void)
{
    uint32_t primask = __get_PRIMASK();

    __disable_irq();
    s_control_time = 0U;

    if (primask == 0U) {
        __enable_irq();
    }
}

ControlTime control_time_source_advance_fast_tick(void)
{
    return control_time_source_advance_slots(1U);
}

ControlTime control_time_source_advance_slots(uint32_t slots)
{
    s_control_time += (ControlTime)slots * REALTIME_MOTOR_IO_FAST_TICK_PERIOD_TICKS;
    return s_control_time;
}

ControlTime control_time_source_now(void)
{
    ControlTime value;
    uint32_t primask = __get_PRIMASK();

    __disable_irq();
    value = s_control_time;

    if (primask == 0U) {
        __enable_irq();
    }

    return value;
}
