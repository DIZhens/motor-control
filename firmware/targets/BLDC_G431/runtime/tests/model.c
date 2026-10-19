/* 用受控 HCC 替身执行正式 runtime / Motor / VOFA；无电气模型或 ST-Link。 */
#include "target_runtime.h"
#include "main.h"
#include "tim.h"
#include "motor.h"
#include "control_runtime.h"
#include "realtime_motor_io.h"
#include "hardware_trip.h"
#include "position_observation.h"
#include "bus_observation.h"
#include "current_capture.h"

#include <stddef.h>

uint32_t SystemCoreClock = 170000000U;
TIM_HandleTypeDef htim6 = { TIM6 };
uint32_t model_ms;
uint32_t model_facts_ok = 1U;
uint32_t model_facts_rate = 20000U;
uint32_t model_facts_period = 1U;
uint32_t model_bind_ok = 1U;
uint32_t model_trip_bind_ok = 1U;
uint32_t model_position_arm_ok = 1U;
uint32_t model_bus_arm_ok = 1U;
uint32_t model_io_arm_ok = 1U;
uint32_t model_trip_arm_ok = 1U;
uint32_t model_start_ok = 1U;
uint32_t model_trip_available = 1U;
uint32_t model_trip_active;
uint32_t model_trip_causes;
uint32_t model_bus_available = 1U;
uint32_t model_bus_valid = 1U;
uint32_t model_bus_age;
uint32_t model_bus_future;
float model_bus_voltage = 24.0f;
/* 默认没有位置 publication；固定时间戳允许区分重复轮询与独立采集。 */
uint32_t model_position_available;
uint32_t model_position_valid = 1U;
uint32_t model_position_referenced = 1U;
uint32_t model_position_use_fixed_time = 1U;
uint32_t model_position_age;
uint32_t model_position_future;
ControlTime model_position_time;
float model_position_turns;
uint32_t model_current_valid = 1U;
uint32_t model_current_mask = PHASE_MASK_B | PHASE_MASK_C;
float model_current_b;
float model_current_c;
uint32_t model_tick_cycles = 8500U;
uint32_t model_current_time_step = 1U;
uint32_t model_in_callback;
uint32_t model_callback_count;
uint32_t model_bind_count;
uint32_t model_arm_count;
uint32_t model_start_count;
uint32_t model_safeoff_count;
uint32_t model_stage_count;
uint32_t model_enable_count;
uint32_t model_rearm_count;
uint32_t model_bus_read_count;
uint32_t model_position_read_count;
uint32_t model_trip_read_count;
uint32_t model_notify_count;
ControlTime model_control_time;

const uint32_t model_motor_offsets[] = {
    offsetof(Motor, state), offsetof(Motor, faults), offsetof(Motor, command_result),
    offsetof(Motor, command_count), offsetof(Motor, target_velocity_rpm)
};
/* 由正式结构导出诊断偏移，测试不假设布局，也不为产品增加访问入口。 */
#define CURRENT_OFFSET(member) (offsetof(ControlRuntime, current_measurement) \
    + offsetof(CurrentMeasurement, member))
const uint32_t model_current_offsets[] = {
    CURRENT_OFFSET(state), CURRENT_OFFSET(failure), CURRENT_OFFSET(sample_count),
    CURRENT_OFFSET(offset_a), CURRENT_OFFSET(mean_a),
    CURRENT_OFFSET(output) + offsetof(CurrentMeasurementOutput, phase_a),
    CURRENT_OFFSET(output) + offsetof(CurrentMeasurementOutput, alpha_a),
    CURRENT_OFFSET(output) + offsetof(CurrentMeasurementOutput, beta_a),
    CURRENT_OFFSET(output) + offsetof(CurrentMeasurementOutput, valid),
    offsetof(ControlRuntime, current_zero_counter)
};
const uint32_t model_current_field_sizes[] = {
    sizeof(CurrentMeasurementState), sizeof(CurrentMeasurementFailure), sizeof(bool)
};
/* 角度只从真实 Runtime 实例读取；测试参数不代表板卡装配标定。 */
const uint32_t model_angle_offsets[] = {
    offsetof(ControlRuntime, electrical_angle) + offsetof(ElectricalAngle, output)
        + offsetof(ElectricalAngleOutput, electrical_turns),
    offsetof(ControlRuntime, electrical_angle) + offsetof(ElectricalAngle, output)
        + offsetof(ElectricalAngleOutput, sin_theta),
    offsetof(ControlRuntime, electrical_angle) + offsetof(ElectricalAngle, output)
        + offsetof(ElectricalAngleOutput, cos_theta),
    offsetof(ControlRuntime, electrical_angle) + offsetof(ElectricalAngle, output)
        + offsetof(ElectricalAngleOutput, valid),
    offsetof(ControlRuntime, position_estimator) + offsetof(PositionEstimator, velocity_valid)
};
const uint32_t model_observation_counter_offset = offsetof(ControlRuntime, observation_counter);
const uint32_t model_velocity_offset = offsetof(ControlRuntime, position_estimator)
    + offsetof(PositionEstimator, velocity_rpm);
