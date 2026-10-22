#ifndef SYSTEM_RUNTIME_TARGET_H
#define SYSTEM_RUNTIME_TARGET_H

#include "runtime_exchange.h"
#include "speed_commissioning.h"

/* 链接期Target集成，不扩展为运行期硬件注册表。 */
uint32_t system_runtime_target_hal_tick(void);
bool system_runtime_target_pre_scheduler(void);
bool system_runtime_target_bootstrap(void);
bool system_runtime_target_read_sample(SpeedCommissioningSample *sample);
bool system_runtime_target_read_telemetry(RuntimeTelemetryConfig *config);
/* 必须先关功率；不可依赖调度器、队列或串口。生产Target不返回。 */
void system_runtime_target_fatal(RuntimeInternalFaultReason reason);

#endif
