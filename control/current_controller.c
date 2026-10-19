#include "current_controller.h"

#include "foc_math.h"

#include <math.h>
#include <stddef.h>

static bool current_controller_config_valid(const CurrentControllerConfig *config)
{
    return config != NULL &&
        isfinite(config->kp_d_v_per_a) && config->kp_d_v_per_a >= 0.0f &&
        isfinite(config->kp_q_v_per_a) && config->kp_q_v_per_a >= 0.0f &&
        isfinite(config->ki_d_v_per_a_s) && config->ki_d_v_per_a_s >= 0.0f &&
        isfinite(config->ki_q_v_per_a_s) && config->ki_q_v_per_a_s >= 0.0f &&
        isfinite(config->tracking_gain_per_s) && config->tracking_gain_per_s >= 0.0f &&
        isfinite(config->integral_limit_v) && config->integral_limit_v > 0.0f &&
        isfinite(config->reference_current_limit_a) && config->reference_current_limit_a > 0.0f &&
        isfinite(config->max_voltage_ratio) && config->max_voltage_ratio > 0.0f &&
        config->max_voltage_ratio <= 0.577350269f &&
        isfinite(config->max_interval_s) && config->max_interval_s > 0.0f;
}

static void current_controller_clear_output(CurrentControllerOutput *output)
{
    if (output == NULL) {
        return;
    }
    output->id_a = 0.0f;
    output->iq_a = 0.0f;
    output->requested_vd_v = 0.0f;
    output->requested_vq_v = 0.0f;
    output->applied_vd_v = 0.0f;
    output->applied_vq_v = 0.0f;
    output->duty_a = 0.5f;
    output->duty_b = 0.5f;
    output->duty_c = 0.5f;
    output->voltage_limited = false;
    output->integral_limited = false;
    output->valid = false;
}

void current_controller_reset(CurrentController *controller)
{
    if (controller == NULL) {
        return;
    }
    controller->integral_d_v = 0.0f;
    controller->integral_q_v = 0.0f;
}

bool current_controller_init(CurrentController *controller,
                             const CurrentControllerConfig *config)
{
    CurrentControllerConfig requested = {0};
    if (controller == NULL) {
        return false;
    }
    if (config != NULL) {
        requested = *config;
    }
    current_controller_reset(controller);
    controller->configured = false;
    controller->config = (CurrentControllerConfig){0};
    if (!current_controller_config_valid(&requested)) {
        return false;
    }
    controller->config = requested;
    controller->configured = true;
    return true;
}

static bool current_controller_reference_valid(float d, float q, float limit)
{
    float normalized_d, normalized_q;
    if (!isfinite(d) || !isfinite(q) || fabsf(d) > limit || fabsf(q) > limit) {
        return false;
    }
    /* 先除以上限再求平方和，避免合法大数的平方溢出。 */
    normalized_d = d / limit;
    normalized_q = q / limit;
    return normalized_d * normalized_d + normalized_q * normalized_q <= 1.0f;
}

static bool current_controller_phasor(float sine, float cosine,
                                       float *normalized_sine, float *normalized_cosine)
{
    FocDq validation;
    float norm;
    /* 复用基础变换的合法性合同；通过后才本地归一化，不放宽外部相量容差。 */
    if (!foc_park(0.0f, 0.0f, sine, cosine, &validation)) {
        return false;
    }
    norm = sqrtf(sine * sine + cosine * cosine);
    if (!isfinite(norm) || norm <= 0.0f) {
        return false;
    }
    *normalized_sine = sine / norm;
    *normalized_cosine = cosine / norm;
    return isfinite(*normalized_sine) && isfinite(*normalized_cosine);
}

static bool current_controller_next_integral(float previous, float error,
                                             float ki, float tracking_gain,
                                             float requested, float applied,
                                             float interval, float limit,
                                             float *next, bool *limited)
{
    float error_term = ki * error;
    float tracking_error = applied - requested;
    float tracking_term = tracking_gain * tracking_error;
    float derivative = error_term + tracking_term;
    float increment = interval * derivative;
    float candidate = previous + increment;
    if (!isfinite(error_term) || !isfinite(tracking_error) ||
        !isfinite(tracking_term) || !isfinite(derivative) ||
        !isfinite(increment) || !isfinite(candidate)) {
        return false;
    }
    if (candidate > limit) {
        candidate = limit;
        *limited = true;
    } else if (candidate < -limit) {
        candidate = -limit;
        *limited = true;
    }
    *next = candidate;
    return true;
}

