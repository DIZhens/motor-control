#ifndef MOTOR_NODE_PROFILE_H
#define MOTOR_NODE_PROFILE_H

#include <stdbool.h>
#include <stdint.h>
#include "protocol_result.h"

/* 历史CORE_*业务编号保持稳定；共享设备值不含UART Header或CAN ID/DLC。 */
#define CORE_MOTOR_RESULT_UNAVAILABLE 0xFFFFU

/* 线上Motor结果值；Firmware绑定核对内部枚举，不把Motor类型泄漏给协议。 */
typedef enum {
    CORE_MOTOR_RESULT_NONE = 0, CORE_MOTOR_RESULT_ACCEPTED = 1,
    CORE_MOTOR_RESULT_NOT_READY = 2, CORE_MOTOR_RESULT_BAD_TARGET = 3,
    CORE_MOTOR_RESULT_WRONG_STATE = 4, CORE_MOTOR_RESULT_FAULT_ACTIVE = 5,
    CORE_MOTOR_RESULT_BAD_COMMAND = 6
} CoreMotorResult;

enum {
    CORE_DEVICE_INFO = 0x0001, CORE_MOTOR_SNAPSHOT = 0x0002,
    CORE_COMMUNICATION_STATUS = 0x0003, CORE_SPEED_PI_DEFAULT = 0x0101,
    CORE_SPEED_PI_STAGED = 0x0102, CORE_SPEED_PI_ACTIVE = 0x0103,
    CORE_TELEMETRY_CONFIG = 0x0201
};
enum {
    CORE_START_VELOCITY = 1, CORE_UPDATE_VELOCITY = 2,
    CORE_STOP = 3, CORE_CLEAR_FAULT = 4
};

/* 只读设备能力与构建身份，不表示本次具备启动许可。 */
typedef struct {
    uint16_t protocol_version, device_contract_version, target_id, control_profile_id;
    uint32_t capability_flags, control_time_ticks_per_second;
    uint64_t firmware_build_id;
} CoreDeviceInfo;

/* 一致设备观测；各数值是否可用由valid_flags决定。 */
typedef struct {
    uint64_t control_time_ticks;
    uint32_t control_time_ticks_per_second, motor_state, fault_flags;
    uint32_t motor_command_result, valid_flags;
    float target_velocity_rpm, velocity_rpm, id_a, iq_a, bus_voltage_v;
} CoreMotorSnapshot;

/* 速度PI权威值、修订与Active使用状态；不持有控制积分。 */
typedef struct { float kp, ki; uint32_t generation; bool currently_in_use; } CoreSpeedPi;
/* 后台遥测配置的完整值；generation仅在本启动内表示修订。 */
typedef struct { uint8_t enabled; uint16_t rate_hz; uint32_t generation; } CoreTelemetryConfig;

/* 速度调试publication的物理值；具体链路决定发送哪些字段及其表示。 */
typedef struct {
    uint32_t sample_sequence;
    uint64_t control_time_ticks, position_observation_time;
    float target_velocity_rpm, velocity_rpm, mechanical_position_turns;
    float iq_reference_a, iq_feedback_a, id_feedback_a, bus_voltage_v;
    uint32_t motor_state, fault_flags, valid_flags;
    uint16_t limit_flags;
    uint32_t active_profile_generation, telemetry_config_generation;
} CoreSpeedSample;

/* 设备publication的值容器；COMMUNICATION_STATUS属于UART自身，不进入共享对象。 */
typedef union {
    CoreDeviceInfo device_info;
    CoreMotorSnapshot motor_snapshot;
    CoreSpeedPi speed_pi;
    CoreTelemetryConfig telemetry_config;
} MotorNodeValue;

#endif
