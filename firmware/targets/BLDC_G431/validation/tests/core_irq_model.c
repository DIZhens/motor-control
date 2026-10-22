/* 执行真实 Core IRQ 时，仅替换通用 TIM HAL 分发；不是保护或外设时延模型。 */
#include "tim.h"
#include "adc.h"
#include "dac.h"
#include "spi.h"

/* 通过已保留的 TIM 替身引用真实 SPI 向量，防止模型链接器将未装入向量表的入口回收。 */
extern void SPI1_IRQHandler(void);
extern void DMA1_Channel1_IRQHandler(void);
extern void TIM6_DAC_IRQHandler(void);
extern void TIM7_IRQHandler(void);
static void (* volatile s_core_spi_vector)(void) = SPI1_IRQHandler;
static void (* volatile s_core_dma_vector)(void) = DMA1_Channel1_IRQHandler;
static void (* volatile s_core_guard_vector)(void) = TIM6_DAC_IRQHandler;
static void (* volatile s_core_hal_tick_vector)(void) = TIM7_IRQHandler;
TIM_HandleTypeDef htim7;
volatile uint32_t model_core_tim7_hal_calls;

volatile uint32_t model_core_hal_calls;
volatile uint32_t model_core_hal_source;
volatile uint32_t model_core_hal_flags;
volatile uint32_t model_core_hal_updates;
volatile uint32_t model_core_hal_breaks;
volatile uint32_t model_core_tim6_hal_calls;
volatile uint32_t model_core_guard_calls;
volatile uint32_t model_core_dac_hal_calls;

void HAL_TIM_IRQHandler(TIM_HandleTypeDef *htim)
{
    /* 与当前 stm32g4xx_hal_tim.c 的 UPDATE/BREAK 分支相同：入口快照后按 source 清旗。
     * 本模型只启用 UIE/BIE，不模拟其余 CC/Trigger；Break callback 仅模拟暂停 BIE。 */
    uint32_t itsource;
    uint32_t itflag;

    (void)s_core_spi_vector;
    (void)s_core_dma_vector;
    (void)s_core_guard_vector;
    (void)s_core_hal_tick_vector;
    if (htim == &htim7) { model_core_tim7_hal_calls++; return; }
    if (htim == &htim6) {
        model_core_tim6_hal_calls++;
        return;
    }
    itsource = htim->Instance->DIER;
    itflag = htim->Instance->SR;
    model_core_hal_calls++;
    model_core_hal_source = itsource;
    model_core_hal_flags = itflag;
    if ((itflag & TIM_FLAG_UPDATE) == TIM_FLAG_UPDATE) {
        if ((itsource & TIM_IT_UPDATE) == TIM_IT_UPDATE) {
            __HAL_TIM_CLEAR_FLAG(htim, TIM_FLAG_UPDATE);
            model_core_hal_updates++;
        }
    }
    if ((itflag & (TIM_FLAG_BREAK | TIM_FLAG_SYSTEM_BREAK)) != 0U) {
        if ((itsource & TIM_IT_BREAK) == TIM_IT_BREAK) {
            __HAL_TIM_CLEAR_FLAG(htim, TIM_FLAG_BREAK | TIM_FLAG_SYSTEM_BREAK);
            model_core_hal_breaks++;
            __HAL_TIM_DISABLE_IT(htim, TIM_IT_BREAK);
        }
    }
    if ((itflag & TIM_FLAG_BREAK2) == TIM_FLAG_BREAK2) {
        if ((itsource & TIM_IT_BREAK) == TIM_IT_BREAK) {
            __HAL_TIM_CLEAR_FLAG(htim, TIM_FLAG_BREAK2);
            model_core_hal_breaks++;
            __HAL_TIM_DISABLE_IT(htim, TIM_IT_BREAK);
        }
    }
}

/* Core 翻译单元的其他向量只需链接；本测试若误调用则计数并失败。 */
DAC_HandleTypeDef hdac1;
DAC_HandleTypeDef hdac3;
SPI_HandleTypeDef hspi1;
DMA_HandleTypeDef hdma_adc1;
TIM_HandleTypeDef htim6;
volatile uint32_t model_core_unexpected_irqs;
volatile uint32_t model_core_position_irq_calls;
volatile uint32_t model_core_spi_hal_calls;
volatile uint32_t model_core_dma_hal_calls;
void HAL_DMA_IRQHandler(DMA_HandleTypeDef *handle)
{
    if (handle != &hdma_adc1) { model_core_unexpected_irqs++; }
    model_core_dma_hal_calls++;
}
void position_observation_irq(void) { model_core_position_irq_calls++; }
void HAL_ADC_IRQHandler(ADC_HandleTypeDef *handle) { (void)handle; model_core_unexpected_irqs++; }
void HAL_DAC_IRQHandler(DAC_HandleTypeDef *handle)
{
    if (handle != &hdac1 && handle != &hdac3) { model_core_unexpected_irqs++; }
    model_core_dac_hal_calls++;
}
void HAL_SPI_IRQHandler(SPI_HandleTypeDef *handle)
{
    (void)handle;
    model_core_spi_hal_calls++;
    model_core_unexpected_irqs++;
}
void HAL_IncTick(void) { model_core_unexpected_irqs++; }
void target_validation_guard_irq(void) { model_core_unexpected_irqs++; }
void target_runtime_guard_irq(void) { model_core_guard_calls++; }
