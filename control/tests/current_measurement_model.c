/* 在 Cortex-M4 模型中执行正式电流处理 C；没有采样驱动和功率操作。 */
#include "current_measurement.h"

#include <stddef.h>

CurrentMeasurement model_measurement;
CurrentMeasurement model_second_measurement;
CurrentMeasurementConfig model_config;
uint32_t model_gain_enabled;
float model_signed_gain[3];
CurrentMeasurementSample model_sample;
uint32_t model_second;
uint32_t model_confirmed;
uint32_t model_result;
uint32_t model_state;
uint32_t model_failure;
uint32_t model_count;
uint32_t model_valid;
float model_offsets[3];
float model_means[3];
float model_minima[3];
float model_maxima[3];
float model_output[5];

static CurrentMeasurement *selected(void)
{
    return model_second != 0U ? &model_second_measurement : &model_measurement;
}

static void capture(void)
{
    uint32_t phase;
    CurrentMeasurement *measurement = selected();
    model_state = (uint32_t)measurement->state;
    model_failure = (uint32_t)measurement->failure;
    model_count = measurement->sample_count;
    model_valid = (uint32_t)measurement->output.valid;
    for (phase = 0U; phase < 3U; ++phase) {
        model_offsets[phase] = measurement->offset_a[phase];
        model_means[phase] = measurement->mean_a[phase];
        model_minima[phase] = measurement->minimum_a[phase];
        model_maxima[phase] = measurement->maximum_a[phase];
        model_output[phase] = measurement->output.phase_a[phase];
    }
    model_output[3] = measurement->output.alpha_a;
    model_output[4] = measurement->output.beta_a;
}

void model_init(void)
{
    uint32_t phase;
    model_config.gain_correction_enabled = model_gain_enabled != 0U;
    for (phase = 0U; phase < 3U; ++phase) {
        model_config.signed_gain[phase] = model_signed_gain[phase];
    }
    model_result = (uint32_t)current_measurement_init(selected(), &model_config);
    capture();
}

void model_legacy_config(void)
{
    /* 四字段聚合初始化的追加成员必须自动为零，保持既有名义电流行为。 */
    const CurrentMeasurementConfig legacy = { CURRENT_MEASUREMENT_PHASE_BC, 4U, 0.1f, 0.05f };
    model_result = (uint32_t)current_measurement_init(selected(), &legacy);
    capture();
}

void model_start(void)
{
    model_result = (uint32_t)current_measurement_start(selected(), model_confirmed != 0U);
    capture();
}

void model_calibrate(void)
{
    model_result = (uint32_t)current_measurement_calibrate(selected(), &model_sample,
                                                        model_confirmed != 0U);
    capture();
}

void model_process(void)
{
    model_result = (uint32_t)current_measurement_process(selected(), &model_sample);
    capture();
}

void model_abort(void)
{
    current_measurement_abort(selected());
    capture();
}

void model_reset(void)
{
    current_measurement_reset(selected());
    capture();
}

void model_select(void)
{
    capture();
}

void model_null_config(void)
{
    model_result = (uint32_t)current_measurement_init(selected(), NULL);
    capture();
}

void model_alias_config(void)
{
    model_result = (uint32_t)current_measurement_init(selected(), &selected()->config);
    capture();
}

void model_null_sample_calibrate(void)
{
    model_result = (uint32_t)current_measurement_calibrate(selected(), NULL, true);
    capture();
}

void model_null_sample_process(void)
{
    model_result = (uint32_t)current_measurement_process(selected(), NULL);
    capture();
}

void model_null_arguments(void)
{
    current_measurement_reset(NULL);
    current_measurement_abort(NULL);
    model_result = (uint32_t)current_measurement_init(NULL, &model_config);
    model_result |= (uint32_t)current_measurement_start(NULL, true);
    model_result |= (uint32_t)current_measurement_calibrate(NULL, &model_sample, true);
    model_result |= (uint32_t)current_measurement_process(NULL, &model_sample);
    capture();
}
