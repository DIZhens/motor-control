#include "core_server.h"
#include "byte_stream_transport.h"
#include <string.h>

static bool sequence_valid(uint32_t sequence) { return sequence != 0U && sequence != 0xFFFFFFFFU; }
static bool same_identity(const CoreFrame *a, const CoreFrame *b)
{ return a->session_id == b->session_id && a->sequence == b->sequence; }
static bool same_request(const CoreFrame *a, const CoreFrame *b)
{
    return same_identity(a, b) && a->version == b->version && a->kind == b->kind
        && a->flags == b->flags && a->header_size == b->header_size
        && a->message_id == b->message_id && a->payload_length == b->payload_length
        && memcmp(a->payload, b->payload, a->payload_length) == 0;
}
static void build_answer(CoreServer *s, const CoreFrame *request, uint16_t result, uint16_t subject)
{
    memset(&s->answer, 0, sizeof(s->answer));
    s->answer.result_code = result; s->answer.subject_id = subject; s->answer.motor_result = 0xFFFFU;
    memset(&s->output, 0, sizeof(s->output));
    s->output.version = 1U; s->output.kind = CORE_RESPONSE; s->output.header_size = CORE_HEADER_SIZE;
    s->output.message_id = request->message_id; s->output.session_id = request->session_id;
    s->output.sequence = request->sequence;
}
static bool encode_answer(CoreServer *s)
{
    size_t count;
    if (core_response_encode(s->output.message_id, &s->answer, s->output.payload,
        sizeof(s->output.payload), &count) != CORE_RESULT_OK) { return false; }
    s->output.payload_length = (uint16_t)count;
    return true;
}
static void queue_unchecked(CoreServer *s, const CoreFrame *response, bool critical)
{
    s->responses[s->response_count].frame = *response;
    s->responses[s->response_count++].critical = critical;
    if (s->response_high_water < s->response_count) { s->response_high_water = s->response_count; }
}
static uint16_t subject_of(const CoreFrame *f)
{
    if ((f->message_id == CORE_READ_OBJECT || f->message_id == CORE_WRITE_OBJECT
        || f->message_id == CORE_EXECUTE_COMMAND) && f->payload_length >= 2U) {
        return (uint16_t)(f->payload[0] | (uint16_t)f->payload[1] << 8);
    }
    return 0U;
}

/* 即时撤销Session，停止事务映射；旧实时请求不重放，迟到Completion仅退休。
 * 若传输仍可用，已在途请求各保留一个关键系统终态；不尝试恢复内部可信性。 */
