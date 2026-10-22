#include "core_protocol_values.h"

#include <string.h>

/* 仅供真实 ARM 模型访问；无 HAL、RTOS、串口或设备命令出口。 */
uint8_t model_input[1024], model_output[144];
uint32_t model_length, model_capacity, model_size, model_status;
uint32_t model_decode_changed_on_reject;
uint32_t model_id, model_write, model_now, model_meta[8];
uint64_t model_fields[32];
uint32_t model_count, model_statuses[128], model_sequences[128], model_stats[8];
static CoreParser parser;
static CoreFrame frame;
static CoreObject object;
static CoreRequest request;
static CoreResponse response;
static CoreSpeedSample sample;
static CoreEvent event;

static bool all_a5(const void *value, size_t size)
{
    const uint8_t *p = value;
    size_t i;
    for (i = 0U; i < size; ++i) { if (p[i] != 0xA5U) { return false; } }
    return true;
}

static uint32_t fb(float f) { uint32_t v; memcpy(&v, &f, 4U); return v; }
static float fv(uint64_t v) { uint32_t b = (uint32_t)v; float f; memcpy(&f, &b, 4U); return f; }

static void object_out(uint16_t id, const CoreObject *v)
{
    const CoreMotorSnapshot *m = &v->motor_snapshot;
    switch (id) {
    case CORE_DEVICE_INFO:
        model_fields[0] = v->device_info.protocol_version; model_fields[1] = v->device_info.device_contract_version;
        model_fields[2] = v->device_info.target_id; model_fields[3] = v->device_info.control_profile_id;
        model_fields[4] = v->device_info.capability_flags; model_fields[5] = v->device_info.control_time_ticks_per_second;
        model_fields[6] = v->device_info.firmware_build_id; break;
    case CORE_MOTOR_SNAPSHOT:
        model_fields[0] = m->control_time_ticks; model_fields[1] = m->control_time_ticks_per_second;
        model_fields[2] = m->motor_state; model_fields[3] = m->fault_flags;
        model_fields[4] = m->motor_command_result; model_fields[5] = m->valid_flags;
        model_fields[6] = fb(m->target_velocity_rpm); model_fields[7] = fb(m->velocity_rpm);
        model_fields[8] = fb(m->id_a); model_fields[9] = fb(m->iq_a); model_fields[10] = fb(m->bus_voltage_v); break;
    case CORE_COMMUNICATION_STATUS:
        model_fields[0] = v->communication_status.session_id;
        model_fields[1] = v->communication_status.rx_bytes;
        model_fields[2] = v->communication_status.tx_bytes;
        model_fields[3] = v->communication_status.event_drop_count;
        model_fields[4] = v->communication_status.rx_discontinuities;
        model_fields[5] = v->communication_status.rx_dma_overruns;
        model_fields[6] = v->communication_status.uart_pe;
        model_fields[7] = v->communication_status.uart_fe;
        model_fields[8] = v->communication_status.uart_ne;
        model_fields[9] = v->communication_status.uart_ore;
        model_fields[10] = v->communication_status.cobs_errors;
        model_fields[11] = v->communication_status.crc_errors;
        model_fields[12] = v->communication_status.length_errors;
        model_fields[13] = v->communication_status.version_errors;
        model_fields[14] = v->communication_status.message_errors;
        model_fields[15] = v->communication_status.parser_timeouts;
        model_fields[16] = v->communication_status.response_fifo_high_water;
        model_fields[17] = v->communication_status.completion_fifo_high_water;
        model_fields[18] = v->communication_status.event_fifo_high_water;
        model_fields[19] = v->communication_status.telemetry_overwrites;
        model_fields[20] = v->communication_status.communication_stack_min_words;
        model_fields[21] = v->communication_status.telemetry_stack_min_words;
        model_fields[22] = v->communication_status.internal_fault_reason;
        model_fields[23] = v->communication_status.internal_fault_count;
        model_fields[24] = v->communication_status.tx_busy_count;
        model_fields[25] = v->communication_status.tx_errors;
        break;
    case CORE_SPEED_PI_DEFAULT: case CORE_SPEED_PI_STAGED: case CORE_SPEED_PI_ACTIVE:
        model_fields[0] = fb(v->speed_pi.kp); model_fields[1] = fb(v->speed_pi.ki);
        model_fields[2] = v->speed_pi.generation; model_fields[3] = v->speed_pi.currently_in_use; break;
    case CORE_TELEMETRY_CONFIG:
        model_fields[0] = v->telemetry_config.enabled; model_fields[1] = v->telemetry_config.rate_hz;
        model_fields[2] = v->telemetry_config.generation; break;
    default: break;
    }
}

