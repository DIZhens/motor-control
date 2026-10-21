#ifndef BLDC_G431_REALTIME_MOTOR_IO_INTERNAL_H
#define BLDC_G431_REALTIME_MOTOR_IO_INTERNAL_H

#include <stdint.h>

/* TIM1 UP / Break 分发中先处理 pending UIF；必须高于 ADC IRQ 优先级。 */
void realtime_motor_io_update_irq(void);

/* injected busy 覆盖对应完整 HAL IRQ 及诊断尾部；regular-only 不占 sample slot。
 * enter 在 HAL 之前，exit 在所有尾部工作之后。 */
void realtime_motor_io_adc_irq_enter(void);
void realtime_motor_io_adc_irq_exit(void);

/* 当前 consumer 所属 slot 的绝对 DWT stage 截止；不创建或延长窗口。 */
void realtime_motor_io_stage_deadline(uint32_t *deadline_cycles);

#endif /* BLDC_G431_REALTIME_MOTOR_IO_INTERNAL_H */
