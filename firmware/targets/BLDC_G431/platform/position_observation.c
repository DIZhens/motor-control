#include "position_observation.h"

#include <stddef.h>
#include <stdint.h>

#include "main.h"
#include "spi.h"

#include "position_observation_internal.h"

/* 当前 20 kHz fast tick 每 20 tick 发起一次 SSI 采集，即 1 kHz。 */
#define POSITION_ACQUISITION_DIVIDER (20U)

#define MT6701_TRANSFER_SIZE_BYTES (4U)
#define MT6701_FRAME_TRAILING_BITS (7U)
#define MT6701_FRAME_MASK (0x00FFFFFFU)
#define MT6701_ANGLE_SHIFT (10U)
#define MT6701_STATUS_SHIFT (6U)
#define MT6701_STATUS_MASK (0x0FU)
#define MT6701_FIELD_STATUS_MASK (0x03U)
#define MT6701_LOSS_OF_TRACK_MASK (0x08U)
#define MT6701_CRC_MASK (0x3FU)
#define MT6701_CRC_DATA_BITS (18U)
#define MT6701_CRC_POLYNOMIAL_LOW (0x03U)
#define MT6701_ANGLE_COUNTS_PER_TURN (16384.0f)

static uint8_t s_rx_frame[MT6701_TRANSFER_SIZE_BYTES];
static PositionObservation s_publications[2];

static volatile uint32_t s_active_publication_index;
static volatile bool s_publication_available;
static volatile bool s_armed;
static volatile bool s_transfer_active;
static volatile bool s_referenced;
static volatile ControlTime s_transfer_observation_time;

static uint32_t s_acquisition_divider_count;

/*
 * MT6701 SSI CRC 覆盖 angle + status 共 18 bit，按 MSB first 输入。
 * 生成多项式为 x^6 + x + 1；宽度固定为 6 bit。
 */
static uint8_t mt6701_crc6(uint32_t data)
{
    uint8_t crc = 0U;
    uint32_t bit_index;

    for (bit_index = MT6701_CRC_DATA_BITS; bit_index > 0U; bit_index--) {
        uint32_t data_bit = (data >> (bit_index - 1U)) & 1U;
        uint32_t crc_msb = (crc >> 5U) & 1U;
        uint32_t feedback = data_bit ^ crc_msb;

        crc = (uint8_t)((crc << 1U) & MT6701_CRC_MASK);
        if (feedback != 0U) {
            crc ^= MT6701_CRC_POLYNOMIAL_LOW;
        }
    }

    return crc;
}

/*
 * SPI IRQ 只写 inactive slot，最后用单个 index 切换 publication。
 * fast ISR 即使抢占 writer，也只会读取 writer 尚未触碰的 active slot。
 */
static void publish_observation(const PositionObservation *observation)
{
    uint32_t next_index = s_active_publication_index ^ 1U;

    s_publications[next_index] = *observation;
    __DMB();

    s_active_publication_index = next_index;
    s_publication_available = true;
}

static void publish_invalid_observation(ControlTime observation_time)
{
    PositionObservation observation;

    observation.mechanical_position_turns = 0.0f;
    observation.observation_time = observation_time;
    observation.referenced = s_referenced;
    observation.acquisition_valid = false;

    publish_observation(&observation);
}

static bool frame_is_valid(uint32_t frame)
{
    uint8_t status = (uint8_t)((frame >> MT6701_STATUS_SHIFT) & MT6701_STATUS_MASK);
    uint8_t received_crc = (uint8_t)(frame & MT6701_CRC_MASK);
    uint8_t calculated_crc = mt6701_crc6(frame >> MT6701_STATUS_SHIFT);

    if (received_crc != calculated_crc) {
        return false;
    }

    if ((status & MT6701_FIELD_STATUS_MASK) != 0U) {
        return false;
    }

    if ((status & MT6701_LOSS_OF_TRACK_MASK) != 0U) {
        return false;
    }

    return true;
}

