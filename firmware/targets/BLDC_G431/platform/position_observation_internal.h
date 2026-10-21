#ifndef BLDC_G431_POSITION_OBSERVATION_INTERNAL_H
#define BLDC_G431_POSITION_OBSERVATION_INTERNAL_H

#include "control_time.h"

/*
 * 由 G431 唯一 fast tick 驱动位置采集调度。
 * 该接口仅供当前 Target 内部组合使用，不属于 Hardware Capability Contract。
 */
void position_observation_fast_tick(ControlTime fast_tick_time);

/* SPI1 IRQ 唯一运行期入口，调用后不再执行 HAL_SPI_IRQHandler。 */
void position_observation_irq(void);

#endif /* BLDC_G431_POSITION_OBSERVATION_INTERNAL_H */
