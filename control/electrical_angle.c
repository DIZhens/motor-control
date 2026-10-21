#include "electrical_angle.h"

#include <math.h>
#include <stddef.h>

#define ELECTRICAL_ANGLE_MAX_POLE_PAIRS 64U
#define ELECTRICAL_ANGLE_TWO_PI 6.2831853071795865f
#define ELECTRICAL_ANGLE_PHASOR_NORM_TOLERANCE 0.001f

void electrical_angle_reset(ElectricalAngle *angle)
{
    if (angle == NULL) {
        return;
    }
    angle->output.electrical_turns = 0.0f;
    angle->output.sin_theta = 0.0f;
    angle->output.cos_theta = 0.0f;
    angle->output.valid = false;
}

static bool config_valid(const ElectricalAngleConfig *config)
{
    return config->pole_pairs >= 1U &&
           config->pole_pairs <= ELECTRICAL_ANGLE_MAX_POLE_PAIRS &&
           (config->encoder_direction == 1 || config->encoder_direction == -1) &&
           isfinite(config->electrical_offset_turns) &&
           config->electrical_offset_turns >= 0.0f &&
           config->electrical_offset_turns < 1.0f &&
           isfinite(config->max_prediction_time_s) &&
           config->max_prediction_time_s >= 0.0f &&
           isfinite(config->max_velocity_rpm) && config->max_velocity_rpm > 0.0f &&
           (config->max_prediction_time_s == 0.0f ||
            config->max_prediction_time_s < 30.0f / config->max_velocity_rpm);
}

bool electrical_angle_init(ElectricalAngle *angle, const ElectricalAngleConfig *config)
{
    ElectricalAngleConfig requested;
    if (angle == NULL) {
        return false;
    }
    /* 先复制以允许对已有实例直接使用 &angle->config 重置输出。 */
    if (config != NULL) {
        requested = *config;
    } else {
        requested.pole_pairs = 0U;
        requested.encoder_direction = 0;
        requested.electrical_offset_turns = 0.0f;
        requested.max_prediction_time_s = 0.0f;
        requested.max_velocity_rpm = 0.0f;
    }
    electrical_angle_reset(angle);
    angle->configured = false;
    angle->config.pole_pairs = 0U;
    angle->config.encoder_direction = 0;
    angle->config.electrical_offset_turns = 0.0f;
    angle->config.max_prediction_time_s = 0.0f;
    angle->config.max_velocity_rpm = 0.0f;
    if (!config_valid(&requested)) {
        return false;
    }
    angle->config = requested;
    angle->configured = true;
    return true;
}

/* 仅供已限制在约 +/-65 圈内的数值使用；转换不会越过有符号整数范围。 */
static float wrap_bounded_turns(float turns)
{
    float wrapped = turns - (float)(int32_t)turns;
    if (wrapped < 0.0f) {
        wrapped += 1.0f;
    }
    /* 极小负数加一可能舍入到 1；周期零点在此统一表示为 0。 */
    if (wrapped >= 1.0f) {
        wrapped = 0.0f;
    }
    return wrapped;
}

bool electrical_angle_update(ElectricalAngle *angle, float position_turns,
                              float velocity_rpm, float observation_age_s)
{
    return electrical_angle_update_with_trig(angle, position_turns, velocity_rpm,
                                              observation_age_s,
                                              electrical_angle_sincos_software, NULL);
}

bool electrical_angle_sincos_software(void *context, float electrical_turns,
                                     float *sine, float *cosine)
{
    float radians;
    (void)context;
    if (sine != NULL) {
        *sine = 0.0f;
    }
    if (cosine != NULL) {
        *cosine = 0.0f;
    }
    if (sine == NULL || cosine == NULL || sine == cosine || !isfinite(electrical_turns) ||
        electrical_turns < 0.0f || electrical_turns >= 1.0f) {
        return false;
    }
    radians = ELECTRICAL_ANGLE_TWO_PI * electrical_turns;
    *sine = sinf(radians);
    *cosine = cosf(radians);
    return isfinite(*sine) && isfinite(*cosine);
}

bool electrical_angle_update_with_trig(ElectricalAngle *angle, float position_turns,
                                        float velocity_rpm, float observation_age_s,
                                        ElectricalAngleTrig trig, void *context)
{
    float delta_turns, predicted_position, electrical_turns, norm;
    float sine = 0.0f;
    float cosine = 0.0f;
    if (angle == NULL) {
        return false;
    }
    electrical_angle_reset(angle);
    if (trig == NULL || !angle->configured ||
        !isfinite(position_turns) || position_turns < 0.0f ||
        position_turns >= 1.0f || !isfinite(velocity_rpm) ||
        fabsf(velocity_rpm) > angle->config.max_velocity_rpm ||
        !isfinite(observation_age_s) || observation_age_s < 0.0f ||
        observation_age_s > angle->config.max_prediction_time_s) {
        return false;
    }
    /* 先除 60，避免合法大速度与年龄直接相乘发生无谓溢出。 */
    delta_turns = (velocity_rpm / 60.0f) * observation_age_s;
    if (!isfinite(delta_turns) || fabsf(delta_turns) >= 0.5f) {
        return false;
    }
    predicted_position = wrap_bounded_turns(position_turns + delta_turns);
    electrical_turns = wrap_bounded_turns(
        (float)angle->config.pole_pairs * (float)angle->config.encoder_direction *
        predicted_position - angle->config.electrical_offset_turns);
    if (!isfinite(electrical_turns) ||
        !trig(context, electrical_turns, &sine, &cosine) ||
        !isfinite(sine) || !isfinite(cosine)) {
        return false;
    }
    norm = sine * sine + cosine * cosine;
    if (!isfinite(norm) || fabsf(norm - 1.0f) > ELECTRICAL_ANGLE_PHASOR_NORM_TOLERANCE) {
        return false;
    }
    angle->output.electrical_turns = electrical_turns;
    angle->output.sin_theta = sine;
    angle->output.cos_theta = cosine;
    angle->output.valid = true;
    return true;
}
