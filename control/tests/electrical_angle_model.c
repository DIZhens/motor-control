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
