#ifndef ODRIVE_V36_405_M0_CONTROL_TIME_SOURCE_H
#define ODRIVE_V36_405_M0_CONTROL_TIME_SOURCE_H

#include <stdbool.h>
#include "control_time.h"

/* TIM2 自由运行，1 tick = 1 us；不依赖 PWM 输出门控或 HAL Tick。 */
bool control_time_source_start(void);
ControlTime control_time_source_now(void);
void control_time_source_overflow_irq(void);

#endif
