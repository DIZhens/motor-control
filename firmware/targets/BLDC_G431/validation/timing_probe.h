#ifndef BLDC_G431_TIMING_PROBE_H
#define BLDC_G431_TIMING_PROBE_H

#include <stdbool.h>
#include <stdint.h>

/* 仅供无功率时序验证；编号与台架 mailbox 命令一致。 */
typedef enum {
    TIMING_PROBE_CASE_IMMEDIATE = 10,
    TIMING_PROBE_CASE_BEFORE = 11,
    TIMING_PROBE_CASE_AFTER = 12,
    TIMING_PROBE_CASE_CROSS_CYCLE = 13,
    TIMING_PROBE_CASE_CANCEL = 14
} TimingProbeCase;

/* PASS 只说明返回值、preload 读回及 MOE，不证明 active compare 同步更新。 */
typedef enum {
    TIMING_PROBE_PASS = 1,
    TIMING_PROBE_PRECONDITION = 2,
    TIMING_PROBE_TIMEOUT = 3,
    TIMING_PROBE_MISMATCH = 4
} TimingProbeOutcome;

/* 顺序 MMIO 读数，不是多个寄存器同一时刻的原子快照。 */
typedef struct {
    uint32_t cycles; /* DWT 周期数，可按 uint32 差值处理回绕。 */
    uint32_t cnt;
    uint32_t cr1;
    uint32_t sr;
    uint32_t bdtr;
    uint32_t ccr1; /* 开启 preload 时读回的是预装载值。 */
    uint32_t ccr2;
    uint32_t ccr3;
} TimingProbeObservation;

/* ADC consumer 唯一写者；读取前后 sequence 相同且为偶数才可使用。
 * 未到达的观察点保持全零，由 stage_called / second_stage_called 区分。
 * cleanup 总会采集；after 在最后 safe-off 前采集，避免覆盖错误写入证据。 */
typedef struct {
    uint32_t sequence;
    uint32_t request_sequence;
    uint32_t case_id;
    uint32_t outcome;
    uint32_t wait_iterations;
    uint32_t elapsed_cycles;
    uint32_t stage_called;
    uint32_t stage_accepted;
    uint32_t second_stage_called;
    uint32_t second_stage_accepted;
    TimingProbeObservation entry;
    TimingProbeObservation before;
    TimingProbeObservation after;
    TimingProbeObservation second_after;
    TimingProbeObservation cleanup;
} TimingProbeCapture;

extern volatile TimingProbeCapture g_target_validation_timing;

/* 仅 ADC consumer 可调用，不可重入；内部静态工作区由该 consumer 独占。
 * 必须由一次新的 sample-ready callback 调用，且本拍尚未 safe_off。
 * eligible 汇总当前采样、保护和低母线前置条件；false 仍记录拒绝及清理。
 * 不屏蔽中断等待、不使能功率、不直接修改 TIM1 / DWT 时基寄存器。 */
TimingProbeOutcome timing_probe_run(uint32_t request_sequence, uint32_t case_id, bool eligible);

#endif /* BLDC_G431_TIMING_PROBE_H */
