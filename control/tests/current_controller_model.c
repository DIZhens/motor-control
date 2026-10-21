/* 执行正式电流 PI 与调制 C；仅暴露数值测试入口，不访问硬件。 */
#include "current_controller.h"

#include <stddef.h>
#include <stdint.h>

CurrentController model_controllers[2];
CurrentControllerConfig model_config;
float model_inputs[8];
uint32_t model_separate_actuation;
float model_actuation_angle[2];
uint32_t model_input_valid[4];
float model_values[11];
uint32_t model_status[5];
uint32_t model_selector;
float model_history[2];

static CurrentController *model_selected(void)
{
    return &model_controllers[model_selector == 1u ? 1u : 0u];
}

static void model_capture(bool result, const CurrentControllerOutput *output)
{
    CurrentController *controller = model_selected();
    model_values[0] = output->id_a;
    model_values[1] = output->iq_a;
    model_values[2] = output->requested_vd_v;
    model_values[3] = output->requested_vq_v;
    model_values[4] = output->applied_vd_v;
    model_values[5] = output->applied_vq_v;
    model_values[6] = output->duty_a;
    model_values[7] = output->duty_b;
    model_values[8] = output->duty_c;
    model_values[9] = controller->integral_d_v;
    model_values[10] = controller->integral_q_v;
    model_status[0] = (uint32_t)result;
    model_status[1] = (uint32_t)output->valid;
    model_status[2] = (uint32_t)output->voltage_limited;
    model_status[3] = (uint32_t)output->integral_limited;
    model_status[4] = (uint32_t)controller->configured;
}

static CurrentControllerInput model_input(void)
{
    CurrentControllerInput input;
    input.current_alpha_a = model_inputs[0];
    input.current_beta_a = model_inputs[1];
    input.sin_electrical_angle = model_inputs[2];
    input.cos_electrical_angle = model_inputs[3];
    input.bus_voltage_v = model_inputs[4];
    input.id_reference_a = model_inputs[5];
    input.iq_reference_a = model_inputs[6];
    input.interval_s = model_inputs[7];
    input.current_valid = model_input_valid[0] != 0u;
    input.angle_valid = model_input_valid[1] != 0u;
    input.bus_voltage_valid = model_input_valid[2] != 0u;
    input.reference_valid = model_input_valid[3] != 0u;
    input.separate_actuation_angle = model_separate_actuation != 0u;
    input.actuation_sin_theta = model_actuation_angle[0];
    input.actuation_cos_theta = model_actuation_angle[1];
    return input;
}

void model_init(void)
{
    CurrentControllerOutput output = {0};
    bool result = current_controller_init(model_selected(), &model_config);
    model_capture(result, &output);
}

void model_update(void)
{
    CurrentControllerOutput output;
    CurrentControllerInput input = model_input();
    bool result = current_controller_update(model_selected(), &input, &output);
    model_capture(result, &output);
}

void model_legacy_input(void)
{
    /* 追加字段省略时为 false/零，保持已有同角聚合初始化的行为。 */
    const CurrentControllerInput input = {
        0.2f, -0.1f, 0.0f, 1.0f, 12.0f, 0.8f, -0.4f, 0.00005f,
        true, true, true, true
    };
    CurrentControllerOutput output;
    bool result = current_controller_update(model_selected(), &input, &output);
    model_capture(result, &output);
}

void model_reset(void)
{
    current_controller_reset(model_selected());
    model_values[9] = model_selected()->integral_d_v;
    model_values[10] = model_selected()->integral_q_v;
    model_status[4] = (uint32_t)model_selected()->configured;
}

void model_null_config(void)
{
    CurrentControllerOutput output = {0};
    bool result = current_controller_init(model_selected(), NULL);
    model_capture(result, &output);
}

void model_alias_config(void)
{
    CurrentControllerOutput output = {0};
    bool result = current_controller_init(model_selected(), &model_selected()->config);
    model_capture(result, &output);
}

void model_null_controller(void)
{
    CurrentControllerOutput output;
    CurrentControllerInput input = model_input();
    bool init_result, update_result;
    current_controller_reset(NULL);
    init_result = current_controller_init(NULL, &model_config);
    update_result = current_controller_update(NULL, &input, &output);
    model_capture(init_result || update_result, &output);
}

void model_null_input(void)
{
    CurrentControllerOutput output;
    bool result = current_controller_update(model_selected(), NULL, &output);
    model_capture(result, &output);
}

void model_null_output(void)
{
    CurrentControllerOutput output = {0};
    CurrentControllerInput input = model_input();
    bool result = current_controller_update(model_selected(), &input, NULL);
    model_capture(result, &output);
}

void model_corrupt_history(void)
{
    model_selected()->integral_d_v = model_history[0];
    model_selected()->integral_q_v = model_history[1];
    model_update();
}

void model_replace_config(void)
{
    /* 公开结构被改写后，update仍必须逐拍检查当前配置，不能依赖init时的缓存。 */
    model_selected()->config = model_config;
    model_update();
}
