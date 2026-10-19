/* 真实 ARM 算法的无功率计时状态机测试；cycle 由可控回调替身提供，不代表实测耗时。 */
#include "compute_probe.h"

#include <stddef.h>
#include <stdint.h>
#include <string.h>

ObserveComputeProbe model_probe;
ObserveComputeProbe model_second;
uint32_t model_request; /* 送给 start 的显式请求身份。 */
uint32_t model_clock_hz; /* 送给 start 的计数器频率。 */
uint32_t model_max_elapsed; /* 送给 start 的单次耗时上界。 */
uint32_t model_permitted; /* 送给 start/step 的调用方许可。 */
uint32_t model_clock_begin; /* 假时钟的第一次读数。 */
uint32_t model_clock_end; /* 假时钟的第二次读数。 */
uint32_t model_clock_fail_read; /* 第几次读取返回 false；零表示全部成功。 */
uint32_t model_clock_calls; /* 最近一次 step 的时钟回调次数。 */
uint32_t model_reason; /* 显式 abort 的原始原因枚举。 */
uint32_t model_result; /* 最近一次公共 API 的 bool 返回值。 */
uint32_t model_words[8]; /* 用固定字宽导出主状态，避免依赖 ARMCC 枚举宽度。 */
uint32_t model_case_words[6][6]; /* 每例 count/min/max/valid/电压限幅/速度限幅。 */
uint64_t model_sums[6]; /* 每例的完整 64 位 cycle 总和。 */
float model_duties[6][3]; /* 每例最近一次诊断 duty。 */
float model_phasors[4]; /* 最近一份测量角及施加角的 sin/cos。 */
float model_integrals[3]; /* 速度、电流 d/q 积分历史，用于核对保持和拒绝路径。 */
uint32_t model_stamp_cycles[16]; /* 每次分段读数的 cycle fixture。 */
uint32_t model_stamp_irqs[16]; /* 与 cycle 同边界的 IRQ 进入计数 fixture。 */
uint32_t model_segment_words[6][6][7]; /* count/min/max/clean_count/clean_min/clean_max/IRQ。 */
uint64_t model_segment_sums[6][6][2]; /* 每段总和与 clean 总和。 */
float model_case_phasors[6][4]; /* 已接受样本对应的测量及施加相量。 */
uint32_t model_trig_calls; /* 本次 step 调用后端次数。 */
uint32_t model_trig_mode; /* 0 软件；1 拒绝；2 非单位结果。 */
float model_trig_turns[2]; /* 本次 step 后端收到的角度。 */
uint32_t model_early_stats_write; /* 任一次 stamp 读取前发现结果已变化即置位。 */
static ObserveComputeProbeCaseResult previous_cases[6];

static bool stamp_read(void *context, ObserveComputeProbeStamp *stamp)
{
    uint32_t *calls = (uint32_t *)context;
    uint32_t index = (*calls)++;
    if (memcmp(previous_cases, model_probe.result.cases, sizeof(previous_cases)) != 0) {
        model_early_stats_write = 1U;
    }
    if (*calls == model_clock_fail_read || index >= 16U) {
        return false;
    }
    stamp->cycles = model_stamp_cycles[index];
    stamp->irq_entries = model_stamp_irqs[index];
    return true;
}

static bool trig_read(void *context, float turns, float *sine, float *cosine)
{
    uint32_t *calls = (uint32_t *)context;
    if (*calls < 2U) {
        model_trig_turns[*calls] = turns;
    }
    (*calls)++;
    if (model_trig_mode == 1U) {
        return false;
    }
    if (model_trig_mode == 2U) {
        *sine = 1.0f;
        *cosine = 1.0f;
        return true;
    }
    return electrical_angle_sincos_software(NULL, turns, sine, cosine);
}

static bool clock_read(void *context, uint32_t *cycles)
{
    uint32_t *calls = context;
    (*calls)++;
    if (*calls == model_clock_fail_read) {
        return false;
    }
    *cycles = *calls == 1U ? model_clock_begin : model_clock_end;
    return true;
}

