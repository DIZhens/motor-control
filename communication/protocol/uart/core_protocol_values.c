#include "core_protocol_values.h"

#include <float.h>
#include <string.h>

#if FLT_RADIX != 2 || FLT_MANT_DIG != 24 || FLT_MAX_EXP != 128
#error Core_Protocol_requires_IEEE754_binary32
#endif
typedef char CoreFloatSize[(sizeof(float) == 4U) ? 1 : -1];

static uint16_t r16(const uint8_t *p) { return (uint16_t)(p[0] | ((uint16_t)p[1] << 8)); }
static uint32_t r32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static uint64_t r64(const uint8_t *p) { return (uint64_t)r32(p) | ((uint64_t)r32(p + 4U) << 32); }
static void w16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void w32(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); p[2] = (uint8_t)(v >> 16); p[3] = (uint8_t)(v >> 24);
}
static void w64(uint8_t *p, uint64_t v) { w32(p, (uint32_t)v); w32(p + 4U, (uint32_t)(v >> 32)); }
static uint32_t bits(float v) { uint32_t b; memcpy(&b, &v, 4U); return b; }
static float rf(const uint8_t *p) { float v; uint32_t b = r32(p); memcpy(&v, &b, 4U); return v; }
static void wf(uint8_t *p, float v) { w32(p, bits(v)); }
static bool finite(float v) { return (bits(v) & 0x7F800000U) != 0x7F800000U; }

static void observation(uint8_t *p, float value, uint32_t bit, uint32_t *valid)
{
    if ((*valid & bit) == 0U || !finite(value)) { *valid &= ~bit; w32(p, 0x7FC00000U); }
    else { wf(p, value); }
}

static bool observation_valid(const uint8_t *p, uint32_t bit, uint32_t valid)
{
    return (valid & bit) != 0U ? finite(rf(p)) : r32(p) == 0x7FC00000U;
}

size_t core_object_size(uint16_t id, bool write_value)
{
    if (write_value) {
        return id == CORE_SPEED_PI_STAGED ? 8U : (id == CORE_TELEMETRY_CONFIG ? 4U : 0U);
    }
    switch (id) {
    case CORE_DEVICE_INFO: return 24U;
    case CORE_MOTOR_SNAPSHOT: return 48U;
    case CORE_COMMUNICATION_STATUS: return 104U;
    case CORE_SPEED_PI_DEFAULT: return 8U;
    case CORE_SPEED_PI_STAGED: return 12U;
    case CORE_SPEED_PI_ACTIVE: return 16U;
    case CORE_TELEMETRY_CONFIG: return 8U;
    default: return 0U;
    }
}

static CoreResult object_check(uint16_t id, bool write_value, size_t length)
{
    size_t size = core_object_size(id, false);
    if (size == 0U) { return CORE_UNKNOWN_OBJECT; }
    if (write_value && core_object_size(id, true) == 0U) { return CORE_READ_ONLY; }
    return length == core_object_size(id, write_value) ? CORE_RESULT_OK : CORE_LENGTH_MISMATCH;
}

