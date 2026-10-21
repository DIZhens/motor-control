#include "irq_timing.h"

#include <string.h>

#include "stm32g4xx.h"

volatile uint32_t g_target_irq_timing_sequence;
volatile G431IrqTimingResult g_target_irq_timing_result;
volatile uint32_t g_target_irq_timing_active;

static G431IrqTimingResult result;
static uint32_t seen_request;
static uint32_t phase;
static uint32_t failure;
static uint32_t stamps[6];

static void publish(void)
{
    g_target_irq_timing_sequence++;
    __DMB();
    g_target_irq_timing_result = result;
    __DMB();
    g_target_irq_timing_sequence++;
}

static void stop(uint32_t reason)
{
    result.state = G431_IRQ_TIMING_FAILED;
    result.failure = reason;
    g_target_irq_timing_active = 0U;
    phase = 0U;
    publish();
}

void g431_irq_timing_init(void)
{
    memset(&result, 0, sizeof(result));
    result.schema_version = 1U;
    seen_request = 0U;
    phase = 0U;
    failure = 0U;
    g_target_irq_timing_sequence = 0U;
    g_target_irq_timing_active = 0U;
    g_target_irq_timing_result = result;
}

void g431_irq_timing_irq_enter(uint32_t cycles, uint32_t request, bool permitted)
{
    if (request != 0U && request != seen_request) {
        uint32_t busy = result.state == G431_IRQ_TIMING_ACTIVE;
        seen_request = request;
        memset(&result, 0, sizeof(result));
        result.schema_version = 1U;
        result.request_sequence = request;
        failure = 0U;
        phase = 0U;
        g_target_irq_timing_active = 0U;
        if (busy != 0U) {
            stop(G431_IRQ_TIMING_FAILURE_BUSY);
        } else if (!permitted) {
            stop(G431_IRQ_TIMING_FAILURE_NOT_READY);
        } else {
            result.state = G431_IRQ_TIMING_ACTIVE;
            publish();
        }
        return; /* 准备与起始发布发生的IRQ不计入稳定窗口。 */
    }
    if (result.state != G431_IRQ_TIMING_ACTIVE) {
        return;
    }
    if (!permitted) {
        stop(G431_IRQ_TIMING_FAILURE_NOT_READY);
        return;
    }
    if (phase != 0U) {
        stop(G431_IRQ_TIMING_FAILURE_ORDER);
        return;
    }
    stamps[0] = cycles;
    failure = 0U;
    phase = 1U;
    g_target_irq_timing_active = 1U;
}

static void mark(uint32_t expected_phase, uint32_t cycles, uint32_t index)
{
    if (g_target_irq_timing_active == 0U) {
        return;
    }
    if (phase != expected_phase) {
        failure = G431_IRQ_TIMING_FAILURE_ORDER;
        return;
    }
    stamps[index] = cycles;
    phase++;
}

void g431_irq_timing_platform_enter(uint32_t existing_anchor_cycles)
{
    mark(1U, existing_anchor_cycles, 1U);
}

void g431_irq_timing_consumer_enter(uint32_t cycles)
{
    mark(2U, cycles, 2U);
}

void g431_irq_timing_observe_enter(uint32_t cycles)
{
    mark(3U, cycles, 3U);
}

void g431_irq_timing_observe_exit(uint32_t cycles)
{
    mark(4U, cycles, 4U);
}

void g431_irq_timing_consumer_exit(uint32_t cycles)
{
    mark(5U, cycles, 5U);
}

void g431_irq_timing_irq_exit(uint32_t cycles, bool permitted)
{
    uint32_t spans[G431_IRQ_TIMING_METRICS];
    uint32_t index;
    uint32_t last;
    uint32_t rows;
    if (!permitted && result.state == G431_IRQ_TIMING_ACTIVE) {
        stop(G431_IRQ_TIMING_FAILURE_NOT_READY);
        return;
    }
    if (g_target_irq_timing_active == 0U) {
        return;
    }
    g_target_irq_timing_active = 0U;
    if (failure != 0U || (phase != 1U && phase != 6U)) {
        stop(failure != 0U ? failure : G431_IRQ_TIMING_FAILURE_MISSING);
        return;
    }
    rows = phase == 6U ? G431_IRQ_TIMING_METRICS : 1U;
    spans[G431_IRQ_TIMING_IRQ] = cycles - stamps[0];
    if (rows == G431_IRQ_TIMING_METRICS) {
        spans[G431_IRQ_TIMING_PREFIX] = stamps[2] - stamps[1];
        spans[G431_IRQ_TIMING_CONSUMER] = stamps[5] - stamps[2];
        spans[G431_IRQ_TIMING_OBSERVE] = stamps[4] - stamps[3];
    }
    for (index = 0U; index < rows; index++) {
        if (spans[index] == 0U || spans[index] > G431_IRQ_TIMING_MAX_CYCLES) {
            stop(G431_IRQ_TIMING_FAILURE_CLOCK);
            return;
        }
    }
    /* 相邻边界必须前进且位于同一个有界IRQ区间；支持DWT单次自然回绕。 */
    last = 0U;
    for (index = 1U; rows == G431_IRQ_TIMING_METRICS && index < 6U; index++) {
        uint32_t offset = stamps[index] - stamps[0];
        if (offset < last || offset > spans[G431_IRQ_TIMING_IRQ]) {
            stop(G431_IRQ_TIMING_FAILURE_CLOCK);
            return;
        }
        last = offset;
    }
    for (index = 0U; index < rows; index++) {
        G431IrqTimingMetric *metric = &result.rows[index];
        if (metric->count == 0U || spans[index] < metric->min_cycles) {
            metric->min_cycles = spans[index];
        }
        if (spans[index] > metric->max_cycles) {
            metric->max_cycles = spans[index];
        }
        metric->sum_cycles += spans[index];
        metric->count++;
    }
    result.irq_count++;
    phase = 0U;
    if (result.irq_count == G431_IRQ_TIMING_SAMPLES) {
        result.state = G431_IRQ_TIMING_COMPLETE;
        publish();
    }
}
