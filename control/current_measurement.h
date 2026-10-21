#ifndef CURRENT_MEASUREMENT_H
#define CURRENT_MEASUREMENT_H

#include <stdbool.h>
#include <stdint.h>

/* 相位集合由装配方固定；不依赖采样驱动或硬件接口中的枚举值。 */
typedef enum {
    CURRENT_MEASUREMENT_PHASE_A = 1U,
    CURRENT_MEASUREMENT_PHASE_B = 2U,
    CURRENT_MEASUREMENT_PHASE_C = 4U,
    CURRENT_MEASUREMENT_PHASE_AB = 3U,
    CURRENT_MEASUREMENT_PHASE_AC = 5U,
    CURRENT_MEASUREMENT_PHASE_BC = 6U,
    CURRENT_MEASUREMENT_PHASE_ABC = 7U
} CurrentMeasurementPhases;

/* 校零生命周期；READY 仅表示零偏可用，不表示增益/方向或闭环已经验收。 */
typedef enum {
    CURRENT_MEASUREMENT_UNCONFIGURED = 0, /* 尚无合法配置。 */
    CURRENT_MEASUREMENT_WAITING, /* 等待调用方显式确认零电流并开始。 */
    CURRENT_MEASUREMENT_CALIBRATING, /* 正在收集有限连续窗口。 */
    CURRENT_MEASUREMENT_READY, /* 完整窗口合格，冻结零偏。 */
    CURRENT_MEASUREMENT_FAILED /* 失败锁定，显式 reset 后才可重试。 */
} CurrentMeasurementState;

/* 锁存第一次失败原因，供装配方诊断；不直接决定 Motor Fault 策略。 */
typedef enum {
    CURRENT_MEASUREMENT_FAILURE_NONE = 0, /* 尚无失败。 */
    CURRENT_MEASUREMENT_FAILURE_ABORTED, /* 调用方撤销本次窗口或就绪零偏。 */
    CURRENT_MEASUREMENT_FAILURE_ZERO_CURRENT_UNCONFIRMED, /* 窗口中失去零电流确认。 */
    CURRENT_MEASUREMENT_FAILURE_INVALID_SAMPLE, /* 缺相或实测相包含非有限值。 */
    CURRENT_MEASUREMENT_FAILURE_NOISE_LIMIT, /* 窗口峰峰值超过配置。 */
    CURRENT_MEASUREMENT_FAILURE_OFFSET_LIMIT, /* 完整窗口均值超过配置。 */
    CURRENT_MEASUREMENT_FAILURE_NUMERIC /* 中间计算不能表示为有限浮点数。 */
} CurrentMeasurementFailure;

/* 阈值均使用校正前的名义 A，只约束零偏窗口，不表示硬件精度或运行电流限制。 */
typedef struct {
    uint32_t measured_phases; /* 仅允许 AB、AC、BC 或 ABC。 */
    uint32_t calibration_sample_count; /* 窗口长度，范围 [2, 16777216]。 */
    float max_offset_abs_a; /* 完整窗口均值绝对值上限 [A]，有限且非负。 */
    float max_peak_to_peak_a; /* 各实测相峰峰值上限 [A]，有限且非负。 */
    bool gain_correction_enabled; /* false 时全部按 1 处理且忽略数组；旧四字段初始化保持此行为。 */
    float signed_gain[3]; /* A/B/C 的无量纲有符号增益；开启后仅实测项要求有限非零。 */
} CurrentMeasurementConfig;

typedef struct {
    float phase_a[3]; /* 名义电流 [A]，下标依次为 A/B/C；未实测项不读取。 */
    uint32_t valid_phases; /* 必须覆盖配置的实测相；额外合法相位不参与计算。 */
} CurrentMeasurementSample;

typedef struct {
    float phase_a[3]; /* 扣除冻结名义零偏、可选增益修正及缺相重构后的 A/B/C 电流 [A]。 */
    float alpha_a; /* 幅值不变 Clarke 的静止 α 轴电流 [A]。 */
    float beta_a; /* 幅值不变 Clarke 的静止 β 轴电流 [A]。 */
    bool valid; /* false 时所有数值置零，零值不能作为真实零电流证据。 */
} CurrentMeasurementOutput;