CoreResult core_object_decode(uint16_t id, bool write_value,
    const uint8_t *p, size_t length, CoreObject *value)
{
    CoreObject v;
    CoreMotorSnapshot *m = &v.motor_snapshot;
    CoreResult result = object_check(id, write_value, length);
    uint32_t valid;
    size_t i;
    if (result != CORE_RESULT_OK) { return result; }
    if (p == NULL || value == NULL) { return CORE_BAD_PAYLOAD; }
    memset(&v, 0, sizeof(v));
    switch (id) {
    case CORE_DEVICE_INFO:
        v.device_info.protocol_version = r16(p); v.device_info.device_contract_version = r16(p + 2U);
        v.device_info.target_id = r16(p + 4U); v.device_info.control_profile_id = r16(p + 6U);
        v.device_info.capability_flags = r32(p + 8U);
        v.device_info.control_time_ticks_per_second = r32(p + 12U);
        v.device_info.firmware_build_id = r64(p + 16U);
        if ((v.device_info.capability_flags & ~CORE_CAPABILITY_MASK) != 0U) { return CORE_BAD_VALUE; }
        break;
    case CORE_MOTOR_SNAPSHOT:
        m->control_time_ticks = r64(p); m->control_time_ticks_per_second = r32(p + 8U);
        m->motor_state = r32(p + 12U); m->fault_flags = r32(p + 16U);
        m->motor_command_result = r32(p + 20U); valid = r32(p + 24U); m->valid_flags = valid;
        if ((valid & ~CORE_SNAPSHOT_VALID_MASK) != 0U) { return CORE_BAD_VALUE; }
        for (i = 0U; i < 5U; ++i) {
            if (!observation_valid(p + 28U + 4U * i, 1U << (i + 1U), valid)) { return CORE_BAD_VALUE; }
        }
        m->target_velocity_rpm = rf(p + 28U); m->velocity_rpm = rf(p + 32U);
        m->id_a = rf(p + 36U); m->iq_a = rf(p + 40U); m->bus_voltage_v = rf(p + 44U);
        break;
    case CORE_COMMUNICATION_STATUS:
        v.communication_status.session_id = r32(p + 0U);
        v.communication_status.rx_bytes = r32(p + 4U);
        v.communication_status.tx_bytes = r32(p + 8U);
        v.communication_status.event_drop_count = r32(p + 12U);
        v.communication_status.rx_discontinuities = r32(p + 16U);
        v.communication_status.rx_dma_overruns = r32(p + 20U);
        v.communication_status.uart_pe = r32(p + 24U);
        v.communication_status.uart_fe = r32(p + 28U);
        v.communication_status.uart_ne = r32(p + 32U);
        v.communication_status.uart_ore = r32(p + 36U);
        v.communication_status.cobs_errors = r32(p + 40U);
        v.communication_status.crc_errors = r32(p + 44U);
        v.communication_status.length_errors = r32(p + 48U);
        v.communication_status.version_errors = r32(p + 52U);
        v.communication_status.message_errors = r32(p + 56U);
        v.communication_status.parser_timeouts = r32(p + 60U);
        v.communication_status.response_fifo_high_water = r32(p + 64U);
        v.communication_status.completion_fifo_high_water = r32(p + 68U);
        v.communication_status.event_fifo_high_water = r32(p + 72U);
        v.communication_status.telemetry_overwrites = r32(p + 76U);
        v.communication_status.communication_stack_min_words = r32(p + 80U);
        v.communication_status.telemetry_stack_min_words = r32(p + 84U);
        v.communication_status.internal_fault_reason = r32(p + 88U);
        v.communication_status.internal_fault_count = r32(p + 92U);
        v.communication_status.tx_busy_count = r32(p + 96U);
        v.communication_status.tx_errors = r32(p + 100U);
        break;
    case CORE_SPEED_PI_DEFAULT: case CORE_SPEED_PI_STAGED: case CORE_SPEED_PI_ACTIVE:
        v.speed_pi.kp = rf(p); v.speed_pi.ki = rf(p + 4U);
        if (!finite(v.speed_pi.kp) || !finite(v.speed_pi.ki) ||
            v.speed_pi.kp < 0.0005F || v.speed_pi.kp > 0.005F ||
            v.speed_pi.ki < 0.0F || v.speed_pi.ki > 0.02F) { return CORE_BAD_VALUE; }
        if (!write_value && id != CORE_SPEED_PI_DEFAULT) { v.speed_pi.generation = r32(p + 8U); }
        if (id == CORE_SPEED_PI_ACTIVE) {
            if (p[12] > 1U || p[13] != 0U || p[14] != 0U || p[15] != 0U) { return CORE_BAD_PAYLOAD; }
            v.speed_pi.currently_in_use = p[12] != 0U;
        }
        break;
    case CORE_TELEMETRY_CONFIG:
        if (p[1] != 0U) { return CORE_BAD_PAYLOAD; }
        v.telemetry_config.enabled = p[0]; v.telemetry_config.rate_hz = r16(p + 2U);
        if (p[0] > 1U || (r16(p + 2U) != 50U && r16(p + 2U) != 250U && r16(p + 2U) != 500U)) {
            return CORE_BAD_VALUE;
        }
        if (!write_value) { v.telemetry_config.generation = r32(p + 4U); }
        break;
    default: return CORE_UNKNOWN_OBJECT;
    }
    *value = v;
    return CORE_RESULT_OK;
}

