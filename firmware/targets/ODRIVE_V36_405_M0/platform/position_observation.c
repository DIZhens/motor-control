#include "position_observation.h"

#include <stddef.h>
#include <stdint.h>
#include "main.h"
#include "spi.h"
#include "control_time_source.h"
#include "position_observation_internal.h"

#define POSITION_ACQUISITION_DIVIDER (20U)
#define POSITION_TRANSACTION_TIMEOUT_US (1000U)
#define MT6701_TRANSFER_BYTES (4U)
#define SPI_TRANSACTION_IRQ_MASK (SPI_CR2_RXNEIE | SPI_CR2_TXEIE | SPI_CR2_ERRIE)

static uint8_t s_rx[MT6701_TRANSFER_BYTES];
static uint32_t s_tx_count;
static uint32_t s_rx_count;
static uint32_t s_divider;
static uint32_t s_spi_config;
static volatile uint32_t s_transaction_epoch;
static ControlTime s_acquisition_time;
static volatile bool s_active;
static bool s_armed;
static bool s_referenced;
static PositionObservation s_publications[2];
static volatile uint32_t s_publication_index;
static volatile bool s_available;

static uint8_t mt6701_crc6(uint32_t data)
{
    uint8_t crc = 0U;
    uint32_t bit;

    /* angle + status 的 18 bit，MSB first，多项式 x^6 + x + 1。 */
    for (bit = 18U; bit > 0U; bit--) {
        uint32_t feedback = ((data >> (bit - 1U)) & 1U) ^ ((crc >> 5U) & 1U);
        crc = (uint8_t)((crc << 1U) & 0x3FU);
        if (feedback != 0U) {
            crc ^= 0x03U;
        }
    }
    return crc;
}

static void publish(const PositionObservation *observation)
{
    uint32_t next = s_publication_index ^ 1U;
    s_publications[next] = *observation;
    __DMB();
    s_publication_index = next;
    s_available = true;
}

static void publish_invalid(ControlTime time)
{
    PositionObservation observation;
    observation.mechanical_position_turns = 0.0f;
    observation.observation_time = time;
    observation.referenced = s_referenced;
    observation.acquisition_valid = false;
    publish(&observation);
}

static void abort_transaction(ControlTime time)
{
    uint32_t primask = __get_PRIMASK();
    volatile uint32_t discarded;

    __disable_irq();
    CLEAR_BIT(SPI3->CR2, SPI_TRANSACTION_IRQ_MASK);
    CLEAR_BIT(SPI3->CR1, SPI_CR1_SPE);
    HAL_GPIO_WritePin(MT6701_CS_GPIO_Port, MT6701_CS_Pin, GPIO_PIN_SET);
    /* DR -> SR 清 OVR；不在 ISR 等 BSY，也不使用 HAL 的超时 polling。 */
    discarded = SPI3->DR;
    discarded = SPI3->SR;
    (void)discarded;
    SPI3->CR1 = s_spi_config;
    s_active = false;
    s_transaction_epoch++;
    publish_invalid(time);
    if (primask == 0U) {
        __enable_irq();
    }
}

PositionTopology position_observation_get_topology(void)
{
    return POSITION_TOPOLOGY_PERIODIC;
}

bool position_observation_arm(void)
{
    if (s_armed || (HAL_SPI_GetState(&hspi3) != HAL_SPI_STATE_READY)) {
        return false;
    }
    HAL_GPIO_WritePin(MT6701_CS_GPIO_Port, MT6701_CS_Pin, GPIO_PIN_SET);
    CLEAR_BIT(SPI3->CR2, SPI_TRANSACTION_IRQ_MASK);
    s_spi_config = SPI3->CR1 & ~SPI_CR1_SPE;
    s_armed = true;
    return true;
}

bool position_observation_get_latest(PositionObservation *out_observation)
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

