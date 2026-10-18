#ifndef MOTOR_H
#define MOTOR_H

#include <stdint.h>

/* RUN 为后续控制闭环保留；本业务骨架不具备进入 RUN 的实现。 */
typedef enum {
    MOTOR_STATE_BOOT = 0,
    MOTOR_STATE_IDLE = 1,
    MOTOR_STATE_RUN = 2,
    MOTOR_STATE_FAULT = 3
} MotorState;

/* Start 和 Update 均带有明确速度模式语义及完整目标。 */
typedef enum {
    MOTOR_COMMAND_START_VELOCITY = 0,
    MOTOR_COMMAND_UPDATE_VELOCITY,
    MOTOR_COMMAND_STOP,
    MOTOR_COMMAND_CLEAR_FAULT
} MotorCommandType;

typedef struct {
    MotorCommandType type;
    float target_velocity_rpm; /* 机械目标 rpm；仅 Start / Update 使用。 */
} MotorCommand;

/* 最近一次业务请求的结果；ACCEPTED 不等价于功率已使能。 */
typedef enum {
    MOTOR_COMMAND_RESULT_NONE = 0,
    MOTOR_COMMAND_RESULT_ACCEPTED = 1,
    MOTOR_COMMAND_RESULT_NOT_READY = 2,
    MOTOR_COMMAND_RESULT_BAD_TARGET = 3,
    MOTOR_COMMAND_RESULT_WRONG_STATE = 4,
    MOTOR_COMMAND_RESULT_FAULT_ACTIVE = 5,
    MOTOR_COMMAND_RESULT_BAD_COMMAND = 6
} MotorCommandResult;

/* 所有写入口由同一实时边界所有者串行调用；后台只发布完整请求。 */
typedef struct {
    MotorState state;
    uint32_t faults; /* 锁存故障；该骨架没有直接清除路径。 */
    MotorCommandResult command_result;
    uint32_t command_count; /* handle 调用数，包含拒绝；按 uint32_t 自然回绕。 */
    float target_velocity_rpm; /* 已接受目标；当前骨架保持安全零目标。 */
} Motor;

void motor_init(Motor *motor);
void motor_finish_boot(Motor *motor);

/* flags == 0 不产生故障；非零 flags 按位锁存，并清目标、进入 FAULT。 */
void motor_latch_fault(Motor *motor, uint32_t flags);

/* Start 尚缺控制与标定前置条件；拒绝不写目标，不会进入 RUN 或清故障。 */
MotorCommandResult motor_handle_command(Motor *motor, const MotorCommand *command);

#endif
