#include "current_hold.h"
#include "electrical_angle.h"

#if defined(__CC_ARM)
#pragma Otime
#endif

#include <math.h>
#include <stddef.h>
#include <string.h>

static bool initialize(CurrentHold *hold, const CurrentHoldConfig *config, bool survey)
{
    /* PI增益保持；命令18只扩大足够用于定向的目标、积分与电压包络。
     * ratio按允许母线上限14V换算，整个10～14V范围不超过各命令的电压上限。 */
    const CurrentControllerConfig controller = {
        0.5f, 0.5f, 10.0f, 10.0f, 50.0f,
        survey ? CURRENT_SURVEY_INTEGRAL_LIMIT_V : 0.3f,
        survey ? CURRENT_SURVEY_MAX_CURRENT_A : 0.08f,
        (survey ? CURRENT_SURVEY_MAX_VOLTAGE_V : 0.4f) / 14.0f, 0.000125f
    };
    CurrentHoldConfig requested = {0};
    float norm;

    if (hold == NULL) {
        return false;
    }
    if (config != NULL) {
        requested = *config;
    }
    memset(hold, 0, sizeof(*hold));
    norm = requested.alpha_reference_a * requested.alpha_reference_a
        + requested.beta_reference_a * requested.beta_reference_a;
    if (!isfinite(norm) || norm < 0.0001f || norm > controller.reference_current_limit_a * controller.reference_current_limit_a
        || !current_controller_init(&hold->controller, &controller)) {
        return false;
    }
    hold->config = requested;
    hold->configured = true;
    hold->survey = survey;
    return true;
}

bool current_hold_init(CurrentHold *hold, const CurrentHoldConfig *config)
{
    return initialize(hold, config, false);
}

bool current_hold_survey_init(CurrentHold *hold, float current_reference_a)
{
    CurrentHoldConfig config = {current_reference_a, 0.0f};
    if (!initialize(hold, &config, true) || current_reference_a <= 0.0f) {
        if (hold != NULL) { hold->configured = false; }
        return false;
    }
    return true;
}

bool current_hold_rotor_init(CurrentHold *hold, const RotorCurrentConfig *config)
{
    CurrentHoldConfig target;
    /* 转子模式需跟踪变化的反电动势。按Rs约3.1Ω，Ki=300给出约十余Hz量级，
     * 不沿用静态试验Ki=10；仍保留低Kp、积分/电压限制，参数先经延迟R/L模型核对。 */
    const CurrentControllerConfig controller = {0.5f, 0.5f, 300.0f, 300.0f, 50.0f,
        CURRENT_SURVEY_INTEGRAL_LIMIT_V, ROTOR_CURRENT_MAX_A, CURRENT_SURVEY_MAX_VOLTAGE_V / 14.0f, 0.000125f};
    const PositionEstimatorConfig velocity = {ROTOR_CURRENT_MAX_RPM, 0.0f};
    ElectricalAngleConfig angle = {7U, 1, 0.0f, 0.002f, ROTOR_CURRENT_MAX_RPM};
    if (hold == NULL) { return false; }
    memset(hold, 0, sizeof(*hold));
    if (config == NULL || !isfinite(config->iq_reference_a)
        || fabsf(config->iq_reference_a) < 0.01f || fabsf(config->iq_reference_a) > ROTOR_CURRENT_MAX_A) {
        return false;
    }
    target.alpha_reference_a = 0.0f;
    target.beta_reference_a = config->iq_reference_a;
    angle.electrical_offset_turns = config->electrical_offset_turns;
    if (!initialize(hold, &target, true) || !current_controller_init(&hold->controller, &controller)
        || !position_estimator_init(&hold->velocity, &velocity)
        || !electrical_angle_init(&hold->measurement_angle, &angle)
        || !electrical_angle_init(&hold->actuation_angle, &angle)) {
        hold->configured = false;
        return false;
    }
    hold->survey = false;
    hold->rotor = true;
    return true;
}

static bool reject_rotor_feedback(CurrentHold *hold)
{
    if (hold != NULL) {
        hold->configured = false;
        hold->feedback_ready = false;
        current_controller_reset(&hold->controller);
        if (hold->speed_mode) { speed_controller_reset(&hold->speed_controller); }
        electrical_angle_reset(&hold->measurement_angle);
        electrical_angle_reset(&hold->actuation_angle);
    }
    return false;
}

