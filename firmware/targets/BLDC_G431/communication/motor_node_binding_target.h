#ifndef MOTOR_NODE_BINDING_TARGET_H
#define MOTOR_NODE_BINDING_TARGET_H

#include "motor_node_profile.h"
#include "motor_snapshot.h"
#include "runtime_parameters.h"
#include "telemetry_mailbox.h"

/* Target提供物理身份和一致publication；Firmware绑定不依赖具体板型、链路或调度器。 */
bool motor_node_binding_target_info(CoreDeviceInfo *value);
bool motor_node_binding_target_snapshot(MotorSnapshot *value);
bool motor_node_binding_target_parameters(RuntimeParameters *value);
RuntimeExchangeResult motor_node_binding_target_submit(const RuntimeRequest *request,
    uint32_t *ticket, uint32_t *cancelled_ticket);
bool motor_node_binding_target_completion(RuntimeCommandCompletion *completion);
bool motor_node_binding_target_permitted(void);
void motor_node_binding_target_fail_closed(RuntimeInternalFaultReason reason);
uint32_t motor_node_binding_target_telemetry_begin(void);
void motor_node_binding_target_telemetry_end(void);
bool motor_node_binding_target_telemetry_peek(RuntimeTelemetryValue *value);
void motor_node_binding_target_telemetry_retire(void);

#endif
