/* 直接执行公共 Runtime；HCC 替身只模拟下一拍 duty 边界和功率 epoch，不接设备。 */
#include "control_runtime.h"
#include <math.h>
#include <stddef.h>

Motor model_motor;
ControlRuntime model_runtime;
MotorSnapshot model_snapshot;
uint32_t model_init_ok;
uint32_t model_enabled = 1U;
uint32_t model_current_confirmed = 1U;
uint32_t model_electrical_confirmed = 1U;
uint32_t model_timing_confirmed = 1U;
uint32_t model_gain_enabled = 1U;
float model_gain_b = 1.25f;
float model_gain_c = -0.8f;
int32_t model_direction = 1;
float model_prediction_s = 0.00005f;
uint32_t model_speed_divider = 20U;
float model_voltage_ratio = 0.45f;
float model_normal_utilization = 0.45f;
float model_current_trip_a = 1.2f;
uint32_t model_facts_ok = 1U;
uint32_t model_zero_permitted = 1U;
uint32_t model_trig_enabled; /* 0保留默认软件；1注入带独立上下文的测试后端。 */
uint32_t model_trig_mode; /* 0软件；1拒绝；2非有限输出；3非单位输出。 */
uint32_t model_trig_fail_call; /* 0每次均按mode处理；1/2仅指定本拍测量/施加角调用失败。 */
float model_trig_scale = 1.0f; /* 有效相量模长误差fixture，验证RUN复用归一化测量值。 */
uint32_t model_trig_calls; /* 后端上下文持有的总调用计数。 */
uint32_t model_trig_step_calls; /* 本拍实际回调次数。 */
uint32_t model_trig_context_ok; /* 实际收到的context与装配时绑定值相同。 */
float model_trig_turns[2]; /* 本拍测量/施加后端分别收到的电角度 [turn]。 */
float model_controller_dq[2]; /* 成功controller内部测量值，用于与RUN快照逐位核对。 */
uint32_t model_stage_ok = 1U;
uint32_t model_enable_ok = 1U;
uint32_t model_current_valid = 1U;
uint32_t model_current_mask = PHASE_MASK_B | PHASE_MASK_C;
uint32_t model_position_available = 1U;
uint32_t model_position_valid = 1U;
uint32_t model_position_referenced = 1U;
uint32_t model_bus_available = 1U;
uint32_t model_bus_valid = 1U;
uint32_t model_position_age;
uint32_t model_bus_age;
float model_position_turns;
float model_bus_voltage = 12.0f;
float model_current_b = 0.02f;
float model_current_c = -0.03f;
uint64_t model_time;
uint32_t model_tick_step = 1U;
uint32_t model_fault_evidence;
uint32_t model_has_command;
uint32_t model_command_null;
uint32_t model_command_type;
float model_command_target;
uint32_t model_power_enabled;
uint32_t model_stage_count;
uint32_t model_enable_count;
uint32_t model_safeoff_count;
uint32_t model_epoch = 1U;
uint32_t model_active_epoch;
uint32_t model_pending_epoch;
uint32_t model_pending_valid;
uint64_t model_pending_time;
uint64_t model_active_source_time;
uint32_t model_enable_boundary_errors;
uint32_t model_enable_non_neutral_errors;
PhaseDutyRequest model_pending_duty;
PhaseDutyRequest model_active_duty = { 0.5f, 0.5f, 0.5f };

/* 固定简单诊断布局；读取的数值来自真实 Runtime 与算法实例。 */
uint32_t model_diag[13];
float model_values[16];

static bool model_angle_trig(void *context, float turns, float *sine, float *cosine)
{
    uint32_t *calls = (uint32_t *)context;
    model_trig_context_ok = context == &model_trig_calls ? 1U : 0U;
    if (model_trig_context_ok == 0U) {
        return false;
    }
    (*calls)++;
    if (model_trig_step_calls < 2U) {
        model_trig_turns[model_trig_step_calls] = turns;
    }
    model_trig_step_calls++;
    if (model_trig_fail_call == 0U || model_trig_fail_call == model_trig_step_calls) {
        if (model_trig_mode == 1U) {
            return false;
        }
        if (model_trig_mode == 2U) {
            *sine = NAN;
            *cosine = 1.0f;
            return true;
        }
        if (model_trig_mode == 3U) {
            *sine = 1.0f;
            *cosine = 1.0f;
            return true;
        }
    }
    if (!electrical_angle_sincos_software(NULL, turns, sine, cosine)) {
        return false;
    }
    *sine *= model_trig_scale;
    *cosine *= model_trig_scale;
    return true;
}