const uint32_t model_capture_offsets[] = {
    offsetof(ObserveCurrentCaptureResult, request_sequence),
    offsetof(ObserveCurrentCaptureResult, state),
    offsetof(ObserveCurrentCaptureResult, failure),
    offsetof(ObserveCurrentCaptureResult, sample_count),
    offsetof(ObserveCurrentCaptureResult, first_sample_time),
    offsetof(ObserveCurrentCaptureResult, last_sample_time),
    offsetof(ObserveCurrentCaptureResult, offset_b_a),
    offsetof(ObserveCurrentCaptureResult, offset_c_a),
    offsetof(ObserveCurrentCaptureResult, phase_b),
    offsetof(ObserveCurrentCaptureResult, phase_c)
};
const uint32_t model_capture_field_sizes[] = {
    sizeof(ObserveCurrentCaptureResult), sizeof(ObserveCurrentCaptureState),
    sizeof(ObserveCurrentCaptureFailure)
};
ControlRuntime model_unpermitted_runtime;
Motor model_unpermitted_motor;
uint32_t model_unpermitted_init_ok;
ControlRuntime model_angle_runtime;
Motor model_angle_motor;
uint32_t model_angle_init_ok;
uint32_t model_angle_calibration_confirmed = 1U;
uint32_t model_angle_pole_pairs = 7U;
int32_t model_angle_direction = 1;
float model_angle_offset = 0.125f;
float model_angle_prediction_limit_s = 0.002f;
uint32_t model_angle_faults;
uint32_t model_angle_snapshot_id_valid;
uint32_t model_angle_snapshot_iq_valid;
static PhaseCurrentSampleReadyHandler s_sample_handler;
static void *s_sample_context;
static HardwareTripHandler s_trip_handler;
static void *s_trip_context;

uint32_t HAL_GetTick(void)
{
    return model_ms;
}

bool realtime_motor_io_get_facts(RealtimeMotorIoFacts *facts)
{
    if (model_facts_ok == 0U) {
        return false;
    }
    facts->normal_voltage_utilization = 0.45f;
    facts->control_time_ticks_per_second = model_facts_rate;
    facts->fast_tick_period_ticks = model_facts_period;
    return true;
}

bool realtime_motor_io_bind_sample_ready(PhaseCurrentSampleReadyHandler handler, void *context)
{
    model_bind_count++;
    if (model_bind_ok == 0U) {
        return false;
    }
    s_sample_handler = handler;
    s_sample_context = context;
    return true;
}

bool realtime_motor_io_arm(void)
{
    model_arm_count++;
    return model_io_arm_ok != 0U;
}

bool realtime_motor_io_start_drive_timing(void)
{
    model_start_count++;
    return model_start_ok != 0U;
}

void realtime_motor_io_safe_off(void)
{
    model_safeoff_count++;
    TIM1->BDTR &= ~TIM_BDTR_MOE;
}

bool realtime_motor_io_stage_phase_duty(const PhaseDutyRequest *request)
{
    (void)request;
    model_stage_count++;
    return true;
}

bool realtime_motor_io_power_enable(void)
{
    model_enable_count++;
    TIM1->BDTR |= TIM_BDTR_MOE;
    return true;
}

bool hardware_trip_bind(HardwareTripHandler handler, void *context)
{
    if (model_trip_bind_ok == 0U) {
        return false;
    }
    s_trip_handler = handler;
    s_trip_context = context;
    return true;
}

bool hardware_trip_arm(void)
{
    return model_trip_arm_ok != 0U;
}

bool hardware_trip_get_snapshot(HardwareTripSnapshot *snapshot)
{
    model_trip_read_count++;
    if (model_trip_available == 0U) {
        return false;
    }
    snapshot->condition_active = model_trip_active != 0U;
    snapshot->causes = model_trip_causes;
    return true;
}

bool hardware_trip_rearm(void)
{
    model_rearm_count++;
    return true;
}

bool position_observation_arm(void)
{
    return model_position_arm_ok != 0U;
}

PositionTopology position_observation_get_topology(void)
{
    return POSITION_TOPOLOGY_PERIODIC;
}

bool position_observation_get_latest(PositionObservation *observation)
{
    model_position_read_count++;
    if (model_position_available == 0U) {
        return false;
    }
    observation->mechanical_position_turns = model_position_turns;
    observation->observation_time = (model_position_use_fixed_time != 0U)
        ? model_position_time
        : model_control_time - model_position_age + model_position_future;
    observation->referenced = model_position_referenced != 0U;
    observation->acquisition_valid = model_position_valid != 0U;
    return true;
}

bool bus_observation_arm(void)
{
    return model_bus_arm_ok != 0U;
}

