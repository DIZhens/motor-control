#ifndef CURRENT_CONTROLLER_H
#define CURRENT_CONTROLLER_H

#include <stdbool.h>

/* 参数由调用方显式提供；本模块不推断电机参数，也不决定是否允许加电。 */
typedef struct {
    float kp_d_v_per_a; /* d 轴比例增益 [V/A]，有限且非负。 */
    float kp_q_v_per_a; /* q 轴比例增益 [V/A]，有限且非负。 */
    float ki_d_v_per_a_s; /* d 轴积分增益 [V/(A*s)]，有限且非负。 */
    float ki_q_v_per_a_s; /* q 轴积分增益 [V/(A*s)]，有限且非负。 */
    float tracking_gain_per_s; /* 限幅电压跟踪增益 [1/s]，有限且非负。 */
    float integral_limit_v; /* 各轴积分电压的绝对值上限 [V]，有限且大于零。 */
    float reference_current_limit_a; /* 完整 Id/Iq 目标矢量的模长上限 [A]。 */
    float max_voltage_ratio; /* 电压矢量上限 / VBUS，范围 (0, 1/sqrt(3)]。 */
    float max_interval_s; /* 允许的最大单拍时间间隔 [s]，有限且大于零。 */
} CurrentControllerConfig;

typedef struct {
    float current_alpha_a; /* 同一拍静止 α 轴电流 [A]。 */
    float current_beta_a; /* 同一拍静止 β 轴电流 [A]。 */
    float sin_electrical_angle; /* 所选控制坐标系的正弦；转子DQ控制须已标定并对齐采样时刻。 */
    float cos_electrical_angle; /* 与正弦属于同一份电角度。 */
    float bus_voltage_v; /* 有效物理母线电压 [V]。 */
    float id_reference_a; /* 完整目标矢量的 d 轴电流 [A]。 */
    float iq_reference_a; /* 完整目标矢量的 q 轴电流 [A]。 */
    float interval_s; /* 本拍实际时间间隔；采样/角度是否新鲜由上层负责判定。 */
    bool current_valid; /* 电流已校正且采样新鲜、完整。 */
    bool angle_valid; /* 调用方确认坐标系有效；转子DQ须确认标定/新鲜度，静止αβ试验可显式用恒等变换。 */
    bool bus_voltage_valid; /* 母线采集与新鲜度由调用方确认。 */
    bool reference_valid; /* 两轴目标必须作为同一拍的完整矢量提交。 */
    bool separate_actuation_angle; /* false 复用测量角并忽略下列值；true 使用独立施加电压角。 */
    float actuation_sin_theta; /* 对齐目标电压施加时刻的正弦；时序预测由调用方负责。 */
    float actuation_cos_theta; /* 与 actuation_sin_theta 属于同一份施加角。 */
} CurrentControllerInput;

typedef struct {
    float id_a; /* 本拍旋转 d 轴电流 [A]。 */
    float iq_a; /* 本拍旋转 q 轴电流 [A]。 */
    float requested_vd_v; /* 圆限幅前的 d 轴 PI 请求 [V]。 */
    float requested_vq_v; /* 圆限幅前的 q 轴 PI 请求 [V]。 */
    float applied_vd_v; /* 调制器实际采用的电压按施加角换回 d 轴后的值 [V]。 */
    float applied_vq_v; /* 调制器实际采用的电压换回 q 轴后的值 [V]。 */
    float duty_a; /* A 相归一化平均 duty，[0, 1]。 */
    float duty_b; /* B 相归一化平均 duty，[0, 1]。 */
    float duty_c; /* C 相归一化平均 duty，[0, 1]。 */
    bool voltage_limited; /* 本拍电压请求经过二维圆限幅。 */
    bool integral_limited; /* 本拍至少一轴积分候选达到配置上限。 */
    bool d_integral_limited; /* 仅复制现有d轴限幅判据，不改变计算。 */
    bool q_integral_limited; /* 仅复制现有q轴限幅判据，不改变计算。 */
    bool valid; /* 调用方只允许使用有效结果；不代表功率许可。 */
} CurrentControllerOutput;

/* 一个实例对应一套独立积分历史；调用方串行更新，不直接改写内部字段。 */
typedef struct {
    CurrentControllerConfig config; /* 初始化后固定的算法配置。 */
    float integral_d_v; /* 下一拍使用的 d 轴积分电压 [V]。 */
    float integral_q_v; /* 下一拍使用的 q 轴积分电压 [V]。 */
    bool configured; /* 已通过数值配置检查；不保证控制参数稳定性。 */
} CurrentController;

/* 非法配置使实例未配置并清空历史；支持用实例内原配置重新初始化。 */
bool current_controller_init(CurrentController *controller,
                             const CurrentControllerConfig *config);

/* 仅清空算法历史，保留配置。它不清除电机故障，也不操作任何功率输出。 */
void current_controller_reset(CurrentController *controller);

/*
 * 使用 u = Kp * (reference - measured) + integral 计算本拍请求电压。
 * 测量角先按 foc_math 的单位圆容差验证并在本模块内归一化，用于实测电流 Park。
 * separate_actuation_angle=false 时沿用测量角并忽略附加值；否则单独校验和归一化
 * 施加角，用于电压逆 Park 及调制结果回 DQ，两次电压变换复用同一正交相量。
 * 因此预测角与测量角的差异及容差内模长误差不会变成虚假回算积分。
 * 电压圆限幅与 SVPWM 后，用实际施加 DQ 电压作积分回算：
 * I_next = I + dt * (Ki * error + tracking_gain * (applied - requested))。
 * 最后分别限制积分电压；两轴候选均合法后才一次提交，不允许半拍更新。
 * 超出目标电流矢量上限直接拒绝；本模块不会静默修改目标。
 * 输入无效、dt 超时或数值无法表示时清空积分，返回 false；输出 valid=false，
 * 电压/电流置零，三个占空比为 0.5。中性占空比不等于关断，调用方必须检查 valid。
 */
bool current_controller_update(CurrentController *controller,
                               const CurrentControllerInput *input,
                               CurrentControllerOutput *output);

#endif
