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
    end = hold->survey ? 100000U : 60000U;
    if (elapsed >= end) {
        hold->complete = true;
        current_controller_reset(&hold->controller);
        return false;
    }
    if (!isfinite(bus_v) || bus_v < 10.0f || bus_v > 14.0f) {
        hold->configured = false;
        current_controller_reset(&hold->controller);
        return false;
    }
    if (elapsed < 10000U) {
        scale = (float)elapsed / 10000.0f;
    } else if (elapsed >= end - 10000U) {
        scale = (float)(end - elapsed) / 10000.0f;
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
    input.bus_voltage_v = bus_v;
    input.id_reference_a = scale * hold->config.alpha_reference_a;
    input.iq_reference_a = scale * hold->config.beta_reference_a;
    input.interval_s = 0.0001f;
    input.current_valid = true;
    input.angle_valid = true;
    input.bus_voltage_valid = true;
    input.reference_valid = true;
    if (!current_controller_update(&hold->controller, &input, output)) {
        hold->configured = false;
        return false;
    }
    return true;
}