static void object_in(uint16_t id, CoreObject *v)
{
    CoreMotorSnapshot *m = &v->motor_snapshot;
    memset(v, 0, sizeof(*v));
    switch (id) {
    case CORE_DEVICE_INFO:
        v->device_info.protocol_version = (uint16_t)model_fields[0];
        v->device_info.device_contract_version = (uint16_t)model_fields[1];
        v->device_info.target_id = (uint16_t)model_fields[2]; v->device_info.control_profile_id = (uint16_t)model_fields[3];
        v->device_info.capability_flags = (uint32_t)model_fields[4];
        v->device_info.control_time_ticks_per_second = (uint32_t)model_fields[5];
        v->device_info.firmware_build_id = model_fields[6]; break;
    case CORE_MOTOR_SNAPSHOT:
        m->control_time_ticks = model_fields[0]; m->control_time_ticks_per_second = (uint32_t)model_fields[1];
        m->motor_state = (uint32_t)model_fields[2]; m->fault_flags = (uint32_t)model_fields[3];
        m->motor_command_result = (uint32_t)model_fields[4]; m->valid_flags = (uint32_t)model_fields[5];
        m->target_velocity_rpm = fv(model_fields[6]); m->velocity_rpm = fv(model_fields[7]);
        m->id_a = fv(model_fields[8]); m->iq_a = fv(model_fields[9]); m->bus_voltage_v = fv(model_fields[10]); break;
    case CORE_COMMUNICATION_STATUS:
        v->communication_status.session_id = (uint32_t)model_fields[0];
        v->communication_status.rx_bytes = (uint32_t)model_fields[1];
        v->communication_status.tx_bytes = (uint32_t)model_fields[2];
        v->communication_status.event_drop_count = (uint32_t)model_fields[3];
        v->communication_status.rx_discontinuities = (uint32_t)model_fields[4];
        v->communication_status.rx_dma_overruns = (uint32_t)model_fields[5];
        v->communication_status.uart_pe = (uint32_t)model_fields[6];
        v->communication_status.uart_fe = (uint32_t)model_fields[7];
        v->communication_status.uart_ne = (uint32_t)model_fields[8];
        v->communication_status.uart_ore = (uint32_t)model_fields[9];
        v->communication_status.cobs_errors = (uint32_t)model_fields[10];
        v->communication_status.crc_errors = (uint32_t)model_fields[11];
        v->communication_status.length_errors = (uint32_t)model_fields[12];
        v->communication_status.version_errors = (uint32_t)model_fields[13];
        v->communication_status.message_errors = (uint32_t)model_fields[14];
        v->communication_status.parser_timeouts = (uint32_t)model_fields[15];
        v->communication_status.response_fifo_high_water = (uint32_t)model_fields[16];
        v->communication_status.completion_fifo_high_water = (uint32_t)model_fields[17];
        v->communication_status.event_fifo_high_water = (uint32_t)model_fields[18];
        v->communication_status.telemetry_overwrites = (uint32_t)model_fields[19];
        v->communication_status.communication_stack_min_words = (uint32_t)model_fields[20];
        v->communication_status.telemetry_stack_min_words = (uint32_t)model_fields[21];
        v->communication_status.internal_fault_reason = (uint32_t)model_fields[22];
        v->communication_status.internal_fault_count = (uint32_t)model_fields[23];
        v->communication_status.tx_busy_count = (uint32_t)model_fields[24];
        v->communication_status.tx_errors = (uint32_t)model_fields[25];
        break;
    case CORE_SPEED_PI_DEFAULT: case CORE_SPEED_PI_STAGED: case CORE_SPEED_PI_ACTIVE:
        v->speed_pi.kp = fv(model_fields[0]); v->speed_pi.ki = fv(model_fields[1]);
        v->speed_pi.generation = (uint32_t)model_fields[2]; v->speed_pi.currently_in_use = model_fields[3] != 0U; break;
    case CORE_TELEMETRY_CONFIG:
        v->telemetry_config.enabled = (uint8_t)model_fields[0]; v->telemetry_config.rate_hz = (uint16_t)model_fields[1];
        v->telemetry_config.generation = (uint32_t)model_fields[2]; break;
    default: break;
    }
}

static void sample_out(void)
{
    model_fields[0] = sample.sample_sequence; model_fields[1] = sample.control_time_ticks;
    model_fields[2] = sample.position_observation_time; model_fields[3] = fb(sample.target_velocity_rpm);
    model_fields[4] = fb(sample.velocity_rpm); model_fields[5] = fb(sample.mechanical_position_turns);
    model_fields[6] = fb(sample.iq_reference_a); model_fields[7] = fb(sample.iq_feedback_a);
    model_fields[8] = fb(sample.id_feedback_a); model_fields[9] = fb(sample.bus_voltage_v);
    model_fields[10] = sample.motor_state; model_fields[11] = sample.fault_flags;
    model_fields[12] = sample.valid_flags; model_fields[13] = sample.limit_flags;
    model_fields[14] = sample.active_profile_generation; model_fields[15] = sample.telemetry_config_generation;
}

