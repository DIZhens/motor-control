#ifndef G431_CURRENT_HOLD_H
#define G431_CURRENT_HOLD_H

#include <stdbool.h>
#include <stdint.h>
#include "current_controller.h"
#include "realtime_motor_io.h"

#define CURRENT_HOLD_DURATION_US 3500000U

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
    uint32_t window_samples; /* 2.0～2.5 s保持窗口的连续10 kHz样本。 */
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
} CurrentHold;

bool current_hold_init(CurrentHold *hold, const CurrentHoldConfig *config);

/* elapsed为实际enable之后的ControlTime，固定20 kHz单位、相邻调用2 tick。
 * 上层先验证原始采样/母线/时序和零点；本函数再次限数值范围并执行有界PI。
 * 正常轨迹0.5 s升流、2 s保持、0.5 s降流；complete或错误均无可用duty。 */
bool current_hold_step(CurrentHold *hold, ControlTime elapsed,
    float alpha_a, float beta_a, float bus_v, CurrentControllerOutput *output);

#endif
