#include "control_runtime.h"

#include <float.h>
#include <stddef.h>
#include <string.h>

#include "realtime_motor_io.h"

static bool finite_float(float value)
{
    return (value == value) && (value <= FLT_MAX) && (value >= -FLT_MAX);
}

static bool fresh(ControlTime observed, ControlTime now, ControlTime maximum_age)
{
    return (observed <= now) && ((now - observed) <= maximum_age);
}

static void reset_position(ControlRuntime *runtime)
{
    position_estimator_reset(&runtime->position_estimator);
    electrical_angle_reset(&runtime->electrical_angle);
    electrical_angle_reset(&runtime->actuation_angle);
    runtime->position_time_available = false;
    runtime->mechanical_position_turns = 0.0f;
}

static void update_position(ControlRuntime *runtime, ControlTime now)
{
    PositionObservation position;
    float interval_s;

    /* 无新 publication 不等于采集失败；仍可保留尚未过期的上次估计。 */
    if (!position_observation_get_latest(&position)) {
        return;
    }
    if (!position.acquisition_valid || !position.referenced
        || !finite_float(position.mechanical_position_turns)
        || (position.mechanical_position_turns < 0.0f)
        || (position.mechanical_position_turns >= 1.0f)
        || !fresh(position.observation_time, now, runtime->config.position_max_age_ticks)) {
        reset_position(runtime);
        return;
    }
    if (runtime->position_time_available) {
        if (position.observation_time < runtime->last_position_time) {
            reset_position(runtime);
            return;
        }
        if (position.observation_time == runtime->last_position_time) {
            return;
        }
        interval_s = (float)(position.observation_time - runtime->last_position_time)
            / (float)runtime->config.control_time_ticks_per_second;
    } else {
        /* 首次只有位置基线，间隔不用于生成速度。 */
        interval_s = 1.0f / (float)runtime->config.control_time_ticks_per_second;
    }
    (void)position_estimator_update(&runtime->position_estimator,
        position.mechanical_position_turns, interval_s);
    runtime->last_position_time = position.observation_time;
    runtime->mechanical_position_turns = position.mechanical_position_turns;
    runtime->position_time_available = true;
}

static void update_electrical_angle(ControlRuntime *runtime, ControlTime now)
{
    float age_s;

    if (!runtime->config.electrical_calibration_confirmed
        || (runtime->motor->faults != 0U)
        || !runtime->position_time_available
        || !runtime->position_estimator.velocity_valid
        || !fresh(runtime->last_position_time, now, runtime->config.position_max_age_ticks)) {
        electrical_angle_reset(&runtime->electrical_angle);
        electrical_angle_reset(&runtime->actuation_angle);
        return;
    }
    age_s = (float)(now - runtime->last_position_time)
        / (float)runtime->config.control_time_ticks_per_second;
    /* 编码器采集较慢；每个 fast tick 按采集年龄外推，不把1ms旧角保持到下一次发布。 */
    (void)electrical_angle_update(&runtime->electrical_angle,
        runtime->mechanical_position_turns, runtime->position_estimator.velocity_rpm, age_s);
    if (runtime->config.control.enabled) {
        (void)electrical_angle_update(&runtime->actuation_angle,
            runtime->mechanical_position_turns, runtime->position_estimator.velocity_rpm,
            age_s + runtime->config.control.actuation_prediction_time_s);
    } else {
        electrical_angle_reset(&runtime->actuation_angle);
    }
}

