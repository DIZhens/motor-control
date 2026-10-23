#include "motor_node_binding_target.h"
#include "../runtime/target_runtime.h"
#include "../runtime/firmware_build_identity.h"

/* Target只连接本机身份与既有Runtime publication/交接，不拥有协议事务。 */
bool motor_node_binding_target_info(CoreDeviceInfo *value)
{
    *value = (CoreDeviceInfo){
        .protocol_version = 1U, .device_contract_version = 1U,
        .target_id = 1U, .control_profile_id = 1U,
        .capability_flags = CORE_CAPABILITY_OBJECT_READ | CORE_CAPABILITY_FINITE_VELOCITY
            | CORE_CAPABILITY_STAGED_SPEED_PI | CORE_CAPABILITY_TELEMETRY
            | CORE_CAPABILITY_EVENT | CORE_CAPABILITY_SESSIONLESS_STOP,
        .control_time_ticks_per_second = 20000U, .firmware_build_id = G431_FIRMWARE_BUILD_ID
    };
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
bool motor_node_binding_target_permitted(void) { return target_runtime_integrity_ok(); }
void motor_node_binding_target_fail_closed(RuntimeInternalFaultReason reason)
{ target_runtime_fail_closed(reason); }
uint32_t motor_node_binding_target_telemetry_begin(void) { return system_runtime_telemetry_begin(); }
void motor_node_binding_target_telemetry_end(void) { system_runtime_telemetry_end(); }
bool motor_node_binding_target_telemetry_peek(RuntimeTelemetryValue *value)
{ return system_runtime_telemetry_peek(value); }
void motor_node_binding_target_telemetry_retire(void) { system_runtime_telemetry_retire(); }
