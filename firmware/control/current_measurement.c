#include "current_measurement.h"

#include <math.h>
#include <stddef.h>

static void clear_output(CurrentMeasurement *measurement)
{
    uint32_t phase;
    for (phase = 0U; phase < 3U; ++phase) {
        measurement->output.phase_a[phase] = 0.0f;
    }
    measurement->output.alpha_a = 0.0f;
    measurement->output.beta_a = 0.0f;
    measurement->output.valid = false;
}

static void fail(CurrentMeasurement *measurement, CurrentMeasurementFailure reason)
{
    uint32_t phase;
    measurement->state = CURRENT_MEASUREMENT_FAILED;
    measurement->failure = reason;
    for (phase = 0U; phase < 3U; ++phase) {
        measurement->offset_a[phase] = 0.0f;
    }
    clear_output(measurement);
}

void current_measurement_reset(CurrentMeasurement *measurement)
{
    uint32_t phase;
    if (measurement == NULL) {
        return;
    }
    if (measurement->state != CURRENT_MEASUREMENT_UNCONFIGURED) {
        measurement->state = CURRENT_MEASUREMENT_WAITING;
    }
    measurement->failure = CURRENT_MEASUREMENT_FAILURE_NONE;
    measurement->sample_count = 0U;
    for (phase = 0U; phase < 3U; ++phase) {
        measurement->offset_a[phase] = 0.0f;
        measurement->mean_a[phase] = 0.0f;
        measurement->minimum_a[phase] = 0.0f;
        measurement->maximum_a[phase] = 0.0f;
    }
    clear_output(measurement);
}

bool current_measurement_init(CurrentMeasurement *measurement,
                              const CurrentMeasurementConfig *config)
{
    CurrentMeasurementConfig requested = {0};
    uint32_t phase;
    if (measurement == NULL) {
        return false;
    }
    if (config != NULL) {
        requested = *config;
    }
    measurement->state = CURRENT_MEASUREMENT_UNCONFIGURED;
    current_measurement_reset(measurement);
    measurement->config = (CurrentMeasurementConfig){0};
    if ((requested.measured_phases != CURRENT_MEASUREMENT_PHASE_AB &&
         requested.measured_phases != CURRENT_MEASUREMENT_PHASE_AC &&
         requested.measured_phases != CURRENT_MEASUREMENT_PHASE_BC &&
         requested.measured_phases != CURRENT_MEASUREMENT_PHASE_ABC) ||
        requested.calibration_sample_count < 2U ||
        requested.calibration_sample_count > 16777216U ||
        !isfinite(requested.max_offset_abs_a) || requested.max_offset_abs_a < 0.0f ||
        !isfinite(requested.max_peak_to_peak_a) || requested.max_peak_to_peak_a < 0.0f) {
        return false;
    }
    if (requested.gain_correction_enabled) {
        for (phase = 0U; phase < 3U; ++phase) {
            if ((requested.measured_phases & (1U << phase)) != 0U &&
                (!isfinite(requested.signed_gain[phase]) || requested.signed_gain[phase] == 0.0f)) {
                return false;
            }
        }
    }
    measurement->config = requested;
    measurement->state = CURRENT_MEASUREMENT_WAITING;
    return true;
}

bool current_measurement_start(CurrentMeasurement *measurement, bool zero_current_confirmed)
{
    if (measurement == NULL || measurement->state != CURRENT_MEASUREMENT_WAITING ||
        !zero_current_confirmed) {
        return false;
    }
    measurement->state = CURRENT_MEASUREMENT_CALIBRATING;
    return true;
}

static bool sample_valid(const CurrentMeasurement *measurement,
                         const CurrentMeasurementSample *sample)
{
    uint32_t phase;
    if (sample == NULL || (sample->valid_phases & ~7U) != 0U ||
        (sample->valid_phases & measurement->config.measured_phases) !=
         measurement->config.measured_phases) {
        return false;
    }
    for (phase = 0U; phase < 3U; ++phase) {
        if ((measurement->config.measured_phases & (1U << phase)) != 0U &&
            !isfinite(sample->phase_a[phase])) {
            return false;
        }
    }
    return true;
}