static void update_current(ControlRuntime *runtime, ControlTime now,
    const PhaseCurrentObservation *current, bool power_disabled)
{
    CurrentMeasurement *measurement = &runtime->current_measurement;
    CurrentMeasurementSample sample = { { 0.0f, 0.0f, 0.0f }, 0U };
    bool consecutive = (current != NULL) && current->acquisition_valid
        && (current->measured_phases & ~(PHASE_MASK_A | PHASE_MASK_B | PHASE_MASK_C)) == 0U
        && (current->observation_time == now)
        && (!runtime->current_time_available
            || ((now > runtime->last_current_time)
                && ((now - runtime->last_current_time) == runtime->config.fast_tick_period_ticks)));
    bool idle_and_off = power_disabled && (runtime->motor->state == MOTOR_STATE_IDLE)
        && (runtime->motor->faults == 0U);
    bool zero_current_confirmed = runtime->config.current_zero_calibration_permitted
        && runtime->position_time_available
        && runtime->position_estimator.velocity_valid
        && fresh(runtime->last_position_time, now, runtime->config.position_max_age_ticks)
        && (runtime->position_estimator.velocity_rpm
            <= runtime->config.current_zero_velocity_limit_rpm)
        && (runtime->position_estimator.velocity_rpm
            >= -runtime->config.current_zero_velocity_limit_rpm);

    runtime->last_current_time = now;
    runtime->current_time_available = true;
    if (!consecutive || runtime->motor->faults != 0U) {
        runtime->current_zero_counter = 0U;
        if (measurement->state != CURRENT_MEASUREMENT_FAILED) {
            current_measurement_abort(measurement);
        }
        return;
    }
    /* 显式适配 HCC 与算法自己的相位集合，不依赖两套枚举恰好数值相同。 */
    if ((current->measured_phases & PHASE_MASK_A) != 0U) {
        sample.valid_phases |= CURRENT_MEASUREMENT_PHASE_A;
        sample.phase_a[0] = current->phase_a_a;
    }
    if ((current->measured_phases & PHASE_MASK_B) != 0U) {
        sample.valid_phases |= CURRENT_MEASUREMENT_PHASE_B;
        sample.phase_a[1] = current->phase_b_a;
    }
    if ((current->measured_phases & PHASE_MASK_C) != 0U) {
        sample.valid_phases |= CURRENT_MEASUREMENT_PHASE_C;
        sample.phase_a[2] = current->phase_c_a;
    }
    if (measurement->state == CURRENT_MEASUREMENT_READY) {
        /* 冻结零偏可用于 RUN 与 Stop；功率打开本身不撤销已经完成的校准。 */
        if (!current_measurement_process(measurement, &sample)) {
            current_measurement_abort(measurement);
        }
        return;
    }
    if (!idle_and_off) {
        runtime->current_zero_counter = 0U;
        if (measurement->state != CURRENT_MEASUREMENT_FAILED) {
            current_measurement_abort(measurement);
        }
        return;
    }
    if (measurement->state == CURRENT_MEASUREMENT_WAITING) {
        if (!zero_current_confirmed) {
            runtime->current_zero_counter = 0U;
            return;
        }
        runtime->current_zero_counter++;
        if (runtime->current_zero_counter < runtime->config.current_zero_settle_samples) {
            return;
        }
        (void)current_measurement_start(measurement, true);
    }
    if (measurement->state == CURRENT_MEASUREMENT_CALIBRATING) {
        (void)current_measurement_calibrate(measurement, &sample, zero_current_confirmed);
    }
    if (measurement->state == CURRENT_MEASUREMENT_READY) {
        /* 就绪后只扣除冻结零偏；即使转轴运动也不重新学习电流零点。 */
        (void)current_measurement_process(measurement, &sample);
    }
}

static bool calibration_confirmed(const ControlRuntime *runtime)
{
    return runtime->config.control.enabled
        && runtime->config.control.current_calibration_confirmed
        && runtime->config.control.timing_confirmed
        && runtime->config.electrical_calibration_confirmed;
}

static float control_velocity(const ControlRuntime *runtime)
{
    float velocity = runtime->position_estimator.velocity_rpm;
    if (runtime->config.electrical_calibration_confirmed) {
        velocity *= (float)runtime->config.electrical_angle.encoder_direction;
    }
    return velocity;
}

static void reset_control(ControlRuntime *runtime)
{
    current_controller_reset(&runtime->current_controller);
    speed_controller_reset(&runtime->speed_controller);
    memset(&runtime->current_output, 0, sizeof(runtime->current_output));
    memset(&runtime->speed_output, 0, sizeof(runtime->speed_output));
    electrical_angle_reset(&runtime->actuation_angle);
    runtime->phase = CONTROL_RUNTIME_OFF;
    runtime->pending_velocity_rpm = 0.0f;
    runtime->speed_counter = 0U;
    runtime->last_speed_time = 0U;
}

