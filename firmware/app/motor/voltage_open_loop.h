#ifndef VOLTAGE_OPEN_LOOP_H
#define VOLTAGE_OPEN_LOOP_H

#include <stdbool.h>
#include <stdint.h>

#include "../../platform_contract/control_time.h"
#include "../../../control/electrical_angle.h"

/* 一次有限电压试转；不表示编码器定向、电流校准或 FOC 闭环已成立。 */
typedef enum {
    VOLTAGE_OPEN_LOOP_IDLE = 0,
    VOLTAGE_OPEN_LOOP_RAMP,
    VOLTAGE_OPEN_LOOP_HOLD,
    VOLTAGE_OPEN_LOOP_ACCELERATE,
    VOLTAGE_OPEN_LOOP_ROTATE,
    VOLTAGE_OPEN_LOOP_RAMP_DOWN,
    VOLTAGE_OPEN_LOOP_COMPLETE,
    VOLTAGE_OPEN_LOOP_FAILED,
    VOLTAGE_OPEN_LOOP_STOPPED,
    VOLTAGE_OPEN_LOOP_MOVE_OUT,
    VOLTAGE_OPEN_LOOP_HOLD_OUT,
    VOLTAGE_OPEN_LOOP_MOVE_BACK,
    VOLTAGE_OPEN_LOOP_HOLD_RETURN
} VoltageOpenLoopState;

typedef enum {
    VOLTAGE_OPEN_LOOP_ROTATING = 0,
    VOLTAGE_OPEN_LOOP_STATIC_SURVEY = 1
} VoltageOpenLoopProfile;

typedef enum {
    VOLTAGE_OPEN_LOOP_REASON_NONE = 0,
    VOLTAGE_OPEN_LOOP_REASON_PREREQUISITE,
    VOLTAGE_OPEN_LOOP_REASON_TIME,
    VOLTAGE_OPEN_LOOP_REASON_FEEDBACK,
    VOLTAGE_OPEN_LOOP_REASON_BUS,
    VOLTAGE_OPEN_LOOP_REASON_NOMINAL_CURRENT,
    VOLTAGE_OPEN_LOOP_REASON_NUMERIC,
    VOLTAGE_OPEN_LOOP_REASON_TIMEOUT,
    VOLTAGE_OPEN_LOOP_REASON_FAULT,
    VOLTAGE_OPEN_LOOP_REASON_STOP
} VoltageOpenLoopReason;

/*
 * 无实物默认参数，调用方显式给出全部限制。先沿 +alpha 缓升定向，再保持、
 * 线性升至有符号电频、恒频旋转，最后维持电频而将电压线性降零并撤销意图。
 * ROTATING 的每段时长均须大于零；终态不能自行重试。
 * STATIC_SURVEY 仅观察固定角度往返，不依赖已确认的电流增益，也不输出可信标定：
 * 缓升电压、保持零角、匀速移至 excursion、保持、匀速返回、保持、缓降电压。
 * 三个保持段均用 alignment_hold_ticks，两次移动均用 frequency_ramp_ticks；
 * rotation_ticks 在该轨迹中不用。两种轨迹的总时长均须小于 timeout_ticks。
 * 名义电流仅监视冻结零偏后的采样量，不声称其增益/极性可信或独立保护可靠。
 */
typedef struct {
    uint32_t ticks_per_second;
    float voltage_v; /* 固定电压矢量模长 [V]；不随频率增加而自动提高。 */
    float target_electrical_hz; /* 有符号电频 [turn/s]；非零，正向 alpha 到 beta。 */
    float max_voltage_ratio; /* (0,1/sqrt(3)]，不能超过 Target 正常采样包络。 */
    float min_bus_voltage_v;
    float max_bus_voltage_v;
    float nominal_current_trip_a; /* 任一相名义值绝对值达到阈值立即撤销。 */
    uint32_t voltage_ramp_ticks;
    uint32_t alignment_hold_ticks;
    uint32_t frequency_ramp_ticks;
    uint32_t rotation_ticks;
    uint32_t voltage_ramp_down_ticks;
    uint32_t timeout_ticks; /* 达到时优先失败；执行方还须提供独立硬关断期限。 */
    uint32_t max_step_ticks; /* 严格递增；超过此间隔失败，不追补漏拍输出。 */
    uint32_t max_current_age_ticks;
    uint32_t max_bus_age_ticks;
    VoltageOpenLoopProfile profile; /* 零初始化保留原旋转轨迹语义。 */
    float excursion_turns; /* STATIC_SURVEY 的电角往返幅度，(0,0.25]；旋转轨迹必须为零。 */
} VoltageOpenLoopConfig;

typedef struct {
    ControlTime now;
    ControlTime current_time;
    ControlTime bus_time;
    float phase_current_a[3]; /* 冻结零偏后的完整三相名义值 [A]，不是精度验收。 */
    float bus_voltage_v;
    bool permitted; /* 调用方每拍确认现场、机械及执行条件允许此次主动试转。 */
    bool current_valid;
    bool bus_valid;
    bool fault_active;
    bool stop_requested;
} VoltageOpenLoopInput;

/* 仅是意图：执行方还须检查 duty 正常包络、提交期限、使能边界和实际撤销。 */
typedef struct {
    float duty_a;
    float duty_b;
    float duty_c;
    float alpha_voltage_v;
    float beta_voltage_v;
    float electrical_turns;
    float electrical_hz;
    bool output_requested; /* false 要求实际关断；三个 0.5 不代表关断。 */
} VoltageOpenLoopOutput;

/* 由实时单写者串行使用，通信和后台只能提交意图，不能直接改实例历史。 */
typedef struct {
    VoltageOpenLoopConfig config;
    VoltageOpenLoopState state;
    VoltageOpenLoopReason reason;
    VoltageOpenLoopOutput output;
    bool configured;
    ControlTime started_at;
    ControlTime last_update_time;
    ControlTime last_current_time;
    ControlTime last_bus_time;
    float last_bus_voltage_v;
    float electrical_turns; /* 逐拍有界积分后的 [0,1) 电角度。 */
    float phase_rounding_error; /* 补偿单精度逐拍累加舍入，避免恒频长段产生累计偏差。 */
} VoltageOpenLoop;

bool voltage_open_loop_init(VoltageOpenLoop *loop, const VoltageOpenLoopConfig *config);
/* 只允许 IDLE 开始；先检查输入，start 本身不请求输出。 */
bool voltage_open_loop_start(VoltageOpenLoop *loop, const VoltageOpenLoopInput *input);
bool voltage_open_loop_update(VoltageOpenLoop *loop, const VoltageOpenLoopInput *input);
/* trig 须有界、非阻塞且串行使用；单位圆检查不能证明后端相位精度。 */
bool voltage_open_loop_update_with_trig(VoltageOpenLoop *loop,
                                       const VoltageOpenLoopInput *input,
                                       ElectricalAngleTrig trig, void *context);
/* 无条件撤销并锁存 STOPPED；不调用硬件，也不清外部故障。 */
void voltage_open_loop_stop(VoltageOpenLoop *loop);

#endif
