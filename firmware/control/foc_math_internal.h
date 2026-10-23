#ifndef FOC_MATH_INTERNAL_H
#define FOC_MATH_INTERNAL_H

/* 算法实现私有：公共入口与电流控制器共用旋转公式，不提供对外的免校验接口。 */
#include "foc_math.h"

#include <math.h>
#include <stddef.h>

#if defined(__CC_ARM)
#define FOC_MATH_INTERNAL_INLINE static __forceinline
#else
#define FOC_MATH_INTERNAL_INLINE static inline
#endif

FOC_MATH_INTERNAL_INLINE bool foc_math_phasor_valid(float sine, float cosine)
{
    float norm;
    if (!isfinite(sine) || !isfinite(cosine) || fabsf(sine) > 1.001f ||
        fabsf(cosine) > 1.001f) {
        return false;
    }
    norm = sine * sine + cosine * cosine;
    return isfinite(norm) && fabsf(norm - 1.0f) <= 0.001f;
}

/*
 * check_phasor=false仅用于本次调用内已经验证并归一化的局部相量；不能把外部
 * angle_valid或历史configured标志当作该条件。向量输入及旋转结果仍逐次拒绝非有限值。
 * 公共Park入口固定传true，保留其原有单位圆容差、清输出和失败语义。
 */
FOC_MATH_INTERNAL_INLINE bool foc_math_park_internal(float alpha, float beta,
    float sin_theta, float cos_theta, bool check_phasor, FocDq *out)
{
    float d, q;
    if (out == NULL) {
        return false;
    }
    out->d = 0.0f;
    out->q = 0.0f;
    out->valid = false;
    if (!isfinite(alpha) || !isfinite(beta) ||
        (check_phasor && !foc_math_phasor_valid(sin_theta, cos_theta))) {
        return false;
    }
    d = alpha * cos_theta + beta * sin_theta;
    q = -alpha * sin_theta + beta * cos_theta;
    if (!isfinite(d) || !isfinite(q)) {
        return false;
    }
    out->d = d;
    out->q = q;
    out->valid = true;
    return true;
}

FOC_MATH_INTERNAL_INLINE bool foc_math_inverse_park_internal(float d, float q,
    float sin_theta, float cos_theta, bool check_phasor, FocAlphaBeta *out)
{
    float alpha, beta;
    if (out == NULL) {
        return false;
    }
    out->alpha = 0.0f;
    out->beta = 0.0f;
    out->valid = false;
    if (!isfinite(d) || !isfinite(q) ||
        (check_phasor && !foc_math_phasor_valid(sin_theta, cos_theta))) {
        return false;
    }
    alpha = d * cos_theta - q * sin_theta;
    beta = d * sin_theta + q * cos_theta;
    if (!isfinite(alpha) || !isfinite(beta)) {
        return false;
    }
    out->alpha = alpha;
    out->beta = beta;
    out->valid = true;
    return true;
}

#undef FOC_MATH_INTERNAL_INLINE

#endif
