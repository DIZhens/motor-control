#include "motor_vofa.h"

#include <string.h>

bool motor_vofa_command(const VofaCommand *source, MotorCommand *destination)
{
    MotorCommand command;

    if (source == NULL || destination == NULL) {
        return false;
    }

    command.target_velocity_rpm = 0.0f;
    switch (source->type) {
    case VOFA_COMMAND_START_VELOCITY:
        command.type = MOTOR_COMMAND_START_VELOCITY;
        command.target_velocity_rpm = source->target_velocity_rpm;
        break;
    case VOFA_COMMAND_UPDATE_VELOCITY:
        command.type = MOTOR_COMMAND_UPDATE_VELOCITY;
        command.target_velocity_rpm = source->target_velocity_rpm;
        break;
    case VOFA_COMMAND_STOP:
        command.type = MOTOR_COMMAND_STOP;
        break;
    case VOFA_COMMAND_CLEAR_FAULT:
        command.type = MOTOR_COMMAND_CLEAR_FAULT;
        break;
    default:
        return false;
    }

    *destination = command;
    return true;
}

void motor_vofa_telemetry(const MotorSnapshot *source, VofaTelemetry *destination)
{
    if (destination == NULL) {
        return;
    }
    memset(destination, 0, sizeof(*destination));
    if (source == NULL) {
        return;
    }

    destination->state = (uint32_t)source->state;
    destination->faults = source->faults;
    destination->command_result = (uint32_t)source->command_result;
    destination->target_velocity_rpm = source->target_velocity_rpm;
    destination->velocity_rpm = source->velocity_rpm;
    destination->id_a = source->id_a;
    destination->iq_a = source->iq_a;
    destination->bus_voltage_v = source->bus_voltage_v;

    if (source->status_valid) {
        destination->valid_flags |= VOFA_VALID_STATE | VOFA_VALID_FAULTS
            | VOFA_VALID_TARGET_VELOCITY | VOFA_VALID_COMMAND_RESULT;
    }
    if (source->control_time_valid && source->control_time_ticks_per_second != 0U) {
        destination->time_s = (float)source->control_time_ticks
            / (float)source->control_time_ticks_per_second;
        destination->valid_flags |= VOFA_VALID_TIME;
    }
    if (source->velocity_valid) {
        destination->valid_flags |= VOFA_VALID_VELOCITY;
    }
    if (source->id_valid) {
        destination->valid_flags |= VOFA_VALID_ID;
    }
    if (source->iq_valid) {
        destination->valid_flags |= VOFA_VALID_IQ;
    }
    if (source->bus_voltage_valid) {
        destination->valid_flags |= VOFA_VALID_BUS_VOLTAGE;
    }
}