/* 单一调用方串行更新；诊断可读，配置、状态及历史不应由外部直接修改。 */
typedef struct {
    CurrentMeasurementConfig config; /* init 复制后固定；reset 保留，包括增益使能与系数。 */
    CurrentMeasurementState state; /* 校零窗口与冻结偏置的生命周期。 */
    CurrentMeasurementFailure failure; /* 第一次失败原因，reset 清除。 */
    uint32_t sample_count; /* 已完整接收的窗口样本数。 */
    float offset_a[3]; /* 冻结的校正前名义 A；仅 READY 可用，完整窗口通过前或 abort 后为零。 */
    float mean_a[3]; /* 当前窗口诊断；只对实测相且 sample_count > 0 有意义。 */
    float minimum_a[3]; /* 窗口内实测相最小值 [A]。 */
    float maximum_a[3]; /* 窗口内实测相最大值 [A]。 */
    CurrentMeasurementOutput output; /* 最近一次 process 的结果及有效性。 */
} CurrentMeasurement;

/*
 * 非法配置使实例保持 UNCONFIGURED；支持 config 与实例自身配置别名。
 * 增益只作数值校验，负值可表达已实测的方向修正，不表示物理标定已通过。
 * 配置仅由 init 复制；包括 READY 在内的运行过程中均不得直接热改增益。
 * 需要重配时应先由上层关闭功率，再显式 init 并重新建立零偏窗口。
 */
bool current_measurement_init(CurrentMeasurement *measurement,
                              const CurrentMeasurementConfig *config);

/* 显式清除窗口、零偏和输出；保留原配置及增益，READY/FAILED 均回到 WAITING。 */
void current_measurement_reset(CurrentMeasurement *measurement);

/*
 * 仅 WAITING 且明确确认真实零电流时接受，拒绝时不改变状态。
 * 确认责任包含关闭功率、静止及无外部注入；仅功率关闭并不充分。
 * 本模块不能从采样值辨别小幅真实电流与偏置，不能自行决定开始校零。
 */
bool current_measurement_start(CurrentMeasurement *measurement, bool zero_current_confirmed);

/*
 * 仅 CALIBRATING 消费一拍；连续合法样本达到配置长度且合格时返回 true。
 * 窗口中失去零电流确认、缺相、坏数值或越限均进入 FAILED，不自动重学。
 * 未实测相允许 NaN/Inf。失败保留已经接收样本的诊断，offset/output 无效。
 * 本窗口与偏置/噪声阈值始终使用校正前名义 A，不应用有符号增益。
 */
bool current_measurement_calibrate(CurrentMeasurement *measurement,
    const CurrentMeasurementSample *sample, bool zero_current_confirmed);

/* 显式使窗口或已就绪零偏失效，进入 FAILED；保留首个失败原因与诊断，重试必须先 reset。 */
void current_measurement_abort(CurrentMeasurement *measurement);

/*
 * 只使用 READY 后冻结的零偏；坏输入只使本拍输出无效，不自动重新校零。
 * 开启增益修正时，实测相按 signed_gain * (raw - offset) 计算后才重构缺相。
 * 未开启时忽略整个增益数组；未实测相的增益始终不参与校验或计算。
 * 相减、增益乘法、重构或 Clarke 溢出仅撤销当拍输出，保留 READY 与冻结零偏。
 * 双相按 A+B+C=0 重构缺相；三相保留各自实测值，不静默消除零序。
 * 幅值不变 Clarke：alpha=(2A-B-C)/3，beta=(B-C)/sqrt(3)。
 * 返回本拍输出是否有效。每次最多处理三相，不含窗口循环或动态分配。
 */
bool current_measurement_process(CurrentMeasurement *measurement,
                                 const CurrentMeasurementSample *sample);

#endif