static void sample_in(void)
{
    sample.sample_sequence = (uint32_t)model_fields[0]; sample.control_time_ticks = model_fields[1];
    sample.position_observation_time = model_fields[2]; sample.target_velocity_rpm = fv(model_fields[3]);
    sample.velocity_rpm = fv(model_fields[4]); sample.mechanical_position_turns = fv(model_fields[5]);
    sample.iq_reference_a = fv(model_fields[6]); sample.iq_feedback_a = fv(model_fields[7]);
    sample.id_feedback_a = fv(model_fields[8]); sample.bus_voltage_v = fv(model_fields[9]);
    sample.motor_state = (uint32_t)model_fields[10]; sample.fault_flags = (uint32_t)model_fields[11];
    sample.valid_flags = (uint32_t)model_fields[12]; sample.limit_flags = (uint16_t)model_fields[13];
    sample.active_profile_generation = (uint32_t)model_fields[14];
    sample.telemetry_config_generation = (uint32_t)model_fields[15];
}

static void sink(void *context, CoreStatus status, const CoreFrame *value)
{
    (void)context;
    if (model_count < 128U) {
        model_statuses[model_count] = (uint32_t)status;
        model_sequences[model_count] = value->sequence;
    }
    ++model_count;
}

void model_init(void)
{
    core_parser_init(&parser); model_count = 0U; model_capacity = CORE_WIRE_MAX;
}