static void latch_control_fault(ControlRuntime *runtime, uint32_t faults)
{
    realtime_motor_io_safe_off();
    motor_latch_fault(runtime->motor, faults);
    reset_control(runtime);
    electrical_angle_reset(&runtime->electrical_angle);
    memset(&runtime->current_dq, 0, sizeof(runtime->current_dq));
    if (runtime->current_measurement.state != CURRENT_MEASUREMENT_FAILED) {
        current_measurement_abort(&runtime->current_measurement);
    }
}

static void update_current_dq(ControlRuntime *runtime)
{
    memset(&runtime->current_dq, 0, sizeof(runtime->current_dq));
    if (calibration_confirmed(runtime) && runtime->motor->faults == 0U
        && runtime->current_measurement.state == CURRENT_MEASUREMENT_READY
        && runtime->current_measurement.output.valid && runtime->electrical_angle.output.valid) {
        (void)foc_park(runtime->current_measurement.output.alpha_a,
            runtime->current_measurement.output.beta_a,
            runtime->electrical_angle.output.sin_theta,
            runtime->electrical_angle.output.cos_theta, &runtime->current_dq);
    }
}

static bool feedback_ready(const ControlRuntime *runtime, bool sample_time_valid)
{
    return sample_time_valid && calibration_confirmed(runtime)
        && runtime->current_controller.configured && runtime->speed_controller.configured
        && runtime->motor->faults == 0U && runtime->position_time_available
        && runtime->position_estimator.velocity_valid
        && runtime->electrical_angle.output.valid && runtime->actuation_angle.output.valid
        && runtime->current_measurement.state == CURRENT_MEASUREMENT_READY
        && runtime->current_measurement.failure == CURRENT_MEASUREMENT_FAILURE_NONE
        && runtime->current_measurement.output.valid && runtime->current_dq.valid
        && runtime->bus_valid
        && runtime->bus.voltage_v >= runtime->config.control.min_bus_voltage_v
        && runtime->bus.voltage_v <= runtime->config.control.max_bus_voltage_v;
}

static bool current_within_limit(const ControlRuntime *runtime)
{
    uint32_t index;
    for (index = 0U; index < 3U; index++) {
        float value = runtime->current_measurement.output.phase_a[index];
        if (!finite_float(value) || value > runtime->config.control.phase_current_trip_a
            || value < -runtime->config.control.phase_current_trip_a) {
            return false;
        }
    }
    return true;
}

static bool control_config_init(ControlRuntime *runtime)
{
    const ControlRuntimeControlConfig *control = &runtime->config.control;
    RealtimeMotorIoFacts facts;
    float fast_interval_s, speed_interval_s;
    if (!control->enabled) {
        return true;
    }
    fast_interval_s = (float)runtime->config.fast_tick_period_ticks
        / (float)runtime->config.control_time_ticks_per_second;
    speed_interval_s = fast_interval_s * (float)control->speed_divider;
    if (!current_controller_init(&runtime->current_controller, &control->current)
        || !speed_controller_init(&runtime->speed_controller, &control->speed)
        || control->speed_divider == 0U || !finite_float(fast_interval_s)
        || fast_interval_s <= 0.0f || !finite_float(speed_interval_s)
        || speed_interval_s <= 0.0f
        || fast_interval_s > control->current.max_interval_s
        || speed_interval_s > control->speed.max_interval_s
        || control->speed.max_iq_a > control->current.reference_current_limit_a
        || control->speed.max_target_velocity_rpm > runtime->config.velocity_estimator.max_velocity_rpm
        || !finite_float(control->min_bus_voltage_v) || control->min_bus_voltage_v <= 0.0f
        || !finite_float(control->max_bus_voltage_v)
        || control->max_bus_voltage_v <= control->min_bus_voltage_v
        || !finite_float(control->phase_current_trip_a) || control->phase_current_trip_a <= 0.0f
        || !finite_float(control->actuation_prediction_time_s)
        || control->actuation_prediction_time_s < 0.0f
        || control->actuation_prediction_time_s > 2.0f * fast_interval_s
        || !realtime_motor_io_get_facts(&facts)
        || facts.control_time_ticks_per_second != runtime->config.control_time_ticks_per_second
        || facts.fast_tick_period_ticks != runtime->config.fast_tick_period_ticks
        || !finite_float(facts.normal_voltage_utilization) || facts.normal_voltage_utilization <= 0.0f
        || control->current.max_voltage_ratio > facts.normal_voltage_utilization) {
        return false;
    }
    return true;
}

