#ifndef BLDC_G431_REALTIME_MOTOR_IO_DIAGNOSTIC_H
#define BLDC_G431_REALTIME_MOTOR_IO_DIAGNOSTIC_H

#include <stdint.h>

/* Target 私有计时入口；仅在同步 ADC consumer 内读取本拍原有锚点。
 * 不创建窗口、不延长截止、不改变 DWT 或定时器。两个输出必须非空。 */
void realtime_motor_io_stage_anchor(uint32_t *cycles, uint32_t *entry_count);

#endif
