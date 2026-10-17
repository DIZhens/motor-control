#include "bus_observation.h"

#include <stddef.h>
#include "adc.h"
#include "bus_observation_internal.h"

#define BUS_ACQUISITION_DIVIDER (20U)
#define VREFINT_NOMINAL_V (1.21f)
#define VBUS_DIVIDER_SCALE ((39.0f + 2.2f) / 2.2f)

__ALIGN_BEGIN static uint16_t s_adc_pair[2] __ALIGN_END;
static BusObservation s_publications[2];
static volatile uint32_t s_publication_index;
static volatile bool s_available;
static bool s_armed;
static volatile bool s_active;
static uint32_t s_divider;
static ControlTime s_acquisition_time;

static void publish(float voltage_v, bool valid, ControlTime time)
{
    uint32_t next = s_publication_index ^ 1U;
    s_publications[next].voltage_v = voltage_v;
    s_publications[next].observation_time = time;
    s_publications[next].acquisition_valid = valid;
    __DMB();
    s_publication_index = next;
    s_available = true;
}

bool bus_observation_arm(void)
{
    if (s_armed || (HAL_ADC_GetState(&hadc1) != HAL_ADC_STATE_READY)) {
        return false;
    }
    s_armed = true;
    return true;
}

bool bus_observation_get_latest(BusObservation *out_observation)
{
    uint32_t primask;
    bool available;

    if (out_observation == NULL) {
        return false;
    }
    primask = __get_PRIMASK();
    __disable_irq();
    available = s_available;
    if (available) {
        *out_observation = s_publications[s_publication_index];
    }
    if (primask == 0U) {
        __enable_irq();
    }
    return available;
}

void bus_observation_fast_tick(ControlTime observation_time)
{
    if (!s_armed || (++s_divider < BUS_ACQUISITION_DIVIDER)) {
        return;
    }
    s_divider = 0U;
    if (s_active) {
        /* 超时事务不被冒充为新数据；不在 fast ISR 阻塞 abort DMA。 */
        publish(0.0f, false, observation_time);
        return;
    }
    s_acquisition_time = observation_time;
    s_active = true;
    if (HAL_ADC_Start_DMA(&hadc1, (uint32_t *)s_adc_pair, 2U) != HAL_OK) {
        s_active = false;
        publish(0.0f, false, observation_time);
    }
}

void HAL_ADC_ConvCpltCallback(ADC_HandleTypeDef *hadc)
{
    uint16_t bus_raw;
    uint16_t reference_raw;
    uint32_t primask;

    if ((hadc != &hadc1) || !s_active) {
        return;
    }
    bus_raw = s_adc_pair[0];
    reference_raw = s_adc_pair[1];
    /*
     * F405 不使用 G4 的 factory VREFINT_CAL。1.21 V 为名义参考；后续精度实测
     * 仍必需。两 rank DMA 完成才发布，不能用逐 rank IRQ 拼出旧新混合数据。
     */
    primask = __get_PRIMASK();
    __disable_irq();
    publish(reference_raw != 0U
        ? ((float)bus_raw / reference_raw) * VREFINT_NOMINAL_V * VBUS_DIVIDER_SCALE
        : 0.0f, reference_raw != 0U, s_acquisition_time);
    s_active = false;
    if (primask == 0U) {
        __enable_irq();
    }
}

void HAL_ADC_ErrorCallback(ADC_HandleTypeDef *hadc)
{
    uint32_t primask;

    if ((hadc != &hadc1) || !s_active) {
        return;
    }
    primask = __get_PRIMASK();
    __disable_irq();
    publish(0.0f, false, s_acquisition_time);
    s_active = false;
    if (primask == 0U) {
        __enable_irq();
    }
}