static void abandon(CoreServer *s)
{
    CorePendingRequest *p;
    uint32_t index;
    if (s->failed) { return; }
    s->failed = true; s->session = 0U; s->highest_sequence = 0U;
    s->active_epoch = 0U; s->event_count = 0U;
    s->response_count = 0U; s->cache_next = 0U; memset(s->cache, 0, sizeof(s->cache));
    for (index = 0U; index < 2U; ++index) {
        p = index == 0U ? &s->stop : &s->ordinary;
        if (!p->active) { continue; }
        build_answer(s, &p->request, CORE_SYSTEM_FAIL_CLOSED, subject_of(&p->request));
        if (encode_answer(s)) { queue_unchecked(s, &s->output, true); }
        p->active = false;
    }
}
static void fail(CoreServer *s, RuntimeInternalFaultReason reason)
{
    if (!s->failed) { core_server_port_fail_closed(reason); abandon(s); }
}
static bool state_valid(CoreServer *s)
{
    if (s->response_count > CORE_RESPONSE_CAPACITY || s->cache_next >= CORE_CACHE_CAPACITY
        || s->event_count > CORE_EVENT_CAPACITY
        || (s->ordinary.active && s->ordinary.ticket == 0U) || (s->stop.active && s->stop.ticket == 0U)
        || (s->ordinary.active && s->stop.active && s->ordinary.ticket == s->stop.ticket)
        || s->highest_sequence == 0xFFFFFFFFU || (s->session == 0U && s->highest_sequence != 0U)) {
        fail(s, RUNTIME_INTERNAL_TRANSACTION_CORRUPT); return false;
    }
    return true;
}
static bool observe_failure(CoreServer *s)
{
    if (!core_server_port_permitted()) { abandon(s); }
    return s->failed;
}
static bool queue(CoreServer *s, const CoreFrame *response, bool critical)
{
    if (s->response_count >= CORE_RESPONSE_CAPACITY) {
        fail(s, RUNTIME_INTERNAL_RESPONSE_OVERFLOW); return false;
    }
    queue_unchecked(s, response, critical); return true;
}
static void retire(CoreServer *s, const CoreFrame *request, bool critical, bool cache)
{
    CoreCompletedRequest *entry;
    if (!encode_answer(s)) { fail(s, RUNTIME_INTERNAL_TRANSACTION_CORRUPT); return; }
    if (!queue(s, &s->output, critical)) { return; }
    if (cache) {
        entry = &s->cache[s->cache_next];
        entry->request = *request; entry->response = s->output; entry->valid = true;
        s->cache_next = (s->cache_next + 1U) % CORE_CACHE_CAPACITY;
    }
}
static void error_answer(CoreServer *s, const CoreFrame *f, uint16_t result, bool critical, bool cache)
{
    build_answer(s, f, result, subject_of(f)); retire(s, f, critical, cache);
}
static bool replay(CoreServer *s, const CoreFrame *f)
{
    uint32_t index;
    CorePendingRequest *p;
    CoreCompletedRequest *entry;
    size_t count;
    for (index = 0U; index < 2U; ++index) {
        p = index == 0U ? &s->stop : &s->ordinary;
        if (!p->active || !same_identity(f, &p->request)) { continue; }
        if (!same_request(f, &p->request)) { error_answer(s, f, CORE_SEQUENCE_CONFLICT, false, false); }
        else {
            /* 非终态提示不占终态FIFO、不写完成缓存；拥塞时等待原终态即可。 */
            build_answer(s, f, CORE_IN_PROGRESS, subject_of(f));
            if (s->response_count == 0U && encode_answer(s)
                && core_frame_encode(&s->output, s->wire, sizeof(s->wire), &count) == CORE_OK) {
                ByteStreamResult result = byte_stream_try_send(s->wire, count);
                if (result != BYTE_STREAM_ACCEPTED && result != BYTE_STREAM_BUSY && !observe_failure(s)) {
                    fail(s, RUNTIME_INTERNAL_TRANSPORT);
                }
            }
        }
        return true;
    }
    for (index = 0U; index < CORE_CACHE_CAPACITY; ++index) {
        entry = &s->cache[index];
        if (!entry->valid || !same_identity(f, &entry->request)) { continue; }
        if (!same_request(f, &entry->request)) { error_answer(s, f, CORE_SEQUENCE_CONFLICT, false, false); }
        else { queue(s, &entry->response, f->message_id == CORE_EXECUTE_COMMAND && subject_of(f) == CORE_STOP); }
        return true;
    }
    return false;
}
static void read_communication(CoreServer *s)
{
    CoreCommunicationStatus *v = &s->answer.value.communication_status;
    v->session_id = s->session;
    v->cobs_errors = s->parser.stats.cobs_errors; v->crc_errors = s->parser.stats.crc_errors;
    v->length_errors = s->parser.stats.length_errors; v->version_errors = s->parser.stats.version_errors;
    v->message_errors = s->parser.stats.message_errors + s->parser.stats.flags_errors + s->message_errors;
    v->parser_timeouts = s->parser.stats.timeouts; v->response_fifo_high_water = s->response_high_water;
    v->event_drop_count = s->event_drop_count; v->event_fifo_high_water = s->event_high_water;
}
static void cancelled(CoreServer *s, uint32_t ticket)
{
    if (!s->ordinary.active || s->ordinary.ticket != ticket) {
        fail(s, RUNTIME_INTERNAL_TRANSACTION_CORRUPT); return;
    }
    error_answer(s, &s->ordinary.request, CORE_CANCELLED_BY_STOP, true, true);
    s->ordinary.active = false;
}
static void dispatch(CoreServer *s, const CoreFrame *f, bool is_stop, bool cache)
{
    RuntimeExchangeResult result;
    uint32_t ticket = 0U, cancelled_ticket = 0U;
    CorePendingRequest *p = is_stop ? &s->stop : &s->ordinary;
    if (p->active || (!is_stop && s->stop.active)) { error_answer(s, f, CORE_BUSY, is_stop, cache); return; }
    if (s->decoded.message_id == CORE_READ_OBJECT) {
        build_answer(s, f, CORE_RESULT_OK, s->decoded.subject_id);
        s->answer.result_code = (uint16_t)core_server_port_read_object(s->decoded.subject_id, &s->answer.value);
        if (s->decoded.subject_id == CORE_COMMUNICATION_STATUS && s->answer.result_code == CORE_RESULT_OK) {
            read_communication(s);
        }
        retire(s, f, false, cache); return;
    }
    memset(&s->runtime_request, 0, sizeof(s->runtime_request));
    if (s->decoded.message_id == CORE_WRITE_OBJECT) {
        if (s->decoded.subject_id == CORE_SPEED_PI_STAGED) {
            s->runtime_request.kind = RUNTIME_REQUEST_SPEED_PI;
            s->runtime_request.value.speed_pi.kp = s->decoded.value.speed_pi.kp;
            s->runtime_request.value.speed_pi.ki = s->decoded.value.speed_pi.ki;
        } else {
            s->runtime_request.kind = RUNTIME_REQUEST_TELEMETRY;
            s->runtime_request.value.telemetry.enabled = s->decoded.value.telemetry_config.enabled != 0U;
            s->runtime_request.value.telemetry.rate_hz = s->decoded.value.telemetry_config.rate_hz;
        }
    } else {
        s->runtime_request.kind = RUNTIME_REQUEST_MOTOR;
        switch (s->decoded.subject_id) {
        case CORE_START_VELOCITY: s->runtime_request.value.motor.type = MOTOR_COMMAND_START_VELOCITY; break;
        case CORE_UPDATE_VELOCITY: s->runtime_request.value.motor.type = MOTOR_COMMAND_UPDATE_VELOCITY; break;
        case CORE_STOP: s->runtime_request.value.motor.type = MOTOR_COMMAND_STOP; break;
        default: s->runtime_request.value.motor.type = MOTOR_COMMAND_CLEAR_FAULT; break;
        }
        s->runtime_request.value.motor.target_velocity_rpm = s->decoded.target_velocity_rpm;
    }
    result = core_server_port_submit(&s->runtime_request, &ticket, &cancelled_ticket);
    if (observe_failure(s)) { error_answer(s, f, CORE_SYSTEM_FAIL_CLOSED, true, false); return; }
    if (result != RUNTIME_EXCHANGE_QUEUED) {
        if (result != RUNTIME_EXCHANGE_BUSY && result != RUNTIME_EXCHANGE_PRECONDITION_FAILED
            && result != RUNTIME_EXCHANGE_BAD_REQUEST) {
            fail(s, RUNTIME_INTERNAL_TRANSACTION_CORRUPT); return;
        }
        error_answer(s, f, result == RUNTIME_EXCHANGE_BUSY ? CORE_BUSY :
            result == RUNTIME_EXCHANGE_PRECONDITION_FAILED ? CORE_PRECONDITION_FAILED : CORE_BAD_REQUEST,
            is_stop, cache); return;
    }
    if (ticket == 0U || (s->ordinary.active && s->ordinary.ticket == ticket)
        || (s->stop.active && s->stop.ticket == ticket) || (!is_stop && cancelled_ticket != 0U)) {
        fail(s, RUNTIME_INTERNAL_TRANSACTION_CORRUPT); return;
    }
    p->request = *f; p->kind = s->runtime_request.kind;
    p->ticket = ticket; p->started_ms = s->now_ms; p->active = true;
    if (cancelled_ticket != 0U) { cancelled(s, cancelled_ticket); }
}
static void received(void *context, CoreStatus status, const CoreFrame *f)
{
    CoreServer *s = context;
    CoreResult decoded;
    bool sessionless_stop, is_stop;
    uint32_t proposed;
    (void)status;
    if (!state_valid(s)) { return; }
    if (f->kind != CORE_REQUEST) { if (status != CORE_FRAME_KIND) { s->message_errors++; } return; }
    decoded = core_request_decode(f, &s->decoded);
    if (decoded == CORE_UNKNOWN_MESSAGE) { s->message_errors++; }
    if (!sequence_valid(f->sequence)) { error_answer(s, f, CORE_SEQUENCE_INVALID, false, false); return; }
    if (observe_failure(s)) { error_answer(s, f, CORE_SYSTEM_FAIL_CLOSED, true, false); return; }
    if (f->message_id == CORE_OPEN_SESSION) {
        if (decoded != CORE_RESULT_OK || f->session_id != 0U) {
            error_answer(s, f, decoded != CORE_RESULT_OK ? decoded : CORE_BAD_REQUEST, false, false); return;
        }
        if (s->ordinary.active) { error_answer(s, f, CORE_SESSION_BUSY, false, false); return; }
        proposed = s->decoded.proposed_session_id;
        if (proposed != s->session) {
            if (s->epoch_counter == 0xFFFFFFFFU) { fail(s, RUNTIME_INTERNAL_TRANSACTION_CORRUPT); return; }
            s->session = proposed; s->highest_sequence = 0U; s->cache_next = 0U;
            s->response_count = 0U; memset(s->cache, 0, sizeof(s->cache));
            s->event_count = 0U; s->event_sequence = 0U; s->snapshot_valid = false;
            s->telemetry_domain_valid = false; s->telemetry_exhausted = false; s->telemetry_sequence = 0U;
            s->active_epoch = ++s->epoch_counter;
            /* 已排队Stop保留原ticket/请求身份；旧会话完成不进入新缓存。 */
        }
        build_answer(s, f, CORE_RESULT_OK, 0U); s->answer.accepted_session_id = proposed;
        retire(s, f, false, false); return;
    }
    is_stop = decoded == CORE_RESULT_OK && f->message_id == CORE_EXECUTE_COMMAND && s->decoded.subject_id == CORE_STOP;
    sessionless_stop = f->session_id == 0U && is_stop;
    if (!sessionless_stop && (f->session_id == 0U || f->session_id != s->session)) {
        error_answer(s, f, f->session_id == 0U ? CORE_SESSION_REQUIRED : CORE_SESSION_INVALID, false, false); return;
    }
    if (replay(s, f)) { return; }
    if (!sessionless_stop) {
        if (f->sequence <= s->highest_sequence) { error_answer(s, f, CORE_SEQUENCE_INVALID, false, false); return; }
        s->highest_sequence = f->sequence;
    }
    if (decoded != CORE_RESULT_OK) { error_answer(s, f, decoded, false, true); return; }
    dispatch(s, f, is_stop, true);
}