bool current_measurement_calibrate(CurrentMeasurement *measurement,
    const CurrentMeasurementSample *sample, bool zero_current_confirmed)
{
    float means[3], minima[3], maxima[3], span;
    uint32_t phase, count;
    bool noisy = false;
    if (measurement == NULL || measurement->state != CURRENT_MEASUREMENT_CALIBRATING) {
        return false;
    }
    if (!zero_current_confirmed) {
        fail(measurement, CURRENT_MEASUREMENT_FAILURE_ZERO_CURRENT_UNCONFIRMED);
        return false;
    }
    if (!sample_valid(measurement, sample)) {
        fail(measurement, CURRENT_MEASUREMENT_FAILURE_INVALID_SAMPLE);
        return false;
    }
    count = measurement->sample_count + 1U;
    for (phase = 0U; phase < 3U; ++phase) {
        means[phase] = measurement->mean_a[phase];
        minima[phase] = measurement->minimum_a[phase];
        maxima[phase] = measurement->maximum_a[phase];
        if ((measurement->config.measured_phases & (1U << phase)) == 0U) {
            continue;
        }
        if (count == 1U) {
            means[phase] = sample->phase_a[phase];
            minima[phase] = sample->phase_a[phase];
            maxima[phase] = sample->phase_a[phase];
        } else {
            means[phase] += (sample->phase_a[phase] - means[phase]) / (float)count;
            if (sample->phase_a[phase] < minima[phase]) {
                minima[phase] = sample->phase_a[phase];
            }
            if (sample->phase_a[phase] > maxima[phase]) {
                maxima[phase] = sample->phase_a[phase];
            }
        }
        span = maxima[phase] - minima[phase];
        if (!isfinite(means[phase]) || !isfinite(span)) {
            fail(measurement, CURRENT_MEASUREMENT_FAILURE_NUMERIC);
            return false;
        }
        if (span > measurement->config.max_peak_to_peak_a) {
            noisy = true;
        }
    }
    /* 一拍全部验证后一起提交，坏相不能留下半拍更新的诊断。 */
    for (phase = 0U; phase < 3U; ++phase) {
        measurement->mean_a[phase] = means[phase];
        measurement->minimum_a[phase] = minima[phase];
        measurement->maximum_a[phase] = maxima[phase];
    }
    measurement->sample_count = count;
    if (noisy) {
        fail(measurement, CURRENT_MEASUREMENT_FAILURE_NOISE_LIMIT);
        return false;
    }
    if (count != measurement->config.calibration_sample_count) {
        return false;
    }
    for (phase = 0U; phase < 3U; ++phase) {
        if (fabsf(means[phase]) > measurement->config.max_offset_abs_a) {
            fail(measurement, CURRENT_MEASUREMENT_FAILURE_OFFSET_LIMIT);
            return false;
        }
    }
    for (phase = 0U; phase < 3U; ++phase) {
        measurement->offset_a[phase] = means[phase];
    }
    measurement->state = CURRENT_MEASUREMENT_READY;
    return true;
}

void current_measurement_abort(CurrentMeasurement *measurement)
{
    if (measurement != NULL && measurement->state != CURRENT_MEASUREMENT_UNCONFIGURED &&
        measurement->state != CURRENT_MEASUREMENT_FAILED) {
        fail(measurement, CURRENT_MEASUREMENT_FAILURE_ABORTED);
    }
}

bool current_measurement_process(CurrentMeasurement *measurement,
                                 const CurrentMeasurementSample *sample)
{
    float phase_a[3], alpha_a, beta_a;
    uint32_t phase;
    if (measurement == NULL) {
        return false;
    }
    clear_output(measurement);
    if (measurement->state != CURRENT_MEASUREMENT_READY ||
        !sample_valid(measurement, sample)) {
        return false;
    }
    for (phase = 0U; phase < 3U; ++phase) {
        phase_a[phase] = 0.0f;
        if ((measurement->config.measured_phases & (1U << phase)) != 0U) {
            phase_a[phase] = sample->phase_a[phase] - measurement->offset_a[phase];
            if (!isfinite(phase_a[phase])) {
                return false;
            }
            if (measurement->config.gain_correction_enabled) {
                phase_a[phase] *= measurement->config.signed_gain[phase];
                if (!isfinite(phase_a[phase])) {
                    return false;
                }
            }
        }
    }
    if (measurement->config.measured_phases == CURRENT_MEASUREMENT_PHASE_AB) {
        phase_a[2] = -phase_a[0] - phase_a[1];
    } else if (measurement->config.measured_phases == CURRENT_MEASUREMENT_PHASE_AC) {
        phase_a[1] = -phase_a[0] - phase_a[2];
    } else if (measurement->config.measured_phases == CURRENT_MEASUREMENT_PHASE_BC) {
        phase_a[0] = -phase_a[1] - phase_a[2];
    }
    /* 分别缩放后求和，避免 2*A 或 B-C 的中间值先溢出。 */
    alpha_a = (2.0f / 3.0f) * phase_a[0] - phase_a[1] / 3.0f - phase_a[2] / 3.0f;
    beta_a = 0.5773502691896258f * phase_a[1] - 0.5773502691896258f * phase_a[2];
    if (!isfinite(phase_a[0]) || !isfinite(phase_a[1]) || !isfinite(phase_a[2]) ||
        !isfinite(alpha_a) || !isfinite(beta_a)) {
        return false;
    }
    for (phase = 0U; phase < 3U; ++phase) {
        measurement->output.phase_a[phase] = phase_a[phase];
    }
    measurement->output.alpha_a = alpha_a;
    measurement->output.beta_a = beta_a;
    measurement->output.valid = true;
    return true;
}
