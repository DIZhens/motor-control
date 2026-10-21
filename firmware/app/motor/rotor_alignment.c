#include "rotor_alignment.h"

#include <math.h>
#include <stddef.h>
#include <string.h>

#include "../../../control/foc_math.h"

#define ALIGNMENT_TWO_PI 6.2831853071795865f
#define ALIGNMENT_INV_SQRT_THREE 0.5773502691896258f

static void clear_output(RotorAlignment *alignment)
{
    alignment->output.duty_a = 0.5f;
    alignment->output.duty_b = 0.5f;
    alignment->output.duty_c = 0.5f;
    alignment->output.alpha_voltage_v = 0.0f;
    alignment->output.beta_voltage_v = 0.0f;
    alignment->output.output_requested = false;
}

static bool terminate(RotorAlignment *alignment, RotorAlignmentState state,
                      RotorAlignmentReason reason)
{
    clear_output(alignment);
    memset(&alignment->candidate, 0, sizeof(alignment->candidate));
    alignment->window_active = false;
    alignment->state = state;
    alignment->reason = reason;
    return false;
}

static bool config_valid(const RotorAlignmentConfig *config)
{
    uint64_t minimum_duration = (uint64_t)config->ramp_duration_ticks +
        config->minimum_hold_ticks + config->settle_duration_ticks;
    return config->ticks_per_second > 0U && config->nominal_pole_pairs >= 1U &&
        config->nominal_pole_pairs <= 64U &&
        isfinite(config->stator_angle_turns) && config->stator_angle_turns >= 0.0f &&
        config->stator_angle_turns < 1.0f &&
        isfinite(config->alignment_voltage_v) && config->alignment_voltage_v > 0.0f &&
        isfinite(config->max_voltage_ratio) && config->max_voltage_ratio > 0.0f &&
        config->max_voltage_ratio <= ALIGNMENT_INV_SQRT_THREE &&
        isfinite(config->min_bus_voltage_v) && config->min_bus_voltage_v > 0.0f &&
        isfinite(config->max_bus_voltage_v) &&
        config->max_bus_voltage_v >= config->min_bus_voltage_v &&
        config->alignment_voltage_v <= config->max_voltage_ratio * config->min_bus_voltage_v &&
        isfinite(config->phase_current_trip_a) && config->phase_current_trip_a > 0.0f &&
        isfinite(config->min_excitation_current_a) && config->min_excitation_current_a > 0.0f &&
        config->min_excitation_current_a < config->phase_current_trip_a &&
        isfinite(config->max_settle_span_turns) && config->max_settle_span_turns > 0.0f &&
        config->max_settle_span_turns < 0.25f / (float)config->nominal_pole_pairs &&
        isfinite(config->max_settle_velocity_rpm) && config->max_settle_velocity_rpm > 0.0f &&
        config->ramp_duration_ticks > 0U && config->minimum_hold_ticks > 0U &&
        config->settle_duration_ticks > 0U && (uint64_t)config->timeout_ticks > minimum_duration &&
        config->max_step_ticks > 0U && config->max_step_ticks <= config->ramp_duration_ticks &&
        config->minimum_position_samples >= 2U && config->minimum_position_samples <= 65535U;
}

bool rotor_alignment_init(RotorAlignment *alignment, const RotorAlignmentConfig *config)
{
    RotorAlignmentConfig requested;
    if (alignment == NULL) {
        return false;
    }
    /* 先复制，支持使用实例内配置重新初始化；active 实例重配也不能代替实际 safe-off。 */
    if (config != NULL) {
        requested = *config;
    } else {
        memset(&requested, 0, sizeof(requested));
    }
    memset(alignment, 0, sizeof(*alignment));
    clear_output(alignment);
    if (!config_valid(&requested)) {
        return false;
    }
    alignment->config = requested;
    alignment->configured = true;
    return true;
}

