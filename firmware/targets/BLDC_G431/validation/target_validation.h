#ifndef BLDC_G431_TARGET_VALIDATION_H
#define BLDC_G431_TARGET_VALIDATION_H

#include <stdbool.h>
#include <stdint.h>

#include "hardware_trip.h"
#include "realtime_motor_io.h"
#include "position_observation.h"
#include "bus_observation.h"
#include "timing_probe.h"
#include "current_hold.h"

/* 首板无功率诊断快照：仅供 Keil Watch 观察，不作为正式 Firmware API。 */
typedef struct
{
    volatile uint32_t status;                /* 1=运行；101~110=启动失败；111=MOE 异常；112=时钟错误。 */
    volatile uint32_t fast_ticks;            /* 实际收到的同步采样 callback 数量。 */
    volatile uint32_t current_valid_ticks;   /* 本次 B/C 电流采集完整的次数。 */
    volatile uint32_t current_invalid_ticks; /* 本次采集未完整的次数。 */
    volatile uint32_t measured_phases;       /* 最近采集实际测得的物理相位位图。 */
    volatile float phase_b_a;                /* 最近实际测得的 B 相电流，A。 */
    volatile float phase_c_a;                /* 最近实际测得的 C 相电流，A。 */
    volatile uint32_t position_samples;      /* 不同 ControlTime 的位置 publication 数。 */
    volatile float position_turns;           /* 最近位置，机械圈。 */
    volatile bool position_valid;            /* 最近位置采集是否有效。 */
    volatile uint32_t bus_samples;           /* 不同 ControlTime 的母线 publication 数。 */
    volatile float bus_voltage_v;            /* 最近母线电压，V。 */
    volatile bool bus_valid;                 /* 最近母线采集是否有效。 */
    volatile uint32_t trip_notifications;    /* 仅统计 rescan 通知，不作为 fault evidence。 */
    volatile uint32_t trip_rescans;          /* 成功读取 authoritative snapshot 的次数。 */
    volatile bool trip_armed;                /* protection arm 已成功；不表示 condition healthy。 */
    volatile bool trip_snapshot_valid;       /* 最近一次 snapshot 读取是否成功。 */
    volatile bool trip_condition_active;     /* authoritative snapshot 的当前物理条件。 */
    volatile HardwareTripCauseFlags trip_causes; /* 尚未恢复 episode 的 sticky cause。 */
    volatile uint32_t moe_checks;            /* 无功率 Bring-up 每个 fast tick 检查 MOE。 */
    volatile uint32_t moe_violations;
} PlatformBringupDebug;

/*
 * 保留原 Bring-up 的 Watch 名称及布局，便于沿用已有离线读回工具。
 * ADC callback、Trip IRQ 和后台分别更新所属字段；整份结构不是一致性快照。
 * 运行中按独立字段观察，不能将一次大块 SWD 读取视为同一采集时刻。
 */
extern volatile PlatformBringupDebug g_platform_bringup;

typedef enum {
    TARGET_VALIDATION_COMMAND_NONE = 0,
    TARGET_VALIDATION_COMMAND_SAFE_OFF = 1,
    TARGET_VALIDATION_COMMAND_STAGE_ONLY = 2,
    TARGET_VALIDATION_COMMAND_PWM_BURST = 3,
    TARGET_VALIDATION_COMMAND_CAPTURE_CURRENT = 4,
    TARGET_VALIDATION_COMMAND_BREAK_OVP = 5,
    TARGET_VALIDATION_COMMAND_BREAK_OCP_C = 6,
    TARGET_VALIDATION_COMMAND_BREAK_OCP_AB = 7,
    TARGET_VALIDATION_COMMAND_CLEAR_INJECTION = 8,
    TARGET_VALIDATION_COMMAND_REARM = 9,
    TARGET_VALIDATION_COMMAND_OPEN_LOOP = 15, /* 有限低电压试转；不复用旧的 10～14 时序命令。 */
    TARGET_VALIDATION_COMMAND_ANGLE_SURVEY = 16, /* 单次有限静态电角往返，仅生成观测证据。 */
    TARGET_VALIDATION_COMMAND_CURRENT_SURVEY = 18, /* 5秒有限电流磁场往返；不授予转子FOC。 */
    TARGET_VALIDATION_COMMAND_CURRENT_HOLD = 17, /* 3秒静止αβ电流PI；不依赖转子电角标定。 */
    TARGET_VALIDATION_COMMAND_TIMING_IMMEDIATE = TIMING_PROBE_CASE_IMMEDIATE,
    TARGET_VALIDATION_COMMAND_TIMING_BEFORE = TIMING_PROBE_CASE_BEFORE,
    TARGET_VALIDATION_COMMAND_TIMING_AFTER = TIMING_PROBE_CASE_AFTER,
    TARGET_VALIDATION_COMMAND_TIMING_CROSS_CYCLE = TIMING_PROBE_CASE_CROSS_CYCLE,
    TARGET_VALIDATION_COMMAND_TIMING_CANCEL = TIMING_PROBE_CASE_CANCEL
} TargetValidationCommand;