void core_server_init(CoreServer *s) { memset(s, 0, sizeof(*s)); core_parser_init(&s->parser); }
void core_server_discontinuity(CoreServer *s) { core_parser_discontinuity(&s->parser); }
void core_server_feed(CoreServer *s, const uint8_t *bytes, size_t count, uint32_t now_ms)
{
    s->now_ms = now_ms;
    core_parser_feed(&s->parser, bytes, count, now_ms, received, s);
}
static void completed(CoreServer *s, const RuntimeCommandCompletion *c)
{
    CorePendingRequest *p;
    bool stop;
    if (s->failed) { return; }
    stop = s->stop.active && s->stop.ticket == c->ticket;
    p = stop ? &s->stop : &s->ordinary;
    if (!p->active || c->ticket == 0U || p->ticket != c->ticket) {
        fail(s, RUNTIME_INTERNAL_TRANSACTION_CORRUPT); return;
    }
    build_answer(s, &p->request, CORE_RESULT_OK, subject_of(&p->request));
    if (c->outcome == RUNTIME_CANCELLED_BY_STOP) { s->answer.result_code = CORE_CANCELLED_BY_STOP; }
    else if (c->outcome == RUNTIME_PRECONDITION_FAILED) { s->answer.result_code = CORE_PRECONDITION_FAILED; }
    else if (c->outcome != RUNTIME_COMPLETED || c->kind != p->kind) {
        fail(s, RUNTIME_INTERNAL_TRANSACTION_CORRUPT); return;
    } else if (p->kind == RUNTIME_REQUEST_MOTOR) {
        if (c->motor_result > MOTOR_COMMAND_RESULT_BAD_COMMAND) { fail(s, RUNTIME_INTERNAL_TRANSACTION_CORRUPT); return; }
        s->answer.motor_result = (uint16_t)c->motor_result;
    } else {
        if (c->parameter_result == RUNTIME_PARAMETER_BAD_VALUE) { s->answer.result_code = CORE_BAD_VALUE; }
        else if (c->parameter_result == RUNTIME_PARAMETER_WRONG_STATE) { s->answer.result_code = CORE_OBJECT_WRONG_STATE; }
        else if (c->parameter_result != RUNTIME_PARAMETER_APPLIED) { fail(s, RUNTIME_INTERNAL_TRANSACTION_CORRUPT); return; }
        else if (p->kind == RUNTIME_REQUEST_SPEED_PI) {
            s->answer.value.speed_pi.kp = c->value.speed_pi.value.kp;
            s->answer.value.speed_pi.ki = c->value.speed_pi.value.ki;
            s->answer.value.speed_pi.generation = c->value.speed_pi.generation;
        } else {
            s->answer.value.telemetry_config.enabled = c->value.telemetry.enabled ? 1U : 0U;
            s->answer.value.telemetry_config.rate_hz = c->value.telemetry.rate_hz;
            s->answer.value.telemetry_config.generation = c->value.telemetry.generation;
        }
    }
    retire(s, &p->request, stop, p->request.session_id == s->session || p->request.session_id == 0U);
    p->active = false;
}
static void event(CoreServer *s, uint16_t message, uint32_t first, uint32_t second, uint64_t time)
{
    CoreQueuedEvent *entry;
    uint32_t index;
    if (s->event_sequence == 0xFFFFFFFEU) { return; }
    if (s->event_count == CORE_EVENT_CAPACITY) {
        for (index = 1U; index < CORE_EVENT_CAPACITY; ++index) { s->events[index - 1U] = s->events[index]; }
        s->event_count--; s->event_drop_count++;
    }
    entry = &s->events[s->event_count++];
    entry->message = message; entry->sequence = ++s->event_sequence;
    entry->value = (CoreEvent){first, second, time};
    if (s->event_count > s->event_high_water) { s->event_high_water = s->event_count; }
}

