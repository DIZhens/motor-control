#ifndef G431_TARGET_CAN_SERVER_H
#define G431_TARGET_CAN_SERVER_H
#include <stdint.h>
void target_can_server_start(void);
void target_can_server_service(void);
uint32_t target_can_server_telemetry_epoch(void);
#endif
