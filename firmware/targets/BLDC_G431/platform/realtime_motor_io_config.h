#ifndef BLDC_G431_REALTIME_MOTOR_IO_CONFIG_H
#define BLDC_G431_REALTIME_MOTOR_IO_CONFIG_H

/*
 * G431 V1 固定低边采样的首版正常工作包络。
 *
 * 按当前 ARR=4250、CCR4=4240 的中心对齐 PWM 静态名义时序推导，
 * 10%~90% duty 可让 B/C 低边采样在 sample trigger 前后保留约 2 us 量级裕量。
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

/* TIM1 与 CPU 同为 170 MHz；只在峰值后的下数半周期提交给下一谷底。
 * 为短临界区、总线写入及 UDIS 释放保留 10 us，不改变 PWM/ADC 静态配置。
 * 真实执行裕量仍须由同一构建的反汇编和无电机实测确认。 */
#define REALTIME_MOTOR_IO_TIMER_CLOCK_HZ (170000000U)
#define REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS (1700U)

#define REALTIME_MOTOR_IO_CONTROL_TIME_TICKS_PER_SECOND (20000U)
#define REALTIME_MOTOR_IO_FAST_TICK_PERIOD_TICKS (1U)

#endif /* BLDC_G431_REALTIME_MOTOR_IO_CONFIG_H */
