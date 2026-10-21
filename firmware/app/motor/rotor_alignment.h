#ifndef ROTOR_ALIGNMENT_H
#define ROTOR_ALIGNMENT_H

#include <stdbool.h>
#include <stdint.h>

#include "../../platform_contract/control_time.h"

/* Motor 内部的一次有限静态定向试验；不是公共 RUN 模式或标定安装接口。 */
typedef enum {
    ROTOR_ALIGNMENT_IDLE = 0, /* 已初始化但尚未开始，不请求功率输出。 */
    ROTOR_ALIGNMENT_RAMP, /* 固定定子方向下将电压从零线性升至目标。 */
    ROTOR_ALIGNMENT_HOLD, /* 维持固定矢量，等待最短保持时间与独立稳定窗口。 */
    ROTOR_ALIGNMENT_SUCCEEDED, /* 输出已撤销，仅保留未经实物确认的静态候选。 */
    ROTOR_ALIGNMENT_FAILED, /* 本次试验已失败，输出与候选均撤销且不自动重试。 */
    ROTOR_ALIGNMENT_STOPPED /* 显式停止终态，重新试验必须重新初始化。 */
} RotorAlignmentState;

/* 失败或停止的终态原因；重新试验必须由调用方显式重新初始化。 */
typedef enum {
    ROTOR_ALIGNMENT_REASON_NONE = 0, /* 尚无终止原因或已经产生候选。 */
    ROTOR_ALIGNMENT_REASON_PREREQUISITE, /* 主动试验许可或可信电流校准前提缺失。 */
    ROTOR_ALIGNMENT_REASON_TIME, /* 控制/观测时间倒退、重复或快照一致性失败。 */
    ROTOR_ALIGNMENT_REASON_FEEDBACK, /* 观测无效、过期、来自未来或数值越界。 */
    ROTOR_ALIGNMENT_REASON_BUS, /* 物理母线电压不在本次允许包络内。 */
    ROTOR_ALIGNMENT_REASON_OVERCURRENT, /* 至少一相正/负电流达到硬撤销阈值。 */
    ROTOR_ALIGNMENT_REASON_NUMERIC, /* 调制、励磁计算或内部计数无法产生可信结果。 */
    ROTOR_ALIGNMENT_REASON_TIMEOUT, /* 达到总时限；优先于同拍满足的成功条件。 */
    ROTOR_ALIGNMENT_REASON_FAULT, /* 调用方提供的外部故障证据已生效。 */
    ROTOR_ALIGNMENT_REASON_STOP /* 调用方显式停止，候选也一并撤销。 */
} RotorAlignmentReason;

/*
 * 参数必须由调用方根据电机和 Target 物理限制提供，本模块没有实物默认值。
 * 固定定子矢量的电压缓升不需要预先知道转子电角度，也不等于电流闭环。
 * max_voltage_ratio 必须不超过 Target 能实现可靠采样的正常包络。
 */