typedef enum {
    TARGET_VALIDATION_STATE_OBSERVE = 0,
    TARGET_VALIDATION_STATE_STAGING = 1,
    TARGET_VALIDATION_STATE_POWERED = 2,
    TARGET_VALIDATION_STATE_CAPTURING_CURRENT = 3,
    TARGET_VALIDATION_STATE_TIMING = 4
} TargetValidationState;

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
    TARGET_VALIDATION_RESULT_PLATFORM_OFF = 10,
    TARGET_VALIDATION_RESULT_HARDWARE_TRIP = 11,
    TARGET_VALIDATION_RESULT_BAD_CLOCK = 12,
    TARGET_VALIDATION_RESULT_PROBE_REJECTED = 13,
    TARGET_VALIDATION_RESULT_BREAK_NOT_OBSERVED = 14,
    TARGET_VALIDATION_RESULT_REARM_REJECTED = 15,
    TARGET_VALIDATION_RESULT_TIMING_REJECTED = 16,
    TARGET_VALIDATION_RESULT_TIMING_TIMEOUT = 17,
    TARGET_VALIDATION_RESULT_OPEN_LOOP_TIMEOUT = 18,
    TARGET_VALIDATION_RESULT_BAD_ZERO = 19,
    TARGET_VALIDATION_RESULT_BAD_POSITION = 20,
    TARGET_VALIDATION_RESULT_OPEN_LOOP_REJECTED = 21,
    TARGET_VALIDATION_RESULT_NOMINAL_CURRENT = 22
} TargetValidationResult;

#define TARGET_VALIDATION_POWER_KEY (0x4310A11EU)
#define TARGET_VALIDATION_MAX_BURST_US (10000U)
#define TARGET_VALIDATION_CURRENT_SAMPLE_COUNT (4096U)
#define TARGET_VALIDATION_PROBE_DURATION_US (2000U)
#define TARGET_VALIDATION_OPEN_LOOP_DURATION_US (3500000U)
#define TARGET_VALIDATION_ANGLE_SURVEY_DURATION_US (5500000U)

/* 先写 payload，最后写新的非零 sequence；等待消费后再提交下一条。
 * power_key 消费即清零，PWM_BURST / BREAK_* / OPEN_LOOP / ANGLE_SURVEY / CURRENT_* 必须逐次提供。不是安全认证。
 * BREAK_* 固定 duration_us=2000 / common_duty=0.5，不允许带电机。
 * TIMING_* 固定 power_key=0 / duration_us=0 / common_duty=0，仅低于 4 V 母线。 */
typedef struct {
    uint32_t sequence;
    uint32_t command;
    uint32_t power_key;
    uint32_t duration_us; /* 旧 stage/burst 为 1000～10000 us；命令 15/16 分别固定 3500000/5500000 us。 */
    float common_duty;   /* 旧 stage/burst 三相相同且电机断开；命令 15/16 固定 0.5，仅用于中性预热。 */
} TargetValidationMailbox;

/* 独立扩展 payload；先写本结构与旧 mailbox payload，最后写旧 sequence。
 * OPEN_LOOP 固定 common_duty=0.5、duration_us=3500000，并须逐次提供 power_key。
 * 配置只在接收时复制；活动事务不读取主机随后修改的 payload。 */
typedef struct {
    float voltage_v; /* (0, 0.5] V，电压矢量模长。 */
    float electrical_frequency_hz; /* 非零，绝对值不超过 5 Hz。 */
} TargetValidationOpenLoopConfig;

/* 命令 16 独立 payload，不改命令 15 的 8 字节语义；duration_us 固定 5500000。 */
typedef struct {
    float voltage_v; /* (0,0.5] V，沿用低电压和名义电流辅助限制。 */
    float excursion_turns; /* (0,1] 电角圈；轨迹为 0 → excursion → 0，一整圈可核对极对数。 */
} TargetValidationAngleSurveyConfig;

/* 每个保持段仅统计末尾 250 ms 中时间戳不同的新 SSI 观测。
 * 不能把保持段开始前采到的旧位置计入，也不把圆均值当作可信标定。 */
typedef struct {
    uint32_t sample_count;
    ControlTime first_position_time;
    ControlTime last_position_time;
    float mean_position_turns; /* 相对窗口锚展开后求均值，再归一化至 [0,1)。 */
    float span_turns; /* 相对同一锚的最大值减最小值，机械圈。 */
    float first_position_turns;
    float last_position_turns;
    float mean_nominal_b_a; /* 消费这些新位置的回调中，同步相电流名义值的均值；不是标定。 */
    float mean_nominal_c_a;
} TargetValidationAngleWindow;

typedef struct {
    uint32_t sequence;
    uint32_t request_sequence;
    uint32_t result;
    uint32_t stable_window_mask; /* 位 0/1/2 对应 0、excursion、返回 0；仅有限稳定观察。 */
    float excursion_turns;
    float voltage_v;
    TargetValidationAngleWindow windows[3];
} TargetValidationAngleSurveySnapshot;

