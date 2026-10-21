/* 执行正式业务适配及既有编码器，不接入 HAL、串口或实时执行状态。 */
#include "motor_vofa.h"

#include <string.h>

VofaCommand model_command_input;
MotorCommand model_command_output;
uint32_t model_command_type;
float model_command_target;
uint32_t model_command_output_type;
float model_command_output_target;
uint32_t model_command_accepted;
uint32_t model_command_changed_on_reject;
uint64_t model_ticks;
uint32_t model_rate;
uint32_t model_validity;
uint32_t model_state;
uint32_t model_faults;
uint32_t model_result;
float model_values[5];
VofaTelemetry model_telemetry;
uint8_t model_frame[VOFA_TELEMETRY_FRAME_SIZE];
uint32_t model_encoded_size;
uint32_t model_snapshot_changed;
uint32_t model_null_failures;

void model_init(void)
{
    model_ticks = 4500U;
    model_rate = 3000U;
    model_validity = 0x3FU;
    model_state = (uint32_t)MOTOR_STATE_IDLE;
    model_faults = 5U;
    model_result = (uint32_t)MOTOR_COMMAND_RESULT_ACCEPTED;
    model_values[0] = -120.5f;
    model_values[1] = 119.25f;
    model_values[2] = -0.125f;
    model_values[3] = 2.75f;
    model_values[4] = 24.0f;
}

void model_command(void)
{
    MotorCommand before;

    model_command_input.type = (VofaCommandType)model_command_type;
    model_command_input.target_velocity_rpm = model_command_target;
    memset(&model_command_output, 0xA5, sizeof(model_command_output));
    before = model_command_output;
    model_command_accepted = (uint32_t)motor_vofa_command(
        &model_command_input, &model_command_output);
    model_command_changed_on_reject = !model_command_accepted
        && memcmp(&before, &model_command_output, sizeof(before)) != 0;
    if (model_command_accepted) {
        model_command_output_type = (uint32_t)model_command_output.type;
        model_command_output_target = model_command_output.target_velocity_rpm;
    }
}

void model_telemetry_map(void)
{
    MotorSnapshot snapshot;
    MotorSnapshot before;

    memset(&snapshot, 0, sizeof(snapshot));
    snapshot.control_time_ticks = model_ticks;
    snapshot.control_time_ticks_per_second = model_rate;
    snapshot.state = (MotorState)model_state;
    snapshot.faults = model_faults;
    snapshot.command_result = (MotorCommandResult)model_result;
    snapshot.target_velocity_rpm = model_values[0];
    snapshot.velocity_rpm = model_values[1];
    snapshot.id_a = model_values[2];
    snapshot.iq_a = model_values[3];
    snapshot.bus_voltage_v = model_values[4];
    snapshot.status_valid = (model_validity & 1U) != 0U;
    snapshot.control_time_valid = (model_validity & 2U) != 0U;
    snapshot.velocity_valid = (model_validity & 4U) != 0U;
    snapshot.id_valid = (model_validity & 8U) != 0U;
    snapshot.iq_valid = (model_validity & 16U) != 0U;
    snapshot.bus_voltage_valid = (model_validity & 32U) != 0U;
    before = snapshot;
    memset(&model_telemetry, 0xA5, sizeof(model_telemetry));
    motor_vofa_telemetry(&snapshot, &model_telemetry);
    model_snapshot_changed = memcmp(&before, &snapshot, sizeof(snapshot)) != 0;
    model_encoded_size = (uint32_t)vofa_encode_telemetry(
        &model_telemetry, model_frame, sizeof(model_frame));
}

void model_null_arguments(void)
{
    MotorCommand before;
    VofaTelemetry zero;

    memset(&model_command_output, 0xA5, sizeof(model_command_output));
    before = model_command_output;
    model_null_failures = (uint32_t)motor_vofa_command(NULL, &model_command_output);
    model_null_failures |= (uint32_t)motor_vofa_command(&model_command_input, NULL);
    model_null_failures |= (uint32_t)motor_vofa_command(NULL, NULL);
    model_null_failures |= memcmp(&before, &model_command_output, sizeof(before)) != 0;
    memset(&model_telemetry, 0xA5, sizeof(model_telemetry));
    memset(&zero, 0, sizeof(zero));
    motor_vofa_telemetry(NULL, &model_telemetry);
    model_null_failures |= memcmp(&zero, &model_telemetry, sizeof(zero)) != 0;
    motor_vofa_telemetry(NULL, NULL);
    model_telemetry_map();
    motor_vofa_telemetry(&(MotorSnapshot){0}, NULL);
}
