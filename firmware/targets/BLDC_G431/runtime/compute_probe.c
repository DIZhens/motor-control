#include "compute_probe.h"

#include <math.h>
#include <stddef.h>
#include <string.h>
#if defined(CONTROL_CURRENT_PROFILE)
#include "controller_profile.h"
#endif

static void fail(ObserveComputeProbe *probe, ObserveComputeProbeFailure reason)
{
    probe->result.failure = reason;
    probe->result.state = OBSERVE_COMPUTE_PROBE_FAILED;
}

void observe_compute_probe_init(ObserveComputeProbe *probe)
{
    if (probe != NULL) {
        memset(probe, 0, sizeof(*probe));
    }
}

static bool prepare_algorithms(ObserveComputeProbe *probe)
{
    /* 以下全部为计算覆盖用合成值，不是 2804 电机的实测参数或运行设置。 */
    const CurrentMeasurementConfig measurement_config = {
        CURRENT_MEASUREMENT_PHASE_BC, 2U, 0.2f, 0.01f,
        true, {0.0f, 1.25f, -0.8f}
    };
    const ElectricalAngleConfig angle_config = {7U, 1, 0.125f, 0.001f, 1000.0f};
    const CurrentControllerConfig current_config = {
        2.0f, 2.0f, 1000.0f, 1000.0f, 200.0f, 4.0f, 0.6f, 0.45f, 0.0001f
    };
    const SpeedControllerConfig speed_config = {
        0.006f, 0.04f, 20.0f, 0.4f, 0.4f, 200.0f, 0.002f
    };
    const CurrentMeasurementSample zero = {{0.0f, 0.02f, -0.03f}, CURRENT_MEASUREMENT_PHASE_BC};
    const SpeedControllerInput initial_speed = {90.0f, 60.0f, 0.001f, true, true};

    if (!current_measurement_init(&probe->measurement, &measurement_config) ||
        !current_measurement_start(&probe->measurement, true)) {
        return false;
    }
    /* 固定两点校零只建立合成 fixture；第一点尚未完成是正式 API 的正常语义。 */
    if (current_measurement_calibrate(&probe->measurement, &zero, true) ||
        !current_measurement_calibrate(&probe->measurement, &zero, true) ||
        !electrical_angle_init(&probe->measurement_angle, &angle_config) ||
        !electrical_angle_init(&probe->actuation_angle, &angle_config) ||
        !current_controller_init(&probe->current, &current_config) ||
        !speed_controller_init(&probe->speed, &speed_config) ||
        !speed_controller_update(&probe->speed, &initial_speed, &probe->held_speed)) {
        return false;
    }
    return true;
}

bool observe_compute_probe_start(ObserveComputeProbe *probe, uint32_t request,
    uint32_t clock_hz, uint32_t max_elapsed_cycles, bool permitted)
{
    bool busy;
    if (probe == NULL) {
        return false;
    }
    if (request != 0U && probe->result.request_sequence == request) {
        return false;
    }
    busy = probe->result.state == OBSERVE_COMPUTE_PROBE_ACTIVE;
    observe_compute_probe_init(probe);
    probe->result.request_sequence = request;
    probe->result.clock_hz = clock_hz;
    probe->result.max_elapsed_cycles = max_elapsed_cycles;
    if (busy) {
        fail(probe, OBSERVE_COMPUTE_PROBE_FAILURE_BUSY);
        return false;
    }
    if (!permitted || request == 0U || clock_hz == 0U ||
        max_elapsed_cycles == 0U || max_elapsed_cycles >= 0x80000000UL) {
        fail(probe, OBSERVE_COMPUTE_PROBE_FAILURE_NOT_READY);
        return false;
    }
    if (!prepare_algorithms(probe)) {
        fail(probe, OBSERVE_COMPUTE_PROBE_FAILURE_ALGORITHM);
        return false;
    }
    probe->result.state = OBSERVE_COMPUTE_PROBE_ACTIVE;
    return true;
}