void model_run(void)
{
    size_t n = 0x1234U;
    CoreStatus status;
    model_decode_changed_on_reject = 0U;
    memset(model_output, 0xA5, sizeof(model_output));
    switch (model_meta[7]) {
    case 0: model_status = core_crc16(model_input, model_length); break;
    case 1: model_status = core_cobs_encode(model_input, model_length, model_output + 4U, model_capacity, &n); break;
    case 2: model_status = core_cobs_decode(model_input, model_length, model_output + 4U, model_capacity, &n); break;
    case 3:
        memset(&frame, 0xA5, sizeof(frame));
        status = core_frame_decode(model_input, model_length, &frame); model_status = status;
        if (status != CORE_OK && status != CORE_FRAME_VERSION && status != CORE_FRAME_FLAGS &&
            status != CORE_FRAME_KIND && !all_a5(&frame, sizeof(frame))) { model_decode_changed_on_reject = 1U; }
        model_meta[0] = frame.version; model_meta[1] = frame.kind; model_meta[2] = frame.flags;
        model_meta[3] = frame.header_size; model_meta[4] = frame.message_id;
        model_meta[5] = frame.session_id; model_meta[6] = frame.sequence;
        if (status == CORE_OK) { model_status = core_frame_encode(&frame, model_output + 4U, model_capacity, &n); }
        break;
    case 4:
        memset(&frame, 0, sizeof(frame));
        frame.version = (uint8_t)model_meta[0]; frame.kind = (uint8_t)model_meta[1];
        frame.flags = (uint8_t)model_meta[2]; frame.header_size = (uint8_t)model_meta[3];
        frame.message_id = (uint16_t)model_meta[4]; frame.session_id = model_meta[5]; frame.sequence = model_meta[6];
        frame.payload_length = (uint16_t)model_length;
        if (model_length <= CORE_PAYLOAD_MAX) { memcpy(frame.payload, model_input, model_length); }
        model_status = core_frame_encode(&frame, model_output + 4U, model_capacity, &n); break;
    case 10:
        memset(model_fields, 0xA5, sizeof(model_fields)); memset(&object, 0xA5, sizeof(object));
        model_status = core_object_decode((uint16_t)model_id, model_write != 0U, model_input, model_length, &object);
        if (model_status != 0U && !all_a5(&object, sizeof(object))) { model_decode_changed_on_reject = 1U; }
        if (model_status == 0U) { object_out((uint16_t)model_id, &object); } break;
    case 11:
        object_in((uint16_t)model_id, &object);
        model_status = core_object_encode((uint16_t)model_id, model_write != 0U, &object,
            model_output + 4U, model_capacity, &n); break;
    case 12:
        memset(&frame, 0, sizeof(frame)); frame.version = 1U; frame.kind = CORE_REQUEST;
        frame.header_size = CORE_HEADER_SIZE; frame.message_id = (uint16_t)model_id;
        frame.payload_length = (uint16_t)model_length;
        if (model_length <= CORE_PAYLOAD_MAX) { memcpy(frame.payload, model_input, model_length); }
        model_status = core_request_decode(&frame, &request);
        model_meta[0] = request.subject_id; model_meta[1] = request.proposed_session_id;
        model_meta[2] = fb(request.target_velocity_rpm);
        if (model_status == 0U && model_id == CORE_WRITE_OBJECT) { object_out(request.subject_id, &request.value); } break;
    case 13:
        memset(&request, 0, sizeof(request)); request.message_id = (uint16_t)model_id;
        request.subject_id = (uint16_t)model_meta[0]; request.proposed_session_id = model_meta[1];
        request.target_velocity_rpm = fv(model_meta[2]); object_in(request.subject_id, &request.value);
        model_status = core_request_encode(&request, model_output + 4U, model_capacity, &n); break;
    case 14:
        memset(&response, 0xA5, sizeof(response));
        model_status = core_response_decode((uint16_t)model_id, model_input, model_length, &response);
        if (model_status != 0U && !all_a5(&response, sizeof(response))) { model_decode_changed_on_reject = 1U; }
        if (model_status == 0U) {
            model_meta[0] = response.result_code; model_meta[1] = response.subject_id;
            model_meta[2] = response.motor_result; model_meta[3] = response.accepted_session_id;
            if (response.result_code == 0U && (model_id == 2U || model_id == 3U)) {
                object_out(response.subject_id, &response.value);
            }
        } break;
    case 15:
        memset(&response, 0, sizeof(response)); response.result_code = (uint16_t)model_meta[0];
        response.subject_id = (uint16_t)model_meta[1]; response.motor_result = (uint16_t)model_meta[2];
        response.accepted_session_id = model_meta[3]; object_in(response.subject_id, &response.value);
        model_status = core_response_encode((uint16_t)model_id, &response, model_output + 4U, model_capacity, &n); break;
    case 16:
        memset(&sample, 0xA5, sizeof(sample));
        model_status = core_sample_decode(model_input, model_length, &sample);
        if (model_status != 0U && !all_a5(&sample, sizeof(sample))) { model_decode_changed_on_reject = 1U; }
        if (model_status == 0U) { sample_out(); } break;
    case 17:
        sample_in(); model_status = core_sample_encode(&sample, model_output + 4U, model_capacity, &n); break;
    case 18:
        memset(&event, 0xA5, sizeof(event));
        model_status = core_event_decode((uint16_t)model_id, model_input, model_length, &event);
        if (model_status != 0U && !all_a5(&event, sizeof(event))) { model_decode_changed_on_reject = 1U; }
        if (model_status == 0U) {
            model_fields[0] = event.first; model_fields[1] = event.second; model_fields[2] = event.control_time_ticks;
        } break;
    case 19:
        event.first = (uint32_t)model_fields[0]; event.second = (uint32_t)model_fields[1];
        event.control_time_ticks = model_fields[2];
        model_status = core_event_encode((uint16_t)model_id, &event, model_output + 4U, model_capacity, &n); break;
    case 20: core_parser_feed(&parser, model_input, model_length, model_now, sink, NULL); break;
    case 21: core_parser_poll(&parser, model_now); break;
    case 22: core_parser_discontinuity(&parser); break;
    case 23: model_init(); break;
    default: break;
    }
    model_size = (uint32_t)n;
    model_stats[0] = parser.stats.cobs_errors; model_stats[1] = parser.stats.crc_errors;
    model_stats[2] = parser.stats.length_errors; model_stats[3] = parser.stats.version_errors;
    model_stats[4] = parser.stats.flags_errors; model_stats[5] = parser.stats.message_errors;
    model_stats[6] = parser.stats.timeouts; model_stats[7] = parser.stats.discontinuities;
}

void model_null_checks(void)
{
    uint8_t data = 1U;
    size_t n = 123U;
    model_status = 0U;
    if (core_frame_encode(NULL, model_output, sizeof(model_output), &n) != CORE_BAD_ARGUMENT || n != 123U ||
        core_frame_decode(&data, 1U, NULL) != CORE_BAD_ARGUMENT ||
        core_cobs_encode(NULL, 1U, model_output, sizeof(model_output), &n) != CORE_BAD_ARGUMENT ||
        core_cobs_decode(NULL, 1U, model_output, sizeof(model_output), &n) != CORE_BAD_ARGUMENT ||
        core_object_decode(CORE_SPEED_PI_STAGED, true, NULL, 8U, &object) != CORE_BAD_PAYLOAD ||
        core_sample_encode(NULL, model_output, sizeof(model_output), &n) != CORE_BAD_PAYLOAD ||
        core_request_decode(NULL, &request) != CORE_BAD_PAYLOAD ||
        core_response_decode(1U, NULL, 8U, &response) != CORE_BAD_PAYLOAD ||
        core_event_decode(CORE_STATE_CHANGED, NULL, 16U, &event) != CORE_BAD_PAYLOAD) { model_status = 1U; }
    core_parser_init(NULL); core_parser_poll(NULL, 0U); core_parser_discontinuity(NULL);
    core_parser_feed(NULL, NULL, 0U, 0U, NULL, NULL);
}
