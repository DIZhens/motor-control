#include "position_estimator.h"

#include <math.h>
#include <stddef.h>

void position_estimator_reset(PositionEstimator *estimator)
{
    if (estimator == NULL) {
        return;
    }
    estimator->velocity_rpm = 0.0f;
    estimator->velocity_valid = false;
    estimator->previous_position_turns = 0.0f;
    estimator->has_position = false;
}

bool position_estimator_init(PositionEstimator *estimator, const PositionEstimatorConfig *config)
{
    PositionEstimatorConfig requested;
    if (estimator == NULL) {
        return false;
    }
    if (config != NULL) {
        requested = *config;
    } else {
        requested.max_velocity_rpm = 0.0f;
        requested.velocity_filter_time_s = 0.0f;
    }
    position_estimator_reset(estimator);
    estimator->configured = false;
    estimator->config.max_velocity_rpm = 0.0f;
    estimator->config.velocity_filter_time_s = 0.0f;
    if (!isfinite(requested.max_velocity_rpm) || requested.max_velocity_rpm <= 0.0f ||
        !isfinite(requested.velocity_filter_time_s) || requested.velocity_filter_time_s < 0.0f) {
        return false;
    }
    estimator->config = requested;
    estimator->configured = true;
    return true;
}

bool position_estimator_update(PositionEstimator *estimator, float position_turns, float interval_s)
{
    float delta_turns, velocity_rpm, alpha, ratio;
    if (estimator == NULL) {
        return false;
    }
    if (!estimator->configured || !isfinite(position_turns) || position_turns < 0.0f ||
        position_turns >= 1.0f || !isfinite(interval_s) || interval_s <= 0.0f ||
        interval_s >= 30.0f / estimator->config.max_velocity_rpm) {
        position_estimator_reset(estimator);
        return false;
    }
    if (!estimator->has_position) {
        estimator->previous_position_turns = position_turns;
        estimator->has_position = true;
        return false;
    }
    delta_turns = position_turns - estimator->previous_position_turns;
    if (delta_turns == 0.5f || delta_turns == -0.5f) {
        position_estimator_reset(estimator);
        return false;
    }
    if (delta_turns > 0.5f) {
        delta_turns -= 1.0f;
    } else if (delta_turns < -0.5f) {
        delta_turns += 1.0f;
    }
    velocity_rpm = delta_turns / interval_s * 60.0f;
    if (!isfinite(velocity_rpm) || fabsf(velocity_rpm) > estimator->config.max_velocity_rpm) {
        position_estimator_reset(estimator);
        return false;
    }
    if (estimator->velocity_valid && estimator->config.velocity_filter_time_s > 0.0f) {
        /* 与 dt / (tau + dt) 等价，避免两个合法大数相加后溢出。 */
        if (estimator->config.velocity_filter_time_s > interval_s) {
            ratio = interval_s / estimator->config.velocity_filter_time_s;
            alpha = ratio / (1.0f + ratio);
        } else {
            alpha = 1.0f / (1.0f + estimator->config.velocity_filter_time_s / interval_s);
        }
        velocity_rpm = (1.0f - alpha) * estimator->velocity_rpm + alpha * velocity_rpm;
        if (!isfinite(velocity_rpm)) {
            position_estimator_reset(estimator);
            return false;
        }
    }
    estimator->previous_position_turns = position_turns;
    estimator->velocity_rpm = velocity_rpm;
    estimator->velocity_valid = true;
    return true;
}
