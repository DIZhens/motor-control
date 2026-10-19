/* 真实 ARM 算法的无功率计时状态机测试；cycle 由可控回调替身提供，不代表实测耗时。 */
#include "compute_probe.h"

#include <stddef.h>
#include <stdint.h>

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
    uint32_t index;
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
    model_result |= (uint32_t)observe_compute_probe_abort(NULL,
        OBSERVE_COMPUTE_PROBE_FAILURE_ABORTED);
    model_result |= (uint32_t)observe_compute_probe_revoke(NULL);
    publish();
}
