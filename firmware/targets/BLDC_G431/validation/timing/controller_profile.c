#include "controller_profile.h"

#include <stddef.h>
#include <string.h>

volatile ObserveControllerProfileResult g_observe_controller_profile_result;
volatile uint32_t g_observe_controller_profile_sequence;

/* 单核后台单写者；与正式控制实例和功率状态隔离，公开快照不暴露活动工作区。 */
static ObserveControllerProfileResult working;
static ObserveComputeProbeStamp stamps[OBSERVE_CONTROLLER_PROFILE_MARK_COUNT];
static ObserveComputeProbeStampRead read_stamp;
static void *read_context;
static uint32_t next_mark;
static uint32_t pending_failure;
static bool call_open;

static void publish(void)
{
    const uint8_t *source = (const uint8_t *)&working;
    volatile uint8_t *destination = (volatile uint8_t *)&g_observe_controller_profile_result;
    uint32_t index;
    uint32_t odd = (g_observe_controller_profile_sequence + 1U) | 1U;
    g_observe_controller_profile_sequence = odd;
    /* 显式 volatile 字节写保证奇数/全部快照字节/偶数的发布顺序，不依赖 HAL。 */
    for (index = 0U; index < sizeof(working); index++) {
        destination[index] = source[index];
    }
    g_observe_controller_profile_sequence = odd + 1U;
}

static void terminal(uint32_t failure)
{
    working.failure = failure;
    working.state = failure == OBSERVE_CONTROLLER_PROFILE_FAILURE_NONE ?
        OBSERVE_CONTROLLER_PROFILE_COMPLETE : OBSERVE_CONTROLLER_PROFILE_FAILED;
    call_open = false;
    read_stamp = NULL;
    read_context = NULL;
    publish();
}

void observe_controller_profile_init(void)
{
    memset(&working, 0, sizeof(working));
    memset(stamps, 0, sizeof(stamps));
    read_stamp = NULL;
    read_context = NULL;
    next_mark = 0U;
    pending_failure = OBSERVE_CONTROLLER_PROFILE_FAILURE_NONE;
    call_open = false;
    publish();
}

bool observe_controller_profile_start(uint32_t request,
    ObserveComputeProbeStampRead stamp_read, void *stamp_context)
{
    if (request != 0U && working.request_sequence == request) {
        return false;
    }
    if (working.state == OBSERVE_CONTROLLER_PROFILE_ACTIVE) {
        terminal(pending_failure != OBSERVE_CONTROLLER_PROFILE_FAILURE_NONE ?
            pending_failure : OBSERVE_CONTROLLER_PROFILE_FAILURE_BUSY);
        return false;
    }
    memset(&working, 0, sizeof(working));
    working.request_sequence = request;
    next_mark = 0U;
    pending_failure = OBSERVE_CONTROLLER_PROFILE_FAILURE_NONE;
    call_open = false;
    if (request == 0U || stamp_read == NULL) {
        terminal(OBSERVE_CONTROLLER_PROFILE_FAILURE_NOT_READY);
        return false;
    }
    read_stamp = stamp_read;
    read_context = stamp_context;
    working.state = OBSERVE_CONTROLLER_PROFILE_ACTIVE;
    publish();
    return true;
}

void control_current_profile_mark(uint32_t mark)
{
    if (working.state != OBSERVE_CONTROLLER_PROFILE_ACTIVE ||
        pending_failure != OBSERVE_CONTROLLER_PROFILE_FAILURE_NONE) {
        return;
    }
    if (mark == 0U) {
        if (call_open) {
            pending_failure = OBSERVE_CONTROLLER_PROFILE_FAILURE_ORDER;
            return;
        }
        if (working.call_count >= OBSERVE_CONTROLLER_PROFILE_VALID_CALLS +
            OBSERVE_CONTROLLER_PROFILE_REJECTED_CALLS) {
            pending_failure = OBSERVE_CONTROLLER_PROFILE_FAILURE_LIMIT;
            return;
        }
        call_open = true;
        next_mark = 0U;
    }
    if (!call_open || mark != next_mark || mark >= OBSERVE_CONTROLLER_PROFILE_MARK_COUNT) {
        pending_failure = OBSERVE_CONTROLLER_PROFILE_FAILURE_ORDER;
        return;
    }
    if (!read_stamp(read_context, &stamps[mark])) {
        pending_failure = OBSERVE_CONTROLLER_PROFILE_FAILURE_CLOCK;
        return;
    }
    next_mark++;
}

