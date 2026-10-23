/* 执行正式速度 PI；仅暴露数值测试入口，不访问硬件。 */
#include "speed_controller.h"

#include <stddef.h>
#include <stdint.h>

SpeedController model_controllers[2];
SpeedControllerConfig model_config;
float model_inputs[3];
uint32_t model_input_valid[2];
float model_values[3];
uint32_t model_status[5];
uint32_t model_selector;
float model_history;

static SpeedController *model_selected(void)
{
    return &model_controllers[model_selector == 1u ? 1u : 0u];
}

static void model_capture(bool result, const SpeedControllerOutput *output)
{
    model_values[0] = output->requested_iq_a;
    model_values[1] = output->iq_reference_a;
    model_values[2] = model_selected()->integral_a;
    model_status[0] = (uint32_t)result;
    model_status[1] = (uint32_t)output->valid;
    model_status[2] = (uint32_t)output->current_limited;
    model_status[3] = (uint32_t)output->integral_limited;
    model_status[4] = (uint32_t)model_selected()->configured;
}

static SpeedControllerInput model_input(void)
{
    SpeedControllerInput input;
    input.target_velocity_rpm = model_inputs[0];
    input.measured_velocity_rpm = model_inputs[1];
    input.interval_s = model_inputs[2];
    input.velocity_valid = model_input_valid[0] != 0u;
    input.reference_valid = model_input_valid[1] != 0u;
    return input;
}

void model_init(void)
{
    SpeedControllerOutput output = {0};
    model_capture(speed_controller_init(model_selected(), &model_config), &output);
}

void model_update(void)
{
    SpeedControllerOutput output;
    SpeedControllerInput input = model_input();
    bool result = speed_controller_update(model_selected(), &input, &output);
    model_capture(result, &output);
}

void model_reset(void)
{
    speed_controller_reset(model_selected());
    model_values[2] = model_selected()->integral_a;
    model_status[4] = (uint32_t)model_selected()->configured;
}

void model_null_config(void)
{
    SpeedControllerOutput output = {0};
    model_capture(speed_controller_init(model_selected(), NULL), &output);
}

void model_alias_config(void)
{
    SpeedControllerOutput output = {0};
    model_capture(speed_controller_init(model_selected(), &model_selected()->config), &output);
}

void model_null_controller(void)
{
    SpeedControllerOutput output;
    SpeedControllerInput input = model_input();
    bool init_result, update_result;
    speed_controller_reset(NULL);
    init_result = speed_controller_init(NULL, &model_config);
    update_result = speed_controller_update(NULL, &input, &output);
    model_capture(init_result || update_result, &output);
}

void model_null_input(void)
{
    SpeedControllerOutput output;
    bool result = speed_controller_update(model_selected(), NULL, &output);
    model_capture(result, &output);
}

void model_null_output(void)
{
    SpeedControllerOutput output = {0};
    SpeedControllerInput input = model_input();
    bool result = speed_controller_update(model_selected(), &input, NULL);
    model_capture(result, &output);
}

void model_corrupt_history(void)
{
    model_selected()->integral_a = model_history;
    model_update();
}

void model_corrupt_config(void)
{
    model_selected()->config = model_config;
    model_update();
}
