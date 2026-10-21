#include "bus_observation.h"

#include <stddef.h>
#include <stdint.h>

#include "adc.h"
#include "stm32g4xx_ll_adc.h"

#include "bus_observation_internal.h"

/* 当前 10 kHz 电流采样每 10 拍发起一次 Bus ADC sequence，即 1 kHz。 */
#define BUS_ACQUISITION_DIVIDER (10U)

#define ADC_FULL_SCALE_COUNTS (4095.0f)
#define MILLIVOLTS_PER_VOLT (1000.0f)
#define VBUS_DIVIDER_SCALE ((47.0f + 5.1f) / 5.1f)

#define ADC1_REGULAR_SEQUENCE_LENGTH (3U)
#define ADC1_VBUS_RANK_INDEX (0U)
#define ADC1_VREFINT_RANK_INDEX (1U)

static BusObservation s_publications[2];
static G431BusAcquisitionDiagnostic s_diagnostics[2];

static volatile uint32_t s_active_publication_index;
static volatile bool s_publication_available;
static volatile bool s_armed;
static volatile bool s_sequence_active;
static volatile ControlTime s_sequence_observation_time;

static uint32_t s_acquisition_divider_count;
static uint32_t s_regular_rank_index;
static uint32_t s_vbus_raw;
static uint32_t s_vrefint_raw;

static bool convert_vbus(float *out_voltage_v)
{
    uint32_t vdda_mv;
    float adc_voltage_v;

    if ((out_voltage_v == NULL) || (s_vrefint_raw == 0U)) {
        return false;
    }

    vdda_mv = __LL_ADC_CALC_VREFANALOG_VOLTAGE(
        s_vrefint_raw,
        LL_ADC_RESOLUTION_12B);

    adc_voltage_v = ((float)s_vbus_raw * (float)vdda_mv)
        / (ADC_FULL_SCALE_COUNTS * MILLIVOLTS_PER_VOLT);

    *out_voltage_v = adc_voltage_v * VBUS_DIVIDER_SCALE;
    return true;
}

/*
 * ADC IRQ 只写 inactive slot，最后用单个 index 切换 publication。
 * fast ISR 与 regular ADC callback 共用 ADC1_2 IRQ，不会并发写同一个 slot。
 */
static void publish_observation(const BusObservation *observation,
    G431BusAcquisitionCause cause, HAL_StatusTypeDef status)
{
    uint32_t next_index = s_active_publication_index ^ 1U;
    G431BusAcquisitionDiagnostic *diagnostic = &s_diagnostics[next_index];

    s_publications[next_index] = *observation;
    diagnostic->observation_time = observation->observation_time;
    diagnostic->cause = (uint32_t)cause;
    diagnostic->hal_status = (uint32_t)status;
    diagnostic->rank_count = s_regular_rank_index;
    diagnostic->vbus_raw = s_vbus_raw;
    diagnostic->vrefint_raw = s_vrefint_raw;
    diagnostic->adc_error = hadc1.ErrorCode;
    diagnostic->adc_state = hadc1.State;
    diagnostic->adc_isr = hadc1.Instance->ISR;
    diagnostic->adc_ier = hadc1.Instance->IER;
    diagnostic->adc_cr = hadc1.Instance->CR;
    __DMB();

    s_active_publication_index = next_index;
    s_publication_available = true;
}

static void publish_invalid_observation(ControlTime observation_time,
    G431BusAcquisitionCause cause, HAL_StatusTypeDef status)
{
    BusObservation observation;

    observation.voltage_v = 0.0f;
    observation.observation_time = observation_time;
    observation.acquisition_valid = false;

    publish_observation(&observation, cause, status);
}

bool bus_observation_arm(void)
{
    if (s_armed || s_sequence_active) {
        return false;
    }

    s_active_publication_index = 0U;
    s_publication_available = false;
    s_sequence_active = false;
    s_sequence_observation_time = 0U;
    s_acquisition_divider_count = 0U;
    s_regular_rank_index = 0U;
    s_vbus_raw = 0U;
    s_vrefint_raw = 0U;
    s_armed = true;

    return true;
}