/* 当前 step 的局部 stamp；提交统计前不写入公开结果，失败也不留下半份样本。 */
typedef struct {
    ObserveComputeProbeStampRead read; /* 调用方提供的只读快照函数。 */
    void *context; /* 快照函数私有状态，本模块不保存到 probe。 */
    ObserveComputeProbeStamp begin[OBSERVE_COMPUTE_PROBE_SEGMENT_COUNT]; /* 各段起点。 */
    ObserveComputeProbeStamp end[OBSERVE_COMPUTE_PROBE_SEGMENT_COUNT]; /* 各段终点。 */
    uint32_t measured_mask; /* 完整读到起止 stamp 的段集合。 */
    bool clock_ok; /* 任意读取失败即保持 false。 */
} ProbeProfile;

static bool profile_begin(ProbeProfile *profile, uint32_t segment)
{
    if (profile == NULL) {
        return true;
    }
    if (!profile->read(profile->context, &profile->begin[segment])) {
        profile->clock_ok = false;
        return false;
    }
    return true;
}

static bool profile_end(ProbeProfile *profile, uint32_t segment)
{
    if (profile == NULL) {
        return true;
    }
    if (!profile->read(profile->context, &profile->end[segment])) {
        profile->clock_ok = false;
        return false;
    }
    profile->measured_mask |= 1UL << segment;
    return true;
}

