#ifndef SYSTEM_RUNTIME_TARGET_H
#define SYSTEM_RUNTIME_TARGET_H

#include "runtime_exchange.h"
#include "motor_telemetry.h"

/* 链接期Target集成，不扩展为运行期硬件注册表。 */
uint32_t system_runtime_target_hal_tick(void);
bool system_runtime_target_pre_scheduler(void);
bool system_runtime_target_bootstrap(void);
/* 任务句柄和Motor装配完成后才绑定事件并开放RX。 */
bool system_runtime_target_start_transport(void);
/* 唯一Communication上下文；最迟1ms调用，限制一次处理预算。 */
void system_runtime_target_communication_service(void);
bool system_runtime_target_read_motor_telemetry(MotorTelemetrySample *sample);
bool system_runtime_target_read_telemetry(RuntimeTelemetryConfig *config);
/* 必须先关功率；不可依赖调度器、队列或串口。生产Target不返回。 */
void system_runtime_target_fatal(RuntimeInternalFaultReason reason);

#endif
