#include "g431_control_profile.h"
#include "cordic_math.h"

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
    .velocity_estimator = {300.0f, 0.005f},
    .current_measurement = {CURRENT_MEASUREMENT_PHASE_BC, 4096U, 0.10f, 0.05f,
        true, {1.0f, -1.0f, -1.0f}},
    .current_zero_settle_samples = 100U,
    .current_zero_velocity_limit_rpm = 5.0f,
    .current_zero_calibration_permitted = true,
    .electrical_angle = {7U, 1, 0.02444341778755188f, 0.002f, 300.0f},
    .electrical_calibration_confirmed = true,
    .control = {
        .enabled = true,
        .current_calibration_confirmed = true,
        .timing_confirmed = true,
        .current = {0.5f, 0.5f, 600.0f, 600.0f, 50.0f, 1.5f, 0.2f,
            1.8f/14.0f, 0.000125f},
        .speed = {0.001f, 0.004f, 20.0f, 0.1f, 0.1f, 80.0f, 0.00125f},
        .speed_divider = 10U, /* 1 kHz 速度环。 */
        .min_bus_voltage_v = 10.0f,
        .max_bus_voltage_v = 14.0f,
        .phase_current_trip_a = 0.65f,
        .actuation_prediction_time_s = 0.000125f
    },
    .angle_trig = g431_cordic_sincos,
    .angle_trig_context = &s_cordic_math
};

bool g431_control_profile_initialize_math(void)
{
    return g431_cordic_math_init(&s_cordic_math);
}
