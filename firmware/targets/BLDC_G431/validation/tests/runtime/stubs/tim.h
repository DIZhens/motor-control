#ifndef RUNTIME_MODEL_TIM_H
#define RUNTIME_MODEL_TIM_H

#include "main.h"

/* 仅替代 HAL handle；寄存器类型、地址和位定义仍来自正式 MCU 头文件。 */
typedef struct {
    TIM_TypeDef *Instance;
} TIM_HandleTypeDef;

extern TIM_HandleTypeDef htim6;

#define TIM_FLAG_UPDATE TIM_SR_UIF
#define __HAL_TIM_CLEAR_FLAG(handle, flag) ((handle)->Instance->SR = ~(flag))

#endif