/*
 * 后台读取时用极短临界区阻止 ADC writer 抢占。
 * fast ISR 与 writer 同属 ADC1_2 IRQ，因此 fast path 不需要等待 writer。
 */
bool bus_observation_get_latest(BusObservation *out_observation)
{
    uint32_t primask;
    uint32_t index;

    if (out_observation == NULL) {
        return false;
    }

    primask = __get_PRIMASK();
    __disable_irq();

    if (!s_publication_available) {
        if (primask == 0U) {
            __enable_irq();
        }
        return false;
    }

    index = s_active_publication_index;
    *out_observation = s_publications[index];

    if (primask == 0U) {
        __enable_irq();
    }

    return true;
}

bool g431_bus_acquisition_get_diagnostic(G431BusAcquisitionDiagnostic *diagnostic)
{
    uint32_t primask;
    bool available;
    if (diagnostic == NULL) { return false; }
    primask = __get_PRIMASK();
    __disable_irq();
    available = s_publication_available;
    if (available) { *diagnostic = s_diagnostics[s_active_publication_index]; }
    if (primask == 0U) { __enable_irq(); }
    return available;
}

void bus_observation_fast_tick(ControlTime fast_tick_time)
{
    HAL_StatusTypeDef status;

    if (!s_armed) {
        return;
    }

    s_acquisition_divider_count++;
    if (s_acquisition_divider_count < BUS_ACQUISITION_DIVIDER) {
        return;
    }

    s_acquisition_divider_count = 0U;
    if (s_sequence_active) {
        return;
    }

    s_sequence_observation_time = fast_tick_time;
    s_regular_rank_index = 0U;
    s_sequence_active = true;

    status = HAL_ADC_Start_IT(&hadc1);
    if (status != HAL_OK) {
        s_sequence_active = false;
        publish_invalid_observation(fast_tick_time, G431_BUS_ACQUISITION_START_FAILED, status);
    }
}

void HAL_ADC_ConvCpltCallback(ADC_HandleTypeDef *hadc)
{
    BusObservation observation;
    uint32_t raw;
    bool end_of_sequence;

    if ((hadc != &hadc1) || !s_sequence_active) {
        return;
    }

    raw = HAL_ADC_GetValue(&hadc1);
    end_of_sequence = (__HAL_ADC_GET_FLAG(&hadc1, ADC_FLAG_EOS) != 0U);

    if (s_regular_rank_index == ADC1_VBUS_RANK_INDEX) {
        s_vbus_raw = raw;
    } else if (s_regular_rank_index == ADC1_VREFINT_RANK_INDEX) {
        s_vrefint_raw = raw;
    }

    s_regular_rank_index++;
    if (!end_of_sequence) {
        return;
    }

    if (s_regular_rank_index != ADC1_REGULAR_SEQUENCE_LENGTH) {
        s_sequence_active = false;
        publish_invalid_observation(s_sequence_observation_time, G431_BUS_ACQUISITION_RANK_MISMATCH, HAL_OK);
        return;
    }

    observation.observation_time = s_sequence_observation_time;
    observation.acquisition_valid = convert_vbus(&observation.voltage_v);

    s_sequence_active = false;
    publish_observation(&observation, observation.acquisition_valid ? G431_BUS_ACQUISITION_OK
        : G431_BUS_ACQUISITION_VREF_ZERO, HAL_OK);
}

void HAL_ADC_ErrorCallback(ADC_HandleTypeDef *hadc)
{
    ControlTime observation_time;

    if ((hadc != &hadc1) || !s_sequence_active) {
        return;
    }

    observation_time = s_sequence_observation_time;
    s_sequence_active = false;

    publish_invalid_observation(observation_time, G431_BUS_ACQUISITION_ADC_ERROR, HAL_ERROR);
}