CoreResult core_object_encode(uint16_t id, bool write_value, const CoreObject *v,
    uint8_t *output, size_t capacity, size_t *length)
{
    uint8_t p[104];
    CoreObject check;
    size_t size = core_object_size(id, write_value);
    uint32_t valid;
    const CoreMotorSnapshot *m;
    CoreResult result = object_check(id, write_value, size);
    if (result != CORE_RESULT_OK) { return result; }
    if (v == NULL || output == NULL || length == NULL) { return CORE_BAD_PAYLOAD; }
    if (capacity < size) { return CORE_LENGTH_MISMATCH; }
    memset(p, 0, sizeof(p));
    switch (id) {
    case CORE_DEVICE_INFO:
        w16(p, v->device_info.protocol_version); w16(p + 2U, v->device_info.device_contract_version);
        w16(p + 4U, v->device_info.target_id); w16(p + 6U, v->device_info.control_profile_id);
        w32(p + 8U, v->device_info.capability_flags);
        w32(p + 12U, v->device_info.control_time_ticks_per_second);
        w64(p + 16U, v->device_info.firmware_build_id); break;
    case CORE_MOTOR_SNAPSHOT:
        m = &v->motor_snapshot; valid = m->valid_flags & CORE_SNAPSHOT_VALID_MASK;
        w64(p, m->control_time_ticks); w32(p + 8U, m->control_time_ticks_per_second);
        w32(p + 12U, m->motor_state); w32(p + 16U, m->fault_flags);
        w32(p + 20U, m->motor_command_result);
        observation(p + 28U, m->target_velocity_rpm, CORE_SNAPSHOT_VALID_TARGET, &valid);
        observation(p + 32U, m->velocity_rpm, CORE_SNAPSHOT_VALID_VELOCITY, &valid);
        observation(p + 36U, m->id_a, CORE_SNAPSHOT_VALID_ID, &valid);
        observation(p + 40U, m->iq_a, CORE_SNAPSHOT_VALID_IQ, &valid);
        observation(p + 44U, m->bus_voltage_v, CORE_SNAPSHOT_VALID_BUS_VOLTAGE, &valid);
        w32(p + 24U, valid); break;
    case CORE_COMMUNICATION_STATUS:
        w32(p + 0U, v->communication_status.session_id);
        w32(p + 4U, v->communication_status.rx_bytes);
        w32(p + 8U, v->communication_status.tx_bytes);
        w32(p + 12U, v->communication_status.event_drop_count);
        w32(p + 16U, v->communication_status.rx_discontinuities);
        w32(p + 20U, v->communication_status.rx_dma_overruns);
        w32(p + 24U, v->communication_status.uart_pe);
        w32(p + 28U, v->communication_status.uart_fe);
        w32(p + 32U, v->communication_status.uart_ne);
        w32(p + 36U, v->communication_status.uart_ore);
        w32(p + 40U, v->communication_status.cobs_errors);
        w32(p + 44U, v->communication_status.crc_errors);
        w32(p + 48U, v->communication_status.length_errors);
        w32(p + 52U, v->communication_status.version_errors);
        w32(p + 56U, v->communication_status.message_errors);
        w32(p + 60U, v->communication_status.parser_timeouts);
        w32(p + 64U, v->communication_status.response_fifo_high_water);
        w32(p + 68U, v->communication_status.completion_fifo_high_water);
        w32(p + 72U, v->communication_status.event_fifo_high_water);
        w32(p + 76U, v->communication_status.telemetry_overwrites);
        w32(p + 80U, v->communication_status.communication_stack_min_words);
        w32(p + 84U, v->communication_status.telemetry_stack_min_words);
        w32(p + 88U, v->communication_status.internal_fault_reason);
        w32(p + 92U, v->communication_status.internal_fault_count);
        w32(p + 96U, v->communication_status.tx_busy_count);
        w32(p + 100U, v->communication_status.tx_errors);
        break;
    case CORE_SPEED_PI_DEFAULT: case CORE_SPEED_PI_STAGED: case CORE_SPEED_PI_ACTIVE:
        wf(p, v->speed_pi.kp); wf(p + 4U, v->speed_pi.ki);
        if (!write_value && id != CORE_SPEED_PI_DEFAULT) { w32(p + 8U, v->speed_pi.generation); }
        if (id == CORE_SPEED_PI_ACTIVE) { p[12] = v->speed_pi.currently_in_use ? 1U : 0U; } break;
    case CORE_TELEMETRY_CONFIG:
        p[0] = v->telemetry_config.enabled; w16(p + 2U, v->telemetry_config.rate_hz);
        if (!write_value) { w32(p + 4U, v->telemetry_config.generation); } break;
    default: return CORE_UNKNOWN_OBJECT;
    }
    result = core_object_decode(id, write_value, p, size, &check);
    if (result != CORE_RESULT_OK) { return result; }
    memcpy(output, p, size); *length = size;
    return CORE_RESULT_OK;
}

