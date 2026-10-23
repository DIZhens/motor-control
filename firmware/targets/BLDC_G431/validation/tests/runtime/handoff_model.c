/* 只包装真实交接 API，供 ARM 模型直接检查；不进入固件工程。 */
#if defined(BLDC_G431_FINITE_RUN)
#include "target_runtime.h"
#else
#include "target_observe_runtime.h"
#endif

#include <stddef.h>
#include "legacy_command.h"

uint32_t model_handoff_command_type;
float model_handoff_target;
uint32_t model_handoff_null;
uint32_t model_handoff_submit_result;
uint32_t model_handoff_read_result;
MotorCommand model_handoff_command;
MotorSnapshot model_handoff_snapshot;
const uint32_t model_handoff_snapshot_size = sizeof(MotorSnapshot);
const uint32_t model_handoff_snapshot_offsets[] = {
    offsetof(MotorSnapshot, state),
    offsetof(MotorSnapshot, faults),
    offsetof(MotorSnapshot, status_valid),
    offsetof(MotorSnapshot, control_time_valid),
    offsetof(MotorSnapshot, velocity_valid),
    offsetof(MotorSnapshot, id_valid),
    offsetof(MotorSnapshot, iq_valid),
    offsetof(MotorSnapshot, bus_voltage_valid)
};

void model_handoff_submit(void)
{
    model_handoff_command.type = (MotorCommandType)model_handoff_command_type;
    model_handoff_command.target_velocity_rpm = model_handoff_target;
    model_handoff_submit_result = (uint32_t)target_runtime_submit_command(
        model_handoff_null != 0U ? NULL : &model_handoff_command);
}

void model_handoff_read(void)
{
    model_handoff_read_result = target_runtime_read_snapshot(
        model_handoff_null != 0U ? NULL : &model_handoff_snapshot) ? 1U : 0U;
}

void model_handoff_change_source(void)
{
    /* 提交后修改源对象，验证邮箱拥有完整副本而不是保留调用者指针。 */
    model_handoff_command.target_velocity_rpm = model_handoff_target;
}
