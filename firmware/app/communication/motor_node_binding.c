#include "protocol_device_port.h"
#include "motor_node_binding_target.h"
#include <string.h>

/* 内部枚举可独立维护，但既有线上编号不能因内部重排而悄悄改变。 */
typedef char MotorResultMappingMustMatch[
    (MOTOR_COMMAND_RESULT_NONE == CORE_MOTOR_RESULT_NONE
    && MOTOR_COMMAND_RESULT_ACCEPTED == CORE_MOTOR_RESULT_ACCEPTED
    && MOTOR_COMMAND_RESULT_NOT_READY == CORE_MOTOR_RESULT_NOT_READY
    && MOTOR_COMMAND_RESULT_BAD_TARGET == CORE_MOTOR_RESULT_BAD_TARGET
    && MOTOR_COMMAND_RESULT_WRONG_STATE == CORE_MOTOR_RESULT_WRONG_STATE
    && MOTOR_COMMAND_RESULT_FAULT_ACTIVE == CORE_MOTOR_RESULT_FAULT_ACTIVE
    && MOTOR_COMMAND_RESULT_BAD_COMMAND == CORE_MOTOR_RESULT_BAD_COMMAND) ? 1 : -1];

bool protocol_device_permitted(void) { return motor_node_binding_target_permitted(); }

void protocol_device_fail_closed(ProtocolDeviceFault reason)
{
    RuntimeInternalFaultReason mapped;
    switch (reason) {
    case PROTOCOL_FAULT_RESPONSE_OVERFLOW: mapped = RUNTIME_INTERNAL_RESPONSE_OVERFLOW; break;
    case PROTOCOL_FAULT_TRANSPORT: mapped = RUNTIME_INTERNAL_TRANSPORT; break;
    case PROTOCOL_FAULT_TIMEOUT: mapped = RUNTIME_INTERNAL_TIMEOUT; break;
    default: mapped = RUNTIME_INTERNAL_TRANSACTION_CORRUPT; break;
    }
    motor_node_binding_target_fail_closed(mapped);
}

CoreResult protocol_device_read_object(uint16_t id, MotorNodeValue *value)
{
    MotorSnapshot snapshot;
    RuntimeParameters parameters;
    const RuntimeSpeedProfile *profile;
    CoreMotorSnapshot *m;
    if (value == NULL) { return CORE_BAD_REQUEST; }
    switch (id) {
    case CORE_DEVICE_INFO:
        return motor_node_binding_target_info(&value->device_info) ? CORE_RESULT_OK : CORE_INTERNAL_ERROR;
    case CORE_MOTOR_SNAPSHOT:
        if (!motor_node_binding_target_snapshot(&snapshot)) { return CORE_INTERNAL_ERROR; }
        m = &value->motor_snapshot;
        m->control_time_ticks = snapshot.control_time_ticks;
        m->control_time_ticks_per_second = snapshot.control_time_ticks_per_second;
        m->motor_state = (uint32_t)snapshot.state; m->fault_flags = snapshot.faults;
        m->motor_command_result = (uint32_t)snapshot.command_result;
        m->valid_flags = (snapshot.status_valid ? 0x1C2U : 0U) | (snapshot.control_time_valid ? 1U : 0U)
            | (snapshot.velocity_valid ? 4U : 0U) | (snapshot.id_valid ? 8U : 0U)
            | (snapshot.iq_valid ? 16U : 0U) | (snapshot.bus_voltage_valid ? 32U : 0U);
        m->target_velocity_rpm = snapshot.target_velocity_rpm; m->velocity_rpm = snapshot.velocity_rpm;
        m->id_a = snapshot.id_a; m->iq_a = snapshot.iq_a; m->bus_voltage_v = snapshot.bus_voltage_v;
        return CORE_RESULT_OK;
    case CORE_SPEED_PI_DEFAULT: case CORE_SPEED_PI_STAGED: case CORE_SPEED_PI_ACTIVE: case CORE_TELEMETRY_CONFIG:
        if (!motor_node_binding_target_parameters(&parameters)) { return CORE_INTERNAL_ERROR; }
        if (id == CORE_TELEMETRY_CONFIG) {
            value->telemetry_config.enabled = parameters.telemetry.enabled ? 1U : 0U;
            value->telemetry_config.rate_hz = parameters.telemetry.rate_hz;
            value->telemetry_config.generation = parameters.telemetry.generation;
        } else if (id == CORE_SPEED_PI_DEFAULT) {
            value->speed_pi = (CoreSpeedPi){parameters.speed_default.kp, parameters.speed_default.ki, 0U, false};
        } else {
            profile = id == CORE_SPEED_PI_STAGED ? &parameters.speed_staged : &parameters.speed_active;
            value->speed_pi = (CoreSpeedPi){profile->value.kp, profile->value.ki, profile->generation, profile->in_use};
        }
        return CORE_RESULT_OK;
    default: return CORE_UNKNOWN_OBJECT;
    }
}