static bool run_case(ObserveComputeProbe *probe, uint32_t case_index,
    CurrentControllerOutput *output, bool *speed_limited, float phasor[4],
    ElectricalAngleTrig trig, void *trig_context, ProbeProfile *profile)
{
    CurrentMeasurementSample sample = {{0.0f, 0.10f, 0.12f}, CURRENT_MEASUREMENT_PHASE_BC};
    SpeedControllerInput speed_input = {90.0f, 60.0f, 0.001f, true, true};
    CurrentControllerInput input = {0};
    const float position = 0.04f +
        (float)(probe->result.completed_steps / OBSERVE_COMPUTE_PROBE_CASE_COUNT) * 0.003f;
    bool measurement_ok, angle_ok, actuation_ok = true, speed_ok = true, control_ok;

    /* 预期拒绝例从无效默认输出开始，避免上一例的 duty 被误认为本例的结果。 */
    memset(output, 0, sizeof(*output));
    output->duty_a = 0.5f;
    output->duty_b = 0.5f;
    output->duty_c = 0.5f;
    *speed_limited = false;
    memset(phasor, 0, 4U * sizeof(*phasor));
    if (case_index == OBSERVE_COMPUTE_PROBE_INVALID_MEASUREMENT) {
        sample.valid_phases = CURRENT_MEASUREMENT_PHASE_B;
    }
    if (!profile_begin(profile, OBSERVE_COMPUTE_PROBE_SEGMENT_MEASUREMENT)) {
        return false;
    }
    measurement_ok = current_measurement_process(&probe->measurement, &sample);
    if (!profile_end(profile, OBSERVE_COMPUTE_PROBE_SEGMENT_MEASUREMENT)) {
        return false;
    }
    if (!measurement_ok) {
        return case_index == OBSERVE_COMPUTE_PROBE_INVALID_MEASUREMENT &&
            !probe->measurement.output.valid;
    }
    if (case_index == OBSERVE_COMPUTE_PROBE_INVALID_MEASUREMENT) {
        return false;
    }
    if (!profile_begin(profile, OBSERVE_COMPUTE_PROBE_SEGMENT_MEASUREMENT_ANGLE)) {
        return false;
    }
    angle_ok = electrical_angle_update_with_trig(&probe->measurement_angle,
        position, 60.0f, 0.00002f, trig, trig_context);
    if (!profile_end(profile, OBSERVE_COMPUTE_PROBE_SEGMENT_MEASUREMENT_ANGLE)) {
        return false;
    }
    if (case_index == OBSERVE_COMPUTE_PROBE_PREDICTED_ANGLE) {
        if (!profile_begin(profile, OBSERVE_COMPUTE_PROBE_SEGMENT_ACTUATION_ANGLE)) {
            return false;
        }
        actuation_ok = electrical_angle_update_with_trig(&probe->actuation_angle,
            position, 60.0f, 0.00007f, trig, trig_context);
        if (!profile_end(profile, OBSERVE_COMPUTE_PROBE_SEGMENT_ACTUATION_ANGLE)) {
            return false;
        }
    }
    phasor[0] = probe->measurement_angle.output.sin_theta;
    phasor[1] = probe->measurement_angle.output.cos_theta;
    phasor[2] = case_index == OBSERVE_COMPUTE_PROBE_PREDICTED_ANGLE ?
        probe->actuation_angle.output.sin_theta : phasor[0];
    phasor[3] = case_index == OBSERVE_COMPUTE_PROBE_PREDICTED_ANGLE ?
        probe->actuation_angle.output.cos_theta : phasor[1];
    if (!profile_begin(profile, OBSERVE_COMPUTE_PROBE_SEGMENT_SPEED)) {
        return false;
    }
    if (case_index != OBSERVE_COMPUTE_PROBE_SPEED_HOLD) {
        if (case_index == OBSERVE_COMPUTE_PROBE_LIMITED) {
            speed_input.target_velocity_rpm = 200.0f;
            speed_input.measured_velocity_rpm = -200.0f;
        }
        speed_ok = speed_controller_update(&probe->speed, &speed_input, &probe->held_speed);
    }
    if (!profile_end(profile, OBSERVE_COMPUTE_PROBE_SEGMENT_SPEED)) {
        return false;
    }
    if (!angle_ok || !actuation_ok || !speed_ok || !probe->held_speed.valid) {
        return false;
    }
    input.current_alpha_a = probe->measurement.output.alpha_a;
    input.current_beta_a = probe->measurement.output.beta_a;
    input.sin_electrical_angle = probe->measurement_angle.output.sin_theta;
    input.cos_electrical_angle = probe->measurement_angle.output.cos_theta;
    input.bus_voltage_v = case_index == OBSERVE_COMPUTE_PROBE_LIMITED ? 0.1f : 12.0f;
    input.id_reference_a = 0.0f;
    input.iq_reference_a = probe->held_speed.iq_reference_a;
    input.interval_s = 0.00005f;
    input.current_valid = true;
    input.angle_valid = true;
    input.bus_voltage_valid = case_index != OBSERVE_COMPUTE_PROBE_INVALID_CONTROLLER;
    input.reference_valid = true;
    input.separate_actuation_angle = case_index == OBSERVE_COMPUTE_PROBE_PREDICTED_ANGLE;
    input.actuation_sin_theta = probe->actuation_angle.output.sin_theta;
    input.actuation_cos_theta = probe->actuation_angle.output.cos_theta;
    *speed_limited = probe->held_speed.current_limited;
    if (!profile_begin(profile, OBSERVE_COMPUTE_PROBE_SEGMENT_CURRENT)) {
        return false;
    }
    control_ok = current_controller_update(&probe->current, &input, output);
    if (!profile_end(profile, OBSERVE_COMPUTE_PROBE_SEGMENT_CURRENT)) {
        return false;
    }
#if defined(CONTROL_CURRENT_PROFILE)
    /* 控制器内部只存边界；在独立电流段末读之后才提交内部统计。 */
    observe_controller_profile_finish(output->valid);
#endif
    if (case_index == OBSERVE_COMPUTE_PROBE_INVALID_CONTROLLER) {
        return !control_ok && !output->valid && output->duty_a == 0.5f &&
            output->duty_b == 0.5f && output->duty_c == 0.5f;
    }
    if (!control_ok || !output->valid ||
        !isfinite(output->duty_a) || output->duty_a < 0.0f || output->duty_a > 1.0f ||
        !isfinite(output->duty_b) || output->duty_b < 0.0f || output->duty_b > 1.0f ||
        !isfinite(output->duty_c) || output->duty_c < 0.0f || output->duty_c > 1.0f) {
        return false;
    }
    return case_index != OBSERVE_COMPUTE_PROBE_LIMITED ||
        (output->voltage_limited && *speed_limited);
}

