#ifndef BLDC_G431_OBSERVE_CURRENT_CAPTURE_H
#define BLDC_G431_OBSERVE_CURRENT_CAPTURE_H

#include <stdbool.h>
#include <stdint.h>

#define OBSERVE_CURRENT_CAPTURE_SAMPLES (4096U)

typedef enum {
    OBSERVE_CURRENT_CAPTURE_IDLE = 0, /* 尚未收到本次统计请求。 */
    OBSERVE_CURRENT_CAPTURE_ACTIVE = 1, /* 接收同步电流样本，尚无完整窗口。 */
    OBSERVE_CURRENT_CAPTURE_COMPLETE = 2, /* 完整固定窗口已冻结。 */
    OBSERVE_CURRENT_CAPTURE_FAILED = 3 /* 请求失败；只保留已接受样本的诊断。 */
} ObserveCurrentCaptureState;

typedef enum {
    OBSERVE_CURRENT_CAPTURE_FAILURE_NONE = 0, /* 无失败。 */
    OBSERVE_CURRENT_CAPTURE_FAILURE_NOT_READY = 1, /* 启动许可或启动参数不合格。 */
    OBSERVE_CURRENT_CAPTURE_FAILURE_ABORTED = 2, /* 调用方撤销进行中的窗口。 */
    OBSERVE_CURRENT_CAPTURE_FAILURE_INVALID_SAMPLE = 3, /* 样本无效或电流不是有限数。 */
    OBSERVE_CURRENT_CAPTURE_FAILURE_TIMING = 4, /* 重复、回退、跨越或不连续的采样时间。 */
    OBSERVE_CURRENT_CAPTURE_FAILURE_NUMERIC = 5, /* 统计中间量溢出或方差不合法。 */
    OBSERVE_CURRENT_CAPTURE_FAILURE_BUSY = 6 /* 活动窗口遭遇新请求；两次请求均不继续。 */
} ObserveCurrentCaptureFailure;

typedef struct {
    float mean_a; /* 原始名义电流均值 [A]，不扣除零偏。 */
    float min_a; /* 原始名义电流最小值 [A]。 */
    float max_a; /* 原始名义电流最大值 [A]。 */
    float variance_a2; /* 原始名义电流无偏样本方差 [A^2]；不足两点时为零。 */
} ObserveCurrentPhaseStatistics;

typedef struct {
    uint32_t request_sequence; /* 本次请求身份；成功启动要求非零。 */
    ObserveCurrentCaptureState state; /* COMPLETE 才能作为完整窗口使用。 */
    ObserveCurrentCaptureFailure failure; /* 首个失败原因，失败后不再覆盖。 */
    uint32_t sample_count; /* 已接受的连续合法样本数，最大为 4096。 */
    uint64_t first_sample_time; /* 首个已接受样本的控制时间，单位由装配方说明。 */
    uint64_t last_sample_time; /* 最后一个已接受样本的控制时间。 */
    float offset_b_a; /* start 时复制冻结的 B 相零偏 [A]，本模块不学习也不应用。 */
    float offset_c_a; /* start 时复制冻结的 C 相零偏 [A]。 */
    ObserveCurrentPhaseStatistics phase_b; /* B 相原始名义电流统计。 */
    ObserveCurrentPhaseStatistics phase_c; /* C 相原始名义电流统计。 */
} ObserveCurrentCaptureResult;

/* Observe 专用串行统计器；不含 HCC、硬件、功率操作或业务命令。 */
typedef struct {
    ObserveCurrentCaptureResult result; /* 进行中的诊断或已经冻结的结果。 */
    uint64_t period_ticks; /* 相邻已接受样本必须严格相等的正时间间隔。 */
    float m2_b_a2; /* B 相 Welford 平方差累加量 [A^2]。 */
    float m2_c_a2; /* C 相 Welford 平方差累加量 [A^2]。 */
} ObserveCurrentCapture;

/* 清除请求及统计，回到 IDLE；空指针无效果。 */
void observe_current_capture_init(ObserveCurrentCapture *capture);

/*
 * 只有 request!=0、period>0、两个 offset 有限且 permitted=true 才进入 ACTIVE。
 * 非 ACTIVE 时先清除旧结果；不合格请求记录本 request 并成为 FAILED/NOT_READY。
 * 遇 ACTIVE 时中止旧窗，清除旧样本并记录新 request 的 FAILED/BUSY，不启动替代窗口。
 * permitted 由 Target 当拍确认；本模块不从采样值推断功率状态或偏置可信。
 * 返回是否已成功进入 ACTIVE；空指针返回 false。
 */
bool observe_current_capture_start(ObserveCurrentCapture *capture, uint32_t request,
    uint64_t period, float offset_b_a, float offset_c_a, bool permitted);

/*
 * 只在 ACTIVE 接收原始名义 B/C 电流；valid=false 或非有限输入以 INVALID_SAMPLE 失败。
 * 首拍时间可为零；后续要求 time>last 且 time-last==period，不接受时间回绕。
 * 两相均计算合法后才提交本拍，任何统计异常以 NUMERIC 失败并保留上拍诊断。
 * 第 4096 点成为 COMPLETE；其他状态下不读取输入、不改变结果。
 * 返回仅表示本次调用发生 ACTIVE→COMPLETE/FAILED，适合触发一次结果发布。
 */
bool observe_current_capture_update(ObserveCurrentCapture *capture, uint64_t time,
    float raw_b_a, float raw_c_a, bool valid);

/*
 * 仅 ACTIVE 转成 FAILED，保留已接受样本与冻结偏置；返回是否发生状态变化。
 * NONE 或枚举范围外 reason 统一记录 ABORTED；已结束窗口保持首因和结果不变。
 */
bool observe_current_capture_abort(ObserveCurrentCapture *capture,
    ObserveCurrentCaptureFailure reason);

#endif
