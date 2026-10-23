#ifndef BLDC_G431_OBSERVE_CONTROLLER_PROFILE_H
#define BLDC_G431_OBSERVE_CONTROLLER_PROFILE_H

#include <stdbool.h>
#include <stdint.h>

#include "compute_probe.h"

#define OBSERVE_CONTROLLER_PROFILE_MARK_COUNT (7U)
#define OBSERVE_CONTROLLER_PROFILE_ROW_COUNT (7U)
#define OBSERVE_CONTROLLER_PROFILE_VALID_CALLS (128U)
#define OBSERVE_CONTROLLER_PROFILE_REJECTED_CALLS (32U)

typedef enum {
    OBSERVE_CONTROLLER_PROFILE_IDLE = 0, /* 尚未启动有限诊断。 */
    OBSERVE_CONTROLLER_PROFILE_ACTIVE = 1, /* 后台串行计算，公开统计仍保持启动快照。 */
    OBSERVE_CONTROLLER_PROFILE_COMPLETE = 2, /* 显式结束且固定有效/拒绝次数完整。 */
    OBSERVE_CONTROLLER_PROFILE_FAILED = 3 /* 首个失败原因及此前接受统计已冻结。 */
} ObserveControllerProfileState;

typedef enum {
    OBSERVE_CONTROLLER_PROFILE_FAILURE_NONE = 0, /* 尚无失败。 */
    OBSERVE_CONTROLLER_PROFILE_FAILURE_NOT_READY = 1, /* 请求身份或 stamp 回调非法。 */
    OBSERVE_CONTROLLER_PROFILE_FAILURE_BUSY = 2, /* 活动请求遭遇不同启动身份。 */
    OBSERVE_CONTROLLER_PROFILE_FAILURE_CLOCK = 3, /* 读取失败、停滞或差值达到半圈。 */
    OBSERVE_CONTROLLER_PROFILE_FAILURE_ORDER = 4, /* mark 重入、跳号或 finish 无对应调用。 */
    OBSERVE_CONTROLLER_PROFILE_FAILURE_INCOMPLETE = 5, /* 成功声明缺少 mark 或固定调用次数。 */
    OBSERVE_CONTROLLER_PROFILE_FAILURE_ABORTED = 6, /* 外层诊断未成功，显式结束。 */
    OBSERVE_CONTROLLER_PROFILE_FAILURE_LIMIT = 7 /* 有效/拒绝次数超过固定有限范围。 */
} ObserveControllerProfileFailure;

/* 仅 start 和终态发布；Host 读取前后相同的偶数 sequence 才能接受完整快照。 */
typedef struct {
    uint32_t request_sequence; /* 非零显式请求身份；相同身份不会重新启动。 */
    uint32_t state; /* ObserveControllerProfileState，固定32位便于调试器读取。 */
    uint32_t failure; /* ObserveControllerProfileFailure，终态保留首因。 */
    uint32_t call_count; /* 已接受的有效与预期拒绝调用总数，最多160。 */
    uint32_t valid_calls; /* 已完整统计的有效调用，成功时为128。 */
    uint32_t rejected_calls; /* 只计数不统计内部时间的预期拒绝调用，成功时为32。 */
    ObserveComputeProbeTiming timing[OBSERVE_CONTROLLER_PROFILE_ROW_COUNT];
    /* timing[0] 为 mark0->6，总段独立计差；timing[1..6] 为相邻 mark 区间。 */
} ObserveControllerProfileResult;

extern volatile ObserveControllerProfileResult g_observe_controller_profile_result;
extern volatile uint32_t g_observe_controller_profile_sequence;

/* 仅初始化或后台静止边界调用；不用于异步撤销，不操作功率或物理时钟。 */
void observe_controller_profile_init(void);

/*
 * 单个有限诊断实例，由后台串行调用；回调提供 coherent cycle/IRQ stamp。
 * 相同非零 request 忽略；ACTIVE 中不同身份锁存 BUSY，不替换已有结果。
 * 成功 start 只发布空 ACTIVE 快照，后续 mark/finish 不逐拍发布统计。
 */
bool observe_controller_profile_start(uint32_t request,
    ObserveComputeProbeStampRead stamp_read, void *stamp_context);

/*
 * CONTROL_CURRENT_PROFILE 构建的私有链接钩子；算法公共头不暴露此函数。
 * mark0 开始暂存，mark1..6 必须依序到达；只读 stamp，不累加或发布统计。
 * 读取/顺序失败暂存首因，由 finish/freeze 在算法调用之外终结与发布。
 * 回调须有界且非阻塞；此单实例不能同时供后台诊断和真实控制 ISR 使用。
 */
void control_current_profile_mark(uint32_t mark);

/*
 * 正式 controller 返回且外层 CURRENT 末 stamp 已读后调用。
 * 有效调用须完整七个 mark，全部差值通过后才一起统计；拒绝调用只计次数。
 * cycle/IRQ uint32 差支持一次回绕；调用方排除整圈歧义。
 * 每个有效区间须为正且小于2^31 cycle；IRQ差为零才归入 clean。
 * clean 只对应回调覆盖的 IRQ；这些带探针读数不是无探针 WCET 证明。
 */
void observe_controller_profile_finish(bool output_valid);

/* success=true 还需恰好128个有效与32个拒绝调用；任何未完成调用都不得成功冻结。 */
void observe_controller_profile_freeze(bool success);

#endif