bool current_hold_speed_init(CurrentHold *hold, const RotorSpeedConfig *config)
{
    RotorCurrentConfig rotor = {ROTOR_SPEED_MAX_IQ_A, 0.0f};
    const SpeedControllerConfig speed = {0.001f, 0.004f, 20.0f,
        ROTOR_SPEED_MAX_IQ_A, ROTOR_SPEED_MAX_IQ_A, ROTOR_SPEED_MAX_RPM, 0.00125f};
    const PositionEstimatorConfig velocity = {ROTOR_CURRENT_MAX_RPM, 0.005f};
    if (hold == NULL) { return false; }
    memset(hold, 0, sizeof(*hold));
    if (config == NULL || !isfinite(config->target_velocity_rpm)
        || fabsf(config->target_velocity_rpm) < 10.0f || fabsf(config->target_velocity_rpm) > ROTOR_SPEED_MAX_RPM) {
        return false;
    }
    rotor.electrical_offset_turns = config->electrical_offset_turns;
    if (!current_hold_rotor_init(hold, &rotor) || !speed_controller_init(&hold->speed_controller, &speed)
        || !position_estimator_init(&hold->velocity, &velocity)) {
        hold->configured = false;
        return false;
    }
    hold->speed_mode = true;
    hold->target_velocity_rpm = config->target_velocity_rpm;
    return true;
}

bool current_hold_rotor_feedback(CurrentHold *hold, const PositionObservation *position,
    ControlTime sample_time)
{
    float age;
    if (hold == NULL || !hold->configured || !hold->rotor || position == NULL
        || !position->acquisition_valid || !position->referenced
        || !isfinite(position->mechanical_position_turns)
        || position->mechanical_position_turns < 0.0f || position->mechanical_position_turns >= 1.0f
        || position->observation_time > sample_time || sample_time - position->observation_time > 30U) {
        return reject_rotor_feedback(hold);
    }
    if (!hold->velocity.has_position) {
        (void)position_estimator_update(&hold->velocity, position->mechanical_position_turns, 0.001f);
        hold->rotor_position = position->mechanical_position_turns;
        hold->rotor_position_time = position->observation_time;
        return true;
    }
    if (position->observation_time < hold->rotor_position_time
        || (position->observation_time == hold->rotor_position_time
            && position->mechanical_position_turns != hold->rotor_position)) {
        return reject_rotor_feedback(hold);
    }
    if (position->observation_time != hold->rotor_position_time) {
        float delta = position->mechanical_position_turns - hold->rotor_position;
        float dt = (float)(position->observation_time - hold->rotor_position_time) / 20000.0f;
        if (delta > 0.5f) { delta -= 1.0f; }
        if (delta < -0.5f) { delta += 1.0f; }
        if (!position_estimator_update(&hold->velocity, position->mechanical_position_turns, dt)) {
            hold->motion_limit = true;
            return reject_rotor_feedback(hold);
        }
        hold->rotor_displacement += delta;
        hold->rotor_position = position->mechanical_position_turns;
        hold->rotor_position_time = position->observation_time;
        if (fabsf(hold->rotor_displacement) >= ROTOR_CURRENT_MAX_DISPLACEMENT) {
            hold->motion_limit = true;
            return reject_rotor_feedback(hold);
        }
    }
    hold->feedback_ready = hold->velocity.velocity_valid;
    if (!hold->feedback_ready) { return true; }
    age = (float)(sample_time - position->observation_time) / 20000.0f;
    hold->rotor_max_age_s = fmaxf(hold->rotor_max_age_s, age);
    if (!electrical_angle_update(&hold->measurement_angle, hold->rotor_position,
            hold->velocity.velocity_rpm, age)
        || !electrical_angle_update(&hold->actuation_angle, hold->rotor_position,
            hold->velocity.velocity_rpm, age + 0.000125f)) {
        return reject_rotor_feedback(hold);
    }
    return true;
}