static void publish(void)
{
    uint32_t index, segment;
    const ObserveComputeProbeResult *result = &model_probe.result;
    model_words[0] = result->request_sequence;
    model_words[1] = (uint32_t)result->state;
    model_words[2] = (uint32_t)result->failure;
    model_words[3] = result->clock_hz;
    model_words[4] = result->max_elapsed_cycles;
    model_words[5] = result->completed_steps;
    model_words[6] = result->next_case;
    model_words[7] = model_clock_calls;
    for (index = 0U; index < OBSERVE_COMPUTE_PROBE_CASE_COUNT; index++) {
        const ObserveComputeProbeCaseResult *item = &result->cases[index];
        model_case_words[index][0] = item->count;
        model_case_words[index][1] = item->min_cycles;
        model_case_words[index][2] = item->max_cycles;
        model_case_words[index][3] = (uint32_t)item->last_output_valid;
        model_case_words[index][4] = (uint32_t)item->last_voltage_limited;
        model_case_words[index][5] = (uint32_t)item->last_speed_limited;
        model_sums[index] = item->sum_cycles;
        model_duties[index][0] = item->last_duty[0];
        model_duties[index][1] = item->last_duty[1];
        model_duties[index][2] = item->last_duty[2];
        memcpy(model_case_phasors[index], item->last_phasor, sizeof(item->last_phasor));
        for (segment = 0U; segment < OBSERVE_COMPUTE_PROBE_SEGMENT_COUNT; segment++) {
            const ObserveComputeProbeTiming *timing = &item->segments[segment];
            model_segment_words[index][segment][0] = timing->count;
            model_segment_words[index][segment][1] = timing->min_cycles;
            model_segment_words[index][segment][2] = timing->max_cycles;
            model_segment_words[index][segment][3] = timing->clean_count;
            model_segment_words[index][segment][4] = timing->clean_min_cycles;
            model_segment_words[index][segment][5] = timing->clean_max_cycles;
            model_segment_words[index][segment][6] = timing->irq_entries;
            model_segment_sums[index][segment][0] = timing->sum_cycles;
            model_segment_sums[index][segment][1] = timing->clean_sum_cycles;
        }
    }
    model_phasors[0] = model_probe.measurement_angle.output.sin_theta;
    model_phasors[1] = model_probe.measurement_angle.output.cos_theta;
    model_phasors[2] = model_probe.actuation_angle.output.sin_theta;
    model_phasors[3] = model_probe.actuation_angle.output.cos_theta;
    model_integrals[0] = model_probe.speed.integral_a;
    model_integrals[1] = model_probe.current.integral_d_v;
    model_integrals[2] = model_probe.current.integral_q_v;
}

void model_init(void)
{
    observe_compute_probe_init(&model_probe);
    model_clock_calls = 0U;
    model_result = 0U;
    publish();
}

void model_start(void)
{
    model_result = (uint32_t)observe_compute_probe_start(&model_probe, model_request,
        model_clock_hz, model_max_elapsed, model_permitted != 0U);
    publish();
}

void model_step(void)
{
    model_clock_calls = 0U;
    model_result = (uint32_t)observe_compute_probe_step(&model_probe,
        model_permitted != 0U, clock_read, &model_clock_calls);
    publish();
}

void model_step_null_clock(void)
{
    model_clock_calls = 0U;
    model_result = (uint32_t)observe_compute_probe_step(&model_probe, true, NULL, NULL);
    publish();
}

static void prepare_profile(void)
{
    model_clock_calls = 0U;
    model_trig_calls = 0U;
    model_early_stats_write = 0U;
    memcpy(previous_cases, model_probe.result.cases, sizeof(previous_cases));
}

void model_step_profiled(void)
{
    prepare_profile();
    model_result = (uint32_t)observe_compute_probe_step_profiled(&model_probe,
        model_permitted != 0U, trig_read, &model_trig_calls, stamp_read, &model_clock_calls);
    publish();
}

void model_step_profiled_null_stamp(void)
{
    prepare_profile();
    model_result = (uint32_t)observe_compute_probe_step_profiled(&model_probe,
        true, trig_read, &model_trig_calls, NULL, NULL);
    publish();
}

void model_step_profiled_null_trig(void)
{
    prepare_profile();
    model_result = (uint32_t)observe_compute_probe_step_profiled(&model_probe,
        true, NULL, NULL, stamp_read, &model_clock_calls);
    publish();
}

void model_second_profiled(void)
{
    observe_compute_probe_init(&model_second);
    model_result = (uint32_t)observe_compute_probe_start(&model_second,
        91U, 170000000U, 20000U, true);
    prepare_profile();
    (void)observe_compute_probe_step_profiled(&model_second, true,
        trig_read, &model_trig_calls, stamp_read, &model_clock_calls);
    publish();
}

void model_abort(void)
{
    model_result = (uint32_t)observe_compute_probe_abort(&model_probe,
        (ObserveComputeProbeFailure)model_reason);
    publish();
}

void model_revoke(void)
{
    model_result = (uint32_t)observe_compute_probe_revoke(&model_probe);
    publish();
}

void model_corrupt_current(void)
{
    /* 模拟独立实例配置失效，观察计时窗口如何拒绝异常算法返回。 */
    model_probe.current.configured = false;
    publish();
}

void model_second_instance(void)
{
    observe_compute_probe_init(&model_second);
    model_result = (uint32_t)observe_compute_probe_start(&model_second, 90U,
        170000000U, 20000U, true);
    (void)observe_compute_probe_step(&model_second, true, clock_read, &model_clock_calls);
    publish();
}

void model_null_arguments(void)
{
    observe_compute_probe_init(NULL);
    model_result = (uint32_t)observe_compute_probe_start(NULL, 1U, 1U, 1U, true);
    model_result |= (uint32_t)observe_compute_probe_step(NULL, true, clock_read, NULL);
    model_result |= (uint32_t)observe_compute_probe_step_profiled(NULL, true,
        trig_read, &model_trig_calls, stamp_read, &model_clock_calls);
    model_result |= (uint32_t)observe_compute_probe_abort(NULL,
        OBSERVE_COMPUTE_PROBE_FAILURE_ABORTED);
    model_result |= (uint32_t)observe_compute_probe_revoke(NULL);
    publish();
}
