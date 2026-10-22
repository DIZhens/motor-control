#ifndef CORE_SERVER_H
#define CORE_SERVER_H

#include "core_protocol_values.h"
#include "runtime_exchange.h"

#define CORE_RESPONSE_CAPACITY 4U
#define CORE_CACHE_CAPACITY 4U
#define CORE_RUNTIME_TIMEOUT_MS 20U

typedef struct { CoreFrame frame; bool critical; } CoreQueuedResponse;
typedef struct { CoreFrame request, response; bool valid; } CoreCompletedRequest;
typedef struct {
    CoreFrame request;
    RuntimeRequestKind kind;
    uint32_t ticket, started_ms;
    bool active;
} CorePendingRequest;

/* 唯一Communication所有者；静态容量，不跨任务共享可变事务状态。 */
typedef struct {
    CoreParser parser;
    uint32_t session, highest_sequence, now_ms, response_count, cache_next;
    uint32_t response_high_water, message_errors;
    bool failed;
    CorePendingRequest ordinary, stop;
    CoreCompletedRequest cache[CORE_CACHE_CAPACITY];
    CoreQueuedResponse responses[CORE_RESPONSE_CAPACITY];
    /* 编码工作区归同一所有者，避免嵌套大对象挤占任务栈。 */
    CoreRequest decoded;
    CoreResponse answer;
    CoreFrame output;
    RuntimeRequest runtime_request;
    uint8_t wire[CORE_WIRE_MAX];
} CoreServer;

void core_server_init(CoreServer *server);
void core_server_feed(CoreServer *server, const uint8_t *bytes, size_t count, uint32_t now_ms);
void core_server_discontinuity(CoreServer *server);
/* 先检查可信性/截止，再有界排空Completion；不等待、不自动重试业务。 */
void core_server_poll(CoreServer *server, uint32_t now_ms);
void core_server_flush(CoreServer *server);

/* 链接期窄集成；没有运行期Ops表。对象取得一致副本，写请求只交给实时所有者。 */
CoreResult core_server_port_read_object(uint16_t object, CoreObject *value);
RuntimeExchangeResult core_server_port_submit(const RuntimeRequest *request,
    uint32_t *ticket, uint32_t *cancelled_ticket);
bool core_server_port_completion(RuntimeCommandCompletion *completion);
bool core_server_port_permitted(void);
void core_server_port_fail_closed(RuntimeInternalFaultReason reason);

#endif
