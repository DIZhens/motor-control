#include "can_codec.h"
#include <string.h>

static uint16_t read16(const uint8_t *p) { return (uint16_t)(p[0] | (uint16_t)p[1] << 8); }
static void write16(uint8_t *p, uint16_t v) { p[0] = (uint8_t)v; p[1] = (uint8_t)(v >> 8); }
static void write32(uint8_t *p, uint32_t v)
{ write16(p, (uint16_t)v); write16(p + 2, (uint16_t)(v >> 16)); }
static bool node_valid(uint8_t node) { return node >= 1U && node <= 127U; }
static CanFrame make_frame(uint16_t id)
{
    CanFrame frame = {0}; frame.id = id; frame.dlc = 8U; return frame;
}

bool can_v1_request_envelope(uint8_t node, const CanFrame *frame)
{
    return node_valid(node) && frame != NULL && !frame->extended && !frame->remote
        && frame->dlc == 8U && (frame->id == CAN_V1_BROADCAST_STOP
        || frame->id == CAN_V1_NODE_STOP_BASE + node || frame->id == CAN_V1_REQUEST_BASE + node);
}

CoreResult can_v1_request_decode(uint8_t node, const CanFrame *frame, CanRequest *request)
{
    CanRequest value;
    uint32_t index;
    if (request == NULL || !can_v1_request_envelope(node, frame)) { return CORE_BAD_REQUEST; }
    memset(&value, 0, sizeof(value)); value.original = *frame;
    value.sessionless_stop = frame->id != CAN_V1_REQUEST_BASE + node;
    value.session = value.sessionless_stop ? 0U : read16(frame->data);
    value.sequence = read16(frame->data + (value.sessionless_stop ? 0 : 2));
    value.service = value.sessionless_stop ? CAN_V1_STOP : frame->data[4];
    value.target_x100 = value.sessionless_stop ? 0 : (int16_t)read16(frame->data + 6);
    *request = value;
    if (value.sessionless_stop) {
        for (index = 2U; index < 8U; ++index) { if (frame->data[index] != 0U) { return CORE_BAD_PAYLOAD; } }
        return CORE_RESULT_OK;
    }
    if (frame->data[5] != 0U) { return CORE_UNSUPPORTED_FLAGS; }
    if (value.service > CAN_V1_CLEAR) { return CORE_UNKNOWN_COMMAND; }
    if (value.service == CAN_V1_OPEN_SESSION) {
        return value.session != 0U && value.sequence == 0U && value.target_x100 == 0
            ? CORE_RESULT_OK : CORE_BAD_REQUEST;
    }
    if (value.service == CAN_V1_START || value.service == CAN_V1_UPDATE) {
        return value.target_x100 >= -8000 && value.target_x100 <= 8000 ? CORE_RESULT_OK : CORE_BAD_VALUE;
    }
    return value.target_x100 == 0 ? CORE_RESULT_OK : CORE_BAD_PAYLOAD;
}

CoreResult can_v1_request_encode(uint8_t node, uint16_t session, uint16_t sequence,
    uint8_t service, int16_t target_x100, CanFrame *frame)
{
    CanFrame value = make_frame((uint16_t)(CAN_V1_REQUEST_BASE + node));
    CanRequest check;
    CoreResult result;
    if (frame == NULL || !node_valid(node)) { return CORE_BAD_REQUEST; }
    write16(value.data, session); write16(value.data + 2, sequence);
    value.data[4] = service; write16(value.data + 6, (uint16_t)target_x100);
    result = can_v1_request_decode(node, &value, &check);
    if (result != CORE_RESULT_OK) { return result; }
    *frame = value; return CORE_RESULT_OK;
}

CoreResult can_v1_stop_encode(uint8_t node, bool broadcast, uint16_t sequence, CanFrame *frame)
{
    CanFrame value;
    if (frame == NULL || !node_valid(node)) { return CORE_BAD_REQUEST; }
    value = make_frame(broadcast ? CAN_V1_BROADCAST_STOP : (uint16_t)(CAN_V1_NODE_STOP_BASE + node));
    write16(value.data, sequence); *frame = value; return CORE_RESULT_OK;
}

void can_v1_response(uint8_t node, uint16_t session, uint16_t sequence,
    CoreResult result, uint16_t motor_result, CanFrame *frame)
{
    *frame = make_frame((uint16_t)(CAN_V1_RESPONSE_BASE + node));
    write16(frame->data, session); write16(frame->data + 2, sequence);
    write16(frame->data + 4, (uint16_t)result); write16(frame->data + 6, motor_result);
}

bool can_v1_rpm_x100(float rpm, int16_t *value)
{
    float scaled = rpm * 100.0F;
    /* 比较同时拒绝NaN/Inf；边界半值向外舍入时不可表示。 */
    if (value == NULL || !(scaled > -32768.5F && scaled < 32767.5F)) { return false; }
    *value = (int16_t)(int32_t)(scaled >= 0.0F ? scaled + 0.5F : scaled - 0.5F);
    return true;
}

void can_v1_state(uint8_t node, uint16_t counter, uint8_t state,
    uint8_t flags, uint32_t faults, CanFrame *frame)
{
    *frame = make_frame((uint16_t)(CAN_V1_STATE_BASE + node));
    write16(frame->data, counter); frame->data[2] = state; frame->data[3] = flags;
    write32(frame->data + 4, faults);
}

void can_v1_motion(uint8_t node, uint16_t sequence, const CoreSpeedSample *sample, CanFrame *frame)
{
    int16_t target = 0, velocity = 0;
    uint16_t flags = 0U;
    *frame = make_frame((uint16_t)(CAN_V1_MOTION_BASE + node));
    if ((sample->valid_flags & CORE_SAMPLE_VALID_TARGET) != 0U && can_v1_rpm_x100(sample->target_velocity_rpm, &target)) {
        flags |= CAN_V1_TARGET_VALID;
    }
    if ((sample->valid_flags & CORE_SAMPLE_VALID_VELOCITY) != 0U && can_v1_rpm_x100(sample->velocity_rpm, &velocity)) {
        flags |= CAN_V1_VELOCITY_VALID;
    }
    flags |= (uint16_t)((sample->limit_flags & CORE_SAMPLE_LIMIT_MASK) << CAN_V1_LIMIT_SHIFT);
    write16(frame->data, sequence); write16(frame->data + 2, (uint16_t)target);
    write16(frame->data + 4, (uint16_t)velocity); write16(frame->data + 6, flags);
}

void can_v1_fault(uint8_t node, uint32_t newly_latched, uint32_t all, CanFrame *frame)
{
    *frame = make_frame((uint16_t)(CAN_V1_FAULT_BASE + node));
    write32(frame->data, newly_latched); write32(frame->data + 4, all);
}
