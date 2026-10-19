#ifndef CONTROL_RUNTIME_H
#define CONTROL_RUNTIME_H

#include <stdbool.h>
#include <stdint.h>

#include "motor.h"
#include "motor_snapshot.h"
#include "position_estimator.h"
#include "current_measurement.h"
#include "electrical_angle.h"
#include "current_controller.h"
#include "speed_controller.h"
#include "foc_math.h"
#include "realtime_motor_io.h"
#include "position_observation.h"
#include "bus_observation.h"

/* 控制流程自身的锁存故障；与 Target 低位硬件证据不重叠。 */
enum {
    CONTROL_RUNTIME_FAULT_FEEDBACK = 1U << 8, /* 活动流程的采样、角度、母线或校正电流失效。 */
    CONTROL_RUNTIME_FAULT_CONTROL = 1U << 9, /* 控制算法计算失败或内部控制状态不一致。 */
    CONTROL_RUNTIME_FAULT_ACTUATION = 1U << 10, /* duty 提交或受控使能失败。 */
    CONTROL_RUNTIME_FAULT_POWER = 1U << 11, /* 实际功率状态与当前流程不符。 */
    CONTROL_RUNTIME_FAULT_OVERCURRENT = 1U << 12 /* 校正相电流超过本装配的快速软件阈值。 */
};

typedef enum {
    CONTROL_RUNTIME_OFF = 0, /* 没有待启动请求，功率应关闭。 */
    CONTROL_RUNTIME_START_QUIET = 1, /* Start 当拍已关断旧 epoch，等待下一拍首次 stage。 */
    CONTROL_RUNTIME_START_STAGED = 2, /* 已 stage 安全零线电压，等待下一确定边界。 */
    CONTROL_RUNTIME_RUNNING = 3 /* 已受控使能并提交 Motor RUN。 */
} ControlRuntimePhase;

/* 数值配置与三项实物确认分离；enabled=false 时整组配置不参与观察流程。 */
typedef struct {
    bool enabled; /* 允许装配完整控制流程，不等于允许实际加电。 */
    bool current_calibration_confirmed; /* 电流增益、极性和校正链已按实物确认。 */
    bool timing_confirmed; /* 本构建采样/调制延迟、可测窗口和执行预算已确认。 */
    CurrentControllerConfig current; /* 纯数值电流控制器参数。 */
    SpeedControllerConfig speed; /* 纯数值速度控制器参数。 */
    uint32_t speed_divider; /* 速度环相对于同步采样的整数分频，至少为 1。 */
    float min_bus_voltage_v; /* 控制允许的母线下限 [V]，有限且大于零。 */
    float max_bus_voltage_v; /* 控制允许的母线上限 [V]，有限且大于下限。 */
    float phase_current_trip_a; /* 校正 A/B/C 任一相绝对电流的软件阈值 [A]。 */
    float actuation_prediction_time_s; /* 本拍采样到电压作用参考时刻 [s]；有限且处于 [0,2*fast_dt]。 */
} ControlRuntimeControlConfig;

/* Target 装配时固定的观测与控制配置；数值参数不替代实物标定。 */
typedef struct {
    uint32_t control_time_ticks_per_second; /* 硬件控制时基，tick/s。 */
    ControlTime fast_tick_period_ticks; /* 相邻同步电流采样的确定间隔。 */
    uint32_t observation_divider; /* 每多少次同步采样读取异步观测。 */
    ControlTime position_max_age_ticks; /* 位置观测允许保留的最大年龄。 */
    ControlTime bus_max_age_ticks; /* 母线观测允许保留的最大年龄。 */
    PositionEstimatorConfig velocity_estimator;
    CurrentMeasurementConfig current_measurement;
    uint32_t current_zero_settle_samples; /* 无功率且静止后，开始零偏窗口前的连续等待拍数。 */
    float current_zero_velocity_limit_rpm; /* 仅用于零偏采集静止门控，不是运行限速。 */
    bool current_zero_calibration_permitted; /* 装配方确认本次无外部相电流的采集许可；默认 false。 */
    ElectricalAngleConfig electrical_angle; /* 仅在下列标定可信标志为 true 时使用。 */
    bool electrical_calibration_confirmed; /* 极对数、方向、偏置均已确认；资料标称不能置位。 */
    ControlRuntimeControlConfig control; /* 默认关闭；Observe 不调用功率或 PI 路径。 */
    ElectricalAngleTrig angle_trig; /* 测量/施加角共用的窄后端；NULL使用软件，失败不回退。 */
    void *angle_trig_context; /* 由装配方持有并串行使用，寿命覆盖Runtime；软件默认实现忽略它。 */
} ControlRuntimeConfig;

