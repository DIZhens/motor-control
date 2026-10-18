#include "motor.h"

#include <float.h>
#include <stdbool.h>
#include <stddef.h>
#include <string.h>

#if FLT_RADIX != 2 || FLT_MANT_DIG != 24 || FLT_MAX_EXP != 128
#error "Motor target validation requires IEEE 754 binary32 float"
#endif

static bool motor_target_is_finite(float target)
{
    uint32_t bits;

    memcpy(&bits, &target, sizeof(bits));
    return (bits & 0x7F800000UL) != 0x7F800000UL;
}

void motor_init(Motor *motor)
{
    if (motor == NULL) {
        return;
    }
    motor->state = MOTOR_STATE_BOOT;
    motor->faults = 0U;
    motor->command_result = MOTOR_COMMAND_RESULT_NONE;
    motor->command_count = 0U;
    motor->target_velocity_rpm = 0.0f;
}

void motor_finish_boot(Motor *motor)
{
    if (motor != NULL && motor->state == MOTOR_STATE_BOOT && motor->faults == 0U) {
        motor->state = MOTOR_STATE_IDLE;
    }
}

void motor_latch_fault(Motor *motor, uint32_t flags)
{
    if (motor == NULL || flags == 0U) {
        return;
    }
    motor->faults |= flags;
    motor->target_velocity_rpm = 0.0f;
    motor->state = MOTOR_STATE_FAULT;
}

static MotorCommandResult motor_apply_command(Motor *motor, const MotorCommand *command)
{
    if (command == NULL) {
        return MOTOR_COMMAND_RESULT_BAD_COMMAND;
    }
    switch (command->type) {
    case MOTOR_COMMAND_START_VELOCITY:
    case MOTOR_COMMAND_UPDATE_VELOCITY:
        if (!motor_target_is_finite(command->target_velocity_rpm)) {
            return MOTOR_COMMAND_RESULT_BAD_TARGET;
        }
        if (motor->faults != 0U || motor->state == MOTOR_STATE_FAULT) {
            return MOTOR_COMMAND_RESULT_FAULT_ACTIVE;
        }
        if (command->type == MOTOR_COMMAND_START_VELOCITY) {
            if (motor->state != MOTOR_STATE_IDLE) {
                return MOTOR_COMMAND_RESULT_WRONG_STATE;
            }
        } else if (motor->state != MOTOR_STATE_RUN) {
            return MOTOR_COMMAND_RESULT_WRONG_STATE;
        }
        /* 尚无控制计算、反馈门控和标定流程，不能伪造接受或进入 RUN。 */
        return MOTOR_COMMAND_RESULT_NOT_READY;

    case MOTOR_COMMAND_STOP:
        motor->target_velocity_rpm = 0.0f;
        if (motor->state == MOTOR_STATE_RUN) {
            motor->state = motor->faults == 0U ? MOTOR_STATE_IDLE : MOTOR_STATE_FAULT;
        }
        return MOTOR_COMMAND_RESULT_ACCEPTED;

    case MOTOR_COMMAND_CLEAR_FAULT:
        if (motor->faults != 0U || motor->state == MOTOR_STATE_FAULT) {
            /* 故障恢复尚未实现，不能普通清零后报告恢复成功。 */
            return MOTOR_COMMAND_RESULT_NOT_READY;
        }
        if (motor->state != MOTOR_STATE_IDLE) {
            return MOTOR_COMMAND_RESULT_WRONG_STATE;
        }
        return MOTOR_COMMAND_RESULT_ACCEPTED;

    default:
        return MOTOR_COMMAND_RESULT_BAD_COMMAND;
    }
}

MotorCommandResult motor_handle_command(Motor *motor, const MotorCommand *command)
{
    if (motor == NULL) {
        return MOTOR_COMMAND_RESULT_BAD_COMMAND;
    }
    motor->command_count++;
    motor->command_result = motor_apply_command(motor, command);
    return motor->command_result;
}
