#ifndef ODRIVE_V36_405_M0_REALTIME_MOTOR_IO_INTERNAL_H
#define ODRIVE_V36_405_M0_REALTIME_MOTOR_IO_INTERNAL_H

/* Target IRQ glue；不是公共 HCC，也不建立 Hardware Trip 能力。 */
void realtime_motor_io_adc_irq(void);
void realtime_motor_io_update_irq(void);

#endif