static bool accept_case(ObserveComputeProbe *probe, uint32_t case_index, uint32_t elapsed,
    const CurrentControllerOutput *output, bool speed_limited, const float phasor[4])
{
    ObserveComputeProbeCaseResult *statistics = &probe->result.cases[case_index];
    if (statistics->count == 0U || elapsed < statistics->min_cycles) {
        statistics->min_cycles = elapsed;
    }
    if (elapsed > statistics->max_cycles) {
        statistics->max_cycles = elapsed;
    }
    statistics->sum_cycles += elapsed;
    statistics->count++;
    statistics->last_duty[0] = output->duty_a;
    statistics->last_duty[1] = output->duty_b;
    statistics->last_duty[2] = output->duty_c;
    statistics->last_output_valid = output->valid;
    statistics->last_voltage_limited = output->voltage_limited;
    statistics->last_speed_limited = speed_limited;
    memcpy(statistics->last_phasor, phasor, sizeof(statistics->last_phasor));
    probe->result.completed_steps++;
    probe->result.next_case = (case_index + 1U) % OBSERVE_COMPUTE_PROBE_CASE_COUNT;
    if (probe->result.completed_steps ==
        OBSERVE_COMPUTE_PROBE_CASE_COUNT * OBSERVE_COMPUTE_PROBE_ITERATIONS) {
        probe->result.state = OBSERVE_COMPUTE_PROBE_COMPLETE;
        return true;
    }
    return false;
}

bool observe_compute_probe_step(ObserveComputeProbe *probe, bool permitted,
    ObserveComputeProbeClockRead clock_read, void *clock_context)
{
    CurrentControllerOutput output;
    float phasor[4];
    uint32_t begin = 0U, end = 0U, elapsed, case_index;
    bool algorithm_ok, speed_limited;
    if (probe == NULL || probe->result.state != OBSERVE_COMPUTE_PROBE_ACTIVE) {
        return false;
    }
    if (!permitted) {
        fail(probe, OBSERVE_COMPUTE_PROBE_FAILURE_NOT_READY);
        return true;
    }
    if (clock_read == NULL || !clock_read(clock_context, &begin)) {
        fail(probe, OBSERVE_COMPUTE_PROBE_FAILURE_CLOCK);
        return true;
    }
    case_index = probe->result.next_case;
    algorithm_ok = run_case(probe, case_index, &output, &speed_limited, phasor,
        electrical_angle_sincos_software, NULL, NULL);
    if (!clock_read(clock_context, &end)) {
        fail(probe, OBSERVE_COMPUTE_PROBE_FAILURE_CLOCK);
        return true;
    }
    elapsed = end - begin;
    if (elapsed == 0U || elapsed > probe->result.max_elapsed_cycles) {
        fail(probe, OBSERVE_COMPUTE_PROBE_FAILURE_CLOCK);
        return true;
    }
    if (!algorithm_ok) {
        fail(probe, OBSERVE_COMPUTE_PROBE_FAILURE_ALGORITHM);
        return true;
    }
    return accept_case(probe, case_index, elapsed, &output, speed_limited, phasor);
}

static void accept_timing(ObserveComputeProbeTiming *statistics,
    const ObserveComputeProbeStamp *begin, const ObserveComputeProbeStamp *end)
{
    uint32_t elapsed = end->cycles - begin->cycles;
    uint32_t entries = end->irq_entries - begin->irq_entries;
    if (statistics->count == 0U || elapsed < statistics->min_cycles) {
        statistics->min_cycles = elapsed;
    }
    if (elapsed > statistics->max_cycles) {
        statistics->max_cycles = elapsed;
    }
    statistics->sum_cycles += elapsed;
    statistics->count++;
    statistics->irq_entries += entries;
    if (entries == 0U) {
        if (statistics->clean_count == 0U || elapsed < statistics->clean_min_cycles) {
            statistics->clean_min_cycles = elapsed;
        }
        if (elapsed > statistics->clean_max_cycles) {
            statistics->clean_max_cycles = elapsed;
        }
        statistics->clean_sum_cycles += elapsed;
        statistics->clean_count++;
    }
}