/* ADC 单写者的独立结果，保持旧 Watch / Mailbox ABI 不变。
 * 前后 sequence 相同且为偶数才一致；ADC 停止时另读 stop_reason / 实际 MOE。
 * 名义电流及圈数仅为本次试转诊断，不安装电流增益、电角度或方向标定。 */
typedef struct {
    uint32_t sequence;
    uint32_t request_sequence;
    uint32_t active;
    uint32_t result;
    uint32_t module_state;
    uint32_t module_reason;
    uint32_t sample_count;
    uint32_t enabled_samples;
    float voltage_v;
    float electrical_frequency_hz;
    float offset_b_a;
    float offset_c_a;
    float peak_abs_current_a;
    float start_position_turns;
    float end_position_turns;
    float displacement_turns;
    float min_duty;
    float max_duty;
} TargetValidationOpenLoopSnapshot;

typedef struct {
    float mean_a;
    float min_a;
    float max_a;
    float variance_a2; /* 无偏样本方差，不应用零点修正。 */
} TargetValidationPhaseStatistics;

typedef struct {
    uint32_t request_sequence;
    uint32_t result;
    uint32_t sample_count;
    uint32_t interval_min_cycles;
    uint32_t interval_max_cycles;
    ControlTime first_sample_time; /* 1 tick = 50 us；正常相邻样本相差 2 tick。 */
    ControlTime last_sample_time;
    TargetValidationPhaseStatistics phase_b;
    TargetValidationPhaseStatistics phase_c;
} TargetValidationCurrentStatistics;

/* 接收请求和结束时发布，期间保持不变；读前后 sequence 相同且为偶数。
 * 仅 result=COMPLETE 且 sample_count=4096 才是完整窗口。 */
typedef struct {
    uint32_t sequence;
    TargetValidationCurrentStatistics statistics;
} TargetValidationCurrentCapture;

/* ADC callback 是唯一运行期写者；整份读回需核对前后 sequence 为同一偶数。 */
typedef struct {
    uint32_t sequence;
    uint32_t started;
    uint32_t state;
    uint32_t result;
    uint32_t command_sequence;
    uint32_t active_request_sequence;
    uint32_t fast_tick_count;
    uint32_t invalid_current_count;
    uint32_t interval_min_cycles;
    uint32_t interval_max_cycles;
    uint32_t callback_max_cycles; /* 只计 consumer，不是整个 ADC ISR WCET。 */
    uint32_t stage_attempts;
    uint32_t stage_successes;
    uint32_t enable_attempts;
    uint32_t enable_successes;
    uint32_t capture_samples;
    uint32_t tim1_bdtr;
    uint32_t tim1_ccer;
    uint32_t guard_irq_count;
    uint32_t trip_valid;
    HardwareTripSnapshot trip;
    RealtimeMotorIoFacts facts;
    PhaseCurrentObservation current;
    PositionObservation position;
    BusObservation bus;
} TargetValidationSnapshot;

extern volatile TargetValidationMailbox g_target_validation_request;
extern volatile TargetValidationSnapshot g_target_validation;
extern volatile TargetValidationCurrentCapture g_target_validation_capture;
extern volatile TargetValidationOpenLoopConfig g_target_validation_open_loop_config;
extern volatile TargetValidationOpenLoopSnapshot g_target_validation_open_loop;
extern volatile TargetValidationAngleSurveyConfig g_target_validation_angle_survey_config;
extern volatile TargetValidationAngleSurveySnapshot g_target_validation_angle_survey;
/* 与电压往返独立的单位及地址；只允许excursion_turns=1。 */
typedef struct {
    float current_reference_a;
    float excursion_turns;
} TargetValidationCurrentSurveyConfig;
typedef struct {
    uint32_t sequence;
    uint32_t request_sequence;
    uint32_t result;
    uint32_t stable_window_mask;
    float excursion_turns;
    float current_reference_a;
    TargetValidationAngleWindow windows[3];
} TargetValidationCurrentSurveySnapshot;
extern volatile TargetValidationCurrentSurveyConfig g_target_validation_current_survey_config;
extern volatile TargetValidationCurrentSurveySnapshot g_target_validation_current_survey;
extern volatile CurrentHoldConfig g_target_validation_current_hold_config;
extern volatile CurrentHoldSnapshot g_target_validation_current_hold;
/* ADC 丢失时快照可能停在旧状态；此字独立记录关断原因。 */
extern volatile uint32_t g_target_validation_stop_reason;
/* 时序用例后的连续正常采样数，饱和至 2；不改变旧诊断结构的 ABI。 */
extern volatile uint32_t g_target_validation_timing_recovery_ticks;

/* 外设初始化后调用一次；默认 OBSERVE，不消费复位前的请求。 */
void target_validation_start(void);
/* 主循环调用，处理保护通知并每 10 ms 轮询慢观测，不阻塞等待外设。 */
void target_validation_service(void);
/* 已配置的 TIM6_DAC IRQ 中调用；只处理 TIM6 update，不占用 DAC handler。 */
void target_validation_guard_irq(void);

#endif /* BLDC_G431_TARGET_VALIDATION_H */