static bool fresh(ControlTime now, ControlTime observed, uint32_t maximum_age)
{
    return observed <= now && now - observed <= (ControlTime)maximum_age;
}

static RotorAlignmentReason check_input(const RotorAlignment *alignment,
                                        const RotorAlignmentInput *input)
{
    uint32_t index;
    if (input->fault_active) {
        return ROTOR_ALIGNMENT_REASON_FAULT;
    }
    if (!input->commissioning_allowed || !input->current_calibration_confirmed) {
        return ROTOR_ALIGNMENT_REASON_PREREQUISITE;
    }
    if (!input->current_valid || !input->position_valid || !input->bus_valid ||
        !fresh(input->now, input->current_time, alignment->config.max_current_age_ticks) ||
        !fresh(input->now, input->position_time, alignment->config.max_position_age_ticks) ||
        !fresh(input->now, input->bus_time, alignment->config.max_bus_age_ticks) ||
        !isfinite(input->position_turns) || input->position_turns < 0.0f ||
        input->position_turns >= 1.0f || !isfinite(input->velocity_rpm)) {
        return ROTOR_ALIGNMENT_REASON_FEEDBACK;
    }
    if (!isfinite(input->bus_voltage_v) ||
        input->bus_voltage_v < alignment->config.min_bus_voltage_v ||
        input->bus_voltage_v > alignment->config.max_bus_voltage_v) {
        return ROTOR_ALIGNMENT_REASON_BUS;
    }
    for (index = 0U; index < 3U; ++index) {
        if (!isfinite(input->phase_current_a[index])) {
            return ROTOR_ALIGNMENT_REASON_FEEDBACK;
        }
        if (fabsf(input->phase_current_a[index]) >= alignment->config.phase_current_trip_a) {
            return ROTOR_ALIGNMENT_REASON_OVERCURRENT;
        }
    }
    return ROTOR_ALIGNMENT_REASON_NONE;
}

void rotor_alignment_stop(RotorAlignment *alignment)
{
    if (alignment != NULL) {
        (void)terminate(alignment, ROTOR_ALIGNMENT_STOPPED, ROTOR_ALIGNMENT_REASON_STOP);
    }
}

bool rotor_alignment_start(RotorAlignment *alignment, const RotorAlignmentInput *input)
{
    RotorAlignmentReason reason;
    if (alignment == NULL || !alignment->configured ||
        alignment->state != ROTOR_ALIGNMENT_IDLE) {
        return false;
    }
    if (input == NULL) {
        return terminate(alignment, ROTOR_ALIGNMENT_FAILED, ROTOR_ALIGNMENT_REASON_FEEDBACK);
    }
    if (input->stop_requested) {
        rotor_alignment_stop(alignment);
        return false;
    }
    reason = check_input(alignment, input);
    if (reason != ROTOR_ALIGNMENT_REASON_NONE) {
        return terminate(alignment, ROTOR_ALIGNMENT_FAILED, reason);
    }
    alignment->started_at = input->now;
    alignment->last_update_time = input->now;
    alignment->last_current_time = input->current_time;
    alignment->last_position_time = input->position_time;
    alignment->last_position_turns = input->position_turns;
    alignment->last_velocity_rpm = input->velocity_rpm;
    alignment->state = ROTOR_ALIGNMENT_RAMP;
    return true;
}

/* 此处输入限于 +/-65 圈内，不进行任意大浮点到整数的转换。 */
static float wrap_bounded(float turns)
{
    float wrapped = turns - (float)(int32_t)turns;
    if (wrapped < 0.0f) {
        wrapped += 1.0f;
    }
    return wrapped < 1.0f ? wrapped : 0.0f;
}

static float circular_delta(float value, float anchor)
{
    float delta = value - anchor;
    if (delta > 0.5f) {
        delta -= 1.0f;
    } else if (delta < -0.5f) {
        delta += 1.0f;
    }
    return delta;
}

