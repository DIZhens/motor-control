#include "can_server.h"
#include "motor_node_binding_target.h"
#include <string.h>

/* 复用UART模型中的设备/Runtime试具，实际CAN源不包含UART头或实现。 */
extern void model_init(void);
extern void model_rt_step(void);
extern void model_unknown_completion(void);
extern void model_produce(uint32_t count);
extern void model_event_state(uint32_t state);
extern RuntimeTelemetryValue model_telemetry;
extern RuntimeExchange model_exchange;
extern uint32_t model_now;

CanServer can_model_server;
CanFrame can_model_input, can_model_tx, can_model_output;
CanFrameStatus can_model_status;
uint32_t can_model_args[8], can_model_tx_pending, can_model_tx_busy, can_model_tx_error;
uint32_t can_model_decoded[5], can_model_round_value;

static float from_bits(uint32_t bits)
{ union { uint32_t bits; float value; } v; v.bits = bits; return v.value; }

void can_model_init(void)
{
    model_init(); can_server_init(&can_model_server, 1U);
    can_model_status.started = true;
}
CanFrameResult can_frame_try_send(const CanFrame *frame)
{
    if (can_model_tx_error) { return CAN_FRAME_HARDWARE_ERROR; }
    if (can_model_tx_busy) { return CAN_FRAME_BUSY; }
    can_model_tx = *frame; can_model_tx_pending = can_model_tx_busy = 1U;
    return CAN_FRAME_ACCEPTED;
}
bool can_frame_read_status(CanFrameStatus *value) { *value = can_model_status; return true; }
void can_model_feed(void) { can_server_feed(&can_model_server, &can_model_input, model_now); }
void can_model_poll(void) { can_server_poll(&can_model_server, model_now); }
void can_model_flush(void) { can_server_flush(&can_model_server); }
void can_model_observe(void) { can_server_observe(&can_model_server); }
void can_model_event_state(uint32_t state) { model_event_state(state); }
void can_model_step(void) { model_rt_step(); }
void can_model_unknown(void) { model_unknown_completion(); }
uint32_t can_model_session(void) { return can_model_server.session; }
uint32_t can_model_highest(void) { return can_model_server.highest_sequence; }
uint32_t can_model_event_drops(void) { return can_model_server.event_drop_count; }
uint32_t can_model_sizeof_server(void) { return sizeof(CanServer); }
void can_model_link_flags(uint32_t flags)
{
    can_model_status.error_warning = (flags & 4U) != 0U;
    can_model_status.error_passive = (flags & 8U) != 0U;
    can_model_status.bus_off = (flags & 16U) != 0U;
}
uint32_t can_model_recover(void) { can_model_status.bus_off = false; return can_server_recovered(&can_model_server); }
void can_model_produce(uint32_t count)
{
    model_produce(count); model_telemetry.epoch = can_model_server.active_epoch;
    model_telemetry.sample.valid_flags = 12U;
    model_telemetry.sample.target_velocity_rpm = 40.0F;
    model_telemetry.sample.velocity_rpm = 39.995F;
}
void can_model_corrupt(uint32_t mode)
{
    if (mode == 0U) { can_model_server.response_count = CAN_V1_RESPONSE_CAPACITY + 1U; }
    else if (mode == 1U) { can_model_server.cache_next = CAN_V1_CACHE_CAPACITY; }
    else if (mode == 2U) { can_model_server.event_count = CAN_V1_EVENT_CAPACITY + 1U; }
    else if (mode == 3U) { can_model_server.ordinary.ticket = 0U; }
    else { can_model_status.rx_lost++; }
}

/* 恢复必须有可信Stop ACCEPTED；不能把拒绝/取消当成已关断的业务完成证明。 */
void can_model_bad_stop_completion(uint32_t mode)
{
    RuntimeCommandCompletion value = {0};
    value.ticket = can_model_server.stop.ticket; value.kind = RUNTIME_REQUEST_MOTOR;
    value.outcome = mode == 0U ? RUNTIME_PRECONDITION_FAILED : RUNTIME_COMPLETED;
    value.motor_result = MOTOR_COMMAND_RESULT_NOT_READY;
    runtime_exchange_complete(&model_exchange, &value);
}

uint32_t can_model_codec(uint32_t mode)
{
    uint8_t node = (uint8_t)can_model_args[0];
    CoreSpeedSample sample = {0};
    CanRequest request;
    CoreResult result;
    int16_t rounded = 0x1234;
    switch (mode) {
    case 0U:
        return can_v1_request_encode(node, (uint16_t)can_model_args[1], (uint16_t)can_model_args[2],
            (uint8_t)can_model_args[3], (int16_t)can_model_args[4], &can_model_output);
    case 1U:
        return can_v1_stop_encode(node, can_model_args[2] != 0U, (uint16_t)can_model_args[1], &can_model_output);
    case 2U:
        can_v1_response(node, (uint16_t)can_model_args[1], (uint16_t)can_model_args[2],
            (CoreResult)can_model_args[3], (uint16_t)can_model_args[4], &can_model_output); break;
    case 3U:
        can_v1_state(node, (uint16_t)can_model_args[1], (uint8_t)can_model_args[2],
            (uint8_t)can_model_args[3], can_model_args[4], &can_model_output); break;
    case 4U:
        sample.target_velocity_rpm = from_bits(can_model_args[2]); sample.velocity_rpm = from_bits(can_model_args[3]);
        sample.valid_flags = can_model_args[4]; sample.limit_flags = (uint16_t)can_model_args[5];
        can_v1_motion(node, (uint16_t)can_model_args[1], &sample, &can_model_output); break;
    case 5U: can_v1_fault(node, can_model_args[1], can_model_args[2], &can_model_output); break;
    case 6U:
        memset(&request, 0x5A, sizeof(request));
        result = can_v1_request_decode(node, &can_model_input, &request);
        can_model_decoded[0] = request.session; can_model_decoded[1] = request.sequence;
        can_model_decoded[2] = request.service; can_model_decoded[3] = (uint32_t)(int32_t)request.target_x100;
        can_model_decoded[4] = request.sessionless_stop ? 1U : 0U;
        return result;
    case 7U:
        result = can_v1_rpm_x100(from_bits(can_model_args[0]), &rounded) ? CORE_RESULT_OK : CORE_BAD_VALUE;
        can_model_round_value = (uint32_t)(int32_t)rounded; return result;
    default: return CORE_BAD_REQUEST;
    }
    return CORE_RESULT_OK;
}