CoreResult core_request_decode(const CoreFrame *frame, CoreRequest *request)
{
    const uint8_t *p;
    size_t n;
    if (frame == NULL || request == NULL) { return CORE_BAD_PAYLOAD; }
    memset(request, 0, sizeof(*request));
    request->message_id = frame->message_id;
    if (frame->version != 1U) { return CORE_UNSUPPORTED_VERSION; }
    if (frame->flags != 0U) { return CORE_UNSUPPORTED_FLAGS; }
    if (frame->header_size != CORE_HEADER_SIZE || frame->kind != CORE_REQUEST ||
        frame->payload_length > CORE_PAYLOAD_MAX) { return CORE_BAD_REQUEST; }
    p = frame->payload; n = frame->payload_length;
    switch (frame->message_id) {
    case CORE_OPEN_SESSION:
        if (n != 4U) { return CORE_BAD_PAYLOAD; }
        request->proposed_session_id = r32(p);
        return r32(p) != 0U ? CORE_RESULT_OK : CORE_BAD_VALUE;
    case CORE_READ_OBJECT: case CORE_WRITE_OBJECT: case CORE_EXECUTE_COMMAND:
        if (n < 2U) { return CORE_BAD_PAYLOAD; }
        request->subject_id = r16(p); break;
    default: return CORE_UNKNOWN_MESSAGE;
    }
    if (frame->message_id == CORE_READ_OBJECT) {
        if (n != 2U) { return CORE_BAD_PAYLOAD; }
        return core_object_size(request->subject_id, false) != 0U ? CORE_RESULT_OK : CORE_UNKNOWN_OBJECT;
    }
    if (frame->message_id == CORE_WRITE_OBJECT) {
        return core_object_decode(request->subject_id, true, p + 2U, n - 2U, &request->value);
    }
    switch (request->subject_id) {
    case CORE_START_VELOCITY: case CORE_UPDATE_VELOCITY:
        if (n != 6U) { return CORE_BAD_PAYLOAD; }
        request->target_velocity_rpm = rf(p + 2U);
        return finite(request->target_velocity_rpm) && request->target_velocity_rpm >= -80.0F &&
            request->target_velocity_rpm <= 80.0F ? CORE_RESULT_OK : CORE_BAD_VALUE;
    case CORE_STOP: case CORE_CLEAR_FAULT: return n == 2U ? CORE_RESULT_OK : CORE_BAD_PAYLOAD;
    default: return CORE_UNKNOWN_COMMAND;
    }
}

