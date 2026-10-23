#include "motor_node_binding_target.h"
#include "../runtime/target_runtime.h"
#include "../runtime/firmware_build_identity.h"

/* Target只连接本机身份与既有Runtime publication/交接，不拥有协议事务。 */
bool motor_node_binding_target_info(CoreDeviceInfo *value)
{
    *value = (CoreDeviceInfo){1U, 1U, 1U, 1U, 0x3FU, 20000U, G431_FIRMWARE_BUILD_ID};
    return true;
}
bool motor_node_binding_target_snapshot(MotorSnapshot *value)
{ return target_runtime_read_snapshot(value); }
bool motor_node_binding_target_parameters(RuntimeParameters *value)
{ return target_runtime_read_parameters(value); }
RuntimeExchangeResult motor_node_binding_target_submit(const RuntimeRequest *request,
    uint32_t *ticket, uint32_t *cancelled)
{ return target_runtime_submit_request(request, ticket, cancelled); }
bool motor_node_binding_target_completion(RuntimeCommandCompletion *value)
{ return target_runtime_pop_completion(value); }
bool motor_node_binding_target_permitted(void) { return target_runtime_session_permitted(); }
void motor_node_binding_target_fail_closed(RuntimeInternalFaultReason reason)
{ target_runtime_fail_closed(reason); }
bool motor_node_binding_target_telemetry_peek(RuntimeTelemetryValue *value)
{ return system_runtime_telemetry_peek(value); }
void motor_node_binding_target_telemetry_retire(void) { system_runtime_telemetry_retire(); }