typedef struct {
    uint32_t ticks_per_second; /* 所有时间戳和以下时长共用的 Control Time 频率。 */
    uint32_t nominal_pole_pairs; /* 1..64；仅用于形成候选，不表示本试验确认了极对数。 */
    float stator_angle_turns; /* 固定 αβ 矢量角 [0,1)，α 沿 A 相，正向朝 β。 */
    float alignment_voltage_v; /* ramp 结束的固定矢量模长 [V]，有限且大于零。 */
    float max_voltage_ratio; /* 允许矢量模长 / VBUS，范围 (0,1/sqrt(3)]。 */
    float min_bus_voltage_v; /* 本试验允许的实际母线下限 [V]，大于零。 */
    float max_bus_voltage_v; /* 实际母线上限 [V]，不小于下限。 */
    float phase_current_trip_a; /* 任一已校正相电流绝对值达到此值就撤销输出 [A]。 */
    float min_excitation_current_a; /* 收敛窗口内所需 αβ 电流模长下限 [A]，避免无励磁假成功。 */
    float max_settle_span_turns; /* 整个稳定窗口的位置跨度上限 [机械 turn]，小于 0.25/pole_pairs。 */
    float max_settle_velocity_rpm; /* 稳定窗口内编码器坐标机械速度绝对值上限 [rpm]。 */
    uint32_t ramp_duration_ticks; /* 从零电压线性升至 alignment_voltage_v 的有限时长。 */
    uint32_t minimum_hold_ticks; /* ramp 后先维持励磁，至少经过此时长才开始稳定窗口。 */
    uint32_t settle_duration_ticks; /* 稳定窗口首尾不同位置采集时间的最小跨度。 */
    uint32_t timeout_ticks; /* start 起的总时限；达到时优先失败，不继续输出。 */
    uint32_t max_step_ticks; /* 相邻 update 控制时刻允许的最大间隔。 */
    uint32_t max_current_age_ticks; /* 本拍电流采集到 now 的最大年龄；允许零。 */
    uint32_t max_position_age_ticks; /* 位置采集到 now 的最大年龄；允许零。 */
    uint32_t max_bus_age_ticks; /* 母线采集到 now 的最大年龄；允许零。 */
    uint32_t minimum_position_samples; /* 稳定窗口独立位置采集数下限，范围 2..65535。 */
} RotorAlignmentConfig;

/*
 * 一份由实时单写者复制和确认的物理快照。电流已完成零偏/增益/极性校正、
 * 缺相重构；position 与 velocity 属于同一采集时刻，不能把新估计速度混入旧位置。
 * 每次 update 的 now 和 current_time 必须严格递增；同一位置发布可重复读取，
 * 但相同 position_time 的位置与速度必须完全一致，且不能重复推进稳定窗口。
 */
typedef struct {
    ControlTime now; /* 当前统一控制时刻，不是 HAL Tick 或墙上时间。 */
    ControlTime current_time; /* 同步三相电流采集时刻。 */
    ControlTime position_time; /* 编码器机械位置与速度所对应的采集时刻。 */
    ControlTime bus_time; /* 母线采集时刻，可重复但必须新鲜。 */
    float phase_current_a[3]; /* 已校正、完整的 Ia/Ib/Ic [A]。 */
    float position_turns; /* 编码器原坐标单圈机械位置，范围 [0,1)。 */
    float velocity_rpm; /* 同一编码器坐标的机械速度 [rpm]，不是电角速度。 */
    float bus_voltage_v; /* 实际物理母线电压 [V]。 */
    bool commissioning_allowed; /* 调用方每拍重新确认场地、机械与执行路径的主动试验许可。 */
    bool current_calibration_confirmed; /* 电流零偏、增益与极性已经独立验证；本试验不能代替。 */
    bool current_valid; /* 本拍电流完整可信；本模块另外检查数值与年龄。 */
    bool position_valid; /* 本拍编码器位置与速度完整可信。 */
    bool bus_valid; /* 母线观测完整可信。 */
    bool fault_active; /* 调用方发现任何硬件/实时/执行故障时必须置位。 */
    bool stop_requested; /* 实时边界消费的停止意图，优先于成功和新输出。 */
} RotorAlignmentInput;

/*
 * 静态试验候选，不是可信 ElectricalAngleConfig，也不授权 RUN。
 * 采用 theta_e=wrap(pole_pairs*direction*encoder_position-offset) 约定。
 * 固定磁场下的静止不能排除堵转、外力或静摩擦；本结果不能确认极对数、方向、
 * 相序或电流极性。必须经独立运动验证与人工审查，再由上层显式决定是否安装。
 */
