#include "can_server.h"
#include <string.h>

static bool is_stop(const CanRequest *r) { return r->service == CAN_V1_STOP; }
static bool same_identity(const CanRequest *a, const CanRequest *b)
{ return a->session == b->session && a->sequence == b->sequence; }
static bool same_content(const CanRequest *a, const CanRequest *b)
{
    return a->original.id == b->original.id && a->original.dlc == b->original.dlc
        && memcmp(a->original.data, b->original.data, 8U) == 0;
}
static void invalidate_session(CanServer *s)
{
    protocol_device_telemetry_end();
    s->session = 0U; s->highest_sequence = 0U; s->subscription_id = 0U;
    s->cache_next = 0U; memset(s->cache, 0, sizeof(s->cache));
}
static void failed(CanServer *s, ProtocolDeviceFault reason)
{
    if (s->failed) { return; }
    s->failed = true;
    /* 先兑现既有独立关断；订阅清理可能调用后台内核，不能排在关断之前。 */
    protocol_device_fail_closed(reason);
    invalidate_session(s);
    s->response_count = s->event_count = 0U; s->heartbeat_pending = false;
    s->ordinary.active = s->stop.active = false;
}
static bool valid_state(CanServer *s)
{
    if (s->node < 1U || s->node > 127U || s->response_count > CAN_V1_RESPONSE_CAPACITY
        || s->cache_next >= CAN_V1_CACHE_CAPACITY || s->event_count > CAN_V1_EVENT_CAPACITY
        || (s->ordinary.active && s->ordinary.ticket == 0U) || (s->stop.active && s->stop.ticket == 0U)
        || (s->ordinary.active && s->stop.active && s->ordinary.ticket == s->stop.ticket)
        || s->highest_sequence == 0xFFFFU || (s->session == 0U && s->highest_sequence != 0U)
        || ((s->session == 0U) != (s->subscription_id == 0U))) {
        failed(s, PROTOCOL_FAULT_TRANSACTION_CORRUPT); return false;
    }
    if (!protocol_device_permitted()) {
        s->failed = true; invalidate_session(s);
        s->response_count = s->event_count = 0U;
    }
    return !s->failed;
}
static bool queue(CanServer *s, const CanFrame *frame, bool critical)
{
    if (s->response_count >= CAN_V1_RESPONSE_CAPACITY) { failed(s, PROTOCOL_FAULT_RESPONSE_OVERFLOW); return false; }
    s->responses[s->response_count].frame = *frame;
    s->responses[s->response_count++].critical = critical;
    if (s->response_count > s->response_high_water) { s->response_high_water = s->response_count; }
    return true;
}
static void answer(CanServer *s, const CanRequest *r, CoreResult result,
    uint16_t motor_result, bool critical, bool cache)
{
    CanFrame frame;
    CanCompleted *entry;
    if (s->suspended && !is_stop(r)) { return; }
    can_v1_response(s->node, r->session, r->sequence, result, motor_result, &frame);
    if (!queue(s, &frame, critical)) { return; }
    if (cache && r->session != 0U && r->session == s->session) {
        entry = &s->cache[s->cache_next];
        entry->request = *r; entry->response = frame; entry->valid = true;
        s->cache_next = (s->cache_next + 1U) % CAN_V1_CACHE_CAPACITY;
    }
}
static void reject(CanServer *s, const CanRequest *r, CoreResult result, bool cache)
{ answer(s, r, result, CORE_MOTOR_RESULT_UNAVAILABLE, is_stop(r), cache); }
static bool replay(CanServer *s, const CanRequest *r)
{
    uint32_t i;
    CanPending *p;
    for (i = 0U; i < 2U; ++i) {
        p = i == 0U ? &s->stop : &s->ordinary;
        if (!p->active || p->internal_stop || !same_identity(r, &p->request)) { continue; }
        if (!same_content(r, &p->request)) { reject(s, r, CORE_SEQUENCE_CONFLICT, false); }
        else if (s->response_count == 0U) {
            CanFrame frame;
            CanFrameResult result;
            can_v1_response(s->node, r->session, r->sequence, CORE_IN_PROGRESS, CORE_MOTOR_RESULT_UNAVAILABLE, &frame);
            result = can_frame_try_send(&frame);
            if (result != CAN_FRAME_ACCEPTED && result != CAN_FRAME_BUSY) { failed(s, PROTOCOL_FAULT_TRANSPORT); }
        }
        return true;
    }
    if (r->sessionless_stop) { return false; }
    for (i = 0U; i < CAN_V1_CACHE_CAPACITY; ++i) {
        if (!s->cache[i].valid || !same_identity(r, &s->cache[i].request)) { continue; }
        if (same_content(r, &s->cache[i].request)) { queue(s, &s->cache[i].response, is_stop(r)); }
        else { reject(s, r, CORE_SEQUENCE_CONFLICT, false); }
        return true;
    }
    return false;
}
static void cancel(CanServer *s, uint32_t ticket)
{
    if (!s->ordinary.active || s->ordinary.ticket != ticket) { failed(s, PROTOCOL_FAULT_TRANSACTION_CORRUPT); return; }
    answer(s, &s->ordinary.request, CORE_CANCELLED_BY_STOP, CORE_MOTOR_RESULT_UNAVAILABLE, true, true);
    s->ordinary.active = false;
}
static void dispatch(CanServer *s, const CanRequest *r, bool internal_stop)
{
    ProtocolDeviceRequest request = {.kind = PROTOCOL_REQUEST_MOTOR};
    CanPending *pending = is_stop(r) ? &s->stop : &s->ordinary;
    ProtocolSubmitResult result;
    uint32_t ticket = 0U, cancelled = 0U;
    if (pending->active || (!is_stop(r) && s->stop.active)) { reject(s, r, CORE_BUSY, !r->sessionless_stop); return; }
    switch (r->service) {
    case CAN_V1_START: request.value.motor.command_id = CORE_START_VELOCITY; break;
    case CAN_V1_UPDATE: request.value.motor.command_id = CORE_UPDATE_VELOCITY; break;
    case CAN_V1_STOP: request.value.motor.command_id = CORE_STOP; break;
    case CAN_V1_CLEAR: request.value.motor.command_id = CORE_CLEAR_FAULT; break;
    default: failed(s, PROTOCOL_FAULT_TRANSACTION_CORRUPT); return;
    }
    request.value.motor.target_velocity_rpm = (float)r->target_x100 / 100.0F;
    result = protocol_device_submit(&request, &ticket, &cancelled);
    if (!valid_state(s)) { return; }
    if (result != PROTOCOL_SUBMIT_QUEUED) {
        if (internal_stop || (result != PROTOCOL_SUBMIT_BUSY && result != PROTOCOL_SUBMIT_BAD_REQUEST
            && result != PROTOCOL_SUBMIT_PRECONDITION_FAILED)) { failed(s, PROTOCOL_FAULT_TRANSACTION_CORRUPT); return; }
        reject(s, r, result == PROTOCOL_SUBMIT_BUSY ? CORE_BUSY
            : result == PROTOCOL_SUBMIT_PRECONDITION_FAILED ? CORE_PRECONDITION_FAILED : CORE_BAD_REQUEST,
            !r->sessionless_stop);
        return;
    }
    if (ticket == 0U || (s->ordinary.active && s->ordinary.ticket == ticket)
        || (s->stop.active && s->stop.ticket == ticket) || (!is_stop(r) && cancelled != 0U)) {
        failed(s, PROTOCOL_FAULT_TRANSACTION_CORRUPT); return;
    }
    pending->request = *r; pending->ticket = ticket; pending->started_ms = s->now_ms;
    pending->active = true; pending->internal_stop = internal_stop;
    if (cancelled != 0U) { cancel(s, cancelled); }
}