bool bus_observation_get_latest(BusObservation *observation)
{
    model_bus_read_count++;
    if (model_bus_available == 0U) {
        return false;
    }
    observation->voltage_v = model_bus_voltage;
    observation->observation_time = model_control_time - model_bus_age + model_bus_future;
    observation->acquisition_valid = model_bus_valid != 0U;
    return true;
}

void model_start(void)
{
    target_runtime_start();
}

void model_current_without_permission(void)
{
    const ControlRuntimeConfig config = {
        20000U, 1U, 20U, 100U, 100U, { 3000.0f, 0.01f },
        { CURRENT_MEASUREMENT_PHASE_BC, 4096U, 0.10f, 0.05f }, 200U, 5.0f, false,
        { 0U, 0, 0.0f, 0.0f, 0.0f }, false, { 0 }
    };
    PhaseCurrentObservation current = { 0 };
    uint32_t tick;

    motor_init(&model_unpermitted_motor);
    motor_finish_boot(&model_unpermitted_motor);
    model_unpermitted_init_ok = control_runtime_init(&model_unpermitted_runtime,
        &model_unpermitted_motor, &config) ? 1U : 0U;
    current.measured_phases = PHASE_MASK_B | PHASE_MASK_C;
    current.acquisition_valid = true;
    /* 位置新鲜、静止且功率关闭仍不代替装配方明确给出的无外部电流许可。 */
    model_position_available = 1U;
    model_position_use_fixed_time = 0U;
    for (tick = 1U; tick <= 500U; tick++) {
        model_control_time = tick;
        current.observation_time = tick;
        (void)control_runtime_step(&model_unpermitted_runtime, tick,
            &current, true, 0U, NULL, false);
    }
}

void model_angle_start(void)
{
    ControlRuntimeConfig config = {
        20000U, 1U, 20U, 100U, 100U, { 3000.0f, 0.01f },
        { CURRENT_MEASUREMENT_PHASE_BC, 4096U, 0.10f, 0.05f }, 200U, 5.0f, false,
        { 0U, 0, 0.0f, 0.0f, 0.0f }, false, { 0 }
    };

    config.electrical_calibration_confirmed = model_angle_calibration_confirmed != 0U;
    config.electrical_angle.pole_pairs = model_angle_pole_pairs;
    config.electrical_angle.encoder_direction = model_angle_direction;
    config.electrical_angle.electrical_offset_turns = model_angle_offset;
    config.electrical_angle.max_prediction_time_s = model_angle_prediction_limit_s;
    config.electrical_angle.max_velocity_rpm = 3000.0f;
    motor_init(&model_angle_motor);
    motor_finish_boot(&model_angle_motor);
    model_angle_init_ok = control_runtime_init(&model_angle_runtime,
        &model_angle_motor, &config) ? 1U : 0U;
}

void model_angle_sample(void)
{
    PhaseCurrentObservation current = { 0 };

    model_control_time += model_current_time_step;
    current.phase_b_a = model_current_b;
    current.phase_c_a = model_current_c;
    current.measured_phases = (PhaseMask)model_current_mask;
    current.observation_time = model_control_time;
    current.acquisition_valid = model_current_valid != 0U;
    (void)control_runtime_step(&model_angle_runtime, model_control_time,
        &current, true, model_angle_faults, NULL, false);
}

void model_angle_command_start(void)
{
    const MotorCommand command = { MOTOR_COMMAND_START_VELOCITY, 120.0f };
    PhaseCurrentObservation current = { 0 };
    MotorSnapshot snapshot;

    model_control_time++;
    current.measured_phases = PHASE_MASK_B | PHASE_MASK_C;
    current.observation_time = model_control_time;
    current.acquisition_valid = true;
    (void)control_runtime_step(&model_angle_runtime, model_control_time,
        &current, true, 0U, &command, true);
    control_runtime_snapshot(&model_angle_runtime, model_control_time, true, &snapshot);
    model_angle_snapshot_id_valid = snapshot.id_valid ? 1U : 0U;
    model_angle_snapshot_iq_valid = snapshot.iq_valid ? 1U : 0U;
}

void model_sample(void)
{
    PhaseCurrentObservation current = { 0 };

    DWT->CYCCNT += model_tick_cycles;
    model_control_time += model_current_time_step;
    current.phase_b_a = model_current_b;
    current.phase_c_a = model_current_c;
    current.measured_phases = (PhaseMask)model_current_mask;
    current.observation_time = model_control_time;
    current.acquisition_valid = model_current_valid != 0U;
    if (s_sample_handler != NULL) {
        model_in_callback = 1U;
        model_callback_count++;
        s_sample_handler(s_sample_context, &current);
        model_in_callback = 0U;
    }
}

void model_notify(void)
{
    if (s_trip_handler != NULL) {
        model_notify_count++;
        s_trip_handler(s_trip_context);
    }
}

void model_service(void)
{
    target_runtime_service();
}

void model_guard(void)
{
    target_runtime_guard_irq();
}
