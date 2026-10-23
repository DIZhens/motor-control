#include "core_server.h"
#include "byte_stream_transport.h"
#include "target_runtime.h"
#include "system_runtime.h"
#include "telemetry_mailbox.h"
#include <string.h>

/* 仅替代Target读端口的输入；实际target_core_server.c负责COMMUNICATION_STATUS映射。 */
volatile SystemRuntimeStatus g_system_runtime_status;
uint32_t model_mapping_communication_stack, model_mapping_telemetry_stack;
uint32_t model_mapping_unexpected_calls;

/* 非对象映射路径没有测试执行资格；若意外进入，计数使模型失败。 */
uint32_t HAL_GetTick(void) { model_mapping_unexpected_calls++; return 0U; }
ByteStreamResult byte_stream_read(uint8_t *bytes, size_t capacity, size_t *count, uint32_t *epoch)
{
    (void)bytes; (void)capacity; *count = 0U; *epoch = 0U;
    model_mapping_unexpected_calls++; return BYTE_STREAM_EMPTY;
}
bool system_runtime_telemetry_peek(RuntimeTelemetryValue *value)
{ (void)value; model_mapping_unexpected_calls++; return false; }
void system_runtime_telemetry_retire(void) { model_mapping_unexpected_calls++; }
void target_runtime_fail_closed(RuntimeInternalFaultReason reason)
{ (void)reason; model_mapping_unexpected_calls++; }
bool target_runtime_pop_completion(RuntimeCommandCompletion *value)
{ (void)value; model_mapping_unexpected_calls++; return false; }
bool target_runtime_integrity_ok(void) { model_mapping_unexpected_calls++; return false; }
RuntimeExchangeResult target_runtime_submit_request(const RuntimeRequest *value,
    uint32_t *ticket, uint32_t *cancelled)
{
    (void)value; *ticket = *cancelled = 0U;
    model_mapping_unexpected_calls++; return RUNTIME_EXCHANGE_BAD_REQUEST;
}

bool byte_stream_read_stats(ByteStreamStats *value)
{ memset(value, 0, sizeof(*value)); return true; }
bool target_runtime_read_internal_fault(RuntimeInternalFaultStatus *value)
{ memset(value, 0, sizeof(*value)); return true; }
uint32_t target_runtime_completion_high_water(void) { return 0U; }
bool target_runtime_read_snapshot(MotorSnapshot *value)
{ memset(value, 0, sizeof(*value)); return true; }
bool target_runtime_read_parameters(RuntimeParameters *value)
{ memset(value, 0, sizeof(*value)); return true; }

CoreResult mapping_core_server_port_read_object(uint16_t id, CoreObject *value);
void model_mapping_read_stacks(void)
{
    CoreObject value;
    if (mapping_core_server_port_read_object(CORE_COMMUNICATION_STATUS, &value) != CORE_RESULT_OK) {
        model_mapping_communication_stack = model_mapping_telemetry_stack = 0xDEADBEEFU;
        return;
    }
    model_mapping_communication_stack = value.communication_status.communication_stack_min_words;
    model_mapping_telemetry_stack = value.communication_status.telemetry_stack_min_words;
}
