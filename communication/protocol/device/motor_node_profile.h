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

/* 冻结的设备能力与有效位；数值与 Wire 合同一致，不表示当前运行许可。 */
#define CORE_CAPABILITY_OBJECT_READ (1U << 0) /* 对象读取。 */
#define CORE_CAPABILITY_FINITE_VELOCITY (1U << 1) /* 显式有限速度命令。 */
#define CORE_CAPABILITY_STAGED_SPEED_PI (1U << 2) /* 暂存速度PI。 */
#define CORE_CAPABILITY_TELEMETRY (1U << 3) /* 本合同遥测。 */
#define CORE_CAPABILITY_EVENT (1U << 4) /* 事件。 */
#define CORE_CAPABILITY_SESSIONLESS_STOP (1U << 5) /* 无会话Stop。 */
#define CORE_CAPABILITY_MASK 0x3FU
#define CORE_SNAPSHOT_VALID_TIME (1U << 0) /* 时间/频率。 */
#define CORE_SNAPSHOT_VALID_TARGET (1U << 1) /* 目标速度。 */
#define CORE_SNAPSHOT_VALID_VELOCITY (1U << 2) /* 实际速度。 */
#define CORE_SNAPSHOT_VALID_ID (1U << 3) /* Id。 */
#define CORE_SNAPSHOT_VALID_IQ (1U << 4) /* Iq。 */
#define CORE_SNAPSHOT_VALID_BUS_VOLTAGE (1U << 5) /* 母线。 */
#define CORE_SNAPSHOT_VALID_STATE (1U << 6) /* 状态。 */
#define CORE_SNAPSHOT_VALID_FAULTS (1U << 7) /* 故障。 */
#define CORE_SNAPSHOT_VALID_COMMAND_RESULT (1U << 8) /* 命令结果。 */
#define CORE_SNAPSHOT_VALID_MASK 0x1FFU
#define CORE_SNAPSHOT_STATUS_VALID (CORE_SNAPSHOT_VALID_TARGET | CORE_SNAPSHOT_VALID_STATE \
    | CORE_SNAPSHOT_VALID_FAULTS | CORE_SNAPSHOT_VALID_COMMAND_RESULT)
#define CORE_SNAPSHOT_EVENT_VALID (CORE_SNAPSHOT_VALID_TIME | CORE_SNAPSHOT_VALID_STATE \
    | CORE_SNAPSHOT_VALID_FAULTS)
#define CORE_SAMPLE_VALID_TIME (1U << 0) /* 控制时间。 */
#define CORE_SAMPLE_VALID_POSITION (1U << 1) /* 位置观测时间和机械坐标。 */
#define CORE_SAMPLE_VALID_TARGET (1U << 2) /* 目标速度。 */
#define CORE_SAMPLE_VALID_VELOCITY (1U << 3) /* 实际速度。 */
#define CORE_SAMPLE_VALID_IQ_REFERENCE (1U << 4) /* Iq参考。 */
#define CORE_SAMPLE_VALID_IQ_FEEDBACK (1U << 5) /* Iq反馈。 */
#define CORE_SAMPLE_VALID_ID_FEEDBACK (1U << 6) /* Id反馈。 */
#define CORE_SAMPLE_VALID_BUS_VOLTAGE (1U << 7) /* 母线电压。 */
#define CORE_SAMPLE_VALID_STATUS (1U << 8) /* 状态和故障。 */
#define CORE_SAMPLE_VALID_GENERATIONS (1U << 9) /* 参数及遥测代际。 */
#define CORE_SAMPLE_VALID_MASK 0x3FFU
#define CORE_SAMPLE_LIMIT_SPEED_CURRENT (1U << 0) /* 速度 PI 输出电流限幅。 */
#define CORE_SAMPLE_LIMIT_D_INTEGRAL (1U << 1) /* d轴电流 PI 积分限幅。 */
#define CORE_SAMPLE_LIMIT_Q_INTEGRAL (1U << 2) /* q轴电流 PI 积分限幅。 */
#define CORE_SAMPLE_LIMIT_VOLTAGE (1U << 3) /* 电压矢量/调制限幅。 */
#define CORE_SAMPLE_LIMIT_MASK 0x0FU

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
