#ifndef G431_TARGET_COMMUNICATION_H
#define G431_TARGET_COMMUNICATION_H
#include <stdbool.h>
#include <stdint.h>

/* 硬件初始化在调度器前；start在Bootstrap，service由唯一Communication任务调用。 */
void target_communication_hardware_init(void);
bool target_communication_start(void);
void target_communication_service(void);
uint32_t target_communication_telemetry_epoch(void);
#endif