ProtocolSubmitResult protocol_device_submit(const ProtocolDeviceRequest *request,
    uint32_t *ticket, uint32_t *cancelled)
{
    RuntimeRequest mapped = {.kind = RUNTIME_REQUEST_MOTOR};
    RuntimeExchangeResult result;
    if (request == NULL || ticket == NULL || cancelled == NULL) { return PROTOCOL_SUBMIT_BAD_REQUEST; }
    switch (request->kind) {
    case PROTOCOL_REQUEST_MOTOR:
        mapped.kind = RUNTIME_REQUEST_MOTOR;
        switch (request->value.motor.command_id) {
        case CORE_START_VELOCITY: mapped.value.motor.type = MOTOR_COMMAND_START_VELOCITY; break;
        case CORE_UPDATE_VELOCITY: mapped.value.motor.type = MOTOR_COMMAND_UPDATE_VELOCITY; break;
        case CORE_STOP: mapped.value.motor.type = MOTOR_COMMAND_STOP; break;
        case CORE_CLEAR_FAULT: mapped.value.motor.type = MOTOR_COMMAND_CLEAR_FAULT; break;
        default: return PROTOCOL_SUBMIT_BAD_REQUEST;
        }
        mapped.value.motor.target_velocity_rpm = request->value.motor.target_velocity_rpm;
        break;
    case PROTOCOL_REQUEST_SPEED_PI:
        mapped.kind = RUNTIME_REQUEST_SPEED_PI;
        mapped.value.speed_pi = (RuntimeSpeedPi){request->value.speed_pi.kp, request->value.speed_pi.ki};
        break;
    case PROTOCOL_REQUEST_TELEMETRY:
        mapped.kind = RUNTIME_REQUEST_TELEMETRY;
        mapped.value.telemetry.enabled = request->value.telemetry.enabled;
        mapped.value.telemetry.rate_hz = request->value.telemetry.rate_hz;
        break;
    default: return PROTOCOL_SUBMIT_BAD_REQUEST;
    }
    result = motor_node_binding_target_submit(&mapped, ticket, cancelled);
    switch (result) {
    case RUNTIME_EXCHANGE_QUEUED: return PROTOCOL_SUBMIT_QUEUED;
    case RUNTIME_EXCHANGE_BUSY: return PROTOCOL_SUBMIT_BUSY;
    case RUNTIME_EXCHANGE_BAD_REQUEST: return PROTOCOL_SUBMIT_BAD_REQUEST;
    case RUNTIME_EXCHANGE_PRECONDITION_FAILED: return PROTOCOL_SUBMIT_PRECONDITION_FAILED;
    default: return PROTOCOL_SUBMIT_FAILED;
    }
}