CoreResult core_request_encode(const CoreRequest *request, uint8_t *output,
    size_t capacity, size_t *length)
{
    CoreFrame frame;
    CoreRequest check;
    CoreResult result;
    size_t n = 0U;
    if (request == NULL || output == NULL || length == NULL) { return CORE_BAD_PAYLOAD; }
    memset(&frame, 0, sizeof(frame));
    frame.version = 1U; frame.kind = CORE_REQUEST; frame.header_size = CORE_HEADER_SIZE;
    frame.message_id = request->message_id;
    if (request->message_id == CORE_OPEN_SESSION) {
        w32(frame.payload, request->proposed_session_id); n = 4U;
    } else {
        w16(frame.payload, request->subject_id); n = 2U;
        if (request->message_id == CORE_WRITE_OBJECT) {
            result = core_object_encode(request->subject_id, true, &request->value,
                frame.payload + 2U, CORE_PAYLOAD_MAX - 2U, &n);
            if (result != CORE_RESULT_OK) { return result; }
            n += 2U;
        } else if (request->message_id == CORE_EXECUTE_COMMAND &&
            (request->subject_id == CORE_START_VELOCITY || request->subject_id == CORE_UPDATE_VELOCITY)) {
            wf(frame.payload + 2U, request->target_velocity_rpm); n = 6U;
        }
    }
    frame.payload_length = (uint16_t)n;
    result = core_request_decode(&frame, &check);
    if (result != CORE_RESULT_OK) { return result; }
    if (capacity < n) { return CORE_LENGTH_MISMATCH; }
    memcpy(output, frame.payload, n); *length = n;
    return CORE_RESULT_OK;
}

CoreResult core_response_decode(uint16_t id, const uint8_t *p, size_t n, CoreResponse *response)
{
    CoreResponse v;
    CoreResult result;
    if (p == NULL || response == NULL || n < 4U) { return CORE_BAD_PAYLOAD; }
    memset(&v, 0, sizeof(v));
    v.result_code = r16(p); v.subject_id = r16(p + 2U); v.motor_result = 0xFFFFU;
    if (id == CORE_OPEN_SESSION && v.subject_id != 0U) { return CORE_BAD_PAYLOAD; }
    if (id == CORE_EXECUTE_COMMAND) {
        if (n != 8U || r16(p + 6U) != 0U) { return CORE_BAD_PAYLOAD; }
        v.motor_result = r16(p + 4U);
        if ((v.result_code != 0U && v.motor_result != 0xFFFFU) ||
            (v.result_code == 0U && v.motor_result > 6U)) { return CORE_BAD_VALUE; }
    } else if (v.result_code != 0U) {
        if (n != 4U) { return CORE_BAD_PAYLOAD; }
    } else if (id == CORE_OPEN_SESSION) {
        if (n != 8U) { return CORE_BAD_PAYLOAD; }
        v.accepted_session_id = r32(p + 4U);
        if (v.accepted_session_id == 0U) { return CORE_BAD_VALUE; }
    } else if (id == CORE_READ_OBJECT || id == CORE_WRITE_OBJECT) {
        result = core_object_decode(v.subject_id, false, p + 4U, n - 4U, &v.value);
        if (result != CORE_RESULT_OK) { return result; }
    } else { return CORE_UNKNOWN_MESSAGE; }
    *response = v;
    return CORE_RESULT_OK;
}