bool current_controller_update(CurrentController *controller,
                               const CurrentControllerInput *input,
                               CurrentControllerOutput *output)
{
    FocDq measured, applied;
    FocAlphaBeta voltage;
    FocModulation modulation;
    CurrentControllerOutput candidate;
    float error_d, error_q, proportional_d, proportional_q;
    float requested_d, requested_q, next_integral_d, next_integral_q;
    float sine, cosine, actuation_sine, actuation_cosine;
    bool integral_limited = false;
    current_controller_clear_output(output);
    if (controller == NULL) {
        return false;
    }
    if (output == NULL || input == NULL || !controller->configured ||
        !current_controller_config_valid(&controller->config) ||
        !isfinite(controller->integral_d_v) || !isfinite(controller->integral_q_v) ||
        fabsf(controller->integral_d_v) > controller->config.integral_limit_v ||
        fabsf(controller->integral_q_v) > controller->config.integral_limit_v ||
        !input->current_valid || !input->angle_valid || !input->bus_voltage_valid ||
        !input->reference_valid || !isfinite(input->bus_voltage_v) ||
        input->bus_voltage_v <= 0.0f || !isfinite(input->interval_s) ||
        input->interval_s <= 0.0f || input->interval_s > controller->config.max_interval_s ||
        !current_controller_reference_valid(input->id_reference_a, input->iq_reference_a,
                                             controller->config.reference_current_limit_a)) {
        current_controller_reset(controller);
        return false;
    }
    if (!current_controller_phasor(input->sin_electrical_angle, input->cos_electrical_angle,
                                    &sine, &cosine) ||
        !foc_park(input->current_alpha_a, input->current_beta_a, sine, cosine, &measured)) {
        current_controller_reset(controller);
        return false;
    }
    if (input->separate_actuation_angle) {
        if (!current_controller_phasor(input->actuation_sin_theta, input->actuation_cos_theta,
                                       &actuation_sine, &actuation_cosine)) {
            current_controller_reset(controller);
            return false;
        }
    } else {
        actuation_sine = sine;
        actuation_cosine = cosine;
    }
    error_d = input->id_reference_a - measured.d;
    error_q = input->iq_reference_a - measured.q;
    proportional_d = controller->config.kp_d_v_per_a * error_d;
    proportional_q = controller->config.kp_q_v_per_a * error_q;
    requested_d = proportional_d + controller->integral_d_v;
    requested_q = proportional_q + controller->integral_q_v;
    if (!isfinite(error_d) || !isfinite(error_q) || !isfinite(proportional_d) ||
        !isfinite(proportional_q) || !isfinite(requested_d) || !isfinite(requested_q) ||
        !foc_inverse_park(requested_d, requested_q, actuation_sine, actuation_cosine, &voltage) ||
        !foc_svpwm(voltage.alpha, voltage.beta, input->bus_voltage_v,
                   controller->config.max_voltage_ratio, &modulation) ||
        !foc_park(modulation.applied_alpha_v, modulation.applied_beta_v,
                  actuation_sine, actuation_cosine, &applied) ||
        !current_controller_next_integral(controller->integral_d_v, error_d,
            controller->config.ki_d_v_per_a_s, controller->config.tracking_gain_per_s,
            requested_d, applied.d, input->interval_s, controller->config.integral_limit_v,
            &next_integral_d, &integral_limited) ||
        !current_controller_next_integral(controller->integral_q_v, error_q,
            controller->config.ki_q_v_per_a_s, controller->config.tracking_gain_per_s,
            requested_q, applied.q, input->interval_s, controller->config.integral_limit_v,
            &next_integral_q, &integral_limited)) {
        current_controller_reset(controller);
        return false;
    }
    candidate.id_a = measured.d;
    candidate.iq_a = measured.q;
    candidate.requested_vd_v = requested_d;
    candidate.requested_vq_v = requested_q;
    candidate.applied_vd_v = applied.d;
    candidate.applied_vq_v = applied.q;
    candidate.duty_a = modulation.duty_a;
    candidate.duty_b = modulation.duty_b;
    candidate.duty_c = modulation.duty_c;
    candidate.voltage_limited = modulation.limited;
    candidate.integral_limited = integral_limited;
    candidate.valid = true;
    controller->integral_d_v = next_integral_d;
    controller->integral_q_v = next_integral_q;
    *output = candidate;
    return true;
}