void model_diagnostics(void)
{
    control_runtime_snapshot(&model_runtime, model_time, true, &model_snapshot);
    model_diag[0] = (uint32_t)model_motor.state;
    model_diag[1] = model_motor.faults;
    model_diag[2] = (uint32_t)model_motor.command_result;
    model_diag[3] = (uint32_t)model_runtime.phase;
    model_diag[4] = (uint32_t)model_runtime.current_measurement.state;
    model_diag[5] = model_runtime.current_dq.valid ? 1U : 0U;
    model_diag[6] = model_runtime.current_output.valid ? 1U : 0U;
    model_diag[7] = model_runtime.speed_output.valid ? 1U : 0U;
    model_diag[8] = model_runtime.speed_counter;
    model_diag[9] = model_runtime.current_measurement.sample_count;
    model_diag[10] = model_runtime.current_measurement.output.valid ? 1U : 0U;
    model_diag[11] = model_snapshot.id_valid ? 1U : 0U;
    model_diag[12] = model_snapshot.velocity_valid ? 1U : 0U;
    model_values[0] = model_motor.target_velocity_rpm;
    model_values[1] = model_snapshot.velocity_rpm;
    model_values[2] = model_runtime.current_dq.d;
    model_values[3] = model_runtime.current_dq.q;
    model_values[4] = model_runtime.speed_output.iq_reference_a;
    model_values[5] = model_runtime.current_controller.integral_d_v;
    model_values[6] = model_runtime.current_controller.integral_q_v;
    model_values[7] = model_runtime.speed_controller.integral_a;
    model_values[8] = model_runtime.current_measurement.offset_a[1];
    model_values[9] = model_runtime.current_measurement.offset_a[2];
    model_values[10] = model_runtime.electrical_angle.output.electrical_turns;
    model_values[11] = model_runtime.actuation_angle.output.electrical_turns;
    model_values[12] = model_runtime.current_output.requested_vd_v;
    model_values[13] = model_runtime.current_output.requested_vq_v;
    model_values[14] = model_runtime.current_output.applied_vd_v;
    model_values[15] = model_runtime.current_output.applied_vq_v;
    model_controller_dq[0] = model_runtime.current_output.id_a;
    model_controller_dq[1] = model_runtime.current_output.iq_a;
}

bool realtime_motor_io_get_facts(RealtimeMotorIoFacts *facts)
{
    facts->normal_voltage_utilization = model_normal_utilization;
    facts->control_time_ticks_per_second = 20000U;
    facts->fast_tick_period_ticks = 1U;
    return model_facts_ok != 0U;
}

void realtime_motor_io_safe_off(void)
{
    const PhaseDutyRequest neutral = {0.5f, 0.5f, 0.5f};
    model_safeoff_count++;
    model_epoch++;
    model_pending_valid = 0U;
    model_power_enabled = 0U;
    model_active_epoch = 0U;
    model_active_duty = neutral;
}

bool realtime_motor_io_stage_phase_duty(const PhaseDutyRequest *duty)
{
    model_stage_count++;
    if (model_stage_ok == 0U || duty == NULL
        || !isfinite(duty->phase_a_duty) || !isfinite(duty->phase_b_duty)
        || !isfinite(duty->phase_c_duty)
        || duty->phase_a_duty < 0.0f || duty->phase_a_duty > 1.0f
        || duty->phase_b_duty < 0.0f || duty->phase_b_duty > 1.0f
        || duty->phase_c_duty < 0.0f || duty->phase_c_duty > 1.0f) {
        return false;
    }
    model_pending_duty = *duty;
    model_pending_epoch = model_epoch;
    model_pending_time = model_time;
    model_pending_valid = 1U;
    return true;
}

bool realtime_motor_io_power_enable(void)
{
    model_enable_count++;
    if (model_active_epoch != model_epoch || model_active_source_time >= model_time) {
        model_enable_boundary_errors++;
        return false;
    }
    if (model_active_duty.phase_a_duty != 0.5f || model_active_duty.phase_b_duty != 0.5f
        || model_active_duty.phase_c_duty != 0.5f) {
        model_enable_non_neutral_errors++;
        return false;
    }
    if (model_enable_ok == 0U) {
        return false;
    }
    model_power_enabled = 1U;
    return true;
}

PositionTopology position_observation_get_topology(void)
{
    return POSITION_TOPOLOGY_PERIODIC;
}

bool position_observation_get_latest(PositionObservation *position)
{
    if (model_position_available == 0U) return false;
    position->mechanical_position_turns = model_position_turns;
    position->observation_time = model_time - model_position_age;
    position->referenced = model_position_referenced != 0U;
    position->acquisition_valid = model_position_valid != 0U;
    return true;
}

bool bus_observation_get_latest(BusObservation *bus)
{
    if (model_bus_available == 0U) return false;
    bus->voltage_v = model_bus_voltage;
    bus->observation_time = model_time - model_bus_age;
    bus->acquisition_valid = model_bus_valid != 0U;
    return true;
}