static void accumulate(ObserveComputeProbeTiming *timing,
    const ObserveComputeProbeStamp *begin, const ObserveComputeProbeStamp *end)
{
    uint32_t elapsed = end->cycles - begin->cycles;
    uint32_t entries = end->irq_entries - begin->irq_entries;
    if (timing->count == 0U || elapsed < timing->min_cycles) {
        timing->min_cycles = elapsed;
    }
    if (elapsed > timing->max_cycles) {
        timing->max_cycles = elapsed;
    }
    timing->sum_cycles += elapsed;
    timing->count++;
    timing->irq_entries += entries;
    if (entries == 0U) {
        if (timing->clean_count == 0U || elapsed < timing->clean_min_cycles) {
            timing->clean_min_cycles = elapsed;
        }
        if (elapsed > timing->clean_max_cycles) {
            timing->clean_max_cycles = elapsed;
        }
        timing->clean_sum_cycles += elapsed;
        timing->clean_count++;
    }
}

void observe_controller_profile_finish(bool output_valid)
{
    uint32_t mark, elapsed;
    if (working.state != OBSERVE_CONTROLLER_PROFILE_ACTIVE) {
        return;
    }
    if (pending_failure != OBSERVE_CONTROLLER_PROFILE_FAILURE_NONE) {
        terminal(pending_failure);
        return;
    }
    if (!call_open) {
        terminal(OBSERVE_CONTROLLER_PROFILE_FAILURE_ORDER);
        return;
    }
    if (output_valid) {
        if (next_mark != OBSERVE_CONTROLLER_PROFILE_MARK_COUNT) {
            terminal(OBSERVE_CONTROLLER_PROFILE_FAILURE_INCOMPLETE);
            return;
        }
        elapsed = stamps[6].cycles - stamps[0].cycles;
        if (elapsed == 0U || elapsed >= 0x80000000UL) {
            terminal(OBSERVE_CONTROLLER_PROFILE_FAILURE_CLOCK);
            return;
        }
        for (mark = 1U; mark < OBSERVE_CONTROLLER_PROFILE_MARK_COUNT; mark++) {
            elapsed = stamps[mark].cycles - stamps[mark - 1U].cycles;
            if (elapsed == 0U || elapsed >= 0x80000000UL) {
                terminal(OBSERVE_CONTROLLER_PROFILE_FAILURE_CLOCK);
                return;
            }
        }
        if (working.valid_calls >= OBSERVE_CONTROLLER_PROFILE_VALID_CALLS) {
            terminal(OBSERVE_CONTROLLER_PROFILE_FAILURE_LIMIT);
            return;
        }
        accumulate(&working.timing[0], &stamps[0], &stamps[6]);
        for (mark = 1U; mark < OBSERVE_CONTROLLER_PROFILE_MARK_COUNT; mark++) {
            accumulate(&working.timing[mark], &stamps[mark - 1U], &stamps[mark]);
        }
        working.valid_calls++;
    } else {
        if (next_mark == OBSERVE_CONTROLLER_PROFILE_MARK_COUNT) {
            terminal(OBSERVE_CONTROLLER_PROFILE_FAILURE_ORDER);
            return;
        }
        if (working.rejected_calls >= OBSERVE_CONTROLLER_PROFILE_REJECTED_CALLS) {
            terminal(OBSERVE_CONTROLLER_PROFILE_FAILURE_LIMIT);
            return;
        }
        working.rejected_calls++;
    }
    working.call_count++;
    call_open = false;
    next_mark = 0U;
}

void observe_controller_profile_freeze(bool success)
{
    if (working.state != OBSERVE_CONTROLLER_PROFILE_ACTIVE) {
        return;
    }
    if (pending_failure != OBSERVE_CONTROLLER_PROFILE_FAILURE_NONE) {
        terminal(pending_failure);
    } else if (!success) {
        terminal(OBSERVE_CONTROLLER_PROFILE_FAILURE_ABORTED);
    } else if (call_open || working.valid_calls != OBSERVE_CONTROLLER_PROFILE_VALID_CALLS ||
        working.rejected_calls != OBSERVE_CONTROLLER_PROFILE_REJECTED_CALLS) {
        terminal(OBSERVE_CONTROLLER_PROFILE_FAILURE_INCOMPLETE);
    } else {
        terminal(OBSERVE_CONTROLLER_PROFILE_FAILURE_NONE);
    }
}
