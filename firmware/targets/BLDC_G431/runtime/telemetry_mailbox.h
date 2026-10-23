#ifndef TELEMETRY_MAILBOX_H
#define TELEMETRY_MAILBOX_H

#include <stdbool.h>
#include "motor_telemetry.h"

/* 协议无关的后台单消费者订阅；不进入控制ISR。
 * 产生计数只由Telemetry推进；订阅身份由SystemRuntime分配，generation来自实时参数。 */
typedef struct {
    MotorTelemetrySample sample;
    uint32_t subscription_id, produced, subscription_first;
} RuntimeTelemetryValue;

/* 唯一消费者上下文调用。begin替换旧订阅并清槽，返回本启动非零身份；
 * 未装配或身份耗尽返回0，调用者必须Fail-closed。end幂等清槽；二者不重置产生/覆盖计数。 */
uint32_t system_runtime_telemetry_begin(void);
void system_runtime_telemetry_end(void);
/* 唯一Communication消费者；peek不取走，只有TX接受或域失效时才退休。 */
bool system_runtime_telemetry_peek(RuntimeTelemetryValue *value);
void system_runtime_telemetry_retire(void);

#endif
