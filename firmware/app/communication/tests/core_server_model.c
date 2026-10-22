#include "core_server.h"
#include "byte_stream_transport.h"
#include "runtime_parameters.h"
#include <string.h>

/* 真实Server/Codec/交接/参数与Motor；无HAL/Kernel/电机。
 * C库复制采用模型逐字节替身；正式构建保持供应C库，真实串口另外验证。 */
void __aeabi_memcpy(void *out, const void *in, size_t count)
{
    volatile uint8_t *to = out; const volatile uint8_t *from = in;
    while (count != 0U) { *to++ = *from++; count--; }
}
void __aeabi_memcpy4(void *out, const void *in, size_t count) { __aeabi_memcpy(out, in, count); }
CoreServer model_server;
RuntimeExchange model_exchange;
RuntimeParameters model_parameters;
Motor model_motor;
uint8_t model_input[1024], model_tx[130];
uint32_t model_now, model_tx_length, model_tx_busy, model_permitted, model_healthy;
uint32_t model_failure, model_failure_count, model_submit_count, model_start_ready;
uint32_t model_tx_bytes, model_tx_error, model_submit_override;
RuntimeTelemetryValue model_telemetry;
uint32_t model_telemetry_pending;
bool core_server_port_telemetry_peek(RuntimeTelemetryValue *value)
{ if (!model_telemetry_pending) { return false; } *value = model_telemetry; return true; }
void core_server_port_telemetry_retire(void) { model_telemetry_pending = 0U; }
void model_produce(uint32_t produced)
{
    model_telemetry = (RuntimeTelemetryValue){0};
    model_telemetry.epoch = model_server.active_epoch;
    model_telemetry.produced = produced; model_telemetry.epoch_first = 1U;
    model_telemetry.sample.sample_sequence = produced * 4U;
    model_telemetry.sample.telemetry_config_generation = model_parameters.telemetry.generation;
    model_telemetry.sample.valid_flags = 0x301U;
    model_telemetry_pending = 1U;
}
void model_event_state(uint32_t state) { model_motor.state = (MotorState)state; model_motor.faults = state == 3U ? 5U : 0U; }
void model_old_epoch(void) { model_telemetry.epoch--; }
void model_exhaust_sequences(void)
{ model_server.event_sequence = 0xFFFFFFFDU; }
void runtime_exchange_barrier(void) { __asm { DMB } }
void model_init(void)
{
    core_server_init(&model_server); runtime_exchange_init(&model_exchange);
    runtime_parameters_init(&model_parameters, 0.001F, 0.004F);
    motor_init(&model_motor); motor_finish_boot(&model_motor);
    model_permitted = model_healthy = 1U;
}
bool core_server_port_permitted(void) { return model_permitted != 0U; }
void core_server_port_fail_closed(RuntimeInternalFaultReason reason)
{
    if (model_failure == 0U) { model_failure = (uint32_t)reason; }
    model_failure_count++; model_permitted = 0U;
}
RuntimeExchangeResult core_server_port_submit(const RuntimeRequest *request, uint32_t *ticket, uint32_t *cancelled)
{
    model_submit_count++;
    if (model_submit_override != 0U) { return (RuntimeExchangeResult)model_submit_override; }
    return runtime_exchange_submit(&model_exchange, request, ticket, cancelled);
}
bool core_server_port_completion(RuntimeCommandCompletion *completion)
{ return runtime_exchange_pop(&model_exchange, completion); }
ByteStreamResult byte_stream_try_send(const uint8_t *bytes, size_t count)
{
    if (model_tx_error) { return BYTE_STREAM_HARDWARE_ERROR; }
    if (model_tx_busy) { return BYTE_STREAM_BUSY; }
    memcpy(model_tx, bytes, count); model_tx_length = count; model_tx_busy = 1U;
    model_tx_bytes += count;
    return BYTE_STREAM_ACCEPTED;
}
CoreResult core_server_port_read_object(uint16_t id, CoreObject *value)
{
    const RuntimeSpeedProfile *profile;
    memset(value, 0, sizeof(*value));
    switch (id) {
    case CORE_DEVICE_INFO: value->device_info = (CoreDeviceInfo){1,1,1,1,0x27,20000,0x123456789ABCDEF0ULL}; break;
    case CORE_MOTOR_SNAPSHOT:
        value->motor_snapshot.motor_state = model_motor.state; value->motor_snapshot.fault_flags = model_motor.faults;
        value->motor_snapshot.motor_command_result = model_motor.command_result;
        value->motor_snapshot.valid_flags = 0x1C3U;
        value->motor_snapshot.control_time_ticks_per_second = 20000U;
        value->motor_snapshot.target_velocity_rpm = model_motor.target_velocity_rpm;
        break;
    case CORE_COMMUNICATION_STATUS:
        value->communication_status.tx_bytes = model_tx_bytes;
        value->communication_status.internal_fault_reason = model_failure;
        value->communication_status.internal_fault_count = model_failure_count;
        value->communication_status.completion_fifo_high_water = model_exchange.completion_high_water;
        break;
    case CORE_SPEED_PI_DEFAULT: case CORE_SPEED_PI_STAGED: case CORE_SPEED_PI_ACTIVE:
        profile = id == CORE_SPEED_PI_ACTIVE ? &model_parameters.speed_active : &model_parameters.speed_staged;
        value->speed_pi = (CoreSpeedPi){profile->value.kp, profile->value.ki, profile->generation, profile->in_use};
        if (id == CORE_SPEED_PI_DEFAULT) { value->speed_pi = (CoreSpeedPi){0.001F,0.004F,0U,false}; }
        break;
    case CORE_TELEMETRY_CONFIG:
        value->telemetry_config = (CoreTelemetryConfig){model_parameters.telemetry.enabled ? 1U : 0U,
            model_parameters.telemetry.rate_hz, model_parameters.telemetry.generation}; break;
    default: return CORE_UNKNOWN_OBJECT;
    }
    return CORE_RESULT_OK;
}
void model_feed(uint32_t count) { core_server_feed(&model_server, model_input, count, model_now); }
void model_poll(void) { core_server_poll(&model_server, model_now); }
void model_flush(void) { core_server_flush(&model_server); }
void model_discontinuity(void) { core_server_discontinuity(&model_server); }
void model_rt_step(void)
{
    RuntimeCommandEnvelope envelope;
    RuntimeCommandCompletion completion = {0};
    uint32_t cancelled;
    if (!runtime_exchange_take(&model_exchange, &envelope, &cancelled)) { return; }
    if (cancelled != 0U) {
        completion.ticket = cancelled; completion.outcome = RUNTIME_CANCELLED_BY_STOP;
        if (!runtime_exchange_complete(&model_exchange, &completion)) { core_server_port_fail_closed(RUNTIME_INTERNAL_COMPLETION_OVERFLOW); }
    }
    if (envelope.ticket == 0U) { return; }
    completion = (RuntimeCommandCompletion){0}; completion.ticket = envelope.ticket;
    completion.kind = envelope.request.kind; completion.outcome = RUNTIME_COMPLETED;
    if (envelope.request.kind == RUNTIME_REQUEST_SPEED_PI) {
        completion.parameter_result = runtime_parameters_write_pi(&model_parameters,
            &envelope.request.value.speed_pi, model_healthy != 0U);
        completion.value.speed_pi = model_parameters.speed_staged;
    } else if (envelope.request.kind == RUNTIME_REQUEST_TELEMETRY) {
        completion.parameter_result = runtime_parameters_write_telemetry(&model_parameters,
            envelope.request.value.telemetry.enabled, envelope.request.value.telemetry.rate_hz, model_healthy != 0U);
        completion.value.telemetry = model_parameters.telemetry;
    } else {
        completion.motor_result = motor_handle_control_command(&model_motor, &envelope.request.value.motor,
            model_start_ready != 0U, 80.0F);
    }
    if (!runtime_exchange_complete(&model_exchange, &completion)) { core_server_port_fail_closed(RUNTIME_INTERNAL_COMPLETION_OVERFLOW); }
}
void model_unknown_completion(void)
{
    RuntimeCommandCompletion completion = {0}; completion.ticket = 999U;
    runtime_exchange_complete(&model_exchange, &completion);
}
void model_corrupt_state(uint32_t mode)
{
    if (mode == 0U) { model_server.response_count = CORE_RESPONSE_CAPACITY + 1U; }
    else if (mode == 1U) { model_server.cache_next = CORE_CACHE_CAPACITY; }
    else { model_server.ordinary.ticket = 0U; }
}
