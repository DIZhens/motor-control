#ifndef ELECTRICAL_ANGLE_H
#define ELECTRICAL_ANGLE_H

#include <stdbool.h>
#include <stdint.h>

/* 纯数值映射参数；配置合法不代表极对数、相序和零位已得到实物标定。 */
typedef struct {
    uint32_t pole_pairs; /* 极对数，范围 1..64；上限用于约束单精度映射误差。 */
    int32_t encoder_direction; /* 编码器坐标到电角度正向的符号，仅允许 +1 或 -1。 */
    float electrical_offset_turns; /* 电角度零位偏置 [turn]，必须位于 [0, 1)。 */
    float max_prediction_time_s; /* 允许的观测年龄 [s]；有限非负，零表示只接受同拍观测。 */
    float max_velocity_rpm; /* 编码器坐标机械速度绝对值上界 [rpm]；有限且大于零。 */
} ElectricalAngleConfig;

typedef struct {
    float electrical_turns; /* 当前预测电角度 [turn]，范围 [0, 1)，仅 valid 时可使用。 */
    float sin_theta; /* sin(2*pi*electrical_turns)，与 Park 变换角度约定一致。 */
    float cos_theta; /* cos(2*pi*electrical_turns)。 */
    bool valid; /* false 时三个数值均为零，不能把无效零位当作对齐结果。 */
} ElectricalAngleOutput;

/* 独立实例由调用方串行使用；init 后外部不修改 config 与 configured。 */
typedef struct {
    ElectricalAngleConfig config; /* 初始化时复制的数值配置。 */
    bool configured; /* 仅表示配置通过数值检查，不表示标定或功率使能许可。 */
    ElectricalAngleOutput output; /* 最近一次映射结果；每次失败都会清零。 */
} ElectricalAngle;

/*
 * 非零 max_prediction_time_s 必须严格小于 30/max_velocity_rpm，
 * 将配置覆盖范围内的机械预测位移限制在半圈以内；这不是速度或标定可信度证明。
 * 非法配置清空配置与输出；允许合法重配，config 可指向实例内部原配置。
 */
bool electrical_angle_init(ElectricalAngle *angle, const ElectricalAngleConfig *config);

/* 仅清空输出，保留初始化得到的配置；不累计机械圈数或预测历史。 */
void electrical_angle_reset(ElectricalAngle *angle);

/*
 * position_turns 是编码器原坐标的周期机械位置 [0, 1)，velocity_rpm 使用同一坐标。
 * observation_age_s 是该位置采集到预测目标时刻的时间差，范围 [0, 配置上限]。
 * theta_e=wrap(pole_pairs*encoder_direction*(position+velocity/60*age)-offset)。
 * 先在机械一圈内归一化，再映射电角度，避免任意大圈数参与单精度取模。
 * 函数只假设短时间匀速；速度、采样新鲜度、实际标定可信与控制授权由调用方门控。
 * 输入非法、速度越界或任何数值异常均清空输出；返回值与 output.valid 一致。
 */
bool electrical_angle_update(ElectricalAngle *angle, float position_turns,
                              float velocity_rpm, float observation_age_s);

#endif
