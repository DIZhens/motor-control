#ifndef BLDC_G431_REALTIME_MOTOR_IO_CONFIG_H
#define BLDC_G431_REALTIME_MOTOR_IO_CONFIG_H

/*
 * G431 V1 固定低边采样的首版正常工作包络。
 *
 * 10%~90% duty 保证在当前 ARR=4250、CCR4=4240 的中心对齐 PWM 下，
 * B/C 两路低边采样在 sample trigger 前后保留约 2 us 量级的开关稳定裕量。
 * 该裕量基于静态时序与器件资料推导，仍需首板 L2 波形验证确认。
 *
 * centered SVPWM 在 |Vdq| <= 0.45 * VBUS 时，理论最坏相 duty 约落在
 * 11.0%~89.0%，因此 normal voltage utilization 取 0.45，而不贴近
 * 10%~90% stage 边界。
 */
#define REALTIME_MOTOR_IO_DUTY_MIN (0.10f)
#define REALTIME_MOTOR_IO_DUTY_MAX (0.90f)
#define REALTIME_MOTOR_IO_NORMAL_VOLTAGE_UTILIZATION (0.45f)

#define REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS (4250U)
#define REALTIME_MOTOR_IO_NEUTRAL_COMPARE_COUNTS (2125U)

#define REALTIME_MOTOR_IO_CONTROL_TIME_TICKS_PER_SECOND (20000U)
#define REALTIME_MOTOR_IO_FAST_TICK_PERIOD_TICKS (1U)

#endif /* BLDC_G431_REALTIME_MOTOR_IO_CONFIG_H */