CoreResult core_response_encode(uint16_t id, const CoreResponse *response,
    uint8_t *output, size_t capacity, size_t *length)
{
    uint8_t p[CORE_PAYLOAD_MAX];
    size_t n = 4U, body;
    CoreResponse check;
    CoreResult result;
    if (response == NULL || output == NULL || length == NULL) { return CORE_BAD_PAYLOAD; }
    w16(p, response->result_code); w16(p + 2U, response->subject_id);
    if (id == CORE_EXECUTE_COMMAND) {
        w16(p + 4U, response->motor_result); w16(p + 6U, 0U); n = 8U;
    } else if (response->result_code == 0U) {
        if (id == CORE_OPEN_SESSION) { w32(p + 4U, response->accepted_session_id); n = 8U; }
        else if (id == CORE_READ_OBJECT || id == CORE_WRITE_OBJECT) {
            result = core_object_encode(response->subject_id, false, &response->value,
                p + 4U, sizeof(p) - 4U, &body);
            if (result != CORE_RESULT_OK) { return result; }
            n += body;
        } else { return CORE_UNKNOWN_MESSAGE; }
    }
    result = core_response_decode(id, p, n, &check);
    if (result != CORE_RESULT_OK) { return result; }
    if (capacity < n) { return CORE_LENGTH_MISMATCH; }
    memcpy(output, p, n); *length = n;
    return CORE_RESULT_OK;
}

CoreResult core_sample_encode(const CoreSpeedSample *v, uint8_t *output, size_t capacity, size_t *length)
{
    uint8_t p[72];
    uint32_t valid;
    if (v == NULL || output == NULL || length == NULL) { return CORE_BAD_PAYLOAD; }
    if (capacity < sizeof(p)) { return CORE_LENGTH_MISMATCH; }
    valid = v->valid_flags & CORE_SAMPLE_VALID_MASK;
    memset(p, 0, sizeof(p));
    w32(p, v->sample_sequence);
    w64(p + 4U, (valid & CORE_SAMPLE_VALID_TIME) != 0U ? v->control_time_ticks : 0U);
    w64(p + 12U, (valid & CORE_SAMPLE_VALID_POSITION) != 0U ? v->position_observation_time : 0U);
    observation(p + 20U, v->target_velocity_rpm, CORE_SAMPLE_VALID_TARGET, &valid);
    observation(p + 24U, v->velocity_rpm, CORE_SAMPLE_VALID_VELOCITY, &valid);
    observation(p + 28U, v->mechanical_position_turns, CORE_SAMPLE_VALID_POSITION, &valid);
    if ((valid & CORE_SAMPLE_VALID_POSITION) == 0U) { w64(p + 12U, 0U); }
    observation(p + 32U, v->iq_reference_a, CORE_SAMPLE_VALID_IQ_REFERENCE, &valid);
    observation(p + 36U, v->iq_feedback_a, CORE_SAMPLE_VALID_IQ_FEEDBACK, &valid);
    observation(p + 40U, v->id_feedback_a, CORE_SAMPLE_VALID_ID_FEEDBACK, &valid);
    observation(p + 44U, v->bus_voltage_v, CORE_SAMPLE_VALID_BUS_VOLTAGE, &valid);
    if ((valid & CORE_SAMPLE_VALID_STATUS) != 0U) { w32(p + 48U, v->motor_state); w32(p + 52U, v->fault_flags); }
    w32(p + 56U, valid); w16(p + 60U, (uint16_t)(v->limit_flags & CORE_SAMPLE_LIMIT_MASK));
    if ((valid & CORE_SAMPLE_VALID_GENERATIONS) != 0U) {
        w32(p + 64U, v->active_profile_generation); w32(p + 68U, v->telemetry_config_generation);
    }
    memcpy(output, p, sizeof(p)); *length = sizeof(p);
    return CORE_RESULT_OK;
}