static bool stage_neutral(ControlRuntime *runtime)
{
    PhaseDutyRequest duty = { 0.5f, 0.5f, 0.5f };
    if (!realtime_motor_io_stage_phase_duty(&duty)) {
        latch_control_fault(runtime, CONTROL_RUNTIME_FAULT_ACTUATION);
        return false;
    }
    return true;
}

static void run_control(ControlRuntime *runtime, ControlTime now)
{
    CurrentControllerInput current = {0};
    PhaseDutyRequest duty;
    runtime->speed_counter++;
    if (!runtime->speed_output.valid
        || runtime->speed_counter >= runtime->config.control.speed_divider) {
        SpeedControllerInput speed;
        speed.target_velocity_rpm = runtime->motor->target_velocity_rpm;
        speed.measured_velocity_rpm = control_velocity(runtime);
        speed.interval_s = (float)(now - runtime->last_speed_time)
            / (float)runtime->config.control_time_ticks_per_second;
        speed.velocity_valid = runtime->position_estimator.velocity_valid;
        speed.reference_valid = true;
        if (now <= runtime->last_speed_time
            || !speed_controller_update(&runtime->speed_controller, &speed, &runtime->speed_output)) {
            latch_control_fault(runtime, CONTROL_RUNTIME_FAULT_CONTROL);
            return;
        }
        runtime->last_speed_time = now;
        runtime->speed_counter = 0U;
    }
    current.current_alpha_a = runtime->current_measurement.output.alpha_a;
    current.current_beta_a = runtime->current_measurement.output.beta_a;
    current.sin_electrical_angle = runtime->electrical_angle.output.sin_theta;
    current.cos_electrical_angle = runtime->electrical_angle.output.cos_theta;
    current.bus_voltage_v = runtime->bus.voltage_v;
    current.id_reference_a = 0.0f;
    current.iq_reference_a = runtime->speed_output.iq_reference_a;
    current.interval_s = (float)runtime->config.fast_tick_period_ticks
        / (float)runtime->config.control_time_ticks_per_second;
    current.current_valid = runtime->current_measurement.output.valid;
    current.angle_valid = runtime->electrical_angle.output.valid;
    current.bus_voltage_valid = runtime->bus_valid;
    current.reference_valid = runtime->speed_output.valid;
    current.separate_actuation_angle = true;
    current.actuation_sin_theta = runtime->actuation_angle.output.sin_theta;
    current.actuation_cos_theta = runtime->actuation_angle.output.cos_theta;
    if (!current_controller_update(&runtime->current_controller, &current, &runtime->current_output)) {
        latch_control_fault(runtime, CONTROL_RUNTIME_FAULT_CONTROL);
        return;
    }
    duty.phase_a_duty = runtime->current_output.duty_a;
    duty.phase_b_duty = runtime->current_output.duty_b;
    duty.phase_c_duty = runtime->current_output.duty_c;
    if (!realtime_motor_io_stage_phase_duty(&duty)) {
        latch_control_fault(runtime, CONTROL_RUNTIME_FAULT_ACTUATION);
    }
}

bool control_runtime_init(ControlRuntime *runtime, Motor *motor,
    const ControlRuntimeConfig *config)
{
    if ((runtime == NULL) || (motor == NULL) || (config == NULL)) {
        return false;
    }
    memset(runtime, 0, sizeof(*runtime));
    runtime->motor = motor;
    runtime->config = *config;
    if ((config->control_time_ticks_per_second == 0U)
        || (config->fast_tick_period_ticks == 0U)
        || (config->observation_divider == 0U)
        || (config->position_max_age_ticks == 0U)
        || (config->bus_max_age_ticks == 0U)
        || (config->current_zero_settle_samples == 0U)
        || !finite_float(config->current_zero_velocity_limit_rpm)
        || (config->current_zero_velocity_limit_rpm < 0.0f)
        || (position_observation_get_topology() != POSITION_TOPOLOGY_PERIODIC)) {
        return false;
    }
    if (!position_estimator_init(&runtime->position_estimator, &config->velocity_estimator)
        || !current_measurement_init(&runtime->current_measurement, &config->current_measurement)) {
        return false;
    }
    /* 未确认标定时正常观察，不能用数值默认值补出一个可信电角度。 */
    if (config->electrical_calibration_confirmed
        && (!electrical_angle_init(&runtime->electrical_angle, &config->electrical_angle)
            || !electrical_angle_init(&runtime->actuation_angle, &config->electrical_angle))) {
        return false;
    }
    return control_config_init(runtime);
}

