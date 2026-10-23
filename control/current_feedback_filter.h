#ifndef CURRENT_FEEDBACK_FILTER_H
#define CURRENT_FEEDBACK_FILTER_H

#include <stdbool.h>
#include <stddef.h>
#include <math.h>

/* 两轴同系数的一阶低通；仅处理数值，不持有采样、保护或 Motor 生命周期。
 * 系数 0 显式旁路，1 同样逐位直通；其余范围为 (0,1)。
 * 指定离散截止频率时，d=1-cos(2*pi*fc/fs)，a=sqrt(d*(d+2))-d。
 * fs 由调用方固定，不在逐拍路径计算三角函数或重新估计采样间隔。 */
typedef struct {
    float coefficient; /* 无量纲，init 后固定；不是截止频率或时间常数。 */
    float alpha_a; /* 最近一次成功输出的 alpha 轴数值。 */
    float beta_a; /* 最近一次成功输出的 beta 轴数值。 */
    bool configured; /* 系数合法且已初始化。 */
    bool initialized; /* 已接收首个有效输入；不代表输入采样新鲜或功率许可。 */
} CurrentFeedbackFilter;

/* 清除历史和输出，保留配置；运行生命周期由调用方安排。 */
static inline void current_feedback_filter_reset(CurrentFeedbackFilter *filter)
{
    if (filter != NULL) {
        filter->alpha_a = 0.0f;
        filter->beta_a = 0.0f;
        filter->initialized = false;
    }
}

static inline bool current_feedback_filter_init(CurrentFeedbackFilter *filter, float coefficient)
{
    if (filter == NULL) { return false; }
    filter->coefficient = coefficient;
    filter->configured = isfinite(coefficient) && coefficient >= 0.0f && coefficient <= 1.0f;
    current_feedback_filter_reset(filter);
    return filter->configured;
}

/* 首个有效输入直接初始化，避免从虚假零电流缓升。失败清历史，不保持旧输出。
 * 输入有效性、新鲜度和原始过流必须在调用前检查；本函数不是保护滤波器。 */
static inline bool current_feedback_filter_update(CurrentFeedbackFilter *filter,
                                                  float alpha_a, float beta_a)
{
    float next_alpha, next_beta, coefficient;
    if (filter == NULL) { return false; }
    coefficient = filter->coefficient;
    if (!filter->configured || !isfinite(coefficient)
        || coefficient < 0.0f || coefficient > 1.0f
        || !isfinite(alpha_a) || !isfinite(beta_a)) {
        current_feedback_filter_reset(filter);
        return false;
    }
    if (!filter->initialized || coefficient == 0.0f || coefficient == 1.0f) {
        next_alpha = alpha_a;
        next_beta = beta_a;
    } else {
        /* 分别加权，避免先求相反符号的大数之差而溢出。 */
        next_alpha = (1.0f - coefficient) * filter->alpha_a + coefficient * alpha_a;
        next_beta = (1.0f - coefficient) * filter->beta_a + coefficient * beta_a;
    }
    if (!isfinite(next_alpha) || !isfinite(next_beta)) {
        current_feedback_filter_reset(filter);
        return false;
    }
    filter->alpha_a = next_alpha;
    filter->beta_a = next_beta;
    filter->initialized = true;
    return true;
}

#endif
