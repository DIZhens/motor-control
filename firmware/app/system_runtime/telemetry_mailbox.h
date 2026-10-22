#ifndef TELEMETRY_MAILBOX_H
#define TELEMETRY_MAILBOX_H

#include <stdbool.h>
#include "speed_commissioning.h"

/* 后台最新值交接；不进入控制ISR。产生计数只由Telemetry任务推进，
 * epoch由Communication发布，配置generation来自实时参数快照。 */
typedef struct {
    SpeedCommissioningSample sample;
    uint32_t epoch, produced, epoch_first;
} RuntimeTelemetryValue;

/* 唯一Communication消费者；peek不取走，只有TX接受或域失效时才退休。 */
bool system_runtime_telemetry_peek(RuntimeTelemetryValue *value);
void system_runtime_telemetry_retire(void);

#endif