bool control_runtime_step(ControlRuntime *runtime, ControlTime now,
    const PhaseCurrentObservation *current, bool power_disabled,
    uint32_t fault_evidence, const MotorCommand *command, bool has_command)
{
    uint32_t previous_faults = runtime->motor->faults;
    bool previous_velocity_valid = runtime->position_estimator.velocity_valid;
    bool previous_bus_valid = runtime->bus_valid;
    bool previous_dq_valid = runtime->current_dq.valid;
    ControlRuntimePhase previous_phase = runtime->phase;
    bool stop_requested = has_command && command != NULL && command->type == MOTOR_COMMAND_STOP;
    bool start_accepted = false;
    bool sample_time_valid = (current != NULL) && (current->observation_time == now)
        && (!runtime->current_time_available
            || ((now > runtime->last_current_time)
                && ((now - runtime->last_current_time) == runtime->config.fast_tick_period_ticks)));

    if (fault_evidence != 0U) {
        latch_control_fault(runtime, fault_evidence);
    }
    if (stop_requested) {
        realtime_motor_io_safe_off();
        reset_control(runtime);
        (void)motor_handle_command(runtime->motor, command);
    } else if (runtime->config.control.enabled && runtime->motor->faults == 0U) {
        /* power_disabled 是本拍入口的硬件事实；Stop 已单独优先处理。 */
        if ((runtime->phase == CONTROL_RUNTIME_RUNNING && power_disabled)
            || (runtime->phase != CONTROL_RUNTIME_RUNNING && !power_disabled)) {
            latch_control_fault(runtime, CONTROL_RUNTIME_FAULT_POWER);
        }
    }
    if (!sample_time_valid) {
        reset_position(runtime);
        runtime->observation_counter = 0U;
    } else if (runtime->position_time_available
        && !fresh(runtime->last_position_time, now, runtime->config.position_max_age_ticks)) {
        reset_position(runtime);
    }
    if (sample_time_valid) {
        runtime->observation_counter++;
    }
    if (sample_time_valid && (runtime->observation_counter >= runtime->config.observation_divider)) {
        BusObservation bus;

        runtime->observation_counter = 0U;
        update_position(runtime, now);
        if (bus_observation_get_latest(&bus)) {
            runtime->bus = bus;
            runtime->bus_available = true;
        }
    }
    runtime->bus_valid = runtime->bus_available && runtime->bus.acquisition_valid
        && fresh(runtime->bus.observation_time, now, runtime->config.bus_max_age_ticks)
        && finite_float(runtime->bus.voltage_v) && (runtime->bus.voltage_v >= 0.0f);
    update_electrical_angle(runtime, now);
    update_current(runtime, now, current, power_disabled);
    update_current_dq(runtime);

    if (runtime->config.control.enabled && runtime->motor->faults == 0U
        && runtime->phase != CONTROL_RUNTIME_OFF && !stop_requested) {
        if (!feedback_ready(runtime, sample_time_valid)) {
            latch_control_fault(runtime, CONTROL_RUNTIME_FAULT_FEEDBACK);
        } else if (!current_within_limit(runtime)) {
            latch_control_fault(runtime, CONTROL_RUNTIME_FAULT_OVERCURRENT);
        }
    }
    if (has_command && !stop_requested) {
        if (runtime->config.control.enabled) {
            bool ready = runtime->phase == CONTROL_RUNTIME_OFF
                && runtime->motor->state == MOTOR_STATE_IDLE && power_disabled
                && feedback_ready(runtime, sample_time_valid) && current_within_limit(runtime);
            MotorCommandResult result = motor_handle_control_command(runtime->motor,
                command, ready, runtime->config.control.speed.max_target_velocity_rpm);
            if (command != NULL && command->type == MOTOR_COMMAND_START_VELOCITY
                && result == MOTOR_COMMAND_RESULT_ACCEPTED) {
                /* 接受意图后先使旧 actuation epoch 失效；本拍不再调用 stage。 */
                realtime_motor_io_safe_off();
                reset_control(runtime);
                runtime->pending_velocity_rpm = command->target_velocity_rpm;
                runtime->phase = CONTROL_RUNTIME_START_QUIET;
                start_accepted = true;
            }
        } else {
            (void)motor_handle_command(runtime->motor, command);
        }
    }
    if (runtime->config.control.enabled && runtime->motor->faults == 0U
        && !stop_requested && !start_accepted) {
        if ((runtime->phase == CONTROL_RUNTIME_START_QUIET
                || runtime->phase == CONTROL_RUNTIME_START_STAGED)
            && runtime->motor->state != MOTOR_STATE_IDLE) {
            latch_control_fault(runtime, CONTROL_RUNTIME_FAULT_CONTROL);
        } else if (runtime->phase == CONTROL_RUNTIME_START_QUIET) {
            if (stage_neutral(runtime)) {
                runtime->phase = CONTROL_RUNTIME_START_STAGED;
            }
        } else if (runtime->phase == CONTROL_RUNTIME_START_STAGED) {
            if (stage_neutral(runtime)) {
                if (!realtime_motor_io_power_enable()) {
                    latch_control_fault(runtime, CONTROL_RUNTIME_FAULT_ACTUATION);
                } else if (!motor_enter_velocity_run(runtime->motor, runtime->pending_velocity_rpm,
                    runtime->config.control.speed.max_target_velocity_rpm)) {
                    latch_control_fault(runtime, CONTROL_RUNTIME_FAULT_CONTROL);
                } else {
                    runtime->phase = CONTROL_RUNTIME_RUNNING;
                    runtime->pending_velocity_rpm = 0.0f;
                    runtime->last_speed_time = now;
                }
            }
        } else if (runtime->phase == CONTROL_RUNTIME_RUNNING) {
            if (runtime->motor->state != MOTOR_STATE_RUN) {
                latch_control_fault(runtime, CONTROL_RUNTIME_FAULT_CONTROL);
            } else {
                run_control(runtime, now);
            }
        }
    }
    return has_command || (previous_faults != runtime->motor->faults)
        || (previous_velocity_valid != runtime->position_estimator.velocity_valid)
        || (previous_bus_valid != runtime->bus_valid)
        || (previous_dq_valid != runtime->current_dq.valid) || previous_phase != runtime->phase;
}

