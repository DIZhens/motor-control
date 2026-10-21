/* DMA传输事件替身；实际bus_observation.c由ARM模型直接执行。 */
#include "adc.h"
#include "bus_observation.h"
#include "bus_observation_internal.h"

DMA_HandleTypeDef hdma_adc1 = {
    .Instance = DMA1_Channel1,
    .Init = {.Request = DMA_REQUEST_ADC1, .Direction = DMA_PERIPH_TO_MEMORY,
        .PeriphInc = DMA_PINC_DISABLE, .MemInc = DMA_MINC_ENABLE,
        .PeriphDataAlignment = DMA_PDATAALIGN_HALFWORD, .MemDataAlignment = DMA_MDATAALIGN_HALFWORD,
        .Mode = DMA_NORMAL},
    .State = HAL_DMA_STATE_READY
};
ADC_HandleTypeDef hadc1 = {.Instance = ADC1, .DMA_Handle = &hdma_adc1};
uint32_t model_start_status, model_start_calls, model_length, model_buffer_address;
uint32_t model_raw, model_defer_completion, model_getvalue_calls;
uint32_t model_bad_config, model_arm_result, model_error_code = HAL_ADC_ERROR_OVR;
uint32_t model_available;
BusObservation model_bus;
G431BusAcquisitionDiagnostic model_diagnostic;

HAL_StatusTypeDef HAL_ADC_Start_DMA(ADC_HandleTypeDef *hadc, uint32_t *data, uint32_t length)
{
    (void)hadc;
    model_start_calls++;
    model_length = length;
    model_buffer_address = (uint32_t)data;
    if (model_start_status == HAL_OK) {
        hdma_adc1.Instance->CNDTR = length;
        hdma_adc1.Instance->CCR = DMA_CCR_EN | DMA_IT_TC | DMA_IT_HT | DMA_IT_TE;
        hdma_adc1.State = HAL_DMA_STATE_BUSY;
        hadc1.ErrorCode = HAL_ADC_ERROR_NONE;
    }
    return (HAL_StatusTypeDef)model_start_status;
}

uint32_t HAL_ADC_GetValue(const ADC_HandleTypeDef *hadc)
{
    (void)hadc;
    model_getvalue_calls++;
    return model_raw;
}

void bus_model_arm(void)
{
    if (model_bad_config == 1U) { hadc1.DMA_Handle = 0; }
    if (model_bad_config == 2U) { hdma_adc1.Init.Mode = DMA_CIRCULAR; }
    if (model_bad_config == 3U) { hdma_adc1.Init.MemInc = DMA_MINC_DISABLE; }
    if (model_bad_config == 4U) { hdma_adc1.Init.MemDataAlignment = DMA_MDATAALIGN_WORD; }
    if (model_bad_config == 5U) { hdma_adc1.Init.Request = DMA_REQUEST_ADC2; }
    if (model_bad_config == 6U) { hdma_adc1.State = HAL_DMA_STATE_BUSY; }
    model_arm_result = bus_observation_arm() ? 1U : 0U;
}

void bus_model_start(void)
{
    uint32_t i;
    for (i = 0U; i < 10U; i++) { bus_observation_fast_tick(20U); }
}

void bus_model_complete(void)
{
    hdma_adc1.State = HAL_DMA_STATE_READY;
    HAL_ADC_ConvCpltCallback(&hadc1);
}

void bus_model_transfer(void)
{
    uint32_t remaining = hdma_adc1.Instance->CNDTR;
    if (remaining == 0U || remaining > 3U) { return; }
    ((volatile uint16_t *)model_buffer_address)[3U - remaining] = (uint16_t)model_raw;
    hdma_adc1.Instance->CNDTR = remaining - 1U;
    if (remaining == 1U && model_defer_completion == 0U) { bus_model_complete(); }
}

void bus_model_error(void) { hadc1.ErrorCode = model_error_code; HAL_ADC_ErrorCallback(&hadc1); }
void bus_model_read(void)
{
    model_available = bus_observation_get_latest(&model_bus) ? 1U : 0U;
    (void)g431_bus_acquisition_get_diagnostic(&model_diagnostic);
}