static void observe_snapshot(CoreServer *s)
{
    CoreMotorSnapshot *m = &s->answer.value.motor_snapshot;
    if (s->session == 0U || s->failed || core_server_port_read_object(CORE_MOTOR_SNAPSHOT,
        &s->answer.value) != CORE_RESULT_OK || (m->valid_flags & 0xC1U) != 0xC1U) { return; }
    if (s->snapshot_valid) {
        if (s->previous_state != m->motor_state) {
            event(s, CORE_STATE_CHANGED, s->previous_state, m->motor_state, m->control_time_ticks);
        }
        if ((m->fault_flags & ~s->previous_faults) != 0U) {
            event(s, CORE_FAULT_LATCHED, m->fault_flags & ~s->previous_faults, m->fault_flags, m->control_time_ticks);
        }
    }
    s->previous_state = m->motor_state; s->previous_faults = m->fault_flags; s->snapshot_valid = true;
}

void core_server_poll(CoreServer *s, uint32_t now_ms)
{
    RuntimeCommandCompletion completion;
    uint32_t index;
    s->now_ms = now_ms; core_parser_poll(&s->parser, now_ms);
    if (!state_valid(s)) { return; }
    observe_failure(s);
    if (!s->failed && ((s->ordinary.active && (uint32_t)(now_ms - s->ordinary.started_ms) >= CORE_RUNTIME_TIMEOUT_MS)
        || (s->stop.active && (uint32_t)(now_ms - s->stop.started_ms) >= CORE_RUNTIME_TIMEOUT_MS))) {
        fail(s, RUNTIME_INTERNAL_TIMEOUT);
    }
    for (index = 0U; index < RUNTIME_COMPLETION_CAPACITY; ++index) {
        if (!core_server_port_completion(&completion)) { break; }
        completed(s, &completion);
    }
    observe_snapshot(s);
}

