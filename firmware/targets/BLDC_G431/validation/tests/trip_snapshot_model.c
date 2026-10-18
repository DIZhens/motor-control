/* Snapshot-only fixture; the getter and its evidence logic are production code. */
#include "hardware_trip.h"
#include "comp.h"
#include "dac.h"
#include "tim.h"

COMP_HandleTypeDef hcomp1 = { .Instance = COMP1 };
COMP_HandleTypeDef hcomp2 = { .Instance = COMP2 };
COMP_HandleTypeDef hcomp3 = { .Instance = COMP3 };
COMP_HandleTypeDef hcomp4 = { .Instance = COMP4 };
TIM_HandleTypeDef htim1 = { .Instance = TIM1 };
HardwareTripSnapshot model_snapshot;
volatile uint32_t model_snapshot_result;
volatile uint32_t model_null_output;
volatile uint32_t model_unexpected_calls;
DAC_HandleTypeDef hdac1;
DAC_HandleTypeDef hdac3;

/* Link-only initialization substitutes; snapshot tests require zero calls. */
HAL_StatusTypeDef HAL_COMP_Start(COMP_HandleTypeDef *handle)
{ (void)handle; model_unexpected_calls++; return HAL_ERROR; }
HAL_StatusTypeDef HAL_COMP_Stop(COMP_HandleTypeDef *handle)
{ (void)handle; model_unexpected_calls++; return HAL_ERROR; }
HAL_StatusTypeDef HAL_DAC_Start(DAC_HandleTypeDef *handle, uint32_t channel)
{ (void)handle; (void)channel; model_unexpected_calls++; return HAL_ERROR; }
HAL_StatusTypeDef HAL_DAC_Stop(DAC_HandleTypeDef *handle, uint32_t channel)
{ (void)handle; (void)channel; model_unexpected_calls++; return HAL_ERROR; }
HAL_StatusTypeDef HAL_DAC_SetValue(DAC_HandleTypeDef *handle, uint32_t channel, uint32_t alignment, uint32_t value)
{ (void)handle; (void)channel; (void)alignment; (void)value; model_unexpected_calls++; return HAL_ERROR; }
void realtime_motor_io_safe_off(void)
{ model_unexpected_calls++; }

void trip_snapshot_model_read(void)
{
    model_snapshot_result = hardware_trip_get_snapshot(model_null_output ? 0 : &model_snapshot);
}
