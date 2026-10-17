#ifndef ODRIVE_V36_405_M0_POSITION_OBSERVATION_INTERNAL_H
#define ODRIVE_V36_405_M0_POSITION_OBSERVATION_INTERNAL_H

#include "control_time.h"

void position_observation_fast_tick(ControlTime observation_time);
void position_observation_spi_irq(void);

#endif