static uint32_t assemble_frame(void)
{
    uint32_t transfer = ((uint32_t)s_rx_frame[0] << 24U)
        | ((uint32_t)s_rx_frame[1] << 16U)
        | ((uint32_t)s_rx_frame[2] << 8U)
        | (uint32_t)s_rx_frame[3];

    /*
     * SPI1 空闲高、首边沿采样：首个下降沿先采到一位空闲 DO，
     * 随后的上升沿才开始移出 D13。4 字节传输覆盖这一个前导位、
     * 完整 24 位 SSI 帧和 7 位尾部数据；CRC 只校验有效帧。
     */
    return (transfer >> MT6701_FRAME_TRAILING_BITS) & MT6701_FRAME_MASK;
}

PositionTopology position_observation_get_topology(void)
{
    return POSITION_TOPOLOGY_PERIODIC;
}

bool position_observation_arm(void)
{
    if (s_armed || s_transfer_active) {
        return false;
    }

    if (HAL_SPI_GetState(&hspi1) != HAL_SPI_STATE_READY) {
        return false;
    }

    HAL_GPIO_WritePin(SPI1_CSN_GPIO_Port, SPI1_CSN_Pin, GPIO_PIN_SET);

    s_active_publication_index = 0U;
    s_publication_available = false;
    s_transfer_active = false;
    s_referenced = false;
    s_transfer_observation_time = 0U;
    s_acquisition_divider_count = 0U;
    s_armed = true;

    return true;
}

/*
 * 后台读取时用极短临界区阻止 SPI writer 抢占；fast ISR 本身优先级高于 SPI IRQ。
 * 与双缓冲配合后，读取不需要自旋或等待 writer 完成。
 */
bool position_observation_get_latest(PositionObservation *out_observation)
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

void position_observation_fast_tick(ControlTime fast_tick_time)
{
    HAL_StatusTypeDef status;

    if (!s_armed) {
        return;
    }

    s_acquisition_divider_count++;
    if (s_acquisition_divider_count < POSITION_ACQUISITION_DIVIDER) {
        return;
    }

    s_acquisition_divider_count = 0U;
    if (s_transfer_active) {
        return;
    }

    s_transfer_observation_time = fast_tick_time;
    s_transfer_active = true;

    HAL_GPIO_WritePin(SPI1_CSN_GPIO_Port, SPI1_CSN_Pin, GPIO_PIN_RESET);
    status = HAL_SPI_Receive_IT(&hspi1, s_rx_frame, MT6701_TRANSFER_SIZE_BYTES);
    if (status != HAL_OK) {
        HAL_GPIO_WritePin(SPI1_CSN_GPIO_Port, SPI1_CSN_Pin, GPIO_PIN_SET);
        s_transfer_active = false;
        publish_invalid_observation(fast_tick_time);
    }
}

void HAL_SPI_RxCpltCallback(SPI_HandleTypeDef *hspi)
{
    PositionObservation observation;
    uint32_t frame;
    uint32_t angle_raw;
    bool valid;

    if ((hspi != &hspi1) || !s_transfer_active) {
        return;
    }

    HAL_GPIO_WritePin(SPI1_CSN_GPIO_Port, SPI1_CSN_Pin, GPIO_PIN_SET);
    s_transfer_active = false;

    frame = assemble_frame();
    angle_raw = frame >> MT6701_ANGLE_SHIFT;
    valid = frame_is_valid(frame);

    if (valid) {
        s_referenced = true;
    }

    observation.mechanical_position_turns =
        (float)angle_raw / MT6701_ANGLE_COUNTS_PER_TURN;
    observation.observation_time = s_transfer_observation_time;
    observation.referenced = s_referenced;
    observation.acquisition_valid = valid;

    publish_observation(&observation);
}

void HAL_SPI_ErrorCallback(SPI_HandleTypeDef *hspi)
{
    ControlTime observation_time;

    if ((hspi != &hspi1) || !s_transfer_active) {
        return;
    }

    HAL_GPIO_WritePin(SPI1_CSN_GPIO_Port, SPI1_CSN_Pin, GPIO_PIN_SET);

    observation_time = s_transfer_observation_time;
    s_transfer_active = false;

    publish_invalid_observation(observation_time);
}
