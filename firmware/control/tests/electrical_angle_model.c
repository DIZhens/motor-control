/* 在 Cortex-M4 模型中执行正式电角度映射；不包含硬件与功率操作。 */
#include "electrical_angle.h"

#include <stddef.h>
#include <stdint.h>

ElectricalAngleConfig model_config;
ElectricalAngle model_angle;
ElectricalAngle model_second_angle;
float model_position;
float model_velocity;
float model_age;
float model_output[3];
uint32_t model_result;
uint32_t model_valid;
uint32_t model_configured;

/* 两个独立回调上下文，验证调用方持有的状态不会串到另一角度实例。 */
typedef struct {
    uint32_t calls; /* 成功或失败调用均计数。 */
    uint32_t mode; /* 0 软件；1 拒绝；2 固定输出；3 写一个输出后拒绝；4 漏写输出。 */
    float received_turns; /* 回调实际收到的归一化电角度 [turn]。 */
    float sine; /* 固定输出模式使用的 sin。 */
    float cosine; /* 固定输出模式使用的 cos。 */
} ModelTrigContext;

ModelTrigContext model_trig_context;
ModelTrigContext model_second_trig_context;
float model_second_output[3];
uint32_t model_second_valid;
uint32_t model_pointer_mode;

static bool model_trig(void *context, float electrical_turns, float *sine, float *cosine)
{
    ModelTrigContext *state = (ModelTrigContext *)context;
    state->calls++;
    state->received_turns = electrical_turns;
    if (state->mode == 1U) {
        return false;
    }
    if (state->mode == 2U) {
        *sine = state->sine;
        *cosine = state->cosine;
        return true;
    }
    if (state->mode == 3U) {
        *sine = state->sine;
        return false;
    }
    if (state->mode == 4U) {
        return true;
    }
    return electrical_angle_sincos_software(context, electrical_turns, sine, cosine);
}

static void publish(void)
{
    model_output[0] = model_angle.output.electrical_turns;
    model_output[1] = model_angle.output.sin_theta;
    model_output[2] = model_angle.output.cos_theta;
    model_valid = (uint32_t)model_angle.output.valid;
    model_configured = (uint32_t)model_angle.configured;
}

void model_init(void)
{
    model_result = (uint32_t)electrical_angle_init(&model_angle, &model_config);
    publish();
}

void model_update(void)
{
    model_result = (uint32_t)electrical_angle_update(&model_angle, model_position,
                                                    model_velocity, model_age);
    publish();
}

void model_update_with_trig(void)
{
    model_result = (uint32_t)electrical_angle_update_with_trig(&model_angle, model_position,
        model_velocity, model_age, model_trig, &model_trig_context);
    publish();
}

void model_null_trig(void)
{
    model_result = (uint32_t)electrical_angle_update_with_trig(&model_angle, model_position,
        model_velocity, model_age, NULL, &model_trig_context);
    publish();
}

void model_null_angle_with_trig(void)
{
    model_result = (uint32_t)electrical_angle_update_with_trig(NULL, model_position,
        model_velocity, model_age, model_trig, &model_trig_context);
    publish();
}

void model_software_trig(void)
{
    float sine = 23.0f;
    float cosine = -17.0f;
    float *sine_pointer = model_pointer_mode == 1U ? NULL : &sine;
    float *cosine_pointer = model_pointer_mode == 2U ? NULL : &cosine;
    if (model_pointer_mode == 3U) {
        cosine_pointer = &sine;
    }
    model_result = (uint32_t)electrical_angle_sincos_software(&model_trig_context,
        model_position, sine_pointer, cosine_pointer);
    model_output[0] = model_position;
    model_output[1] = sine;
    model_output[2] = cosine;
}

void model_reset(void)
{
    electrical_angle_reset(&model_angle);
    publish();
}

void model_null_arguments(void)
{
    model_result = (uint32_t)electrical_angle_init(NULL, &model_config);
    model_result |= (uint32_t)electrical_angle_update(NULL, 0.0f, 0.0f, 0.0f);
    electrical_angle_reset(NULL);
    publish();
}

void model_null_config(void)
{
    model_result = (uint32_t)electrical_angle_init(&model_angle, NULL);
    publish();
}

void model_alias_config(void)
{
    model_result = (uint32_t)electrical_angle_init(&model_angle, &model_angle.config);
    publish();
}

void model_second_instance(void)
{
    ElectricalAngleConfig other = { 7U, -1, 0.25f, 0.0f, 3000.0f };
    model_result = (uint32_t)electrical_angle_init(&model_second_angle, &other);
    model_result &= (uint32_t)electrical_angle_update(&model_second_angle, 0.125f,
                                                      0.0f, 0.0f);
    publish();
}

void model_second_instance_with_trig(void)
{
    ElectricalAngleConfig other = { 7U, -1, 0.25f, 0.0f, 3000.0f };
    model_result = (uint32_t)electrical_angle_init(&model_second_angle, &other);
    model_result &= (uint32_t)electrical_angle_update_with_trig(&model_second_angle,
        0.125f, 0.0f, 0.0f, model_trig, &model_second_trig_context);
    model_second_output[0] = model_second_angle.output.electrical_turns;
    model_second_output[1] = model_second_angle.output.sin_theta;
    model_second_output[2] = model_second_angle.output.cos_theta;
    model_second_valid = (uint32_t)model_second_angle.output.valid;
    publish();
}