CoreResult core_sample_decode(const uint8_t *p, size_t n, CoreSpeedSample *sample)
{
    CoreSpeedSample v;
    static const uint32_t bit[7] = {CORE_SAMPLE_VALID_TARGET, CORE_SAMPLE_VALID_VELOCITY,
        CORE_SAMPLE_VALID_POSITION, CORE_SAMPLE_VALID_IQ_REFERENCE, CORE_SAMPLE_VALID_IQ_FEEDBACK,
        CORE_SAMPLE_VALID_ID_FEEDBACK, CORE_SAMPLE_VALID_BUS_VOLTAGE};
    uint32_t valid;
    size_t i;
    if (p == NULL || sample == NULL || n != 72U) { return CORE_BAD_PAYLOAD; }
    valid = r32(p + 56U);
    if ((valid & ~CORE_SAMPLE_VALID_MASK) != 0U
        || (r16(p + 60U) & ~CORE_SAMPLE_LIMIT_MASK) != 0U || r16(p + 62U) != 0U) {
        return CORE_BAD_PAYLOAD;
    }
    for (i = 0U; i < 7U; ++i) {
        if (!observation_valid(p + 20U + 4U * i, bit[i], valid)) { return CORE_BAD_VALUE; }
    }
    if (((valid & CORE_SAMPLE_VALID_TIME) == 0U && r64(p + 4U) != 0U) ||
        ((valid & CORE_SAMPLE_VALID_POSITION) == 0U && r64(p + 12U) != 0U) ||
        ((valid & CORE_SAMPLE_VALID_STATUS) == 0U && (r32(p + 48U) != 0U || r32(p + 52U) != 0U)) ||
        ((valid & CORE_SAMPLE_VALID_GENERATIONS) == 0U && (r32(p + 64U) != 0U || r32(p + 68U) != 0U))) {
        return CORE_BAD_VALUE;
    }
    memset(&v, 0, sizeof(v));
    v.sample_sequence = r32(p); v.control_time_ticks = r64(p + 4U);
    v.position_observation_time = r64(p + 12U);
    v.target_velocity_rpm = rf(p + 20U); v.velocity_rpm = rf(p + 24U);
    v.mechanical_position_turns = rf(p + 28U); v.iq_reference_a = rf(p + 32U);
    v.iq_feedback_a = rf(p + 36U); v.id_feedback_a = rf(p + 40U); v.bus_voltage_v = rf(p + 44U);
    v.motor_state = r32(p + 48U); v.fault_flags = r32(p + 52U); v.valid_flags = valid;
    v.limit_flags = r16(p + 60U); v.active_profile_generation = r32(p + 64U);
    v.telemetry_config_generation = r32(p + 68U); *sample = v;
    return CORE_RESULT_OK;
}

CoreResult core_event_decode(uint16_t id, const uint8_t *p, size_t n, CoreEvent *event)
{
    CoreEvent v;
    if (id != CORE_STATE_CHANGED && id != CORE_FAULT_LATCHED) { return CORE_UNKNOWN_MESSAGE; }
    if (p == NULL || event == NULL || n != 16U) { return CORE_BAD_PAYLOAD; }
    v.first = r32(p); v.second = r32(p + 4U); v.control_time_ticks = r64(p + 8U);
    *event = v; return CORE_RESULT_OK;
}

CoreResult core_event_encode(uint16_t id, const CoreEvent *event,
    uint8_t *output, size_t capacity, size_t *length)
{
    if (id != CORE_STATE_CHANGED && id != CORE_FAULT_LATCHED) { return CORE_UNKNOWN_MESSAGE; }
    if (event == NULL || output == NULL || length == NULL) { return CORE_BAD_PAYLOAD; }
    if (capacity < 16U) { return CORE_LENGTH_MISMATCH; }
    w32(output, event->first); w32(output + 4U, event->second); w64(output + 8U, event->control_time_ticks);
    *length = 16U; return CORE_RESULT_OK;
}
