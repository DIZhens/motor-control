#include "target_core_server.h"
#include "target_runtime.h"
#include "system_runtime.h"
#include "core_server.h"
#include "byte_stream_transport.h"
#include "firmware_build_identity.h"
#include "main.h"

static CoreServer s_server;
uint32_t target_core_server_telemetry_epoch(void)
{ return target_runtime_session_permitted() ? s_server.active_epoch : 0U; }
bool core_server_port_telemetry_peek(RuntimeTelemetryValue *value)
{ return system_runtime_telemetry_peek(value); }
void core_server_port_telemetry_retire(void) { system_runtime_telemetry_retire(); }

void target_core_server_start(void) { core_server_init(&s_server); }
void target_core_server_service(void)
{
    uint8_t bytes[128];
    uint32_t epoch, batch;
    size_t count;
    ByteStreamResult result;
    core_server_poll(&s_server, HAL_GetTick());
    for (batch = 0U; batch < 4U; ++batch) {
        result = byte_stream_read(bytes, sizeof(bytes), &count, &epoch);
        if (result == BYTE_STREAM_DISCONTINUITY) { core_server_discontinuity(&s_server); continue; }
        if (result == BYTE_STREAM_EMPTY || result == BYTE_STREAM_HARDWARE_ERROR) { break; }
        if (result != BYTE_STREAM_OK) { target_runtime_fail_closed(RUNTIME_INTERNAL_TRANSPORT); break; }
        core_server_feed(&s_server, bytes, count, HAL_GetTick());
    }
    core_server_poll(&s_server, HAL_GetTick());
    core_server_flush(&s_server);
    /* 先处理完请求/Completion并尝试续帧，再在DMA传输期间采集最佳努力事件。 */
    core_server_observe(&s_server);
}

bool core_server_port_permitted(void) { return target_runtime_session_permitted(); }
void core_server_port_fail_closed(RuntimeInternalFaultReason reason) { target_runtime_fail_closed(reason); }
RuntimeExchangeResult core_server_port_submit(const RuntimeRequest *request, uint32_t *ticket, uint32_t *cancelled)
{ return target_runtime_submit_request(request, ticket, cancelled); }
bool core_server_port_completion(RuntimeCommandCompletion *completion)
{ return target_runtime_pop_completion(completion); }

CoreResult core_server_port_read_object(uint16_t id, CoreObject *value)
{
    MotorSnapshot snapshot;
    RuntimeParameters parameters;
    RuntimeInternalFaultStatus fault;
    ByteStreamStats transport;
    CoreMotorSnapshot *m = &value->motor_snapshot;
    CoreCommunicationStatus *c = &value->communication_status;
    const RuntimeSpeedProfile *profile;
    switch (id) {
    case CORE_DEVICE_INFO:
        value->device_info = (CoreDeviceInfo){1U, 1U, 1U, 1U, 0x3FU, 20000U, G431_FIRMWARE_BUILD_ID};
        return CORE_RESULT_OK;
    case CORE_MOTOR_SNAPSHOT:
        if (!target_runtime_read_snapshot(&snapshot)) { return CORE_INTERNAL_ERROR; }
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
    case CORE_COMMUNICATION_STATUS:
        if (!byte_stream_read_stats(&transport) || !target_runtime_read_internal_fault(&fault)) { return CORE_INTERNAL_ERROR; }
        *c = (CoreCommunicationStatus){0};
        c->rx_bytes = transport.rx_bytes; c->tx_bytes = transport.tx_bytes;
        c->rx_discontinuities = transport.rx_discontinuities; c->rx_dma_overruns = transport.buffer_overruns;
        c->uart_pe = transport.parity_errors; c->uart_fe = transport.framing_errors;
        c->uart_ne = transport.noise_errors; c->uart_ore = transport.hardware_rx_overruns;
        c->completion_fifo_high_water = target_runtime_completion_high_water();
        c->communication_stack_min_words = g_system_runtime_status.communication_stack_free;
        c->telemetry_stack_min_words = g_system_runtime_status.telemetry_stack_free;
        c->internal_fault_reason = (uint32_t)fault.first_reason; c->internal_fault_count = fault.count;
        c->tx_busy_count = transport.tx_busy_count; c->tx_errors = transport.tx_errors;
        c->telemetry_overwrites = g_system_runtime_status.telemetry_overwrites;
        return CORE_RESULT_OK;
    case CORE_SPEED_PI_DEFAULT: case CORE_SPEED_PI_STAGED: case CORE_SPEED_PI_ACTIVE: case CORE_TELEMETRY_CONFIG:
        if (!target_runtime_read_parameters(&parameters)) { return CORE_INTERNAL_ERROR; }
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
