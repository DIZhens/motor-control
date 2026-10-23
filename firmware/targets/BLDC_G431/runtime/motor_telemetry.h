#ifndef MOTOR_TELEMETRY_H
#define MOTOR_TELEMETRY_H

#include <stdint.h>

/* 内部观测有效位与限幅位；不把无效的数值零当作真实观测。 */
#define MOTOR_TELEMETRY_VALID_TIME (1U << 0) /* 控制时间。 */
#define MOTOR_TELEMETRY_VALID_POSITION (1U << 1) /* 位置观测时间和机械坐标。 */
#define MOTOR_TELEMETRY_VALID_TARGET (1U << 2) /* 目标速度。 */
#define MOTOR_TELEMETRY_VALID_VELOCITY (1U << 3) /* 实际速度。 */
#define MOTOR_TELEMETRY_VALID_IQ_REFERENCE (1U << 4) /* Iq参考。 */
#define MOTOR_TELEMETRY_VALID_IQ_FEEDBACK (1U << 5) /* Iq反馈。 */
#define MOTOR_TELEMETRY_VALID_ID_FEEDBACK (1U << 6) /* Id反馈。 */
#define MOTOR_TELEMETRY_VALID_BUS_VOLTAGE (1U << 7) /* 母线电压。 */
#define MOTOR_TELEMETRY_VALID_STATUS (1U << 8) /* 状态和故障。 */
#define MOTOR_TELEMETRY_VALID_GENERATIONS (1U << 9) /* 参数及遥测代际。 */
#define MOTOR_TELEMETRY_LIMIT_SPEED_CURRENT (1U << 0) /* 速度 PI 输出电流限幅。 */
#define MOTOR_TELEMETRY_LIMIT_D_INTEGRAL (1U << 1) /* d轴电流 PI 积分限幅。 */
#define MOTOR_TELEMETRY_LIMIT_Q_INTEGRAL (1U << 2) /* q轴电流 PI 积分限幅。 */
#define MOTOR_TELEMETRY_LIMIT_VOLTAGE (1U << 3) /* 电压矢量/调制限幅。 */

/* 实时侧固定观测值，非Wire帧。有效位沿用设备字段含义，协议层另行显式序列化。
 * mechanical_position_turns为编码器原点的单圈坐标，正向与速度一致，允许跨圈跳变。
 */
typedef struct {
    uint32_t sample_sequence;
    uint64_t control_time_ticks, position_observation_time;
    float target_velocity_rpm, velocity_rpm, mechanical_position_turns;
    float iq_reference_a, iq_feedback_a, id_feedback_a, bus_voltage_v;
    uint32_t motor_state, fault_flags, valid_flags;
    uint16_t limit_flags;
    uint32_t active_profile_generation, telemetry_config_generation;
} MotorTelemetrySample;

#endif
