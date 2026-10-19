#ifndef POSITION_ESTIMATOR_H
#define POSITION_ESTIMATOR_H

#include <stdbool.h>

/* 周期位置的排歧与滤波参数；不表示电机允许运行的速度范围。 */
typedef struct {
    float max_velocity_rpm; /* 机械速度绝对值上界，必须有限且大于零。 */
    float velocity_filter_time_s; /* 一阶低通时间常数 [s]；有限且非负，零表示不滤波。 */
} PositionEstimatorConfig;

/* 由调用方串行更新的独立实例；外部只读输出，不直接修改历史字段。 */
typedef struct {
    float velocity_rpm; /* 机械速度 [rpm]；仅 velocity_valid 时可使用。 */
    bool velocity_valid;
    PositionEstimatorConfig config;
    float previous_position_turns; /* 上个合法周期位置 [turn]，范围 [0, 1)。 */
    bool has_position;
    bool configured;
} PositionEstimator;

/* 非法配置使实例保持未配置且输出无效；允许用合法配置重新初始化。 */
bool position_estimator_init(PositionEstimator *estimator, const PositionEstimatorConfig *config);

/* 清空位置与滤波历史、输出置零且无效；保留 init 得到的配置。 */
void position_estimator_reset(PositionEstimator *estimator);

/*
 * interval_s 是相邻两次传入位置的采集时间差，不是调用或发布间隔。
 * 每次都要求位置有限且位于 [0, 1)，interval_s 有限且大于零；首样本也如此。
 * 首个合法位置只建基线并返回 false；之后按最近周期差估速，返回速度是否有效。
 * interval_s 必须严格小于 30 / max_velocity_rpm，避免速度上界内仍有半圈混叠。
 * 恰好半圈、估计速度越界或任何输入失效均清空历史；恢复需两个连续合法位置。
 * 无效输出始终为零，不能把零值当作静止证据；单圈位置无法排除实际运动超出配置。
 */
bool position_estimator_update(PositionEstimator *estimator, float position_turns,
                               float interval_s);

#endif