/*
 * Motor 内部的实时执行实例；启动完成后仅由同步 sample-ready 边界修改。
 * 算法持有自身历史，Runtime 决定其调用、失效与 reset 的顺序。
 */
typedef struct {
    Motor *motor; /* 由 Target 持有生命周期，本实例独占其运行期写入。 */
    ControlRuntimeConfig config;
    PositionEstimator position_estimator;
    CurrentMeasurement current_measurement; /* 零偏窗口、冻结校正值与本拍相电流/αβ 反馈。 */
    ElectricalAngle electrical_angle; /* 本拍外推到同步采样时刻的电角度；未标定时始终无效。 */
    ElectricalAngle actuation_angle; /* 外推到显式配置的电压作用参考时刻，独立于测量角。 */
    CurrentController current_controller; /* 电流 PI 积分历史，仅实时所有者更新。 */
    SpeedController speed_controller; /* 速度 PI 积分历史。 */
    CurrentControllerOutput current_output; /* 本拍电压与 duty 数值结果；无效时不得提交。 */
    SpeedControllerOutput speed_output; /* 速度慢环最近一次完整输出，两次更新之间保持。 */
    FocDq current_dq; /* RUN复用控制器归一化测量DQ；IDLE独立Park；无物理确认时无效。 */
    ControlRuntimePhase phase; /* 内部跨拍启动/运行流程，不新增公共 Motor 生命周期。 */
    float pending_velocity_rpm; /* 已接受但尚未使能的完整启动目标。 */
    uint32_t speed_counter; /* 速度环自上次计算以来的同步采样拍数。 */
    ControlTime last_speed_time; /* 上次速度 PI 时刻；首次 RUN 计算以使能时刻为起点。 */
    BusObservation bus; /* 已复制的最近一次母线 publication。 */
    ControlTime last_position_time; /* 最近一次已消费的位置采集时刻。 */
    float mechanical_position_turns; /* 最近一次合法位置 publication 的机械圈坐标。 */
    uint32_t observation_counter;
    uint32_t current_zero_counter; /* 尚未开始零偏采集时的连续合格等待拍数。 */
    ControlTime last_current_time; /* 用于拒绝重复、跳拍或回退的同步采集。 */
    bool bus_available;
    bool bus_valid; /* 上拍的母线有效性，用于及时发布过期或恢复变化。 */
    bool position_time_available;
    bool current_time_available;
} ControlRuntime;

/* 在启动硬件事件前初始化；不启动采集、不改变功率，也不修改 Motor 生命周期。 */
bool control_runtime_init(ControlRuntime *runtime, Motor *motor,
    const ControlRuntimeConfig *config);

/*
 * 仅在同步采样边界调用：故障/Stop 优先，随后更新观测并消费其余完整请求。
 * current 只在本次调用中读取，不保留指针；power_disabled 必须来自当拍硬件证据。
 * 零偏采集还要求无故障 IDLE、有效静止位置反馈；仅功率关闭不足以证明没有外部电流。
 * has_command=true 且 command=NULL 表示已完整接收但被拒绝的请求。
 * Start 必须跨两次后续采样完成 neutral stage/确定边界/再 stage/enable。
 * 控制配置未开启或任一物理确认缺失时维持 NOT_READY，不默认填充真实电机参数。
 * 返回本拍是否需要立即发布业务、流程或反馈有效性变化。
 */
bool control_runtime_step(ControlRuntime *runtime, ControlTime now,
    const PhaseCurrentObservation *current, bool power_disabled,
    uint32_t fault_evidence, const MotorCommand *command, bool has_command);

/* 仅由同一实时所有者构造快照；后台读取 Target 发布的副本。 */
void control_runtime_snapshot(const ControlRuntime *runtime, ControlTime now,
    bool has_sample, MotorSnapshot *snapshot);

#endif
