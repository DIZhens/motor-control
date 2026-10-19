#include "foc_math.h"
#include "foc_math_internal.h"

#include <math.h>
#include <stddef.h>

#define FOC_INV_SQRT_THREE 0.5773502691896258f
#define FOC_SQRT_THREE_OVER_TWO 0.8660254037844386f
#define FOC_DUTY_ROUNDING_TOLERANCE 0.000001f

bool foc_park(float alpha, float beta, float sin_theta, float cos_theta, FocDq *out)
{
    return foc_math_park_internal(alpha, beta, sin_theta, cos_theta, true, out);
}

bool foc_inverse_park(float d, float q, float sin_theta, float cos_theta, FocAlphaBeta *out)
{
    return foc_math_inverse_park_internal(d, q, sin_theta, cos_theta, true, out);
}

static float clamp_rounding(float duty)
{
    if (duty < 0.0f) {
        return 0.0f;
    }
    if (duty > 1.0f) {
        return 1.0f;
    }
    return duty;
}

bool foc_svpwm(float alpha_v, float beta_v, float vbus_v, float max_voltage_ratio,
               FocModulation *out)
{
    float maximum, limit_v, normalized_alpha, normalized_beta, direction_norm, bound;
    float applied_alpha, applied_beta, phase_a, phase_b, phase_c, high, low, common;
    float duty_a, duty_b, duty_c;
    bool limited = false;
    if (out == NULL) {
        return false;
    }
    out->applied_alpha_v = 0.0f;
    out->applied_beta_v = 0.0f;
    out->duty_a = 0.0f;
    out->duty_b = 0.0f;
    out->duty_c = 0.0f;
    out->limited = false;
    out->valid = false;
    if (!isfinite(alpha_v) || !isfinite(beta_v) || !isfinite(vbus_v) || vbus_v <= 0.0f ||
        !isfinite(max_voltage_ratio) || max_voltage_ratio <= 0.0f ||
        max_voltage_ratio > FOC_INV_SQRT_THREE) {
        return false;
    }
    limit_v = max_voltage_ratio * vbus_v;
    if (!isfinite(limit_v) || limit_v <= 0.0f) {
        return false;
    }
    applied_alpha = alpha_v;
    applied_beta = beta_v;
    /* 入口已拒绝非有限值，显式比较不承担 fmaxf 的 NaN 选择语义。 */
    maximum = fabsf(alpha_v);
    if (fabsf(beta_v) > maximum) {
        maximum = fabsf(beta_v);
    }
    if (maximum > 0.0f) {
        /* 先缩放到最大分量，避免 alpha^2+beta^2 或矢量模本身溢出。 */
        normalized_alpha = alpha_v / maximum;
        normalized_beta = beta_v / maximum;
        direction_norm = sqrtf(normalized_alpha * normalized_alpha +
                               normalized_beta * normalized_beta);
        bound = limit_v / direction_norm;
        if (!isfinite(direction_norm) || !isfinite(bound) || bound <= 0.0f) {
            return false;
        }
        if (maximum > bound) {
            applied_alpha = normalized_alpha * bound;
            applied_beta = normalized_beta * bound;
            limited = true;
        }
    }
    /* 按母线归一化后做逆 Clarke，避免合法大电压的三相中间和溢出。 */
    normalized_alpha = applied_alpha / vbus_v;
    normalized_beta = applied_beta / vbus_v;
    phase_a = normalized_alpha;
    phase_b = -0.5f * normalized_alpha + FOC_SQRT_THREE_OVER_TWO * normalized_beta;
    phase_c = -0.5f * normalized_alpha - FOC_SQRT_THREE_OVER_TWO * normalized_beta;
    /* 归一化相电压有界；相等零值的选取不改变最终占空比。 */
    high = phase_a;
    low = phase_a;
    if (phase_b > high) {
        high = phase_b;
    }
    if (phase_c > high) {
        high = phase_c;
    }
    if (phase_b < low) {
        low = phase_b;
    }
    if (phase_c < low) {
        low = phase_c;
    }
    common = 0.5f * (high + low);
    duty_a = 0.5f + phase_a - common;
    duty_b = 0.5f + phase_b - common;
    duty_c = 0.5f + phase_c - common;
    if (!isfinite(applied_alpha) || !isfinite(applied_beta) || !isfinite(duty_a) ||
        !isfinite(duty_b) || !isfinite(duty_c) ||
        duty_a < -FOC_DUTY_ROUNDING_TOLERANCE || duty_a > 1.0f + FOC_DUTY_ROUNDING_TOLERANCE ||
        duty_b < -FOC_DUTY_ROUNDING_TOLERANCE || duty_b > 1.0f + FOC_DUTY_ROUNDING_TOLERANCE ||
        duty_c < -FOC_DUTY_ROUNDING_TOLERANCE || duty_c > 1.0f + FOC_DUTY_ROUNDING_TOLERANCE) {
        return false;
    }
    out->applied_alpha_v = applied_alpha;
    out->applied_beta_v = applied_beta;
    /* 仅消除边界浮点舍入；超出容差时整拍失败，不逐相限幅改变空间矢量。 */
    out->duty_a = clamp_rounding(duty_a);
    out->duty_b = clamp_rounding(duty_b);
    out->duty_c = clamp_rounding(duty_c);
    out->limited = limited;
    out->valid = true;
    return true;
}
