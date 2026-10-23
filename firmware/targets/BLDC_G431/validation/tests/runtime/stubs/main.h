#ifndef RUNTIME_MODEL_MAIN_H
#define RUNTIME_MODEL_MAIN_H

#include "stm32g431xx.h"

#define SET_BIT(reg, bit) ((reg) |= (bit))

uint32_t HAL_GetTick(void);

#endif
