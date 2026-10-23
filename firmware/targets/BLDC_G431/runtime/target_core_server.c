#include "target_core_server.h"
#include "target_runtime.h"
#include "system_runtime.h"
#include "core_server.h"
#include "byte_stream_transport.h"
#include "main.h"

static CoreServer s_server;
uint32_t target_core_server_telemetry_epoch(void)
{ return target_runtime_session_permitted() ? s_server.active_epoch : 0U; }
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

/* UART专属统计留在此适配器；共享设备值由Firmware Binding提供。 */
CoreResult core_server_port_read_object(uint16_t id, CoreObject *value)
{
    RuntimeInternalFaultStatus fault;
    ByteStreamStats transport;
    CoreCommunicationStatus *c = &value->communication_status;
    MotorNodeValue node;
    CoreResult result;
    if (id != CORE_COMMUNICATION_STATUS) {
        result = protocol_device_read_object(id, &node);
        if (result != CORE_RESULT_OK) { return result; }
        switch (id) {
        case CORE_DEVICE_INFO: value->device_info = node.device_info; break;
        case CORE_MOTOR_SNAPSHOT: value->motor_snapshot = node.motor_snapshot; break;
        case CORE_SPEED_PI_DEFAULT: case CORE_SPEED_PI_STAGED: case CORE_SPEED_PI_ACTIVE:
            value->speed_pi = node.speed_pi; break;
        case CORE_TELEMETRY_CONFIG: value->telemetry_config = node.telemetry_config; break;
        default: return CORE_UNKNOWN_OBJECT;
        }
        return CORE_RESULT_OK;
    }
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
}
