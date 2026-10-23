/* ARM 模型直接执行 Observe 电流统计模块，不链接硬件或业务层。 */
#include "current_capture.h"

#include <stddef.h>
#include <stdint.h>

ObserveCurrentCapture model_capture;
ObserveCurrentCapture model_second;
uint32_t model_request;
uint64_t model_period;
uint64_t model_time;
float model_raw_b;
float model_raw_c;
float model_offset_b;
float model_offset_c;
uint32_t model_permitted;
uint32_t model_valid;
uint32_t model_reason;
uint32_t model_result;
uint32_t model_words[4];
uint64_t model_times[2];
float model_output[12];
float model_batch_samples[512][2];
uint32_t model_batch_count;
uint32_t model_batch_transitions;

static void publish(void)
{
    const ObserveCurrentCaptureResult *r = &model_capture.result;
    model_words[0] = r->request_sequence;
    model_words[1] = (uint32_t)r->state;
    model_words[2] = (uint32_t)r->failure;
    model_words[3] = r->sample_count;
    model_times[0] = r->first_sample_time;
    model_times[1] = r->last_sample_time;
    model_output[0] = r->offset_b_a;
    model_output[1] = r->offset_c_a;
    model_output[2] = r->phase_b.mean_a;
    model_output[3] = r->phase_b.min_a;
    model_output[4] = r->phase_b.max_a;
    model_output[5] = r->phase_b.variance_a2;
    model_output[6] = r->phase_c.mean_a;
    model_output[7] = r->phase_c.min_a;
    model_output[8] = r->phase_c.max_a;
    model_output[9] = r->phase_c.variance_a2;
    model_output[10] = model_capture.m2_b_a2;
    model_output[11] = model_capture.m2_c_a2;
}

void model_init(void)
{
    observe_current_capture_init(&model_capture);
    model_result = 0U;
    publish();
}

void model_start(void)
{
    model_result = (uint32_t)observe_current_capture_start(&model_capture, model_request,
        model_period, model_offset_b, model_offset_c, model_permitted != 0U);
    publish();
}

void model_update(void)
{
    model_result = (uint32_t)observe_current_capture_update(&model_capture, model_time,
        model_raw_b, model_raw_c, model_valid != 0U);
    publish();
}

void model_abort(void)
{
    model_result = (uint32_t)observe_current_capture_abort(&model_capture,
        (ObserveCurrentCaptureFailure)model_reason);
    publish();
}

void model_batch(void)
{
    uint32_t i;
    model_batch_transitions = 0U;
    for (i = 0U; i < model_batch_count && i < 512U; i++) {
        model_batch_transitions += (uint32_t)observe_current_capture_update(&model_capture,
            model_time, model_batch_samples[i][0], model_batch_samples[i][1], true);
        model_time += model_period;
    }
    publish();
}

void model_corrupt_m2(void)
{
    model_capture.m2_b_a2 = model_raw_b;
    model_capture.m2_c_a2 = model_raw_c;
    publish();
}

void model_second_instance(void)
{
    observe_current_capture_init(&model_second);
    model_result = (uint32_t)observe_current_capture_start(&model_second, 99U, 2U,
        0.02f, -0.01f, true);
    (void)observe_current_capture_update(&model_second, 70U, 0.21f, -0.32f, true);
    (void)observe_current_capture_update(&model_second, 72U, 0.22f, -0.31f, true);
    publish();
}

void model_null_arguments(void)
{
    observe_current_capture_init(NULL);
    model_result = (uint32_t)observe_current_capture_start(NULL, 1U, 1U, 0.0f, 0.0f, true);
    model_result |= (uint32_t)observe_current_capture_update(NULL, 0U, 0.0f, 0.0f, true);
    model_result |= (uint32_t)observe_current_capture_abort(NULL,
        OBSERVE_CURRENT_CAPTURE_FAILURE_ABORTED);
    publish();
}