void can_server_init(CanServer *s, uint8_t node)
{
    memset(s, 0, sizeof(*s)); s->node = node;
    if (node < 1U || node > 127U) { failed(s, PROTOCOL_FAULT_TRANSACTION_CORRUPT); }
}

void can_server_feed(CanServer *s, const CanFrame *frame, uint32_t now_ms)
{
    CanRequest request;
    CoreResult decoded;
    s->now_ms = now_ms;
    if (!valid_state(s) || s->suspended) { return; }
    if (!can_v1_request_envelope(s->node, frame)) { s->rejected_frames++; return; }
    decoded = can_v1_request_decode(s->node, frame, &request);
    if (request.service == CAN_V1_OPEN_SESSION && !request.sessionless_stop) {
        if (decoded != CORE_RESULT_OK) { reject(s, &request, decoded, false); return; }
        if (s->ordinary.active) { reject(s, &request, CORE_SESSION_BUSY, false); return; }
        if (request.session != s->session) {
            invalidate_session(s);
            s->subscription_id = protocol_device_telemetry_begin();
            if (s->subscription_id == 0U) { failed(s, PROTOCOL_FAULT_TRANSACTION_CORRUPT); return; }
            s->session = request.session;
            s->response_count = s->event_count = 0U; s->snapshot_valid = false;
        }
        reject(s, &request, CORE_RESULT_OK, false); return;
    }
    if (!request.sessionless_stop) {
        if (request.sequence == 0U || request.sequence == 0xFFFFU) { reject(s, &request, CORE_SEQUENCE_INVALID, false); return; }
        if (request.session == 0U || request.session != s->session) {
            reject(s, &request, request.session == 0U ? CORE_SESSION_REQUIRED : CORE_SESSION_INVALID, false); return;
        }
    } else if (decoded != CORE_RESULT_OK) {
        s->rejected_frames++; return; /* reserved不合法的高优先级帧不获Stop身份。 */
    }
    if (replay(s, &request)) { return; }
    if (!request.sessionless_stop) {
        if (request.sequence <= s->highest_sequence) { reject(s, &request, CORE_SEQUENCE_INVALID, false); return; }
        s->highest_sequence = request.sequence;
    }
    if (decoded != CORE_RESULT_OK) { reject(s, &request, decoded, true); return; }
    dispatch(s, &request, false);
}

