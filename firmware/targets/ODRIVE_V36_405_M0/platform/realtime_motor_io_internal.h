#ifndef ODRIVE_V36_405_M0_REALTIME_MOTOR_IO_INTERNAL_H
#define ODRIVE_V36_405_M0_REALTIME_MOTOR_IO_INTERNAL_H

#include <stdint.h>

/*
 * Target 私有台架诊断，不参与使能判定，也不修改定时器或 actuation epoch。
 * flags 的 bit 0..6 依次为 armed、started、window、本拍 staged、committed、
 * epoch 相同、仍有提交余量；0x7f 仅表示读取时这些软件条件均满足。
 */
typedef struct {
    uint32_t capture_count;
    uint32_t flags;
    uint32_t tim1_cr1;
    uint32_t tim1_cnt;
    uint32_t tim1_bdtr;
    uint32_t enable_epoch;
    uint32_t callback_epoch;
    uint64_t boundary_count;
} RealtimeMotorIoEnableDiagnostics;

extern volatile RealtimeMotorIoEnableDiagnostics g_realtime_motor_io_enable_diagnostics;
void realtime_motor_io_capture_enable_diagnostics(void);

/* Target IRQ glue；不是公共 HCC，也不建立 Hardware Trip 能力。 */
void realtime_motor_io_adc_irq(void);
void realtime_motor_io_update_irq(void);

#endif
