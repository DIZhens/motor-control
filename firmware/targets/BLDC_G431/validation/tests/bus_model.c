/* 仅替代HAL起动/读DR；实际bus_observation.c由ARM模型直接执行。 */
#include "adc.h"
#include "bus_observation.h"
#include "bus_observation_internal.h"

ADC_HandleTypeDef hadc1 = {.Instance = ADC1};
uint32_t model_start_status;
uint32_t model_start_calls;
uint32_t model_raw;
uint32_t model_eos_after_read;
uint32_t model_available;
BusObservation model_bus;
G431BusAcquisitionDiagnostic model_diagnostic;

HAL_StatusTypeDef HAL_ADC_Start_IT(ADC_HandleTypeDef *hadc)
{
    (void)hadc;
    model_start_calls++;
    return (HAL_StatusTypeDef)model_start_status;
}

uint32_t HAL_ADC_GetValue(const ADC_HandleTypeDef *hadc)
{
    uint32_t value = model_raw;
    (void)hadc;
    if (model_eos_after_read != 0U) {
        /* 在DR交付当前rank后，下一次转换完成；模拟硬件重新置EOC/EOS。 */
        model_eos_after_read = 0U;
        hadc1.Instance->ISR |= ADC_FLAG_EOC | ADC_FLAG_EOS;
        model_raw = 900U;
    }
    return value;
}

void bus_model_start(void)
{
    uint32_t i;
    for (i = 0U; i < 10U; i++) { bus_observation_fast_tick(20U); }
}

void bus_model_rank(void) { HAL_ADC_ConvCpltCallback(&hadc1); }
void bus_model_error(void) { hadc1.ErrorCode = HAL_ADC_ERROR_OVR; HAL_ADC_ErrorCallback(&hadc1); }
void bus_model_read(void)
{
    model_available = bus_observation_get_latest(&model_bus) ? 1U : 0U;
    (void)g431_bus_acquisition_get_diagnostic(&model_diagnostic);
}
