#ifndef BLDC_G431_COMPUTE_COMPARISON_H
#define BLDC_G431_COMPUTE_COMPARISON_H

#include "compute_probe.h"

#define OBSERVE_COMPUTE_COMPARISON_SCHEMA (1U)
#define OBSERVE_COMPUTE_COMPARISON_SWEEP (257U)

/* 同一合成输入的两份独立算法实例；结果不构成功率或实时截止许可。 */
typedef struct {
    uint32_t schema_version; /* 主机必须核对版本，不能按旧单后端布局解码。 */
    uint32_t request_sequence; /* 唯一显式请求身份。 */
    ObserveComputeProbeState state; /* 完整配对和全周扫描后才 COMPLETE。 */
    ObserveComputeProbeFailure failure; /* 首个失败原因。 */
    uint32_t completed_steps; /* 两后端合计 192 个算法算例。 */
    uint32_t sweep_count; /* 全周 256 等分点和 1 的前一浮点数，共 257 点。 */
    float max_sine_error; /* 全周硬件相对软件 sin 的最大绝对误差。 */
    float max_cosine_error; /* 全周硬件相对软件 cos 的最大绝对误差。 */
    float max_norm_error; /* 全周硬件 sin²+cos² 相对 1 的最大误差。 */
    float max_phasor_error; /* 配对正式角度路径的最大分量误差。 */
    float max_duty_error; /* 同输入和独立积分历史下三相 duty 的最大误差。 */
    ObserveComputeProbeResult software; /* 完成或失败时冻结的软件统计。 */
    ObserveComputeProbeResult cordic; /* 完成或失败时冻结的硬件统计。 */
} ObserveComputeComparisonResult;

typedef struct {
    ObserveComputeComparisonResult result; /* 公开的终态统计。 */
    ObserveComputeProbe software; /* 软件后端独立历史。 */
    ObserveComputeProbe cordic; /* 硬件后端独立历史。 */
    ElectricalAngleTrig hardware_trig; /* 专属硬件适配，失败不得回退软件。 */
    void *hardware_context; /* 只由后台访问。 */
} ObserveComputeComparison;

void observe_compute_comparison_init(ObserveComputeComparison *comparison);
bool observe_compute_comparison_start(ObserveComputeComparison *comparison,
    uint32_t request, uint32_t clock_hz, uint32_t max_elapsed_cycles, bool permitted,
    ElectricalAngleTrig hardware_trig, void *hardware_context);
/* 每轮至多一个带计时算例或一个全周对照点；不等待中断、不操作功率。 */
bool observe_compute_comparison_step(ObserveComputeComparison *comparison, bool permitted,
    ObserveComputeProbeStampRead stamp_read, void *stamp_context);
bool observe_compute_comparison_abort(ObserveComputeComparison *comparison,
    ObserveComputeProbeFailure reason);
bool observe_compute_comparison_revoke(ObserveComputeComparison *comparison);

#endif
