/* 直接运行正式 Motor C；无 HAL、通信、HCC 或功率路径。 */
#include "motor.h"

#include <stddef.h>

Motor model_motor;
MotorCommand model_command;
uint32_t model_fault_flags;
uint32_t model_state;
uint32_t model_faults;
uint32_t model_result;
uint32_t model_count;
uint32_t model_returned_result;
uint32_t model_null_result;
float model_target;
float model_injected_target;

static void model_capture(void)
{
    model_state = (uint32_t)model_motor.state;
    model_faults = model_motor.faults;
    model_result = (uint32_t)model_motor.command_result;
    model_count = model_motor.command_count;
    model_target = model_motor.target_velocity_rpm;
}

void model_init(void)
{
    motor_init(&model_motor);
    model_capture();
}

void model_finish_boot(void)
{
    motor_finish_boot(&model_motor);
    model_capture();
}

void model_latch_fault(void)
{
    motor_latch_fault(&model_motor, model_fault_flags);
    model_capture();
}

void model_handle_command(void)
{
    model_returned_result = (uint32_t)motor_handle_command(&model_motor, &model_command);
    model_capture();
}

void model_null_command(void)
{
    model_returned_result = (uint32_t)motor_handle_command(&model_motor, NULL);
    model_capture();
}

void model_null_arguments(void)
{
    motor_init(NULL);
    motor_finish_boot(NULL);
    motor_latch_fault(NULL, 1U);
    model_null_result = (uint32_t)motor_handle_command(NULL, &model_command);
    model_capture();
}

void model_inject_run(void)
{
    /* 仅测试保留状态下的拒绝和 Stop 兜底，不构成业务进入 RUN 的路径。 */
    model_motor.state = MOTOR_STATE_RUN;
    model_motor.target_velocity_rpm = model_injected_target;
    model_capture();
}
