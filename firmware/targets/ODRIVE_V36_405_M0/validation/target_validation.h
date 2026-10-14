#ifndef ODRIVE_V36_405_M0_TARGET_VALIDATION_H
#define ODRIVE_V36_405_M0_TARGET_VALIDATION_H

#include <stdbool.h>
#include <stdint.h>
#include "realtime_motor_io.h"
#include "position_observation.h"
#include "bus_observation.h"

/* 仅用于本 Target 的台架验收，不是 Motor Command 或公共 HCC。 */
typedef enum {
    TARGET_VALIDATION_COMMAND_NONE = 0,
    TARGET_VALIDATION_COMMAND_SAFE_OFF = 1,
    TARGET_VALIDATION_COMMAND_STAGE_ONLY = 2,
    TARGET_VALIDATION_COMMAND_PWM_BURST = 3,
    TARGET_VALIDATION_COMMAND_CAPTURE_CURRENT = 4
} TargetValidationCommand;

/* 上电默认 OBSERVE；结束后回到 OBSERVE，不自动重复或重新使能。 */
typedef enum {
    TARGET_VALIDATION_STATE_OBSERVE = 0,
    TARGET_VALIDATION_STATE_STAGING = 1,
    TARGET_VALIDATION_STATE_POWERED = 2,
    TARGET_VALIDATION_STATE_CAPTURING_CURRENT = 3
} TargetValidationState;

/* 台架动作的结束/拒绝原因；不代替系统 Fault policy。 */
typedef enum {
    TARGET_VALIDATION_RESULT_NONE = 0,
    TARGET_VALIDATION_RESULT_COMPLETE = 1,
    TARGET_VALIDATION_RESULT_OPERATOR_STOP = 2,
    TARGET_VALIDATION_RESULT_LOCKED = 3,
    TARGET_VALIDATION_RESULT_BAD_REQUEST = 4,
    TARGET_VALIDATION_RESULT_BUSY = 5,
    TARGET_VALIDATION_RESULT_BAD_CURRENT = 6,
    TARGET_VALIDATION_RESULT_BAD_BUS = 7,
    TARGET_VALIDATION_RESULT_STAGE_REJECTED = 8,
    TARGET_VALIDATION_RESULT_ENABLE_REJECTED = 9,
    TARGET_VALIDATION_RESULT_PLATFORM_OFF = 10
} TargetValidationResult;

#define TARGET_VALIDATION_POWER_KEY (0x4050A11EU)
#define TARGET_VALIDATION_MAX_BURST_US (10000U)
#define TARGET_VALIDATION_CURRENT_SAMPLE_COUNT (4096U)

/* 单相连续采样统计；均值/min/max 单位 A，样本方差单位 A^2，不应用零点修正。 */
typedef struct {
    float mean_a;
    float min_a;
    float max_a;
    float variance_a2;
} TargetValidationPhaseStatistics;

/*
 * 只在 MOE=0 时收集完整 B/C 事务。完成后保持结果，直到新的 CAPTURE_CURRENT。
 * request_sequence 标识所属请求；只有本次 COMPLETE 且 sample_count=4096 才是完整窗口。
 */
typedef struct {
    uint32_t request_sequence;
    uint32_t sample_count;
    ControlTime first_sample_time;
    ControlTime last_sample_time;
    TargetValidationPhaseStatistics phase_b;
    TargetValidationPhaseStatistics phase_c;
} TargetValidationCurrentStatistics;

/*
 * 调试器先写 payload，sequence 最后写。每个新 sequence 只消费一次；
 * 同一请求在消费时复制到私有状态，不保留指向 mailbox 的指针。
 * power_key 是单次操作确认，不是安全认证；复位为 0，消费后清 0。
 * CAPTURE_CURRENT 只消费 sequence/command，其余 payload 不参与采集配置。
 */
typedef struct {
    uint32_t sequence;           /* 最后写入的提交序号，0 表示没有请求。 */
    uint32_t command;            /* TargetValidationCommand。 */
    uint32_t power_key;          /* PWM_BURST 必须显式提供单次确认值。 */
    uint32_t duration_us;        /* 总测试时间 1000～10000 us，包含预提交。 */
    float common_duty;          /* 三相相同 duty，范围 0.10～0.90；仅供断开电机的台架。 */
} TargetValidationMailbox;

/*
 * sample-ready 是本快照的唯一运行期写者。sequence 奇数表示正在更新；
 * 连续运行读取时，前后 sequence 必须相同且为偶数，才使用整份快照。
 */
typedef struct {
    uint32_t sequence;
    uint32_t started;            /* 三项观测和持续 Drive Timing 已启动。 */
    uint32_t state;              /* TargetValidationState。 */
    uint32_t result;             /* 最近一次台架动作的 TargetValidationResult。 */
    uint32_t command_sequence;   /* 最近消费的 mailbox sequence。 */
    uint32_t fast_tick_count;
    uint32_t invalid_current_count;
    uint32_t current_interval_min_us; /* 已观察到的连续有效采集时间间隔。 */
    uint32_t current_interval_max_us;
    uint32_t current_time_regressions;
    uint32_t callback_max_cycles; /* 只计本 consumer 执行时间，不是整个 ADC ISR WCET。 */
    uint32_t position_available;
    uint32_t bus_available;
    uint32_t position_count;     /* 按新 observation_time 计数，不计重复 getter。 */
    uint32_t invalid_position_count;
    uint32_t bus_count;
    uint32_t invalid_bus_count;
    uint32_t stage_attempts;
    uint32_t stage_successes;
    uint32_t enable_attempts;
    uint32_t enable_successes;
    uint32_t tim1_cr1;           /* CEN / DIR 等台架证据，不作为物理 Gate 关断证明。 */
    uint32_t tim1_bdtr;          /* MOE 等寄存器证据。 */
    uint32_t tim1_ccer;
    uint32_t tim1_ccr1;
    uint32_t tim1_ccr2;
    uint32_t tim1_ccr3;
    RealtimeMotorIoFacts facts;
    PhaseCurrentObservation current;
    PositionObservation position;
    BusObservation bus;
    TargetValidationCurrentStatistics current_statistics;
} TargetValidationSnapshot;

extern volatile TargetValidationMailbox g_target_validation_request;
extern volatile TargetValidationSnapshot g_target_validation;
/*
 * 更新中断可独立完成截止关断。若 ADC 已丢失，快照可能来不及更新；
 * 此独立字保留结束原因，恢复 ADC 后才同步进入快照。
 */
extern volatile uint32_t g_target_validation_stop_reason;

bool target_validation_start(void);
void target_validation_update_irq(void);

#endif
