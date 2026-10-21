#include "current_capture.h"

#include <math.h>
#include <stddef.h>

void observe_current_capture_init(ObserveCurrentCapture *capture)
{
    ObserveCurrentCapture empty = {0};
    if (capture != NULL) {
        *capture = empty;
    }
}

bool observe_current_capture_abort(ObserveCurrentCapture *capture,
    ObserveCurrentCaptureFailure reason)
{
    if (capture == NULL || capture->result.state != OBSERVE_CURRENT_CAPTURE_ACTIVE) {
        return false;
    }
    if (reason <= OBSERVE_CURRENT_CAPTURE_FAILURE_NONE ||
        reason > OBSERVE_CURRENT_CAPTURE_FAILURE_BUSY) {
        reason = OBSERVE_CURRENT_CAPTURE_FAILURE_ABORTED;
    }
    capture->result.failure = reason;
    capture->result.state = OBSERVE_CURRENT_CAPTURE_FAILED;
    return true;
}

bool observe_current_capture_start(ObserveCurrentCapture *capture, uint32_t request,
    uint64_t period, float offset_b_a, float offset_c_a, bool permitted)
{
    bool was_active;
    if (capture == NULL) {
        return false;
    }
    was_active = capture->result.state == OBSERVE_CURRENT_CAPTURE_ACTIVE;
    observe_current_capture_init(capture);
    capture->result.request_sequence = request;
    if (was_active) {
        /* 旧样本不得冠以新 request 身份发布；忙时不隐式开始另一窗。 */
        capture->result.state = OBSERVE_CURRENT_CAPTURE_FAILED;
        capture->result.failure = OBSERVE_CURRENT_CAPTURE_FAILURE_BUSY;
        return false;
    }
    if (request == 0U || period == 0U || !isfinite(offset_b_a) ||
        !isfinite(offset_c_a) || !permitted) {
        capture->result.state = OBSERVE_CURRENT_CAPTURE_FAILED;
        capture->result.failure = OBSERVE_CURRENT_CAPTURE_FAILURE_NOT_READY;
        return false;
    }
    capture->period_ticks = period;
    capture->result.offset_b_a = offset_b_a;
    capture->result.offset_c_a = offset_c_a;
    capture->result.state = OBSERVE_CURRENT_CAPTURE_ACTIVE;
    return true;
}

static bool accumulate(const ObserveCurrentPhaseStatistics *previous, float previous_m2,
    float sample, uint32_t count, ObserveCurrentPhaseStatistics *next, float *next_m2)
{
    float delta, delta_after, product, m2;
    if (count == 1U) {
        next->mean_a = sample;
        next->min_a = sample;
        next->max_a = sample;
        next->variance_a2 = 0.0f;
        *next_m2 = 0.0f;
        return true;
    }
    delta = sample - previous->mean_a;
    next->mean_a = previous->mean_a + delta / (float)count;
    delta_after = sample - next->mean_a;
    product = delta * delta_after;
    m2 = previous_m2 + product;
    next->min_a = sample < previous->min_a ? sample : previous->min_a;
    next->max_a = sample > previous->max_a ? sample : previous->max_a;
    next->variance_a2 = m2 / (float)(count - 1U);
    if (!isfinite(delta) || !isfinite(delta_after) || !isfinite(product) ||
        !isfinite(next->mean_a) || !isfinite(previous_m2) || previous_m2 < 0.0f ||
        !isfinite(m2) || m2 < 0.0f || !isfinite(next->variance_a2) ||
        next->variance_a2 < 0.0f) {
        return false;
    }
    *next_m2 = m2;
    return true;
}

bool observe_current_capture_update(ObserveCurrentCapture *capture, uint64_t time,
    float raw_b_a, float raw_c_a, bool valid)
{
    uint32_t count;
    ObserveCurrentPhaseStatistics next_b, next_c;
    float next_m2_b, next_m2_c;
    if (capture == NULL || capture->result.state != OBSERVE_CURRENT_CAPTURE_ACTIVE) {
        return false;
    }
    if (!valid || !isfinite(raw_b_a) || !isfinite(raw_c_a)) {
        return observe_current_capture_abort(capture,
            OBSERVE_CURRENT_CAPTURE_FAILURE_INVALID_SAMPLE);
    }
    if (capture->result.sample_count > 0U &&
        (time <= capture->result.last_sample_time ||
         time - capture->result.last_sample_time != capture->period_ticks)) {
        return observe_current_capture_abort(capture, OBSERVE_CURRENT_CAPTURE_FAILURE_TIMING);
    }
    count = capture->result.sample_count + 1U;
    if (count > OBSERVE_CURRENT_CAPTURE_SAMPLES ||
        !accumulate(&capture->result.phase_b, capture->m2_b_a2, raw_b_a, count,
            &next_b, &next_m2_b) ||
        !accumulate(&capture->result.phase_c, capture->m2_c_a2, raw_c_a, count,
            &next_c, &next_m2_c)) {
        return observe_current_capture_abort(capture, OBSERVE_CURRENT_CAPTURE_FAILURE_NUMERIC);
    }
    /* 两相均合格后一次提交，失败拍不会污染另一相的统计。 */
    capture->result.phase_b = next_b;
    capture->result.phase_c = next_c;
    capture->m2_b_a2 = next_m2_b;
    capture->m2_c_a2 = next_m2_c;
    if (count == 1U) {
        capture->result.first_sample_time = time;
    }
    capture->result.last_sample_time = time;
    capture->result.sample_count = count;
    if (count == OBSERVE_CURRENT_CAPTURE_SAMPLES) {
        capture->result.state = OBSERVE_CURRENT_CAPTURE_COMPLETE;
        return true;
    }
    return false;
}