static bool telemetry_frame(CoreServer *s)
{
    RuntimeTelemetryValue value;
    CoreSpeedSample sample;
    uint32_t offset, sequence;
    size_t length;
    if (!core_server_port_telemetry_peek(&value)) { return false; }
    if (s->session == 0U || s->failed || s->telemetry_exhausted || value.epoch != s->active_epoch
        || core_server_port_read_object(CORE_TELEMETRY_CONFIG, &s->answer.value) != CORE_RESULT_OK
        || !s->answer.value.telemetry_config.enabled
        || value.sample.telemetry_config_generation != s->answer.value.telemetry_config.generation) {
        core_server_port_telemetry_retire(); return false;
    }
    if (!s->telemetry_domain_valid) {
        s->telemetry_domain_valid = true; s->telemetry_first = value.epoch_first;
    }
    offset = value.produced - s->telemetry_first;
    if (offset >= 0xFFFFFFFEU) {
        s->telemetry_exhausted = true; core_server_port_telemetry_retire(); return false;
    }
    sequence = offset + 1U;
    s->telemetry_sequence = sequence; /* 生成值决定序号，BUSY不重新分配或成功后才编号。 */
#define COPY_FIELD(name) sample.name = value.sample.name
    COPY_FIELD(sample_sequence); COPY_FIELD(control_time_ticks); COPY_FIELD(position_observation_time);
    COPY_FIELD(target_velocity_rpm); COPY_FIELD(velocity_rpm); COPY_FIELD(mechanical_position_turns);
    COPY_FIELD(iq_reference_a); COPY_FIELD(iq_feedback_a); COPY_FIELD(id_feedback_a); COPY_FIELD(bus_voltage_v);
    COPY_FIELD(motor_state); COPY_FIELD(fault_flags); COPY_FIELD(valid_flags); COPY_FIELD(limit_flags);
    COPY_FIELD(active_profile_generation); COPY_FIELD(telemetry_config_generation);
#undef COPY_FIELD
    memset(&s->output, 0, sizeof(s->output));
    s->output.version = 1U; s->output.kind = CORE_TELEMETRY;
    s->output.header_size = CORE_HEADER_SIZE; s->output.session_id = s->session;
    s->output.message_id = CORE_SPEED_SAMPLE; s->output.sequence = sequence;
    if (core_sample_encode(&sample, s->output.payload, sizeof(s->output.payload), &length) != CORE_RESULT_OK) {
        fail(s, RUNTIME_INTERNAL_TRANSACTION_CORRUPT); return false;
    }
    s->output.payload_length = (uint16_t)length;
    return true;
}

