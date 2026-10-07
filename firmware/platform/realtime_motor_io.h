#ifndef REALTIME_MOTOR_IO_H
#define REALTIME_MOTOR_IO_H

#include <stdbool.h>
#include <stdint.h>

#include "control_time.h"

typedef uint8_t PhaseMask;

enum
{
    PHASE_MASK_NONE = 0U,
    PHASE_MASK_A = (1U << 0),
    PHASE_MASK_B = (1U << 1),
    PHASE_MASK_C = (1U << 2)
};

/*
 * 下一确定 PWM 周期希望实现的 A/B/C 三相归一化平均 duty。
 * 每相正常范围为 [0, 1]，不表达 Timer count、3PWM/6PWM、极性或 dead-time。
 */
typedef struct
{
    float phase_a_duty;
    float phase_b_duty;
    float phase_c_duty;
} PhaseDutyRequest;

/*
 * 一次同步相电流采集事务的物理观测结果。
 *
 * measured_phases 只标记本次由硬件独立观测到的相。
 * 未被标记相的数值没有业务意义，Platform 不在这里做缺相重构。
 */
typedef struct
{
    float phase_a_a;
    float phase_b_a;
    float phase_c_a;
    PhaseMask measured_phases;
    ControlTime observation_time;
    bool acquisition_valid;
} PhaseCurrentObservation;

/*
 * 当前 Target 的 bind-time immutable Realtime Motor I/O 事实。
 *
 * normal_voltage_utilization 表示 V1 正常工作包络中的保守电压利用因子：
 * |Vdq|max = normal_voltage_utilization * VBUS。
 * 它必须建立在该 Target 能正常实现 PWM 且可靠完成 V1 相电流采集的保证上，
 * 不是理论 SVPWM 极限的别名。
 */
typedef struct
{
    float normal_voltage_utilization;
    uint32_t control_time_ticks_per_second;
    ControlTime fast_tick_period_ticks;
} RealtimeMotorIoFacts;

/*
 * 同步 hard-realtime sample-ready callback。
 * observation 只在 callback 调用期间有效；handler 必须 bounded、non-blocking，
 * 且 Platform 不得重入同一个 handler。
 */
typedef void (*PhaseCurrentSampleReadyHandler)(
    void *context,
    const PhaseCurrentObservation *observation);

/*
 * 获取当前 Target 的 immutable facts。
 * true 表示 out_facts 得到完整结果；false 时调用方不得读取 out_facts。
 */
bool realtime_motor_io_get_facts(RealtimeMotorIoFacts *out_facts);

/*
 * 绑定唯一 sample-ready consumer。
 * 必须在 arm / start_drive_timing 之前完成；V1 不支持运行期动态 rebind。
 */
bool realtime_motor_io_bind_sample_ready(
    PhaseCurrentSampleReadyHandler handler,
    void *context);

/*
 * 准备同步采集、事件和必要硬件路径，同时保持 power disabled。
 * 不启动 Drive Timing，也不等价于 Power Enable。
 */
bool realtime_motor_io_arm(void);

/*
 * 一次性启动确定性 Drive Timing。
 * 成功后时基在 IDLE / RUN / FAULT 中持续运行，不提供正常运行期 stop 对称接口。
 */
bool realtime_motor_io_start_drive_timing(void);

/*
 * 为下一确定 update boundary stage 一组三相 duty。
 *
 * true 表示整个请求按 Hardware Capability Contract 被接受并能够正常实现。
 * 如果请求非法、超出 Target 正常包络、错过 staging deadline，或 Platform 必须
 * 改变请求才能落到硬件，则返回 false；safety clamp 不能伪装成正常成功。
 */
bool realtime_motor_io_stage_phase_duty(
    const PhaseDutyRequest *request);

/*
 * 在已经存在本次 Start 的 fresh safe actuation，并跨过必要确定性边界后，
 * 才允许真正使能功率输出。该函数成功不直接修改 Motor 生命周期。
 */
bool realtime_motor_io_power_enable(void);

/*
 * 无条件 software safe-off。
 *
 * 必须 bounded、non-blocking、idempotent；保持 Drive Timing 继续运行，禁止功率输出，
 * 抑制未完成的 enable，并使旧 actuation epoch 失效，使旧 staged duty 不能在以后
 * 的 enable 中重新生效。
 */
void realtime_motor_io_safe_off(void);

#endif /* REALTIME_MOTOR_IO_H */
