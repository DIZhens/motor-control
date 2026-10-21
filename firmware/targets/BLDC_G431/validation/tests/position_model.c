/* 正式 SSI backend 的 MMIO 测试入口；没有 SPI 收发替身。 */
#include "position_observation.h"
#include "position_observation_internal.h"
#include "control_time_source.h"
#include "spi.h"
#include "main.h"

SPI_HandleTypeDef hspi1 = { .Instance = SPI1 };
volatile ControlTime model_time;
volatile uint32_t model_arm_result;
volatile uint32_t model_latest_result;
volatile uint32_t model_cs_high;
volatile uint32_t model_clear_pending;
volatile uint32_t model_auto_arm = 1U;
PositionObservation model_observation;
const uint32_t model_observation_size = sizeof(PositionObservation);
const uint32_t model_spi_init_size = sizeof(hspi1.Init);

void position_model_init(void)
{
    hspi1.Init.Direction = SPI_DIRECTION_2LINES_RXONLY;
    SPI1->CR1 = SPI_CR1_MSTR | SPI_CR1_CPOL | SPI_CR1_SSM | SPI_CR1_SSI
        | SPI_CR1_RXONLY | SPI_BAUDRATEPRESCALER_16;
    SPI1->CR2 = SPI_DATASIZE_8BIT | SPI_CR2_FRXTH | SPI_CR2_NSSP;
    if (model_auto_arm != 0U) { model_arm_result = position_observation_arm(); }
}
void position_model_arm(void) { model_arm_result = position_observation_arm(); }
void position_model_tick(void) { model_time += 2U; position_observation_fast_tick(model_time); }
void position_model_irq(void) { position_observation_irq(); }
void position_model_latest(void) { model_latest_result = position_observation_get_latest(&model_observation); }
ControlTime control_time_source_now(void) { return model_time; }
HAL_SPI_StateTypeDef HAL_SPI_GetState(const SPI_HandleTypeDef *spi) { (void)spi; return HAL_SPI_STATE_READY; }
void HAL_NVIC_ClearPendingIRQ(IRQn_Type irq) { (void)irq; model_clear_pending++; }
void HAL_GPIO_WritePin(GPIO_TypeDef *port, uint16_t pin, GPIO_PinState state)
{
    model_cs_high = state == GPIO_PIN_SET;
    port->BSRR = state == GPIO_PIN_SET ? pin : (uint32_t)pin << 16U;
}
