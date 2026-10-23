#ifndef BLDC_G431_TARGET_OBSERVE_RUNTIME_H
#define BLDC_G431_TARGET_OBSERVE_RUNTIME_H

#include "../../runtime/target_runtime.h"

#include "../timing/current_capture.h"
#include "../timing/compute_comparison.h"
#include "../timing/stage_budget.h"
/* 当前 Observe 入口所有可返回异常的进入计数，用于后台计时的抢占分类。 */
extern volatile uint32_t g_target_runtime_irq_entries[8];
/*
 * Observe 专用无功率诊断，不属于 Motor 命令或 UART 协议。
 * 主机仅写 request 一个非零新序号；ADC 边界消费，重复序号不重放。
 * capture 仅在开始/完成/失败时锁存；读取前后 sequence 相同且为偶数才一致。
 * ACTIVE 发布不代表完整窗口；只有 COMPLETE 的 2048 点统计可用于电流对照。
 */
extern volatile uint32_t g_target_runtime_current_request;
extern volatile uint32_t g_target_runtime_current_sequence;
extern volatile ObserveCurrentCaptureResult g_target_runtime_current_capture;

/* 后台独立计算计时：仅写一次非零新 request，不使用真实控制状态或功率许可。
 * result 使用奇偶 sequence 发布；只有 COMPLETE 才含完整固定次数统计。
 * CPU cycle 读数包含抢占与测量开销，不代表完整 ISR 或 stage 截止验收。 */
extern volatile uint32_t g_target_runtime_compute_request;
extern volatile uint32_t g_target_runtime_compute_sequence;
extern volatile ObserveComputeComparisonResult g_target_runtime_compute_result;

/* 独立试具的有限提交预算窗口；只写一次新身份，不表示实物标定或真实RUN。 */
extern volatile uint32_t g_target_runtime_stage_request;
/* 仅由 ADC IRQ 所有者读取当拍无功率诊断条件，不作为后台授权接口。 */
bool target_runtime_timing_permitted(void);

#endif
