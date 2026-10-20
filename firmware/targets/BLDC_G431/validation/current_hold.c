#include "current_hold.h"

#if defined(__CC_ARM)
#pragma Otime
#endif

#include <math.h>
#include <stddef.h>
#include <string.h>

bool current_hold_init(CurrentHold *hold, const CurrentHoldConfig *config)
{
    /* 保守低带宽试验参数，不由尚未辨识的电机L反推整定。
     * 允许母线上限14 V，因此ratio固定0.4/14，整个范围内请求电压不超过0.4 V。 */
    const CurrentControllerConfig controller = {
        0.5f, 0.5f, 10.0f, 10.0f, 50.0f, 0.3f, 0.08f, 0.4f / 14.0f, 0.000125f
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
    if (!isfinite(norm) || norm < 0.0001f || norm > 0.08f * 0.08f
        || !current_controller_init(&hold->controller, &controller)) {
        return false;
    }
    hold->config = requested;
    hold->configured = true;
    return true;
}

bool current_hold_step(CurrentHold *hold, ControlTime elapsed,
    float alpha_a, float beta_a, float bus_v, CurrentControllerOutput *output)
{
    CurrentControllerInput input = {0};
    float scale = 1.0f;

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
    if (elapsed >= 60000U) {
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
    } else if (elapsed >= 50000U) {
        scale = (float)(60000U - elapsed) / 10000.0f;
    }
    input.current_alpha_a = alpha_a;
    input.current_beta_a = beta_a;
    /* 显式恒等坐标系：d=α、q=β；没有冒充已标定的转子电角度。 */
    input.cos_electrical_angle = 1.0f;
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