bool current_hold_step(CurrentHold *hold, ControlTime elapsed,
    float alpha_a, float beta_a, float bus_v, CurrentControllerOutput *output)
{
    CurrentControllerInput input = {0};
    float scale = 1.0f;
    float phase = 0.0f;
    ControlTime end;

    if (output == NULL) {
        return false;
    }
    memset(output, 0, sizeof(*output));
    if (hold == NULL || !hold->configured || hold->complete) {
        return false;
    }
    if (elapsed <= hold->last_elapsed || elapsed - hold->last_elapsed != 2U) {
        hold->configured = false;
        current_controller_reset(&hold->controller);
        return false;
    }
    hold->last_elapsed = elapsed;
    end = hold->speed_mode ? 30000U : (hold->rotor ? 20000U : (hold->survey ? 100000U : 60000U));
    if (elapsed >= end) {
        hold->complete = true;
        current_controller_reset(&hold->controller);
        if (hold->speed_mode) { speed_controller_reset(&hold->speed_controller); }
        return false;
    }
    if (!isfinite(bus_v) || bus_v < 10.0f || bus_v > 14.0f) {
        hold->configured = false;
        current_controller_reset(&hold->controller);
        return false;
    }
    if (elapsed < (hold->rotor ? 4000U : 10000U)) {
        scale = (float)elapsed / (hold->rotor ? 4000.0f : 10000.0f);
    } else if (elapsed >= end - (hold->rotor ? 4000U : 10000U)) {
        scale = (float)(end - elapsed) / (hold->rotor ? 4000.0f : 10000.0f);
    }
    input.current_alpha_a = alpha_a;
    input.current_beta_a = beta_a;
    hold->stator_sine = 0.0f;
    hold->stator_cosine = 1.0f;
    if (hold->survey) {
        if (elapsed >= 30000U && elapsed < 40000U) {
            phase = (float)(elapsed - 30000U) / 10000.0f;
        } else if (elapsed >= 60000U && elapsed < 70000U) {
            phase = (float)(70000U - elapsed) / 10000.0f;
        }
        /* 其余保持角为0或1整圈，统一为周期零点。选定的是施加磁场坐标，
         * 不是转子DQ；目标d为励磁电流，q=0，无需先安装转子电角标定。 */
        if (phase >= 1.0f) { phase -= 1.0f; }
        if (phase != 0.0f && !electrical_angle_sincos_software(NULL, phase, &hold->stator_sine,
            &hold->stator_cosine)) {
            hold->configured = false;
            current_controller_reset(&hold->controller);
            return false;
        }
    }
    input.sin_electrical_angle = hold->stator_sine;
    input.cos_electrical_angle = hold->stator_cosine;
    if (hold->rotor) {
        if (!hold->feedback_ready || !hold->measurement_angle.output.valid || !hold->actuation_angle.output.valid) {
            hold->configured = false;
            current_controller_reset(&hold->controller);
            return false;
        }
        input.sin_electrical_angle = hold->measurement_angle.output.sin_theta;
        input.cos_electrical_angle = hold->measurement_angle.output.cos_theta;
        input.separate_actuation_angle = true;
        input.actuation_sin_theta = hold->actuation_angle.output.sin_theta;
        input.actuation_cos_theta = hold->actuation_angle.output.cos_theta;
        hold->stator_sine = input.actuation_sin_theta;
        hold->stator_cosine = input.actuation_cos_theta;
    }
    input.bus_voltage_v = bus_v;
    input.id_reference_a = scale * hold->config.alpha_reference_a;
    input.iq_reference_a = scale * hold->config.beta_reference_a;
    if (hold->speed_mode) {
        if (hold->last_speed_elapsed == 0U || elapsed - hold->last_speed_elapsed >= 20U) {
            SpeedControllerInput speed_input = {0};
            SpeedControllerOutput speed_output;
            float reference_scale = elapsed < 6000U ? (float)elapsed / 6000.0f
                : (elapsed >= 24000U ? (float)(30000U - elapsed) / 6000.0f : 1.0f);
            if (hold->last_speed_elapsed != 0U && elapsed - hold->last_speed_elapsed != 20U) {
                return reject_rotor_feedback(hold);
            }
            speed_input.target_velocity_rpm = hold->target_velocity_rpm * reference_scale;
            speed_input.measured_velocity_rpm = hold->velocity.velocity_rpm;
            speed_input.interval_s = 0.001f;
            speed_input.velocity_valid = hold->feedback_ready;
            speed_input.reference_valid = true;
            if (!speed_controller_update(&hold->speed_controller, &speed_input, &speed_output)) {
                return reject_rotor_feedback(hold);
            }
            hold->active_iq_reference_a = speed_output.iq_reference_a;
            hold->last_speed_elapsed = elapsed;
            hold->speed_update_count++;
            hold->speed_current_limited_updates += speed_output.current_limited ? 1U : 0U;
        }
        input.id_reference_a = 0.0f;
        input.iq_reference_a = hold->active_iq_reference_a;
    }
    input.interval_s = 0.0001f;
    input.current_valid = true;
    input.angle_valid = true;
    input.bus_voltage_valid = true;
    input.reference_valid = true;
    if (!current_controller_update(&hold->controller, &input, output)) {
        hold->configured = false;
        return false;
    }
    if (hold->speed_mode && output->integral_limited) { hold->current_integral_limited_samples++; }
    return true;
}