bool protocol_device_completion(ProtocolDeviceCompletion *value)
{
    RuntimeCommandCompletion raw;
    ProtocolDeviceCompletion mapped = {0};
    if (value == NULL || !motor_node_binding_target_completion(&raw)) { return false; }
    mapped.ticket = raw.ticket; mapped.motor_result = CORE_MOTOR_RESULT_UNAVAILABLE;
    if (raw.outcome == RUNTIME_CANCELLED_BY_STOP) { mapped.outcome = PROTOCOL_CANCELLED_BY_STOP; }
    else if (raw.outcome == RUNTIME_PRECONDITION_FAILED) { mapped.outcome = PROTOCOL_PRECONDITION_FAILED; }
    else if (raw.outcome != RUNTIME_COMPLETED) { goto corrupt; }
    else {
        mapped.outcome = PROTOCOL_COMPLETED;
        switch (raw.kind) {
        case RUNTIME_REQUEST_MOTOR:
            if (raw.motor_result > MOTOR_COMMAND_RESULT_BAD_COMMAND) { goto corrupt; }
            mapped.kind = PROTOCOL_REQUEST_MOTOR;
            mapped.motor_result = (uint16_t)raw.motor_result;
            break;
        case RUNTIME_REQUEST_SPEED_PI: case RUNTIME_REQUEST_TELEMETRY:
            mapped.kind = raw.kind == RUNTIME_REQUEST_SPEED_PI ? PROTOCOL_REQUEST_SPEED_PI : PROTOCOL_REQUEST_TELEMETRY;
            if (raw.parameter_result == RUNTIME_PARAMETER_BAD_VALUE) { mapped.result_code = CORE_BAD_VALUE; }
            else if (raw.parameter_result == RUNTIME_PARAMETER_WRONG_STATE) { mapped.result_code = CORE_OBJECT_WRONG_STATE; }
            else if (raw.parameter_result != RUNTIME_PARAMETER_APPLIED) { goto corrupt; }
            else if (raw.kind == RUNTIME_REQUEST_SPEED_PI) {
                mapped.value.speed_pi = (CoreSpeedPi){raw.value.speed_pi.value.kp, raw.value.speed_pi.value.ki,
                    raw.value.speed_pi.generation, raw.value.speed_pi.in_use};
            } else {
                mapped.value.telemetry = (CoreTelemetryConfig){raw.value.telemetry.enabled ? 1U : 0U,
                    raw.value.telemetry.rate_hz, raw.value.telemetry.generation};
            }
            break;
        default: goto corrupt;
        }
    }
    *value = mapped;
    return true;
corrupt:
    motor_node_binding_target_fail_closed(RUNTIME_INTERNAL_TRANSACTION_CORRUPT);
    return false;
}

bool protocol_device_telemetry_peek(ProtocolTelemetryValue *value)
{
    RuntimeTelemetryValue raw;
    if (value == NULL || !motor_node_binding_target_telemetry_peek(&raw)) { return false; }
#define COPY_FIELD(name) value->sample.name = raw.sample.name
    COPY_FIELD(sample_sequence); COPY_FIELD(control_time_ticks); COPY_FIELD(position_observation_time);
    COPY_FIELD(target_velocity_rpm); COPY_FIELD(velocity_rpm); COPY_FIELD(mechanical_position_turns);
    COPY_FIELD(iq_reference_a); COPY_FIELD(iq_feedback_a); COPY_FIELD(id_feedback_a); COPY_FIELD(bus_voltage_v);
    COPY_FIELD(motor_state); COPY_FIELD(fault_flags); COPY_FIELD(valid_flags); COPY_FIELD(limit_flags);
    COPY_FIELD(active_profile_generation); COPY_FIELD(telemetry_config_generation);
#undef COPY_FIELD
    value->epoch = raw.epoch; value->produced = raw.produced; value->epoch_first = raw.epoch_first;
    return true;
}

void protocol_device_telemetry_retire(void) { motor_node_binding_target_telemetry_retire(); }