void position_observation_fast_tick(ControlTime observation_time)
{
    if (!s_armed || (++s_divider < POSITION_ACQUISITION_DIVIDER)) {
        return;
    }
    s_divider = 0U;
    if (s_active) {
        if ((observation_time - s_acquisition_time) >= POSITION_TRANSACTION_TIMEOUT_US) {
            abort_transaction(observation_time);
        }
        return;
    }
    if ((SPI3->SR & SPI_SR_BSY) != 0U) {
        publish_invalid(observation_time);
        return;
    }
    s_acquisition_time = control_time_source_now();
    s_tx_count = 0U;
    s_rx_count = 0U;
    s_transaction_epoch++;
    s_active = true;
    HAL_GPIO_WritePin(MT6701_CS_GPIO_Port, MT6701_CS_Pin, GPIO_PIN_RESET);
    SET_BIT(SPI3->CR1, SPI_CR1_SPE);
    SET_BIT(SPI3->CR2, SPI_TRANSACTION_IRQ_MASK);
}

void position_observation_spi_irq(void)
{
    uint32_t status;
    uint32_t transfer;
    uint32_t frame;
    uint32_t epoch;
    uint32_t primask;
    uint32_t sensor_status;
    PositionObservation observation;

    primask = __get_PRIMASK();
    __disable_irq();
    status = SPI3->SR;
    if (!s_active) {
        CLEAR_BIT(SPI3->CR2, SPI_TRANSACTION_IRQ_MASK);
        if (primask == 0U) {
            __enable_irq();
        }
        return;
    }
    if ((status & (SPI_SR_OVR | SPI_SR_MODF)) != 0U) {
        abort_transaction(s_acquisition_time);
        if (primask == 0U) {
            __enable_irq();
        }
        return;
    }
    /* 每次 IRQ 至多一次接收和一次发送，无循环、无 BSY 等待。 */
    if ((status & SPI_SR_RXNE) != 0U) {
        s_rx[s_rx_count++] = *(__IO uint8_t *)&SPI3->DR;
    }
    if (s_rx_count == MT6701_TRANSFER_BYTES) {
        CLEAR_BIT(SPI3->CR2, SPI_TRANSACTION_IRQ_MASK);
        HAL_GPIO_WritePin(MT6701_CS_GPIO_Port, MT6701_CS_Pin, GPIO_PIN_SET);
        transfer = ((uint32_t)s_rx[0] << 24U) | ((uint32_t)s_rx[1] << 16U)
            | ((uint32_t)s_rx[2] << 8U) | s_rx[3];
        epoch = s_transaction_epoch;
        observation.observation_time = s_acquisition_time;
        s_active = false;
        if (primask == 0U) {
            __enable_irq();
        }
        /*
         * 本 Target 选择 CPOL=HIGH / CPHA=1EDGE：首下降沿读到前导 idle bit。
         * 32 clock = 1 leading + 24 frame + 7 trailing；这是 SPI3 framing 配置，
         * 不是所有 MT6701 transport 通用规则，需 F405 无功率 L1 核对。
         */
        frame = (transfer >> 7U) & 0xFFFFFFU;
        sensor_status = (frame >> 6U) & 0x0FU;
        observation.mechanical_position_turns = (float)(frame >> 10U) / 16384.0f;
        observation.acquisition_valid = ((frame & 0x3FU) == mt6701_crc6(frame >> 6U))
            && ((sensor_status & 0x0BU) == 0U);

        primask = __get_PRIMASK();
        __disable_irq();
        if (epoch == s_transaction_epoch) {
            if (observation.acquisition_valid) {
                s_referenced = true;
            }
            observation.referenced = s_referenced;
            publish(&observation);
        }
        if (primask == 0U) {
            __enable_irq();
        }
        return;
    }
    if ((status & SPI_SR_TXE) != 0U) {
        if (s_tx_count < MT6701_TRANSFER_BYTES) {
            *(__IO uint8_t *)&SPI3->DR = 0xFFU;
            s_tx_count++;
        }
        if (s_tx_count == MT6701_TRANSFER_BYTES) {
            CLEAR_BIT(SPI3->CR2, SPI_CR2_TXEIE);
        }
    }
    if (primask == 0U) {
        __enable_irq();
    }
}
