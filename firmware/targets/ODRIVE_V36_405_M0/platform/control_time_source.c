#include "control_time_source.h"

#include "tim.h"

static volatile ControlTime s_wrap_base;
static bool s_started;

bool control_time_source_start(void)
{
    if (s_started || (htim2.Init.Prescaler != 83U)
        || (htim2.Init.Period != 0xFFFFFFFFU)) {
        return false;
    }

    s_wrap_base = 0U;
    TIM2->CNT = 0U;
    __HAL_TIM_CLEAR_FLAG(&htim2, TIM_FLAG_UPDATE);
    if (HAL_TIM_Base_Start_IT(&htim2) != HAL_OK) {
        return false;
    }
    s_started = true;
    return true;
}

ControlTime control_time_source_now(void)
{
    uint32_t primask = __get_PRIMASK();
    uint32_t counter;
    ControlTime base;

    __disable_irq();
    base = s_wrap_base;
    counter = TIM2->CNT;
    /* 处理 overflow 已发生、低优先级 TIM2 IRQ 尚未运行的窗口。 */
    if (((TIM2->SR & TIM_SR_UIF) != 0U) && (counter < 0x80000000U)) {
        base += ((ControlTime)1U << 32U);
    }
    if (primask == 0U) {
        __enable_irq();
    }
    return base + counter;
}

void control_time_source_overflow_irq(void)
{
    uint32_t primask = __get_PRIMASK();

    __disable_irq();
    if ((TIM2->SR & TIM_SR_UIF) != 0U) {
        s_wrap_base += ((ControlTime)1U << 32U);
        __HAL_TIM_CLEAR_FLAG(&htim2, TIM_FLAG_UPDATE);
    }
    if (primask == 0U) {
        __enable_irq();
    }
}
