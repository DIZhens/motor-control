#include "runtime_exchange.h"
#include <stddef.h>
#include <string.h>

void runtime_exchange_init(RuntimeExchange *exchange)
{
    if (exchange != NULL) { memset(exchange, 0, sizeof(*exchange)); }
}

static bool request_valid(const RuntimeRequest *request)
{
    if (request == NULL) { return false; }
    if (request->kind == RUNTIME_REQUEST_SPEED_PI || request->kind == RUNTIME_REQUEST_TELEMETRY) {
        return true; /* 参数数值与状态必须在权威实时边界判定。 */
    }
    return request->kind == RUNTIME_REQUEST_MOTOR
        && (unsigned int)request->value.motor.type <= (unsigned int)MOTOR_COMMAND_CLEAR_FAULT;
}

RuntimeExchangeResult runtime_exchange_submit(RuntimeExchange *e, const RuntimeRequest *request,
    uint32_t *ticket, uint32_t *cancelled_ticket)
{
    RuntimeCommandEnvelope next;
    uint32_t cancelled = 0U;
    bool stop;
    if (e == NULL || ticket == NULL || cancelled_ticket == NULL || !request_valid(request)) {
        return RUNTIME_EXCHANGE_BAD_REQUEST;
    }
    stop = request->kind == RUNTIME_REQUEST_MOTOR && request->value.motor.type == MOTOR_COMMAND_STOP;
    if (e->stop_pending || e->internal_stop || (!stop && e->ordinary_pending)) { return RUNTIME_EXCHANGE_BUSY; }
    if (e->last_ticket == UINT32_MAX) { return RUNTIME_EXCHANGE_TICKET_EXHAUSTED; }
    next.ticket = e->last_ticket + 1U;
    next.request = *request;
    if (stop) {
        if (e->ordinary_pending) { cancelled = e->ordinary.ticket; }
        e->ordinary_pending = false;
        e->stop = next;
        runtime_exchange_barrier();
        e->stop_pending = true;
    } else {
        e->ordinary = next;
        runtime_exchange_barrier();
        e->ordinary_pending = true;
    }
    e->last_ticket = next.ticket;
    *ticket = next.ticket; *cancelled_ticket = cancelled;
    return RUNTIME_EXCHANGE_QUEUED;
}

void runtime_exchange_request_internal_stop(RuntimeExchange *e)
{
    if (e != NULL) { e->internal_stop = true; }
}

bool runtime_exchange_take(RuntimeExchange *e, RuntimeCommandEnvelope *envelope, uint32_t *cancelled_ticket)
{
    RuntimeCommandEnvelope next;
    uint32_t cancelled = 0U;
    if (e == NULL || envelope == NULL || cancelled_ticket == NULL) { return false; }
    if (e->stop_pending || e->internal_stop) {
        runtime_exchange_barrier();
        if (e->ordinary_pending) { cancelled = e->ordinary.ticket; }
        if (e->stop_pending) { next = e->stop; }
        else {
            memset(&next, 0, sizeof(next));
            next.request.kind = RUNTIME_REQUEST_MOTOR;
            next.request.value.motor.type = MOTOR_COMMAND_STOP;
        }
        e->ordinary_pending = false; e->stop_pending = false; e->internal_stop = false;
    } else if (e->ordinary_pending) {
        runtime_exchange_barrier();
        next = e->ordinary;
        e->ordinary_pending = false;
    } else { return false; }
    *envelope = next; *cancelled_ticket = cancelled;
    return true;
}

bool runtime_exchange_complete(RuntimeExchange *e, const RuntimeCommandCompletion *completion)
{
    uint32_t head, depth;
    if (e == NULL || completion == NULL || completion->ticket == 0U) { return false; }
    head = e->head;
    runtime_exchange_barrier();
    depth = head - e->tail;
    if (depth >= RUNTIME_COMPLETION_CAPACITY) { return false; }
    e->completions[head & (RUNTIME_COMPLETION_CAPACITY - 1U)] = *completion;
    runtime_exchange_barrier();
    e->head = head + 1U;
    if (depth + 1U > e->completion_high_water) { e->completion_high_water = depth + 1U; }
    return true;
}

bool runtime_exchange_pop(RuntimeExchange *e, RuntimeCommandCompletion *completion)
{
    uint32_t tail;
    RuntimeCommandCompletion next;
    if (e == NULL || completion == NULL) { return false; }
    tail = e->tail;
    if (tail == e->head) { return false; }
    runtime_exchange_barrier();
    next = e->completions[tail & (RUNTIME_COMPLETION_CAPACITY - 1U)];
    runtime_exchange_barrier();
    e->tail = tail + 1U;
    *completion = next;
    return true;
}
