#include "speed_controller.h"

#include <math.h>
#include <stddef.h>

static bool speed_controller_config_valid(const SpeedControllerConfig *config)
{
    return config != NULL &&
        isfinite(config->kp_a_per_rpm) && config->kp_a_per_rpm >= 0.0f &&
        isfinite(config->ki_a_per_rpm_s) && config->ki_a_per_rpm_s >= 0.0f &&
        isfinite(config->tracking_gain_per_s) && config->tracking_gain_per_s >= 0.0f &&
        isfinite(config->max_iq_a) && config->max_iq_a > 0.0f &&
        isfinite(config->integral_limit_a) && config->integral_limit_a > 0.0f &&
        isfinite(config->max_target_velocity_rpm) && config->max_target_velocity_rpm > 0.0f &&
        isfinite(config->max_interval_s) && config->max_interval_s > 0.0f;
}

void speed_controller_reset(SpeedController *controller)
{
    if (controller != NULL) {
        controller->integral_a = 0.0f;
    }
}

bool speed_controller_init(SpeedController *controller, const SpeedControllerConfig *config)
{
    SpeedControllerConfig requested = {0};
    if (controller == NULL) {
        return false;
    }
    if (config != NULL) {
        requested = *config;
    }
    speed_controller_reset(controller);
    controller->config = (SpeedControllerConfig){0};
    controller->configured = false;
    if (!speed_controller_config_valid(&requested)) {
        return false;
    }
    controller->config = requested;
    controller->configured = true;
    return true;
}

static void speed_controller_clear_output(SpeedControllerOutput *output)
{
    if (output != NULL) {
        output->requested_iq_a = 0.0f;
        output->iq_reference_a = 0.0f;
        output->current_limited = false;
        output->integral_limited = false;
        output->valid = false;
    }
}

static float speed_controller_limit(float value, float limit, bool *limited)
{
    if (value > limit) {
        *limited = true;
        return limit;
    }
    if (value < -limit) {
        *limited = true;
        return -limit;
    }
    return value;
}

bool speed_controller_update(SpeedController *controller, const SpeedControllerInput *input,
                             SpeedControllerOutput *output)
{
    SpeedControllerOutput candidate;
    float error, proportional, requested, iq;
    float error_term, tracking_error, tracking_term, derivative, increment, next_integral;
    bool current_limited = false;
    bool integral_limited = false;
    speed_controller_clear_output(output);
    if (controller == NULL) {
        return false;
    }
    if (output == NULL || input == NULL || !controller->configured ||
        !speed_controller_config_valid(&controller->config) ||
        !isfinite(controller->integral_a) ||
        fabsf(controller->integral_a) > controller->config.integral_limit_a ||
        !input->velocity_valid || !input->reference_valid ||
        !isfinite(input->target_velocity_rpm) || !isfinite(input->measured_velocity_rpm) ||
        fabsf(input->target_velocity_rpm) > controller->config.max_target_velocity_rpm ||
        !isfinite(input->interval_s) || input->interval_s <= 0.0f ||
        input->interval_s > controller->config.max_interval_s) {
        speed_controller_reset(controller);
        return false;
    }
    error = input->target_velocity_rpm - input->measured_velocity_rpm;
    proportional = controller->config.kp_a_per_rpm * error;
    requested = proportional + controller->integral_a;
    if (!isfinite(error) || !isfinite(proportional) || !isfinite(requested)) {
        speed_controller_reset(controller);
        return false;
    }
    iq = speed_controller_limit(requested, controller->config.max_iq_a, &current_limited);
    error_term = controller->config.ki_a_per_rpm_s * error;
    tracking_error = iq - requested;
    tracking_term = controller->config.tracking_gain_per_s * tracking_error;
    derivative = error_term + tracking_term;
    increment = input->interval_s * derivative;
    next_integral = controller->integral_a + increment;
    if (!isfinite(error_term) || !isfinite(tracking_error) || !isfinite(tracking_term) ||
        !isfinite(derivative) || !isfinite(increment) || !isfinite(next_integral)) {
        speed_controller_reset(controller);
        return false;
    }
    next_integral = speed_controller_limit(next_integral, controller->config.integral_limit_a,
                                           &integral_limited);
    candidate.requested_iq_a = requested;
    candidate.iq_reference_a = iq;
    candidate.current_limited = current_limited;
    candidate.integral_limited = integral_limited;
    candidate.valid = true;
    controller->integral_a = next_integral;
    *output = candidate;
    return true;
}
