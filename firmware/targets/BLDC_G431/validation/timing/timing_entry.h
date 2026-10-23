#ifndef BLDC_G431_TIMING_ENTRY_H
#define BLDC_G431_TIMING_ENTRY_H

#include <stdbool.h>
#include <stdint.h>

enum {
    G431_TIMING_FAULT_STARTUP = 1U,
    G431_TIMING_FAULT_CLOCK = 2U,
    G431_TIMING_FAULT_CADENCE = 4U,
    G431_TIMING_FAULT_CURRENT = 8U,
    G431_TIMING_FAULT_TRIP = 16U,
    G431_TIMING_FAULT_UNEXPECTED_POWER = 32U
};

/* 仅供独立无功率计时Target使用的真实健康快照，布局64字节。
 * 不是Motor状态；合成RUN只存在于隔离试具。ADC单写，序列相同且为偶数才有效。
 * guard故障另有独立锁存量，主机必须同时检查，不能沿用此处的旧值。 */
typedef struct {
    uint32_t schema_version;
    uint32_t sample_count;
    uint32_t time_low;
    uint32_t time_high;
    uint32_t last_sample_cycles;
    uint32_t last_sample_interval_cycles;
    uint32_t faults;
    uint32_t guard_trips;
    uint32_t drive_started;
    uint32_t current_valid;
    uint32_t power_disabled;
    uint32_t bus_valid;
    float bus_voltage_v;
    uint32_t position_valid;
    uint32_t permitted;
    uint32_t reserved;
} G431TimingHealth;

extern volatile uint32_t g_target_runtime_timing_request;
extern volatile uint32_t g_target_runtime_timing_guard_faults;
extern volatile uint32_t g_target_runtime_timing_health_sequence;
extern volatile G431TimingHealth g_target_runtime_timing_health;

/* 只启动持续采样和关闭功率的诊断入口，无使能、自动重试或清故障路径。 */
void timing_entry_start(void);
void timing_entry_guard_irq(void);
bool timing_entry_permitted(void);

#endif
