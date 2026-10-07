#include "control_time_source.h"

#include "stm32g4xx.h"

static volatile ControlTime s_control_time;

void control_time_source_reset(void)
{
    uint32_t primask = __get_PRIMASK();

    __disable_irq();
    s_control_time = 0U;

    if (primask == 0U)
    {
        __enable_irq();
    }
}

ControlTime control_time_source_advance_fast_tick(void)
{
    s_control_time++;
    return s_control_time;
}

ControlTime control_time_source_now(void)
{
    ControlTime value;
    uint32_t primask = __get_PRIMASK();

    __disable_irq();
    value = s_control_time;

    if (primask == 0U)
    {
        __enable_irq();
    }

    return value;
}
