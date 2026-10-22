#ifndef SYSTEM_RUNTIME_H
#define SYSTEM_RUNTIME_H

#include <stdint.h>

/* 链接到RTOS Target的后台装配入口；不从控制ISR调用。 */
void system_runtime_start(void);
void system_runtime_assert_failed(void);

typedef enum {
    SYSTEM_RUNTIME_RESET = 0,
    SYSTEM_RUNTIME_SCHEDULER_START = 1,
    SYSTEM_RUNTIME_BOOTSTRAP = 2,
    SYSTEM_RUNTIME_READY = 3,
    SYSTEM_RUNTIME_FAILED = 4
} SystemRuntimePhase;

/* 调试只读；跨任务字段各自单写，不把整份结构视为原子快照。栈单位为word。 */
typedef struct {
    uint32_t phase;
    uint32_t hal_tick_before_scheduler, hal_tick_bootstrap, kernel_tick_bootstrap;
    uint32_t communication_passes, telemetry_publications, telemetry_consumed;
    uint32_t latest_sample_sequence;
    uint32_t bootstrap_stack_free, communication_stack_free, telemetry_stack_free, idle_stack_free;
} SystemRuntimeStatus;
extern volatile SystemRuntimeStatus g_system_runtime_status;

#endif
