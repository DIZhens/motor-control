#ifndef MOTOR_TELEMETRY_H
#define MOTOR_TELEMETRY_H

#include <stdint.h>

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
