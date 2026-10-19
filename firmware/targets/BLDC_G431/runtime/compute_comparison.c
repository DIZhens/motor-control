#include "compute_comparison.h"

#include <math.h>
#include <stddef.h>
#include <string.h>

/* 误差门限用于发现传输、Q 格式、顺序和相位错误，不替代闭环精度验收。 */
#define ERROR_LIMIT (0.00002f)

static void freeze(ObserveComputeComparison *comparison)
{
    comparison->result.software = comparison->software.result;
    comparison->result.cordic = comparison->cordic.result;
}

static bool fail(ObserveComputeComparison *comparison, ObserveComputeProbeFailure reason)
{
    comparison->result.state = OBSERVE_COMPUTE_PROBE_FAILED;
    comparison->result.failure = reason;
    (void)observe_compute_probe_abort(&comparison->software, reason);
    (void)observe_compute_probe_abort(&comparison->cordic, reason);
    freeze(comparison);
    return true;
}

void observe_compute_comparison_init(ObserveComputeComparison *comparison)
{
    if (comparison != NULL) {
        memset(comparison, 0, sizeof(*comparison));
        comparison->result.schema_version = OBSERVE_COMPUTE_COMPARISON_SCHEMA;
    }
}

bool observe_compute_comparison_start(ObserveComputeComparison *comparison,
    uint32_t request, uint32_t clock_hz, uint32_t max_elapsed_cycles, bool permitted,
    ElectricalAngleTrig hardware_trig, void *hardware_context)
{
    bool busy;
    if (comparison == NULL || (request != 0U && request == comparison->result.request_sequence)) {
        return false;
    }
    busy = comparison->result.state == OBSERVE_COMPUTE_PROBE_ACTIVE;
    observe_compute_comparison_init(comparison);
    comparison->result.request_sequence = request;
    if (busy || !permitted || hardware_trig == NULL) {
        (void)fail(comparison, busy ? OBSERVE_COMPUTE_PROBE_FAILURE_BUSY :
            OBSERVE_COMPUTE_PROBE_FAILURE_NOT_READY);
        return false;
    }
    comparison->hardware_trig = hardware_trig;
    comparison->hardware_context = hardware_context;
    if (!observe_compute_probe_start(&comparison->software, request, clock_hz,
            max_elapsed_cycles, permitted) ||
        !observe_compute_probe_start(&comparison->cordic, request, clock_hz,
            max_elapsed_cycles, permitted)) {
        (void)fail(comparison, OBSERVE_COMPUTE_PROBE_FAILURE_NOT_READY);
        return false;
    }
    comparison->result.state = OBSERVE_COMPUTE_PROBE_ACTIVE;
    freeze(comparison);
    return true;
}

static bool error(float a, float b, float *maximum)
{
    float difference = fabsf(a - b);
    if (!isfinite(difference) || difference > ERROR_LIMIT) {
        return false;
    }
    if (difference > *maximum) {
        *maximum = difference;
    }
    return true;
}

static bool compare_pair(ObserveComputeComparison *comparison, uint32_t index)
{
    uint32_t component;
    const ObserveComputeProbeCaseResult *a = &comparison->software.result.cases[index];
    const ObserveComputeProbeCaseResult *b = &comparison->cordic.result.cases[index];
    if (a->count != b->count || a->last_output_valid != b->last_output_valid ||
        a->last_voltage_limited != b->last_voltage_limited ||
        a->last_speed_limited != b->last_speed_limited) {
        return false;
    }
    for (component = 0U; component < 3U; component++) {
        if (!error(a->last_duty[component], b->last_duty[component],
                &comparison->result.max_duty_error)) {
            return false;
        }
    }
    for (component = 0U; component < 4U; component++) {
        if (!error(a->last_phasor[component], b->last_phasor[component],
                &comparison->result.max_phasor_error)) {
            return false;
        }
    }
    return true;
}

bool observe_compute_comparison_step(ObserveComputeComparison *comparison, bool permitted,
    ObserveComputeProbeStampRead stamp_read, void *stamp_context)
{
    ObserveComputeProbe *probe;
    uint32_t index;
    float turns, software_sine, software_cosine, hardware_sine, hardware_cosine;
    if (comparison == NULL || comparison->result.state != OBSERVE_COMPUTE_PROBE_ACTIVE) {
        return false;
    }
    if (!permitted) {
        return fail(comparison, OBSERVE_COMPUTE_PROBE_FAILURE_NOT_READY);
    }
    if (comparison->result.completed_steps <
            2U * OBSERVE_COMPUTE_PROBE_CASE_COUNT * OBSERVE_COMPUTE_PROBE_ITERATIONS) {
        bool hardware = (comparison->result.completed_steps & 1U) != 0U;
        probe = hardware ? &comparison->cordic : &comparison->software;
        index = probe->result.next_case;
        (void)observe_compute_probe_step_profiled(probe, permitted,
            hardware ? comparison->hardware_trig : electrical_angle_sincos_software,
            hardware ? comparison->hardware_context : NULL, stamp_read, stamp_context);
        if (probe->result.state == OBSERVE_COMPUTE_PROBE_FAILED) {
            return fail(comparison, probe->result.failure);
        }
        comparison->result.completed_steps++;
        if (hardware && !compare_pair(comparison, index)) {
            return fail(comparison, OBSERVE_COMPUTE_PROBE_FAILURE_ALGORITHM);
        }
        return false;
    }
    /* 每点仅对照一次；加入 1 的前一浮点数以覆盖 Q1.31 上端取整边界。 */
    turns = comparison->result.sweep_count == 256U ? 0.999999940395355224609375f :
        (float)comparison->result.sweep_count / 256.0f;
    if (!electrical_angle_sincos_software(NULL, turns, &software_sine, &software_cosine) ||
        !comparison->hardware_trig(comparison->hardware_context, turns, &hardware_sine, &hardware_cosine) ||
        !error(software_sine, hardware_sine, &comparison->result.max_sine_error) ||
        !error(software_cosine, hardware_cosine, &comparison->result.max_cosine_error) ||
        !error(1.0f, hardware_sine * hardware_sine + hardware_cosine * hardware_cosine,
            &comparison->result.max_norm_error)) {
        return fail(comparison, OBSERVE_COMPUTE_PROBE_FAILURE_ALGORITHM);
    }
    comparison->result.sweep_count++;
    if (comparison->result.sweep_count == OBSERVE_COMPUTE_COMPARISON_SWEEP) {
        comparison->result.state = OBSERVE_COMPUTE_PROBE_COMPLETE;
        freeze(comparison);
        return true;
    }
    return false;
}

bool observe_compute_comparison_abort(ObserveComputeComparison *comparison,
    ObserveComputeProbeFailure reason)
{
    if (comparison == NULL || comparison->result.state != OBSERVE_COMPUTE_PROBE_ACTIVE) {
        return false;
    }
    if (reason <= OBSERVE_COMPUTE_PROBE_FAILURE_NONE || reason > OBSERVE_COMPUTE_PROBE_FAILURE_BUSY) {
        reason = OBSERVE_COMPUTE_PROBE_FAILURE_ABORTED;
    }
    return fail(comparison, reason);
}

bool observe_compute_comparison_revoke(ObserveComputeComparison *comparison)
{
    if (comparison == NULL || (comparison->result.state != OBSERVE_COMPUTE_PROBE_ACTIVE &&
            comparison->result.state != OBSERVE_COMPUTE_PROBE_COMPLETE)) {
        return false;
    }
    return fail(comparison, OBSERVE_COMPUTE_PROBE_FAILURE_NOT_READY);
}