void core_server_flush(CoreServer *s)
{
    uint32_t index, selected = 0U;
    size_t count;
    uint32_t source;
    ByteStreamResult result;
    if (!state_valid(s)) { return; }
    observe_failure(s);
    if (s->response_count != 0U) {
        source = 0U;
        for (index = 0U; index < s->response_count; ++index) {
            if (s->responses[index].critical) { selected = index; break; }
        }
        s->output = s->responses[selected].frame;
    } else if (!s->failed && s->event_count != 0U) {
        source = 1U;
        memset(&s->output, 0, sizeof(s->output));
        s->output.version = 1U; s->output.kind = CORE_EVENT; s->output.header_size = CORE_HEADER_SIZE;
        s->output.message_id = s->events[0].message; s->output.sequence = s->events[0].sequence;
        s->output.session_id = s->session;
        if (core_event_encode(s->output.message_id, &s->events[0].value, s->output.payload,
            sizeof(s->output.payload), &count) != CORE_RESULT_OK) { fail(s, RUNTIME_INTERNAL_TRANSACTION_CORRUPT); return; }
        s->output.payload_length = (uint16_t)count;
    } else {
        source = 2U;
        if (!telemetry_frame(s)) { return; }
    }
    if (core_frame_encode(&s->output, s->wire, sizeof(s->wire), &count) != CORE_OK) {
        fail(s, RUNTIME_INTERNAL_TRANSACTION_CORRUPT); return;
    }
    result = byte_stream_try_send(s->wire, count);
    if (result == BYTE_STREAM_BUSY) { return; }
    if (result != BYTE_STREAM_ACCEPTED) {
        if (!observe_failure(s)) { fail(s, RUNTIME_INTERNAL_TRANSPORT); } return;
    }
    if (source == 0U) {
        for (index = selected + 1U; index < s->response_count; ++index) { s->responses[index - 1U] = s->responses[index]; }
        s->response_count--;
    } else if (source == 1U) {
        for (index = 1U; index < s->event_count; ++index) { s->events[index - 1U] = s->events[index]; }
        s->event_count--;
    } else { core_server_port_telemetry_retire(); }
}
