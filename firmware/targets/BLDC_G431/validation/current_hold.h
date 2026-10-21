#ifndef G431_CURRENT_HOLD_H
#define G431_CURRENT_HOLD_H

#include <stdbool.h>
#include <stdint.h>
#include "current_controller.h"
#include "realtime_motor_io.h"
#include "position_observation.h"
#include "position_estimator.h"
#include "electrical_angle.h"
#include "speed_controller.h"

#define CURRENT_HOLD_DURATION_US 3500000U
#define CURRENT_SURVEY_DURATION_US 5500000U
/* 命令18的定向包络；命令17仍沿用原低幅值限制。 */
#define CURRENT_SURVEY_MAX_CURRENT_A 0.5f
#define CURRENT_SURVEY_MAX_VOLTAGE_V 1.8f
#define CURRENT_SURVEY_NOMINAL_TRIP_A 0.65f
#define CURRENT_SURVEY_INTEGRAL_LIMIT_V 1.5f
#define ROTOR_CURRENT_DURATION_US 1500000U
#define ROTOR_CURRENT_MAX_A 0.2f
#define ROTOR_CURRENT_MAX_RPM 300.0f
#define ROTOR_CURRENT_MAX_DISPLACEMENT 2.0f
#define ROTOR_SPEED_DURATION_US 2000000U
#define ROTOR_SPEED_MAX_RPM 80.0f
#define ROTOR_SPEED_MAX_IQ_A 0.1f

typedef struct {
    float target_velocity_rpm; /* 命令20：正负10～80rpm，1.5秒有限轨迹。 */
    float electrical_offset_turns;
} RotorSpeedConfig;

typedef struct {
    uint32_t sequence, request_sequence, active, result;
    uint32_t sample_count, enabled_samples, window_samples, voltage_limited_samples;
    float target_velocity_rpm, electrical_offset_turns, mean_id_a, mean_iq_a;
    float peak_current_tracking_error_a, max_voltage_v, peak_abs_current_a, min_duty, max_duty;
    float start_position_turns, end_position_turns, displacement_turns;
    float max_abs_velocity_rpm, last_velocity_rpm, mean_velocity_rpm, max_position_age_s;
    float mean_iq_reference_a, mean_speed_error_rpm, peak_speed_error_rpm, last_iq_reference_a;
    uint32_t speed_update_count, speed_current_limited_updates, speed_window_updates, current_integral_limited_samples;
} RotorSpeedSnapshot;

/* 命令19仅接收已审查的7极对、正方向候选；不修改正式Motor配置。 */
typedef struct {
    float iq_reference_a; /* Id固定0；允许正负0.01～0.2名义A。 */
    float electrical_offset_turns;
} RotorCurrentConfig;

typedef struct {
    uint32_t sequence, request_sequence, active, result;
    uint32_t sample_count, enabled_samples, window_samples, voltage_limited_samples;
    float iq_reference_a, electrical_offset_turns, mean_id_a, mean_iq_a;
    float peak_tracking_error_a, max_voltage_v, peak_abs_current_a, min_duty, max_duty;
    float start_position_turns, end_position_turns, displacement_turns;
    float max_abs_velocity_rpm, last_velocity_rpm, mean_velocity_rpm, max_position_age_s;
} RotorCurrentSnapshot;

/* 仅验证入口的完整αβ目标；不安装电机标定，也不授予正式Motor RUN。 */
typedef struct {
    float alpha_reference_a; /* 名义A，矢量模长最多0.08 A，非零。 */
    float beta_reference_a;
} CurrentHoldConfig;

/* ADC单写者；开始撤销旧结果，结束后发布同一偶数版本。 */
typedef struct {
    uint32_t sequence;
    uint32_t request_sequence;
    uint32_t active;
    uint32_t result; /* 沿用TargetValidationResult；完成不表示精度或转子FOC已验收。 */
    uint32_t sample_count;
    uint32_t enabled_samples;
    uint32_t window_samples; /* 命令17最后0.5秒保持共5000点；命令18三份0.25秒保持共7500点。 */
    uint32_t voltage_limited_samples;
    float alpha_reference_a;
    float beta_reference_a;
    float offset_b_a; /* 本启动真实4096点关断窗口的冻结均值，校正前名义A。 */
    float offset_c_a;
    float mean_alpha_a; /* 已扣零并修正方向，幅值仍为名义A。 */
    float mean_beta_a;
    float peak_abs_current_a;
    float max_voltage_v; /* 实际调制请求的αβ矢量模长，不是实测端电压。 */
    float min_duty;
    float max_duty;
    float mean_voltage_alpha_v;
    float mean_voltage_beta_v;
    float peak_tracking_error_a; /* 仅保持窗口的矢量误差峰值，名义A。 */
} CurrentHoldSnapshot;

/* 诊断专用有限计算器；不持有硬件、接线确认或业务生命周期。 */
typedef struct {
    CurrentController controller;
    CurrentHoldConfig config;
    ControlTime last_elapsed; /* 拒绝重复、回退或漏拍，PI不以假定dt掩盖时序失效。 */
    bool configured;
    bool complete;
    bool survey; /* 完整一电角圈往返；不使用转子角标定。 */
    float stator_sine;
    float stator_cosine;
    bool rotor;
    bool feedback_ready;
    bool motion_limit;
    PositionEstimator velocity;
    ElectricalAngle measurement_angle;
    ElectricalAngle actuation_angle;
    ControlTime rotor_position_time;
    float rotor_position;
    float rotor_displacement;
    float rotor_max_age_s;
    bool speed_mode;
    SpeedController speed_controller;
    float target_velocity_rpm;
    float active_iq_reference_a;
    ControlTime last_speed_elapsed;
    uint32_t speed_update_count;
    uint32_t speed_current_limited_updates;
    uint32_t current_integral_limited_samples;
} CurrentHold;

bool current_hold_init(CurrentHold *hold, const CurrentHoldConfig *config);

/* 只供命令18：0.01～0.5名义A，完整一电角圈，五秒有限轨迹。 */
bool current_hold_survey_init(CurrentHold *hold, float current_reference_a);

bool current_hold_rotor_init(CurrentHold *hold, const RotorCurrentConfig *config);
bool current_hold_speed_init(CurrentHold *hold, const RotorSpeedConfig *config);
/* 同一ADC单写者消费SSI；两份新观测建立速度后才允许上层stage/enable。
 * 测量角预测到sample，施加角显式预测至下一保持区间中点（sample后125us）。 */
bool current_hold_rotor_feedback(CurrentHold *hold, const PositionObservation *position,
    ControlTime sample_time);

/* elapsed为实际enable之后的ControlTime，固定20 kHz单位、相邻调用2 tick。
 * 上层先验证原始采样/母线/时序和零点；本函数再次限数值范围并执行有界PI。
 * 命令17为0.5 s升流、2 s保持、0.5 s降流；命令18为五秒往返；
 * complete或错误均无可用duty。 */
bool current_hold_step(CurrentHold *hold, ControlTime elapsed,
    float alpha_a, float beta_a, float bus_v, CurrentControllerOutput *output);

#endif
