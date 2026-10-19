#ifndef MOTOR_H
#define MOTOR_H

#include <stdint.h>
#include <stdbool.h>

/* RUN 由内部 Runtime 在受控使能成功后提交；默认观察装配仍拒绝启动。 */
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

/* 携带明确速度模式与完整目标的 Motor 业务请求。 */
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
    float target_velocity_rpm; /* 当前 RUN 的完整目标；尚在启动准备时仍为零。 */
} Motor;

void motor_init(Motor *motor);
void motor_finish_boot(Motor *motor);

/* flags == 0 不产生故障；非零 flags 按位锁存，并清目标、进入 FAULT。 */
void motor_latch_fault(Motor *motor, uint32_t flags);

/* 默认观察入口；不授予控制许可，Start 返回 NOT_READY。 */
MotorCommandResult motor_handle_command(Motor *motor, const MotorCommand *command);

/*
 * 仅由 Motor 内部 Runtime 调用。start_ready 是当拍反馈/标定/时序许可，
 * max_velocity_rpm 是本装配的有限正目标上限；不合法上限表示尚未就绪。
 * Start 接受后仍保持 IDLE 和零活动目标，Runtime 独占保存完整待启动请求。
 * Update 仅在 RUN 接受，普通拒绝保留已有目标；此函数不操作功率或清故障。
 */
MotorCommandResult motor_handle_control_command(Motor *motor, const MotorCommand *command,
    bool start_ready, float max_velocity_rpm);

/*
 * Runtime 在本次 Start 的 fresh actuation 已跨边界且 enable 成功后提交 RUN。
 * 不新增一次外部命令，不代表之后持续功率状态；失败时 Runtime 必须关断并锁故障。
 */
bool motor_enter_velocity_run(Motor *motor, float target_velocity_rpm, float max_velocity_rpm);

#endif
