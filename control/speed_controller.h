#ifndef SPEED_CONTROLLER_H
#define SPEED_CONTROLLER_H

#include <stdbool.h>

/* 调用方显式提供速度环参数；数值合法不代表实际电机闭环稳定。 */
typedef struct {
    float kp_a_per_rpm; /* 比例增益 [A/rpm]，有限且非负。 */
    float ki_a_per_rpm_s; /* 积分增益 [A/(rpm*s)]，有限且非负。 */
    float tracking_gain_per_s; /* 限幅电流跟踪增益 [1/s]，有限且非负。 */
    float max_iq_a; /* 输出 Iq 的绝对值上限 [A]，有限且大于零。 */
    float integral_limit_a; /* 积分电流的绝对值上限 [A]，有限且大于零。 */
    float max_target_velocity_rpm; /* 允许的目标机械转速绝对值上限 [rpm]。 */
    float max_interval_s; /* 允许的最大单拍间隔 [s]，有限且大于零。 */
} SpeedControllerConfig;

typedef struct {
    float target_velocity_rpm; /* 本拍完整目标机械转速 [rpm]。 */
    float measured_velocity_rpm; /* 与目标使用相同正方向的实测机械转速 [rpm]。 */
    float interval_s; /* 速度外环本拍实际时间间隔 [s]。 */
    bool velocity_valid; /* 反馈有效性与新鲜度由调用方确认。 */
    bool reference_valid; /* 目标有效性与模式许可由调用方确认。 */
} SpeedControllerInput;

typedef struct {
    float requested_iq_a; /* 限幅前的 PI 电流请求 [A]。 */
    float iq_reference_a; /* 限制在正负 max_iq_a 内的 Iq 目标 [A]。 */
    bool current_limited; /* 本拍 Iq 请求经过输出限幅。 */
    bool integral_limited; /* 下一拍积分候选经过独立的积分限幅。 */
    bool valid; /* 有效数值结果；不代表角度有效、功率许可或实际电流已跟踪。 */
} SpeedControllerOutput;

/* 每个实例独立保存积分；调用方串行更新，不直接改写内部字段。 */
typedef struct {
    SpeedControllerConfig config;
    float integral_a; /* 下一拍使用的积分电流 [A]。 */
    bool configured; /* 仅表示配置通过数值检查。 */
} SpeedController;

/* 非法配置清空实例；允许通过实例内的原配置重新初始化。 */
bool speed_controller_init(SpeedController *controller, const SpeedControllerConfig *config);

/* 清除积分历史，保留配置；不改变电机状态，也不操作功率输出。 */
void speed_controller_reset(SpeedController *controller);

/*
 * 本拍先用 u = Kp * (target - measured) + I 计算电流请求，再限幅得到 Iq。
 * 下拍积分为 I_next = I + dt * (Ki * error + tracking_gain * (Iq - u))，
 * 最后施加积分上限；全部数值候选合法后才提交结果和历史。
 * 越界目标、无效反馈、非法 dt 或数值溢出清历史并返回 false，输出置零且无效。
 * 本模块只给出 Iq 目标；Id、角度、状态门控与内环跟踪情况由上层负责。
 */
bool speed_controller_update(SpeedController *controller, const SpeedControllerInput *input,
                             SpeedControllerOutput *output);

#endif