static void completed(CanServer *s, const ProtocolDeviceCompletion *value)
{
    CanPending *p = s->stop.active && s->stop.ticket == value->ticket ? &s->stop : &s->ordinary;
    CoreResult result;
    uint16_t motor = CORE_MOTOR_RESULT_UNAVAILABLE;
    if (!p->active || value->ticket == 0U || p->ticket != value->ticket) { failed(s, PROTOCOL_FAULT_TRANSACTION_CORRUPT); return; }
    if (value->outcome == PROTOCOL_CANCELLED_BY_STOP) { result = CORE_CANCELLED_BY_STOP; }
    else if (value->outcome == PROTOCOL_PRECONDITION_FAILED) { result = CORE_PRECONDITION_FAILED; }
    else if (value->outcome != PROTOCOL_COMPLETED || value->kind != PROTOCOL_REQUEST_MOTOR) {
        failed(s, PROTOCOL_FAULT_TRANSACTION_CORRUPT); return;
    } else { result = CORE_RESULT_OK; motor = value->motor_result; }
    if (s->suspended && is_stop(&p->request) && (result != CORE_RESULT_OK || motor != CORE_MOTOR_RESULT_ACCEPTED)) {
        failed(s, PROTOCOL_FAULT_TRANSACTION_CORRUPT); return;
    }
    if (!p->internal_stop) { answer(s, &p->request, result, motor, is_stop(&p->request), true); }
    p->active = false;
}

void can_server_link_down(CanServer *s)
{
    uint32_t i, kept = 0U;
    CanRequest stop;
    if (!valid_state(s) || s->suspended) { return; }
    s->suspended = true; invalidate_session(s); s->event_count = 0U; s->heartbeat_pending = false;
    for (i = 0U; i < s->response_count; ++i) {
        if (s->responses[i].critical) { s->responses[kept++] = s->responses[i]; }
    }
    s->response_count = kept; protocol_device_telemetry_retire();
    if (!s->stop.active) {
        memset(&stop, 0, sizeof(stop)); stop.service = CAN_V1_STOP; stop.sessionless_stop = true;
        dispatch(s, &stop, true);
    }
}

bool can_server_recovered(CanServer *s)
{
    if (!valid_state(s)) { return false; }
    if (!s->suspended || s->ordinary.active || s->stop.active) { failed(s, PROTOCOL_FAULT_TRANSACTION_CORRUPT); return false; }
    s->suspended = false; s->snapshot_valid = false;
    return true; /* Session仍为0；绝不重放旧Start/Update。 */
}

