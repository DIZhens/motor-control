#ifndef CAN_SERVER_H
#define CAN_SERVER_H

#include "can_codec.h"
#include "protocol_device_port.h"

#define CAN_V1_CACHE_CAPACITY 4U
#define CAN_V1_RESPONSE_CAPACITY 4U
#define CAN_V1_EVENT_CAPACITY 4U
#define CAN_V1_COMPLETION_TIMEOUT_MS 20U
#define CAN_V1_HEARTBEAT_MS 100U

typedef struct {
    CanRequest request;
    uint32_t ticket, started_ms;
    bool active, internal_stop;
} CanPending;

typedef struct { CanRequest request; CanFrame response; bool valid; } CanCompleted;
typedef struct { CanFrame frame; bool critical; } CanResponse;

/* 一个Communication所有者，普通/Stop各一槽；全部固定容量，无动态分配。 */
typedef struct {
    uint8_t node;
    uint16_t session, highest_sequence, heartbeat_counter;
    uint32_t now_ms, last_heartbeat_ms, epoch_counter;
    volatile uint32_t active_epoch;
    uint32_t response_count, cache_next, event_count;
    uint32_t rejected_frames, response_high_water, event_drop_count, rx_lost_seen;
    bool failed, suspended, snapshot_valid, heartbeat_pending;
    uint32_t previous_faults;
    CoreMotorSnapshot snapshot;
    CanPending ordinary, stop;
    CanCompleted cache[CAN_V1_CACHE_CAPACITY];
    CanResponse responses[CAN_V1_RESPONSE_CAPACITY];
    CanFrame events[CAN_V1_EVENT_CAPACITY];
} CanServer;

void can_server_init(CanServer *server, uint8_t node);
void can_server_feed(CanServer *server, const CanFrame *frame, uint32_t now_ms);
void can_server_poll(CanServer *server, uint32_t now_ms);
void can_server_observe(CanServer *server);
void can_server_flush(CanServer *server);
/* Bus-off只撤销会话并优先交接Stop；Target在任务上下文完成硬件恢复后显式确认。 */
void can_server_link_down(CanServer *server);
bool can_server_recovered(CanServer *server);

#endif