static void begin_window(RotorAlignment *alignment, const RotorAlignmentInput *input)
{
    alignment->window_active = true;
    alignment->window_first_time = input->position_time;
    alignment->window_anchor_turns = input->position_turns;
    alignment->window_min_delta = 0.0f;
    alignment->window_max_delta = 0.0f;
    alignment->window_mean_delta = 0.0f;
    alignment->window_samples = 1U;
}

static bool observe_settling(RotorAlignment *alignment, const RotorAlignmentInput *input,
                             bool new_position, float current_magnitude)
{
    float delta, low, high, position;
    RotorAlignmentCandidate candidate;
    ControlTime elapsed = input->now - alignment->started_at;
    ControlTime minimum_held = (uint64_t)alignment->config.ramp_duration_ticks +
        alignment->config.minimum_hold_ticks;
    if (elapsed < (uint64_t)alignment->config.ramp_duration_ticks +
        alignment->config.minimum_hold_ticks ||
        current_magnitude < alignment->config.min_excitation_current_a ||
        fabsf(input->velocity_rpm) > alignment->config.max_settle_velocity_rpm) {
        alignment->window_active = false;
        return false;
    }
    /* 旧位置即使仍新鲜也不能重复计数；窗口首样本也必须在保持阶段真正采集。 */
    if (!new_position || input->position_time < alignment->started_at ||
        input->position_time - alignment->started_at < minimum_held) {
        return false;
    }
    if (!alignment->window_active) {
        begin_window(alignment, input);
        return false;
    }
    delta = circular_delta(input->position_turns, alignment->window_anchor_turns);
    low = fminf(alignment->window_min_delta, delta);
    high = fmaxf(alignment->window_max_delta, delta);
    if (high - low > alignment->config.max_settle_span_turns) {
        begin_window(alignment, input);
        return false;
    }
    if (alignment->window_samples == UINT32_MAX) {
        return terminate(alignment, ROTOR_ALIGNMENT_FAILED, ROTOR_ALIGNMENT_REASON_NUMERIC);
    }
    ++alignment->window_samples;
    alignment->window_min_delta = low;
    alignment->window_max_delta = high;
    alignment->window_mean_delta += (delta - alignment->window_mean_delta) /
        (float)alignment->window_samples;
    if (input->position_time - alignment->window_first_time < alignment->config.settle_duration_ticks ||
        alignment->window_samples < alignment->config.minimum_position_samples) {
        return false;
    }
    position = wrap_bounded(alignment->window_anchor_turns + alignment->window_mean_delta);
    memset(&candidate, 0, sizeof(candidate));
    candidate.encoder_position_turns = position;
    candidate.stator_angle_turns = alignment->config.stator_angle_turns;
    candidate.positive_direction_offset_turns = wrap_bounded(
        (float)alignment->config.nominal_pole_pairs * position - candidate.stator_angle_turns);
    candidate.negative_direction_offset_turns = wrap_bounded(
        -(float)alignment->config.nominal_pole_pairs * position - candidate.stator_angle_turns);
    candidate.nominal_pole_pairs = alignment->config.nominal_pole_pairs;
    candidate.position_samples = alignment->window_samples;
    candidate.first_position_time = alignment->window_first_time;
    candidate.last_position_time = input->position_time;
    candidate.valid = true;
    clear_output(alignment);
    alignment->candidate = candidate;
    alignment->window_active = false;
    alignment->state = ROTOR_ALIGNMENT_SUCCEEDED;
    return true;
}