bool observe_compute_probe_step_profiled(ObserveComputeProbe *probe, bool permitted,
    ElectricalAngleTrig trig, void *trig_context, ObserveComputeProbeStampRead stamp_read,
    void *stamp_context)
{
    ProbeProfile profile;
    CurrentControllerOutput output;
    float phasor[4];
    uint32_t segment, elapsed, case_index;
    bool algorithm_ok, speed_limited;
    if (probe == NULL || probe->result.state != OBSERVE_COMPUTE_PROBE_ACTIVE) {
        return false;
    }
    if (!permitted) {
        fail(probe, OBSERVE_COMPUTE_PROBE_FAILURE_NOT_READY);
        return true;
    }
    if (stamp_read == NULL) {
        fail(probe, OBSERVE_COMPUTE_PROBE_FAILURE_CLOCK);
        return true;
    }
    memset(&profile, 0, sizeof(profile));
    profile.read = stamp_read;
    profile.context = stamp_context;
    profile.clock_ok = true;
    if (!profile_begin(&profile, OBSERVE_COMPUTE_PROBE_SEGMENT_TOTAL)) {
        fail(probe, OBSERVE_COMPUTE_PROBE_FAILURE_CLOCK);
        return true;
    }
    case_index = probe->result.next_case;
    algorithm_ok = run_case(probe, case_index, &output, &speed_limited, phasor,
        trig, trig_context, &profile);
    (void)profile_end(&profile, OBSERVE_COMPUTE_PROBE_SEGMENT_TOTAL);
    if (!profile.clock_ok) {
        fail(probe, OBSERVE_COMPUTE_PROBE_FAILURE_CLOCK);
        return true;
    }
    for (segment = 0U; segment < OBSERVE_COMPUTE_PROBE_SEGMENT_COUNT; segment++) {
        if ((profile.measured_mask & (1UL << segment)) != 0U) {
            elapsed = profile.end[segment].cycles - profile.begin[segment].cycles;
            if (elapsed == 0U || elapsed > probe->result.max_elapsed_cycles) {
                fail(probe, OBSERVE_COMPUTE_PROBE_FAILURE_CLOCK);
                return true;
            }
        }
    }
    if (!algorithm_ok) {
        fail(probe, OBSERVE_COMPUTE_PROBE_FAILURE_ALGORITHM);
        return true;
    }
    /* total 已结束且全部段通过检查，此后才一起更新可见统计。 */
    for (segment = 0U; segment < OBSERVE_COMPUTE_PROBE_SEGMENT_COUNT; segment++) {
        if ((profile.measured_mask & (1UL << segment)) != 0U) {
            accept_timing(&probe->result.cases[case_index].segments[segment],
                &profile.begin[segment], &profile.end[segment]);
        }
    }
    elapsed = profile.end[OBSERVE_COMPUTE_PROBE_SEGMENT_TOTAL].cycles -
        profile.begin[OBSERVE_COMPUTE_PROBE_SEGMENT_TOTAL].cycles;
    return accept_case(probe, case_index, elapsed, &output, speed_limited, phasor);
}

bool observe_compute_probe_abort(ObserveComputeProbe *probe, ObserveComputeProbeFailure reason)
{
    if (probe == NULL || probe->result.state != OBSERVE_COMPUTE_PROBE_ACTIVE) {
        return false;
    }
    if (reason <= OBSERVE_COMPUTE_PROBE_FAILURE_NONE || reason > OBSERVE_COMPUTE_PROBE_FAILURE_BUSY) {
        reason = OBSERVE_COMPUTE_PROBE_FAILURE_ABORTED;
    }
    fail(probe, reason);
    return true;
}

bool observe_compute_probe_revoke(ObserveComputeProbe *probe)
{
    if (probe == NULL ||
        (probe->result.state != OBSERVE_COMPUTE_PROBE_ACTIVE &&
         probe->result.state != OBSERVE_COMPUTE_PROBE_COMPLETE)) {
        return false;
    }
    fail(probe, OBSERVE_COMPUTE_PROBE_FAILURE_NOT_READY);
    return true;
}
