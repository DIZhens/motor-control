/* 执行正式静态对齐 C 的离线入口；没有 Platform、HAL、UART 或功率函数。 */
#include "rotor_alignment.h"

#include <stddef.h>
#include <stdint.h>
#include <string.h>

RotorAlignmentConfig model_config;
RotorAlignment model_alignment;
RotorAlignment model_second;
ControlTime model_now;
ControlTime model_current_time;
ControlTime model_position_time;
ControlTime model_bus_time;
ControlTime model_candidate_times[2];
float model_inputs[6];
float model_values[9];
uint32_t model_flags;
uint32_t model_result;
uint32_t model_diag[9];

static RotorAlignmentInput input_snapshot(void)
{
    RotorAlignmentInput input;
    memset(&input, 0, sizeof(input));
    input.now = model_now;
    input.current_time = model_current_time;
    input.position_time = model_position_time;
    input.bus_time = model_bus_time;
    input.phase_current_a[0] = model_inputs[0];
    input.phase_current_a[1] = model_inputs[1];
    input.phase_current_a[2] = model_inputs[2];
    input.position_turns = model_inputs[3];
    input.velocity_rpm = model_inputs[4];
    input.bus_voltage_v = model_inputs[5];
    input.commissioning_allowed = (model_flags & 1U) != 0U;
    input.current_calibration_confirmed = (model_flags & 2U) != 0U;
    input.current_valid = (model_flags & 4U) != 0U;
    input.position_valid = (model_flags & 8U) != 0U;
    input.bus_valid = (model_flags & 16U) != 0U;
    input.fault_active = (model_flags & 32U) != 0U;
    input.stop_requested = (model_flags & 64U) != 0U;
    return input;
}

static void publish(void)
{
    RotorAlignment *alignment = &model_alignment;
    model_diag[0] = (uint32_t)alignment->state;
    model_diag[1] = (uint32_t)alignment->reason;
    model_diag[2] = (uint32_t)alignment->configured;
    model_diag[3] = (uint32_t)alignment->output.output_requested;
    model_diag[4] = (uint32_t)alignment->candidate.valid;
    model_diag[5] = alignment->window_samples;
    model_diag[6] = (uint32_t)alignment->window_active;
    model_diag[7] = alignment->candidate.position_samples;
    model_diag[8] = alignment->candidate.nominal_pole_pairs;
    model_values[0] = alignment->output.duty_a;
    model_values[1] = alignment->output.duty_b;
    model_values[2] = alignment->output.duty_c;
    model_values[3] = alignment->output.alpha_voltage_v;
    model_values[4] = alignment->output.beta_voltage_v;
    model_values[5] = alignment->candidate.encoder_position_turns;
    model_values[6] = alignment->candidate.stator_angle_turns;
    model_values[7] = alignment->candidate.positive_direction_offset_turns;
    model_values[8] = alignment->candidate.negative_direction_offset_turns;
    model_candidate_times[0] = alignment->candidate.first_position_time;
    model_candidate_times[1] = alignment->candidate.last_position_time;
}

void model_init(void)
{
    model_result = (uint32_t)rotor_alignment_init(&model_alignment, &model_config);
    publish();
}

void model_start(void)
{
    RotorAlignmentInput input = input_snapshot();
    model_result = (uint32_t)rotor_alignment_start(&model_alignment, &input);
    publish();
}

void model_update(void)
{
    RotorAlignmentInput input = input_snapshot();
    model_result = (uint32_t)rotor_alignment_update(&model_alignment, &input);
    publish();
}

void model_stop(void)
{
    rotor_alignment_stop(&model_alignment);
    model_result = 0U;
    publish();
}

void model_alias_init(void)
{
    model_result = (uint32_t)rotor_alignment_init(&model_alignment, &model_alignment.config);
    publish();
}

void model_nulls(void)
{
    model_result = (uint32_t)rotor_alignment_init(NULL, &model_config);
    model_result |= (uint32_t)rotor_alignment_start(NULL, NULL);
    model_result |= (uint32_t)rotor_alignment_update(NULL, NULL);
    rotor_alignment_stop(NULL);
    publish();
}

void model_null_init(void)
{
    model_result = (uint32_t)rotor_alignment_init(&model_alignment, NULL);
    publish();
}

void model_null_start(void)
{
    model_result = (uint32_t)rotor_alignment_start(&model_alignment, NULL);
    publish();
}

void model_null_update(void)
{
    model_result = (uint32_t)rotor_alignment_update(&model_alignment, NULL);
    publish();
}

void model_other_instance(void)
{
    RotorAlignmentInput input = input_snapshot();
    model_result = (uint32_t)rotor_alignment_init(&model_second, &model_config);
    model_result &= (uint32_t)rotor_alignment_start(&model_second, &input);
    rotor_alignment_stop(&model_second);
    publish();
}