bool rotor_alignment_update(RotorAlignment *alignment, const RotorAlignmentInput *input)
{
    RotorAlignmentReason reason;
    ControlTime elapsed;
    float voltage, radians, alpha, beta, current_alpha, current_beta, current_magnitude;
    FocModulation modulation;
    bool new_position;
    if (alignment == NULL) {
        return false;
    }
    clear_output(alignment);
    if (!alignment->configured || (alignment->state != ROTOR_ALIGNMENT_RAMP &&
        alignment->state != ROTOR_ALIGNMENT_HOLD)) {
        return false;
    }
    if (input == NULL) {
        return terminate(alignment, ROTOR_ALIGNMENT_FAILED, ROTOR_ALIGNMENT_REASON_FEEDBACK);
    }
    if (input->stop_requested) {
        rotor_alignment_stop(alignment);
        return false;
    }
    reason = check_input(alignment, input);
    if (reason != ROTOR_ALIGNMENT_REASON_NONE) {
        return terminate(alignment, ROTOR_ALIGNMENT_FAILED, reason);
    }
    if (input->now <= alignment->last_update_time ||
        input->now - alignment->last_update_time > alignment->config.max_step_ticks ||
        input->current_time <= alignment->last_current_time ||
        input->position_time < alignment->last_position_time ||
        (input->position_time == alignment->last_position_time &&
         (input->position_turns != alignment->last_position_turns ||
          input->velocity_rpm != alignment->last_velocity_rpm))) {
        return terminate(alignment, ROTOR_ALIGNMENT_FAILED, ROTOR_ALIGNMENT_REASON_TIME);
    }
    elapsed = input->now - alignment->started_at;
    if (elapsed >= alignment->config.timeout_ticks) {
        return terminate(alignment, ROTOR_ALIGNMENT_FAILED, ROTOR_ALIGNMENT_REASON_TIMEOUT);
    }
    new_position = input->position_time != alignment->last_position_time;
    alignment->last_update_time = input->now;
    alignment->last_current_time = input->current_time;
    alignment->last_position_time = input->position_time;
    alignment->last_position_turns = input->position_turns;
    alignment->last_velocity_rpm = input->velocity_rpm;
    /* 完整幅值不变 Clarke 消除三相公共模；励磁证据不能把零序电流当成转矩矢量。 */
    current_alpha = (2.0f * input->phase_current_a[0] - input->phase_current_a[1] -
                     input->phase_current_a[2]) / 3.0f;
    current_beta = (input->phase_current_a[1] - input->phase_current_a[2]) *
        ALIGNMENT_INV_SQRT_THREE;
    current_magnitude = sqrtf(current_alpha * current_alpha + current_beta * current_beta);
    if (!isfinite(current_magnitude)) {
        return terminate(alignment, ROTOR_ALIGNMENT_FAILED, ROTOR_ALIGNMENT_REASON_NUMERIC);
    }
    if (elapsed >= alignment->config.ramp_duration_ticks) {
        alignment->state = ROTOR_ALIGNMENT_HOLD;
        voltage = alignment->config.alignment_voltage_v;
        if (observe_settling(alignment, input, new_position, current_magnitude) ||
            alignment->state == ROTOR_ALIGNMENT_FAILED) {
            return false;
        }
    } else {
        voltage = alignment->config.alignment_voltage_v *
            ((float)elapsed / (float)alignment->config.ramp_duration_ticks);
    }
    radians = ALIGNMENT_TWO_PI * alignment->config.stator_angle_turns;
    alpha = voltage * cosf(radians);
    beta = voltage * sinf(radians);
    if (!foc_svpwm(alpha, beta, input->bus_voltage_v, alignment->config.max_voltage_ratio,
                   &modulation) || modulation.limited) {
        return terminate(alignment, ROTOR_ALIGNMENT_FAILED, ROTOR_ALIGNMENT_REASON_NUMERIC);
    }
    alignment->output.duty_a = modulation.duty_a;
    alignment->output.duty_b = modulation.duty_b;
    alignment->output.duty_c = modulation.duty_c;
    alignment->output.alpha_voltage_v = modulation.applied_alpha_v;
    alignment->output.beta_voltage_v = modulation.applied_beta_v;
    alignment->output.output_requested = true;
    return true;
}
