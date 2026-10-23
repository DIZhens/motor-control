#include "g431_control_profile.h"
#include "../platform/cordic_math.h"

/* Target 装配持有后端上下文，其寿命覆盖 ControlRuntime；不成为可写参数接口。 */
static G431CordicMath s_cordic_math;

/* 仅适用于原 G431、2804、MT6701 接线和固定空载包络。
 * 静态 1.236 V / 0.394 名义 A 约为 3.14 ohm，与独立绕组 Rs 约 3.1 ohm 相符；
 * 电流幅值仍未完成独立精度标定，不能由此扩大运行许可。 */
const ControlRuntimeConfig g431_control_config = {
    .control_time_ticks_per_second = 20000U,
    .fast_tick_period_ticks = 2U, /* 20 kHz PWM、10 kHz 电流环。 */
    .observation_divider = 1U, /* 1 kHz 采集，每个 fast tick 消费已发布缓存。 */
    .position_max_age_ticks = 30U,
    .bus_max_age_ticks = 100U,
    /* 300 rpm 是估计排歧/撤销上界，不是可命令的目标上限。 */
    .velocity_estimator = {
        .max_velocity_rpm = 300.0f, .velocity_filter_time_s = 0.005f
    },
    .current_measurement = {
        .measured_phases = CURRENT_MEASUREMENT_PHASE_BC,
        .calibration_sample_count = 4096U,
        .max_offset_abs_a = 0.10f, .max_peak_to_peak_a = 0.05f,
        .gain_correction_enabled = true, .signed_gain = {1.0f, -1.0f, -1.0f}
    },
    .current_zero_settle_samples = 100U,
    .current_zero_velocity_limit_rpm = 5.0f,
    .current_zero_calibration_permitted = true,
    /* 当前 2804 / MT6701 / 原接线的极对数、正方向和电角零位 [turn]。 */
    .electrical_angle = {
        .pole_pairs = 7U, .encoder_direction = 1,
        .electrical_offset_turns = 0.02444341778755188f,
        .max_prediction_time_s = 0.002f, .max_velocity_rpm = 300.0f
    },
    .electrical_calibration_confirmed = true,
    .control = {
        .enabled = true,
        .current_calibration_confirmed = true,
        .timing_confirmed = true,
        /* 0.5 / 600 为当前固定 Run 电流 PI，不声明理论最优。 */
        .current = {
            .kp_d_v_per_a = 0.5f, .kp_q_v_per_a = 0.5f,
            .ki_d_v_per_a_s = 600.0f, .ki_q_v_per_a_s = 600.0f,
            .tracking_gain_per_s = 50.0f, .integral_limit_v = 1.5f,
            .reference_current_limit_a = 0.2f,
            .max_voltage_ratio = 1.8f/14.0f, .max_interval_s = 0.000125f
        },
        /* 0.001 / 0.004 为默认速度 PI 基线；活动参数由 Runtime 锁存。 */
        .speed = {
            .kp_a_per_rpm = 0.001f, .ki_a_per_rpm_s = 0.004f,
            .tracking_gain_per_s = 20.0f, .max_iq_a = 0.1f,
            .integral_limit_a = 0.1f, .max_target_velocity_rpm = 80.0f,
            .max_interval_s = 0.00125f
        },
        .speed_divider = 10U, /* 1 kHz 速度环。 */
        .min_bus_voltage_v = 10.0f,
        .max_bus_voltage_v = 14.0f,
        .phase_current_trip_a = 0.65f,
        .actuation_prediction_time_s = 0.000125f,
        /* 0.4558867801 是 10 kHz 采样下一阶低通的离散系数，-3 dB 为 1 kHz；0旁路。
         * 新增信号群延迟不作为采样/执行边界或电角度预测时间的修改。 */
        .current_feedback_filter_coefficient = 0.4558867801f
    },
    .angle_trig = g431_cordic_sincos,
    .angle_trig_context = &s_cordic_math
};

bool g431_control_profile_initialize_math(void)
{
    return g431_cordic_math_init(&s_cordic_math);
}
