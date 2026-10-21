/* 在 Cortex-M4 模型中执行正式估计器 C，不引入平台或控制生命周期。 */
#include "position_estimator.h"

#include <stddef.h>
#include <stdint.h>

PositionEstimator model_estimator;
PositionEstimator model_second_estimator;
PositionEstimatorConfig model_config;
float model_position;
float model_interval;
float model_velocity;
uint32_t model_result;
uint32_t model_valid;
uint32_t model_has_position;
uint32_t model_configured;

static void model_capture(void)
{
    model_velocity = model_estimator.velocity_rpm;
    model_valid = (uint32_t)model_estimator.velocity_valid;
    model_has_position = (uint32_t)model_estimator.has_position;
    model_configured = (uint32_t)model_estimator.configured;
}

void model_init(void)
{
    model_result = (uint32_t)position_estimator_init(&model_estimator, &model_config);
    model_capture();
}

void model_update(void)
{
    model_result = (uint32_t)position_estimator_update(&model_estimator, model_position,
                                                     model_interval);
    model_capture();
}

void model_reset(void)
{
    position_estimator_reset(&model_estimator);
    model_capture();
}

void model_null_config(void)
{
    model_result = (uint32_t)position_estimator_init(&model_estimator, NULL);
    model_capture();
}

void model_alias_config(void)
{
    model_result = (uint32_t)position_estimator_init(&model_estimator, &model_estimator.config);
    model_capture();
}

void model_null_arguments(void)
{
    position_estimator_reset(NULL);
    model_result = (uint32_t)position_estimator_init(NULL, &model_config);
    model_result |= (uint32_t)position_estimator_update(NULL, model_position, model_interval);
    model_capture();
}

void model_second_init(void)
{
    model_result = (uint32_t)position_estimator_init(&model_second_estimator, &model_config);
    model_capture();
}

void model_second_update(void)
{
    model_result = (uint32_t)position_estimator_update(&model_second_estimator, model_position,
                                                     model_interval);
    model_capture();
}
