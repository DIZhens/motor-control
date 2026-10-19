#ifndef BLDC_G431_TARGET_RUNTIME_H
#define BLDC_G431_TARGET_RUNTIME_H

#include <stdint.h>

#include "current_capture.h"
#include "compute_comparison.h"

/* Observe 入口的锁存诊断；当前没有清除或功率使能路径。 */
enum {
    TARGET_RUNTIME_FAULT_STARTUP = 1U << 0, /* 装配或能力准备失败。 */
    TARGET_RUNTIME_FAULT_CURRENT = 1U << 1, /* 同步电流采集结果无效。 */
    TARGET_RUNTIME_FAULT_CADENCE = 1U << 2, /* 采样间隔异常或采样丢失。 */
    TARGET_RUNTIME_FAULT_TRIP = 1U << 3, /* 保护快照不可用，或存在保护条件、锁存原因。 */
    TARGET_RUNTIME_FAULT_UNEXPECTED_POWER = 1U << 4, /* 功率应关闭时检测到 MOE 使能。 */
    TARGET_RUNTIME_FAULT_CLOCK = 1U << 5 /* 时钟配置异常或 DWT 计数未推进。 */
};

/* TIM6 独立写入；ADC 不再执行时，不能靠旧 Motor 快照判断健康。 */
extern volatile uint32_t g_target_runtime_guard_faults;
/* 当前 Observe 入口所有可返回异常的进入计数，用于后台计时的抢占分类。 */
extern volatile uint32_t g_target_runtime_irq_entries[8];

/*
 * Observe 专用无功率诊断，不属于 Motor 命令或 UART 协议。
 * 主机仅写 request 一个非零新序号；ADC 边界消费，重复序号不重放。
 * capture 仅在开始/完成/失败时锁存；读取前后 sequence 相同且为偶数才一致。
 * ACTIVE 发布不代表完整窗口；只有 COMPLETE 的 4096 点统计可用于电流对照。
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

/* 一次装配，默认关断；不包含 stage、enable 或保护注入命令。 */
void target_runtime_start(void);
/* 主循环至多收/发一个 UART 字节、计算一个诊断算例，不等待外设。 */
void target_runtime_service(void);
/* 复用已配置的 TIM6_DAC IRQ，仅处理 TIM6 update。 */
void target_runtime_guard_irq(void);

#endif
