#ifndef BLDC_G431_STAGE_BUDGET_H
#define BLDC_G431_STAGE_BUDGET_H

#include <stdbool.h>
#include <stdint.h>

#include "control_time.h"
#include "electrical_angle.h"

#define G431_STAGE_BUDGET_SAMPLES (80U)
#define G431_STAGE_BUDGET_ROW_COUNT (6U)

typedef enum {
    G431_STAGE_BUDGET_IDLE = 0, /* 默认不执行诊断。 */
    G431_STAGE_BUDGET_READY = 1, /* 准备拍已完成隔离试具，等待下一真实采样。 */
    G431_STAGE_BUDGET_ACTIVE = 2, /* ADC 所有者逐拍执行有限窗口。 */
    G431_STAGE_BUDGET_COMPLETE = 3, /* 全部预定拍完成，结果冻结。 */
    G431_STAGE_BUDGET_FAILED = 4 /* 首个失败锁存，不自动重试。 */
} G431StageBudgetState;

typedef enum {
    G431_STAGE_BUDGET_FAILURE_NONE = 0, /* 尚无失败。 */
    G431_STAGE_BUDGET_FAILURE_NOT_READY = 1, /* 现场许可或装配条件不成立。 */
    G431_STAGE_BUDGET_FAILURE_ABORTED = 2, /* Stop 或调用方显式取消。 */
    G431_STAGE_BUDGET_FAILURE_BUSY = 3, /* 活动期间出现新的请求身份。 */
    G431_STAGE_BUDGET_FAILURE_CLOCK = 4, /* 时钟、计数方向或原始锚点异常。 */
    G431_STAGE_BUDGET_FAILURE_CONTROL = 5, /* 同源控制链未得到完整有效输出。 */
    G431_STAGE_BUDGET_FAILURE_STAGE = 6, /* 真实 HCC stage 拒绝或错过截止。 */
    G431_STAGE_BUDGET_FAILURE_POWER = 7, /* 任一读点 MOE 打开或尝试 enable。 */
    G431_STAGE_BUDGET_FAILURE_INCOMPLETE = 8 /* 完整窗口没有覆盖预定慢拍。 */
} G431StageBudgetFailure;

/* 原 Platform callback 锚点不得在诊断中重置；其他寄存器是依次读取的现场值。 */
typedef struct {
    uint32_t cycles; /* 连续 DWT cycle；读取本字段应在其他 MMIO 读数之后。 */
    uint32_t timer_count; /* 当前 TIM1 CNT。 */
    uint32_t timer_cr1; /* 当前 TIM1 CR1，用于确认下数半周和时基。 */
    uint32_t timer_bdtr; /* 当前 TIM1 BDTR，MOE 必须为零。 */
    uint32_t callback_start_cycles; /* 本拍 Platform 原始 DWT 锚点。 */
    uint32_t callback_entry_count; /* 本拍 Platform 原始 CNT 锚点。 */
} G431StageBudgetStamp;

/* 只读当拍时基与现场功率事实；不修改计时器或 DWT。 */
typedef bool (*G431StageBudgetStampRead)(void *context, G431StageBudgetStamp *stamp);

typedef struct {
    uint32_t count; /* 此路径已完整接受的拍数。 */
    uint32_t min_cycles; /* 原始 cycle 差的最小值。 */
    uint32_t max_cycles; /* 最大值，包含测量入口开销。 */
    uint32_t sum_cycles; /* 最多 80*4250，固定上界下不会溢出。 */
} G431StageBudgetTiming;

/* 只在 READY 和终态发布；读取前后 sequence 同值且为偶数才可使用。 */
typedef struct {
    uint32_t schema_version; /* 本私有诊断布局版本，固定为 1。 */
    uint32_t request_sequence; /* 显式非零请求身份。 */
    uint32_t state; /* G431StageBudgetState。 */
    uint32_t failure; /* G431StageBudgetFailure；只保存首个失败。 */
    uint32_t completed_samples; /* 成功调用同源 step 且真实 stage 被接受的完整拍数。 */
    uint32_t stage_calls; /* 诊断端口真实调用 HCC stage 的次数。 */
    uint32_t stage_accepted; /* 真实 HCC 返回 true 的次数，不能由模拟端口代替。 */
    uint32_t enable_attempts; /* 必须始终为零；端口永远拒绝。 */
    uint32_t observation_refreshes; /* 同源 step 中实际调用合成 position latest 的拍数。 */
    uint32_t speed_updates; /* 正式速度 PI 的 last_speed_time 实际发生更新的拍数。 */
    uint32_t min_remaining_counts; /* stage 末 CNT 距 1700 截止线的最小读数。 */
    uint32_t last_shadow_faults; /* 隔离模拟 Motor 的故障，不是现场业务故障。 */
    uint32_t limited_samples; /* 正式 SVPWM 电压圆限幅实际触发的拍数。 */
    uint32_t fast_samples; /* 同拍未更新速度、位置的普通快拍数。 */
    uint32_t slow_samples; /* 同拍更新速度或位置的慢拍数，两者重合只记一拍。 */
    uint32_t last_elapsed_cycles; /* 最近一次真实 stage 返回后相对原 callback 锚点的耗时，含失败拍。 */
    uint32_t last_step_cycles; /* 同一 stage 返回点相对本诊断 step 入口的耗时。 */
    uint32_t last_timer_count; /* 同一读点 TIM1 CNT，不用成功点统计掩盖拒绝原因。 */
    uint32_t last_entry_count; /* 该拍 Platform 原始 CNT 锚点。 */
    uint32_t last_cr1; /* 同一读点 TIM1 CR1。 */
    uint32_t last_bdtr; /* 同一读点 TIM1 BDTR。 */
    G431StageBudgetTiming rows[G431_STAGE_BUDGET_ROW_COUNT]; /* 0原锚点/1step入口/2速度拍/3观测刷新拍/4普通快拍/5慢拍；分类行不能相加充当总时长。 */
} G431StageBudgetResult;

extern volatile uint32_t g_target_stage_budget_sequence;
extern volatile uint32_t g_target_stage_budget_active; /* READY/ACTIVE 为1，最后一步发布；只读提示。 */
extern volatile G431StageBudgetResult g_target_stage_budget_result;

/* 启动硬件前初始化并建立一次隔离试具模板；只读 HCC facts，不调用功率动作。
 * trig 由装配方串行持有，NULL 选择软件参考；之后不再重建或改写模板。 */
void g431_stage_budget_init(ElectricalAngleTrig trig, void *trig_context,
    G431StageBudgetStampRead stamp_read, void *stamp_context);

/* 仅 ADC 所有者在独立准备拍复制合成试具模板；不重新配参、校零或计算初值。
 * 该拍不测量、不 stage。
 * 真实标定、Motor、观测和使能许可均不改变。同请求忽略；READY/ACTIVE 时拒绝
 * 且不改任何对象，由同一 ADC 所有者调 cancel(BUSY)。 */
bool g431_stage_budget_prepare(uint32_t request, bool permitted);

/* 仅 ADC 同步 consumer 可调用；每拍至多一次同源 ControlRuntime step。
 * 返回 true 表示本拍执行过诊断（包括拒绝/清理），false 表示没有活动请求。
 * 合成 RUN 与 power_disabled=false 只属于私有试具，绝不作为现场硬件事实发布。 */
bool g431_stage_budget_step(ControlTime now, bool permitted);

/* 仅 ADC 所有者调用；立即实际 safe_off 并冻结，终态/未准备时无副作用。 */
void g431_stage_budget_cancel(G431StageBudgetFailure reason);

#endif
