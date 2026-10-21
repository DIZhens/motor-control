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
static volatile bool s_failed;
static volatile ControlTime s_sequence_observation_time;
static __ALIGNED(4) volatile uint16_t s_dma_samples[ADC1_REGULAR_SEQUENCE_LENGTH];

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
 * DMA1_Channel1与ADC1_2使用相同抢占优先级，两个writer不能互相抢占。
 * 只写inactive slot，最后用单个index同时切换观测与采集诊断。
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
    DMA_HandleTypeDef *dma = hadc1.DMA_Handle;
    if (s_armed || s_sequence_active || dma == NULL || dma->Instance != DMA1_Channel1
        || dma->Init.Request != DMA_REQUEST_ADC1 || dma->Init.Direction != DMA_PERIPH_TO_MEMORY
        || dma->State != HAL_DMA_STATE_READY || dma->Init.Mode != DMA_NORMAL || dma->Init.MemInc != DMA_MINC_ENABLE
        || dma->Init.PeriphInc != DMA_PINC_DISABLE
        || dma->Init.PeriphDataAlignment != DMA_PDATAALIGN_HALFWORD
        || dma->Init.MemDataAlignment != DMA_MDATAALIGN_HALFWORD
        || NVIC_GetPriority(DMA1_Channel1_IRQn) != 1U || NVIC_GetPriority(ADC1_2_IRQn) != 1U) {
        return false;
    }

    s_active_publication_index = 0U;
    s_publication_available = false;
    s_sequence_active = false;
    s_failed = false;
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
 * ADC与DMA writer同抢占优先级，不会互相抢占；后台读取用短临界区复制完整slot。
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

    if (!s_armed || s_failed) {
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
    s_vbus_raw = 0U;
    s_vrefint_raw = 0U;
    s_sequence_active = true;

    status = HAL_ADC_Start_DMA(&hadc1, (uint32_t *)s_dma_samples, ADC1_REGULAR_SEQUENCE_LENGTH);
    if (status != HAL_OK) {
        s_failed = true;
        s_sequence_active = false;
        publish_invalid_observation(fast_tick_time, G431_BUS_ACQUISITION_START_FAILED, status);
    } else {
        /* 只交付整组TC；半传输不能生成母线publication，也不需要额外IRQ。 */
        __HAL_DMA_DISABLE_IT(hadc1.DMA_Handle, DMA_IT_HT);
    }
}

void HAL_ADC_ConvCpltCallback(ADC_HandleTypeDef *hadc)
{
    BusObservation observation;
    uint32_t remaining;

    if ((hadc != &hadc1) || !s_sequence_active) {
        return;
    }

    /* DMA TC保证三个halfword已入RAM；不再读DR或用实时EOS推断当前rank。 */
    remaining = __HAL_DMA_GET_COUNTER(hadc1.DMA_Handle);
    __DMB();
    s_regular_rank_index = remaining <= ADC1_REGULAR_SEQUENCE_LENGTH ? ADC1_REGULAR_SEQUENCE_LENGTH - remaining : 0U;
    if (s_regular_rank_index >= 1U) { s_vbus_raw = s_dma_samples[ADC1_VBUS_RANK_INDEX]; }
    if (s_regular_rank_index >= 2U) { s_vrefint_raw = s_dma_samples[ADC1_VREFINT_RANK_INDEX]; }
    if (remaining != 0U || hadc1.DMA_Handle->State != HAL_DMA_STATE_READY) {
        s_failed = true;
        s_sequence_active = false;
        publish_invalid_observation(s_sequence_observation_time, G431_BUS_ACQUISITION_DMA_INCOMPLETE, HAL_ERROR);
        return;
    }
    if (hadc1.ErrorCode != HAL_ADC_ERROR_NONE || (hadc1.Instance->ISR & ADC_FLAG_OVR) != 0U) {
        HAL_ADC_ErrorCallback(&hadc1);
        return;
    }
    if (s_vbus_raw > 4095U || s_vrefint_raw > 4095U || s_dma_samples[2] > 4095U) {
        s_failed = true;
        s_sequence_active = false;
        publish_invalid_observation(s_sequence_observation_time, G431_BUS_ACQUISITION_DMA_INCOMPLETE, HAL_ERROR);
        return;
    }

    observation.observation_time = s_sequence_observation_time;
    observation.acquisition_valid = convert_vbus(&observation.voltage_v);

    s_failed = !observation.acquisition_valid;
    publish_observation(&observation, observation.acquisition_valid ? G431_BUS_ACQUISITION_OK
        : G431_BUS_ACQUISITION_VREF_ZERO, HAL_OK);
    s_sequence_active = false;
}

void HAL_ADC_ErrorCallback(ADC_HandleTypeDef *hadc)
{
    ControlTime observation_time;
    uint32_t remaining;

    if ((hadc != &hadc1) || !s_sequence_active) {
        return;
    }

    observation_time = s_sequence_observation_time;
    remaining = __HAL_DMA_GET_COUNTER(hadc1.DMA_Handle);
    __DMB();
    s_regular_rank_index = remaining <= ADC1_REGULAR_SEQUENCE_LENGTH ? ADC1_REGULAR_SEQUENCE_LENGTH - remaining : 0U;
    if (s_regular_rank_index >= 1U) { s_vbus_raw = s_dma_samples[0]; }
    if (s_regular_rank_index >= 2U) { s_vrefint_raw = s_dma_samples[1]; }
    s_failed = true;
    s_sequence_active = false;

    publish_invalid_observation(observation_time, (hadc1.ErrorCode & HAL_ADC_ERROR_DMA) != 0U
        ? G431_BUS_ACQUISITION_DMA_ERROR : G431_BUS_ACQUISITION_ADC_ERROR, HAL_ERROR);
}