typedef struct {
    float encoder_position_turns; /* 稳定窗口圆均值 [机械 turn]。 */
    float stator_angle_turns; /* 本次施加的固定定子矢量角 [电 turn]。 */
    float positive_direction_offset_turns; /* 假设 encoder_direction=+1 的 offset [0,1)。 */
    float negative_direction_offset_turns; /* 假设 encoder_direction=-1 的 offset [0,1)。 */
    uint32_t nominal_pole_pairs; /* 原样记录本次使用的标称极对数，不升级为确认事实。 */
    uint32_t position_samples; /* 真正参与最终稳定窗口的不同采集时刻数。 */
    ControlTime first_position_time; /* 最终稳定窗口第一份位置的采集时刻。 */
    ControlTime last_position_time; /* 最后一份位置的采集时刻。 */
    bool valid; /* 仅表示本试验数值/观察条件满足；上述物理歧义依然存在。 */
} RotorAlignmentCandidate;

/* duty 只是固定定子电压意图；本模块不调用 Platform 或修改 Motor 生命周期。 */
typedef struct {
    float duty_a; /* 归一化 A 相平均 duty，仅 output_requested=true 时可采用。 */
    float duty_b; /* 归一化 B 相平均 duty。 */
    float duty_c; /* 归一化 C 相平均 duty。 */
    float alpha_voltage_v; /* 本拍请求的 α 轴电压 [V]。 */
    float beta_voltage_v; /* 本拍请求的 β 轴电压 [V]。 */
    bool output_requested; /* false 要求执行方撤销功率；三个 0.5 本身不代表关断。 */
} RotorAlignmentOutput;

/* 调用方串行使用；内部历史不可从通信/后台直接改写。 */
typedef struct {
    RotorAlignmentConfig config; /* init 时固定复制的配置。 */
    RotorAlignmentState state; /* 只由本模块入口推进，终态不会自动重试。 */
    RotorAlignmentReason reason; /* 最近一次终态原因，成功时为 NONE。 */
    RotorAlignmentOutput output; /* 失败、Stop 和成功均立即撤销输出。 */
    RotorAlignmentCandidate candidate; /* 仅成功终态有效，init/Stop/失败时清空。 */
    bool configured; /* 仅表示数值配置有效，不是试验许可。 */
    bool window_active; /* 当前是否正在积累一段连续稳定观测。 */
    ControlTime started_at; /* 本次显式 start 的统一控制时刻。 */
    ControlTime last_update_time; /* 上一次控制时刻，用于拒绝重入/倒退/超期。 */
    ControlTime last_current_time; /* 已消费电流采集时刻，防止旧采样冒充新拍。 */
    ControlTime last_position_time; /* 最后消费的编码器位置采集时刻。 */
    ControlTime window_first_time; /* 当前稳定窗口首个位置采集时刻。 */
    float last_position_turns; /* 同采集时刻重读时用于检查快照一致性。 */
    float last_velocity_rpm; /* 与 last_position_turns 属于同一份位置快照。 */
    float window_anchor_turns; /* 稳定窗口的圆差展开参考，不累计任意多圈。 */
    float window_min_delta; /* 相对锚点的最小圆差 [机械 turn]。 */
    float window_max_delta; /* 相对锚点的最大圆差 [机械 turn]。 */
    float window_mean_delta; /* 窗口内相对锚点圆差的在线均值。 */
    uint32_t window_samples; /* 只在新的位置采集时刻递增。 */
} RotorAlignment;

/* 非法配置清空实例并返回 false；允许 config 指向实例内原配置。 */
bool rotor_alignment_init(RotorAlignment *alignment, const RotorAlignmentConfig *config);

/* 只允许 IDLE 显式开始；先检查快照，初次 start 不请求输出或执行硬件动作。 */
bool rotor_alignment_start(RotorAlignment *alignment, const RotorAlignmentInput *input);

/*
 * 每拍返回本次是否生成可用输出意图；终态均 false，成功另看 candidate.valid。
 * 执行方必须兑现安全 staging / enable boundary、deadline、独立关断及故障反馈。
 * 函数无法在不再被调用时自行超时，绝不能替代硬件/实时 watchdog。
 */
bool rotor_alignment_update(RotorAlignment *alignment, const RotorAlignmentInput *input);

/* 无条件撤销意图和候选，进入 STOPPED；不操作实际硬件，不清除外部故障。 */
void rotor_alignment_stop(RotorAlignment *alignment);

#endif