void can_server_poll(CanServer *s, uint32_t now_ms)
{
    ProtocolDeviceCompletion completion;
    CanFrameStatus status;
    uint32_t i;
    s->now_ms = now_ms;
    if (!valid_state(s)) { return; }
    if (!can_frame_read_status(&status)) { failed(s, PROTOCOL_FAULT_TRANSPORT); return; }
    if (status.rx_lost != s->rx_lost_seen) { failed(s, PROTOCOL_FAULT_TRANSPORT); return; }
    if (status.bus_off) { can_server_link_down(s); }
    if ((s->ordinary.active && (uint32_t)(now_ms - s->ordinary.started_ms) >= CAN_V1_COMPLETION_TIMEOUT_MS)
        || (s->stop.active && (uint32_t)(now_ms - s->stop.started_ms) >= CAN_V1_COMPLETION_TIMEOUT_MS)) {
        failed(s, PROTOCOL_FAULT_TIMEOUT); return;
    }
    for (i = 0U; i < CAN_V1_RESPONSE_CAPACITY; ++i) {
        if (!protocol_device_completion(&completion)) { break; }
        if (!s->failed) { completed(s, &completion); }
    }
    if (!s->suspended && (uint32_t)(now_ms - s->last_heartbeat_ms) >= CAN_V1_HEARTBEAT_MS) {
        s->last_heartbeat_ms = now_ms; s->heartbeat_counter++; s->heartbeat_pending = true;
    }
}

void can_server_observe(CanServer *s)
{
    MotorNodeValue value;
    uint32_t i, newly;
    if (!valid_state(s) || s->suspended) { return; }
    if (protocol_device_read_object(CORE_MOTOR_SNAPSHOT, &value) != CORE_RESULT_OK
        || (value.motor_snapshot.valid_flags & 0xC1U) != 0xC1U) { s->snapshot_valid = false; return; }
    newly = s->snapshot_valid ? value.motor_snapshot.fault_flags & ~s->previous_faults : 0U;
    if (newly != 0U) {
        if (s->event_count == CAN_V1_EVENT_CAPACITY) {
            for (i = 1U; i < CAN_V1_EVENT_CAPACITY; ++i) { s->events[i - 1U] = s->events[i]; }
            s->event_count--; s->event_drop_count++;
        }
        can_v1_fault(s->node, newly, value.motor_snapshot.fault_flags, &s->events[s->event_count++]);
    }
    s->snapshot = value.motor_snapshot; s->previous_faults = s->snapshot.fault_flags; s->snapshot_valid = true;
}

void can_server_flush(CanServer *s)
{
    CanFrame frame;
    CanFrameStatus status;
    CanFrameResult result;
    ProtocolTelemetryValue telemetry;
    uint32_t i, selected = 0U, source;
    uint8_t flags;
    if (!valid_state(s) || s->suspended) { return; }
    if (s->response_count != 0U) {
        source = 0U;
        for (i = 0U; i < s->response_count; ++i) { if (s->responses[i].critical) { selected = i; break; } }
        frame = s->responses[selected].frame;
    } else if (s->event_count != 0U) { source = 1U; frame = s->events[0]; }
    else if (s->heartbeat_pending) {
        source = 2U;
        if (!can_frame_read_status(&status)) { failed(s, PROTOCOL_FAULT_TRANSPORT); return; }
        flags = (s->snapshot_valid ? 1U : 0U) | (s->session != 0U ? 2U : 0U)
            | (status.error_warning ? 4U : 0U) | (status.error_passive ? 8U : 0U)
            | (status.bus_off || status.recovering ? 16U : 0U) | (s->failed ? 32U : 0U);
        can_v1_state(s->node, s->heartbeat_counter, s->snapshot_valid ? (uint8_t)s->snapshot.motor_state : 0U,
            flags, s->snapshot_valid ? s->snapshot.fault_flags : 0U, &frame);
    } else {
        source = 3U;
        if (!protocol_device_telemetry_peek(&telemetry)) { return; }
        if (s->session == 0U || telemetry.subscription_id != s->subscription_id) { protocol_device_telemetry_retire(); return; }
        can_v1_motion(s->node, (uint16_t)telemetry.sample.sample_sequence, &telemetry.sample, &frame);
    }
    result = can_frame_try_send(&frame);
    if (result == CAN_FRAME_BUSY) { return; }
    if (result != CAN_FRAME_ACCEPTED) { failed(s, PROTOCOL_FAULT_TRANSPORT); return; }
    if (source == 0U) {
        for (i = selected + 1U; i < s->response_count; ++i) { s->responses[i - 1U] = s->responses[i]; }
        s->response_count--;
    } else if (source == 1U) {
        for (i = 1U; i < s->event_count; ++i) { s->events[i - 1U] = s->events[i]; }
        s->event_count--;
    } else if (source == 2U) { s->heartbeat_pending = false; }
    else { protocol_device_telemetry_retire(); }
}