void model_init(void)
{
    ControlRuntimeConfig config = {0};
    config.control_time_ticks_per_second = 20000U;
    config.fast_tick_period_ticks = 1U;
    config.observation_divider = 20U;
    config.position_max_age_ticks = 100U;
    config.bus_max_age_ticks = 100U;
    config.velocity_estimator.max_velocity_rpm = 3000.0f;
    config.velocity_estimator.velocity_filter_time_s = 0.001f;
    config.current_measurement.measured_phases = CURRENT_MEASUREMENT_PHASE_BC;
    config.current_measurement.calibration_sample_count = 16U;
    config.current_measurement.max_offset_abs_a = 0.10f;
    config.current_measurement.max_peak_to_peak_a = 0.05f;
    config.current_measurement.gain_correction_enabled = model_gain_enabled != 0U;
    config.current_measurement.signed_gain[0] = 1.0f;
    config.current_measurement.signed_gain[1] = model_gain_b;
    config.current_measurement.signed_gain[2] = model_gain_c;
    config.current_zero_settle_samples = 4U;
    config.current_zero_velocity_limit_rpm = 5.0f;
    config.current_zero_calibration_permitted = model_zero_permitted != 0U;
    config.electrical_angle.pole_pairs = 7U;
    config.electrical_angle.encoder_direction = model_direction;
    config.electrical_angle.electrical_offset_turns = 0.0f;
    config.electrical_angle.max_prediction_time_s = 0.002f;
    config.electrical_angle.max_velocity_rpm = 3000.0f;
    config.electrical_calibration_confirmed = model_electrical_confirmed != 0U;
    config.angle_trig = model_trig_enabled != 0U ? model_angle_trig : NULL;
    config.angle_trig_context = &model_trig_calls;
    config.control.enabled = model_enabled != 0U;
    config.control.current_calibration_confirmed = model_current_confirmed != 0U;
    config.control.timing_confirmed = model_timing_confirmed != 0U;
    config.control.current.kp_d_v_per_a = 2.0f;
    config.control.current.kp_q_v_per_a = 2.0f;
    config.control.current.ki_d_v_per_a_s = 1000.0f;
    config.control.current.ki_q_v_per_a_s = 1000.0f;
    config.control.current.tracking_gain_per_s = 200.0f;
    config.control.current.integral_limit_v = 4.0f;
    config.control.current.reference_current_limit_a = 0.6f;
    config.control.current.max_voltage_ratio = model_voltage_ratio;
    config.control.current.max_interval_s = 0.000051f;
    config.control.speed.kp_a_per_rpm = 0.006f;
    config.control.speed.ki_a_per_rpm_s = 0.04f;
    config.control.speed.tracking_gain_per_s = 20.0f;
    config.control.speed.max_iq_a = 0.4f;
    config.control.speed.integral_limit_a = 0.4f;
    config.control.speed.max_target_velocity_rpm = 200.0f;
    config.control.speed.max_interval_s = 0.00101f;
    config.control.speed_divider = model_speed_divider;
    config.control.min_bus_voltage_v = 8.0f;
    config.control.max_bus_voltage_v = 16.0f;
    config.control.phase_current_trip_a = model_current_trip_a;
    config.control.actuation_prediction_time_s = model_prediction_s;
    motor_init(&model_motor);
    motor_finish_boot(&model_motor);
    model_init_ok = control_runtime_init(&model_runtime, &model_motor, &config) ? 1U : 0U;
    model_diagnostics();
}

void model_step(void)
{
    PhaseCurrentObservation current = {0};
    MotorCommand command;
    model_trig_step_calls = 0U;
    model_time += model_tick_step;
    if (model_pending_valid != 0U && model_pending_time < model_time
        && model_pending_epoch == model_epoch) {
        model_active_duty = model_pending_duty;
        model_active_epoch = model_pending_epoch;
        model_active_source_time = model_pending_time;
        model_pending_valid = 0U;
    }
    current.phase_b_a = model_current_b;
    current.phase_c_a = model_current_c;
    current.measured_phases = (PhaseMask)model_current_mask;
    current.observation_time = model_time;
    current.acquisition_valid = model_current_valid != 0U;
    command.type = (MotorCommandType)model_command_type;
    command.target_velocity_rpm = model_command_target;
    (void)control_runtime_step(&model_runtime, model_time, &current,
        model_power_enabled == 0U, model_fault_evidence,
        model_command_null ? NULL : &command, model_has_command != 0U);
    model_has_command = 0U;
    model_diagnostics();
}

/* 只用于验证控制计算遇坏内部数值也不能继续输出；不为产品增加诊断写入口。 */
void model_poison_integral(void)
{
    model_runtime.current_controller.integral_q_v = NAN;
}