void control_runtime_snapshot(const ControlRuntime *runtime, ControlTime now,
    bool has_sample, MotorSnapshot *snapshot)
{
    memset(snapshot, 0, sizeof(*snapshot));
    snapshot->state = runtime->motor->state;
    snapshot->faults = runtime->motor->faults;
    snapshot->target_velocity_rpm = runtime->motor->target_velocity_rpm;
    snapshot->command_result = runtime->motor->command_result;
    snapshot->status_valid = true;
    if (!has_sample) {
        return;
    }
    snapshot->control_time_ticks = now;
    snapshot->control_time_ticks_per_second = runtime->config.control_time_ticks_per_second;
    snapshot->control_time_valid = true;
    if (runtime->position_time_available && runtime->position_estimator.velocity_valid
        && fresh(runtime->last_position_time, now, runtime->config.position_max_age_ticks)) {
        snapshot->velocity_rpm = control_velocity(runtime);
        snapshot->velocity_valid = true;
    }
    if (runtime->bus_available && runtime->bus.acquisition_valid
        && fresh(runtime->bus.observation_time, now, runtime->config.bus_max_age_ticks)
        && finite_float(runtime->bus.voltage_v) && (runtime->bus.voltage_v >= 0.0f)) {
        snapshot->bus_voltage_v = runtime->bus.voltage_v;
        snapshot->bus_voltage_valid = true;
    }
    if (calibration_confirmed(runtime) && runtime->motor->faults == 0U
        && runtime->current_dq.valid && runtime->current_time_available
        && now == runtime->last_current_time) {
        snapshot->id_a = runtime->current_dq.d;
        snapshot->iq_a = runtime->current_dq.q;
        snapshot->id_valid = true;
        snapshot->iq_valid = true;
    }
}
