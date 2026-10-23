#ifndef G431_CAN_LOOPBACK_H
#define G431_CAN_LOOPBACK_H
#include "can_frame_port.h"
#include "can_server.h"

/* 仅内部回环专项构建引用；正式Run不建立诊断状态或调用这些入口。 */
void g431_can_loopback_configure(void);
void g431_can_loopback_service(void);
void g431_can_loopback_tx_accepted(const CanFrame *frame);
void g431_can_loopback_server_progress(const CanServer *server);
void g431_can_loopback_irq(uint32_t events);
#endif
