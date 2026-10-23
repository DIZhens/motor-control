/* 真实 ARM 双编译对照：同一正式控制算法仅切换诊断标记，不复制数值参考。 */
#include "controller_profile.h"

#include <stddef.h>
#include <stdint.h>
#include <string.h>

extern bool unmarked_current_controller_init(CurrentController *, const CurrentControllerConfig *);
extern bool unmarked_current_controller_update(CurrentController *,
    const CurrentControllerInput *, CurrentControllerOutput *);

uint32_t model_request;
uint32_t model_start_null;
uint32_t model_mark;
uint32_t model_valid;
uint32_t model_success;
uint32_t model_mode; /* 1空输入；2空输出；3空实例；4配置损坏；5历史损坏。 */
uint32_t model_fail_read;
uint32_t model_read_calls;
uint32_t model_result;
uint32_t model_public_changed_during_mark;
uint32_t model_cycles[7];
uint32_t model_irqs[7];
float model_config[9];
float model_inputs[10]; /* alpha/beta、测量sin/cos、母线、Id/Iq、dt、施加sin/cos。 */
uint32_t model_flags[5]; /* current/angle/bus/reference有效、独立施加角。 */
float model_bad_value;
float model_outputs[2][11]; /* 输出9个float及d/q积分；两种编译版本逐位对照。 */
uint32_t model_status[2][4]; /* 返回值、valid、电压限幅、积分限幅。 */
uint32_t model_words[6];
uint32_t model_timing_words[7][7];
uint64_t model_sums[7][2];
uint32_t model_sizes[2];

static CurrentController controllers[2];
static CurrentControllerOutput outputs[2];
static ObserveControllerProfileResult before_marks;
static uint32_t before_sequence;

static void prepare_reads(void)
{
    model_read_calls = 0U;
    model_public_changed_during_mark = 0U;
    before_marks = g_observe_controller_profile_result;
    before_sequence = g_observe_controller_profile_sequence;
}

static bool stamp_read(void *context, ObserveComputeProbeStamp *stamp)
{
    uint32_t *calls = (uint32_t *)context;
    uint32_t index = (*calls)++;
    const volatile uint8_t *published =
        (const volatile uint8_t *)&g_observe_controller_profile_result;
    const uint8_t *expected = (const uint8_t *)&before_marks;
    uint32_t byte;
    for (byte = 0U; byte < sizeof(before_marks); byte++) {
        if (published[byte] != expected[byte]) {
            model_public_changed_during_mark = 1U;
        }
    }
    if (before_sequence != g_observe_controller_profile_sequence) {
        model_public_changed_during_mark = 1U;
    }
    if (*calls == model_fail_read || index >= 7U) {
        return false;
    }
    stamp->cycles = model_cycles[index];
    stamp->irq_entries = model_irqs[index];
    return true;
}

static void publish(void)
{
    uint32_t row, index;
    ObserveControllerProfileResult result = g_observe_controller_profile_result;
    model_words[0] = result.request_sequence;
    model_words[1] = result.state;
    model_words[2] = result.failure;
    model_words[3] = result.call_count;
    model_words[4] = result.valid_calls;
    model_words[5] = result.rejected_calls;
    model_sizes[0] = sizeof(ObserveComputeProbeTiming);
    model_sizes[1] = sizeof(ObserveControllerProfileResult);
    for (row = 0U; row < OBSERVE_CONTROLLER_PROFILE_ROW_COUNT; row++) {
        const ObserveComputeProbeTiming *timing = &result.timing[row];
        model_timing_words[row][0] = timing->count;
        model_timing_words[row][1] = timing->min_cycles;
        model_timing_words[row][2] = timing->max_cycles;
        model_timing_words[row][3] = timing->clean_count;
        model_timing_words[row][4] = timing->clean_min_cycles;
        model_timing_words[row][5] = timing->clean_max_cycles;
        model_timing_words[row][6] = timing->irq_entries;
        model_sums[row][0] = timing->sum_cycles;
        model_sums[row][1] = timing->clean_sum_cycles;
    }
    for (index = 0U; index < 2U; index++) {
        const CurrentControllerOutput *out = &outputs[index];
        model_outputs[index][0] = out->id_a;
        model_outputs[index][1] = out->iq_a;
        model_outputs[index][2] = out->requested_vd_v;
        model_outputs[index][3] = out->requested_vq_v;
        model_outputs[index][4] = out->applied_vd_v;
        model_outputs[index][5] = out->applied_vq_v;
        model_outputs[index][6] = out->duty_a;
        model_outputs[index][7] = out->duty_b;
        model_outputs[index][8] = out->duty_c;
        model_outputs[index][9] = controllers[index].integral_d_v;
        model_outputs[index][10] = controllers[index].integral_q_v;
        model_status[index][1] = (uint32_t)out->valid;
        model_status[index][2] = (uint32_t)out->voltage_limited;
        model_status[index][3] = (uint32_t)out->integral_limited;
    }
}

void model_init(void)
{
    observe_controller_profile_init();
    prepare_reads();
    publish();
}

void model_start(void)
{
    model_result = (uint32_t)observe_controller_profile_start(model_request,
        model_start_null != 0U ? NULL : stamp_read, &model_read_calls);
    prepare_reads();
    publish();
}

void model_prepare(void)
{
    prepare_reads();
    publish();
}

void model_record_mark(void)
{
    control_current_profile_mark(model_mark);
    publish();
}

void model_finish(void)
{
    observe_controller_profile_finish(model_valid != 0U);
    publish();
}

void model_freeze(void)
{
    observe_controller_profile_freeze(model_success != 0U);
    publish();
}

void model_configure(void)
{
    CurrentControllerConfig config;
    memcpy(&config, model_config, sizeof(config));
    model_result = (uint32_t)current_controller_init(&controllers[0], &config);
    model_result &= (uint32_t)unmarked_current_controller_init(&controllers[1], &config);
    publish();
}

void model_controller(void)
{
    CurrentControllerInput input;
    uint32_t index;
    input.current_alpha_a = model_inputs[0];
    input.current_beta_a = model_inputs[1];
    input.sin_electrical_angle = model_inputs[2];
    input.cos_electrical_angle = model_inputs[3];
    input.bus_voltage_v = model_inputs[4];
    input.id_reference_a = model_inputs[5];
    input.iq_reference_a = model_inputs[6];
    input.interval_s = model_inputs[7];
    input.actuation_sin_theta = model_inputs[8];
    input.actuation_cos_theta = model_inputs[9];
    input.current_valid = model_flags[0] != 0U;
    input.angle_valid = model_flags[1] != 0U;
    input.bus_voltage_valid = model_flags[2] != 0U;
    input.reference_valid = model_flags[3] != 0U;
    input.separate_actuation_angle = model_flags[4] != 0U;
    for (index = 0U; index < 2U; index++) {
        if (model_mode == 4U) {
            controllers[index].config.kp_d_v_per_a = model_bad_value;
        } else if (model_mode == 5U) {
            controllers[index].integral_q_v = model_bad_value;
        }
        memset(&outputs[index], 0, sizeof(outputs[index]));
    }
    prepare_reads();
    model_status[0][0] = (uint32_t)current_controller_update(
        model_mode == 3U ? NULL : &controllers[0],
        model_mode == 1U ? NULL : &input, model_mode == 2U ? NULL : &outputs[0]);
    model_status[1][0] = (uint32_t)unmarked_current_controller_update(
        model_mode == 3U ? NULL : &controllers[1],
        model_mode == 1U ? NULL : &input, model_mode == 2U ? NULL : &outputs[1]);
    publish();
}
