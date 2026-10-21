#ifndef BLDC_G431_OBSERVE_COMPUTE_PROBE_H
#define BLDC_G431_OBSERVE_COMPUTE_PROBE_H

#include <stdbool.h>
#include <stdint.h>

#include "current_controller.h"
#include "current_measurement.h"
#include "electrical_angle.h"
#include "speed_controller.h"

#define OBSERVE_COMPUTE_PROBE_CASE_COUNT (6U)
#define OBSERVE_COMPUTE_PROBE_ITERATIONS (16U)
#define OBSERVE_COMPUTE_PROBE_SEGMENT_COUNT (6U)

typedef enum {
    OBSERVE_COMPUTE_PROBE_IDLE = 0, /* 初始化后尚无请求。 */
    OBSERVE_COMPUTE_PROBE_ACTIVE = 1, /* 后台逐例执行有限次合成计算。 */
    OBSERVE_COMPUTE_PROBE_COMPLETE = 2, /* 所有固定算例完成，结果冻结。 */
    OBSERVE_COMPUTE_PROBE_FAILED = 3 /* 请求失败，已完成统计保留且不自动重试。 */
} ObserveComputeProbeState;

typedef enum {
    OBSERVE_COMPUTE_PROBE_FAILURE_NONE = 0, /* 尚无失败。 */
    OBSERVE_COMPUTE_PROBE_FAILURE_NOT_READY = 1, /* 调用方未许可，或启动参数非法。 */
    OBSERVE_COMPUTE_PROBE_FAILURE_ABORTED = 2, /* 调用方显式取消。 */
    OBSERVE_COMPUTE_PROBE_FAILURE_CLOCK = 3, /* 时钟读取失败、停滞或耗时超过上界。 */
    OBSERVE_COMPUTE_PROBE_FAILURE_ALGORITHM = 4, /* 正式算法未产生算例约定的结果。 */
    OBSERVE_COMPUTE_PROBE_FAILURE_BUSY = 5 /* 活动请求遭遇新身份，不启动替代请求。 */
} ObserveComputeProbeFailure;

typedef enum {
    OBSERVE_COMPUTE_PROBE_NORMAL = 0, /* 电流处理、测量角、速度更新及电流控制。 */
    OBSERVE_COMPUTE_PROBE_SPEED_HOLD = 1, /* 保持已有速度输出，其余快环计算照常执行。 */
    OBSERVE_COMPUTE_PROBE_PREDICTED_ANGLE = 2, /* 分开计算测量角和电压施加预测角。 */
    OBSERVE_COMPUTE_PROBE_LIMITED = 3, /* 速度电流限幅与电流控制电压限幅。 */
    OBSERVE_COMPUTE_PROBE_INVALID_MEASUREMENT = 4, /* 合成坏电流样本被正式测量模块拒绝。 */
    OBSERVE_COMPUTE_PROBE_INVALID_CONTROLLER = 5 /* 完成前段计算后拒绝无效母线标志。 */
} ObserveComputeProbeCase;

/* 各段独立读取起止 stamp；total 不是分段相减或相加的估算。 */
typedef enum {
    OBSERVE_COMPUTE_PROBE_SEGMENT_TOTAL = 0, /* 完整单例，包括分段测量开销。 */
    OBSERVE_COMPUTE_PROBE_SEGMENT_MEASUREMENT = 1, /* 电流校正与重构。 */
    OBSERVE_COMPUTE_PROBE_SEGMENT_MEASUREMENT_ANGLE = 2, /* 测量时刻电角度。 */
    OBSERVE_COMPUTE_PROBE_SEGMENT_ACTUATION_ANGLE = 3, /* 仅双角算例计算施加角。 */
    OBSERVE_COMPUTE_PROBE_SEGMENT_SPEED = 4, /* 速度更新或保持已有输出。 */
    OBSERVE_COMPUTE_PROBE_SEGMENT_CURRENT = 5 /* 电流控制计算，包括预期拒绝路径。 */
} ObserveComputeProbeSegment;

typedef struct {
    uint32_t count; /* 本段接受次数；未执行段保持为零。 */
    uint32_t min_cycles; /* 所有已接受段的最小耗时 [CPU cycle]。 */
    uint32_t max_cycles; /* 所有已接受段的最大耗时，包含抢占。 */
    uint64_t sum_cycles; /* 所有已接受段的完整 64 位总耗时。 */
    uint32_t clean_count; /* IRQ 进入计数未变化的段样本数。 */
    uint32_t clean_min_cycles; /* clean 样本最小耗时；尚无 clean 样本时为零。 */
    uint32_t clean_max_cycles; /* clean 样本最大耗时。 */
    uint64_t clean_sum_cycles; /* clean 样本完整 64 位总耗时。 */
    uint32_t irq_entries; /* 已接受段中 IRQ 进入计数差的累计值。 */
} ObserveComputeProbeTiming;

typedef struct {
    uint32_t count; /* 本算例已接受的有效计时次数，完成时为 16。 */
    uint32_t min_cycles; /* 最小前后读数差 [CPU cycle]；count=0 时为零。 */
    uint32_t max_cycles; /* 最大前后读数差 [CPU cycle]，包含测量开销和中断抢占。 */
    uint64_t sum_cycles; /* 所有已接受读数差之和，用于主机计算均值。 */
    float last_duty[3]; /* 最近一次诊断 duty；仅存储，绝不送往硬件。 */
    bool last_output_valid; /* 最近一次电流控制输出有效；预期拒绝路径为 false。 */
    bool last_voltage_limited; /* 最近一次正式电流控制的电压限幅标志。 */
    bool last_speed_limited; /* 最近一次速度输出的限流标志，包括保持路径。 */
    ObserveComputeProbeTiming segments[OBSERVE_COMPUTE_PROBE_SEGMENT_COUNT]; /* 分段独立统计。 */
    float last_phasor[4]; /* 测量 sin/cos、施加 sin/cos；未计算施加角时复制测量值。 */
} ObserveComputeProbeCaseResult;

typedef struct {
    uint32_t request_sequence; /* 本次显式请求身份，非零且不得等于上次身份。 */
    ObserveComputeProbeState state; /* COMPLETE 才表示所有算例和固定次数完整。 */
    ObserveComputeProbeFailure failure; /* 第一次失败原因，结束后冻结。 */
    uint32_t clock_hz; /* 调用方确认的 CPU cycle 计数频率 [Hz]，不是控制时域频率。 */
    uint32_t max_elapsed_cycles; /* 单例允许的正读数差上界，必须小于 2^31。 */
    uint32_t completed_steps; /* 已接受计时样本总数，完整时为 6*16。 */
    uint32_t next_case; /* ACTIVE 时下一例下标；结束时只作为诊断。 */
    ObserveComputeProbeCaseResult cases[OBSERVE_COMPUTE_PROBE_CASE_COUNT]; /* 每例独立统计。 */
} ObserveComputeProbeResult;

/* 回调只读取连续运行的 32 位 cycle 计数器；失败返回 false，不修改或复位计数器。 */
typedef bool (*ObserveComputeProbeClockRead)(void *context, uint32_t *cycles);

/* 调用方须保证一份 stamp 的 cycle 和 IRQ 进入计数是同一读取边界的稳定快照。 */
typedef struct {
    uint32_t cycles; /* 连续运行的 CPU cycle 计数器；不在本模块复位。 */
    uint32_t irq_entries; /* 连续递增的 IRQ 进入计数，不是当前中断嵌套深度。 */
} ObserveComputeProbeStamp;

typedef bool (*ObserveComputeProbeStampRead)(void *context, ObserveComputeProbeStamp *stamp);

/* Target 私有后台单写对象；与实时控制实例、观测、标定数据及功率许可完全独立。 */
typedef struct {
    ObserveComputeProbeResult result; /* 当前请求统计和冻结结果。 */
    CurrentMeasurement measurement; /* 合成 B/C 样本使用的独立校正和重构实例。 */
    ElectricalAngle measurement_angle; /* 合成位置到电流测量时刻的独立映射。 */
    ElectricalAngle actuation_angle; /* 合成位置到目标施加时刻的独立映射。 */
    CurrentController current; /* 本诊断独有的 PI 历史，绝不复用正式 Runtime。 */
    SpeedController speed; /* 本诊断独有的速度 PI 历史。 */
    SpeedControllerOutput held_speed; /* 速度保持算例使用的独立已有效输出。 */
} ObserveComputeProbe;

/* 清空全部诊断和算法实例；空指针无效果。不得用于活动请求的异步取消。 */
void observe_compute_probe_init(ObserveComputeProbe *probe);

/*
 * 由后台串行调用。request!=0、clock_hz>0、0<max_elapsed_cycles<2^31 且
 * permitted=true 时装配固定合成参数并进入 ACTIVE；不采集或学习真实标定。
 * 相同的非零 request 完全忽略，不覆盖旧结果；失败后须提交不同身份才可重试。
 * 活动中收到不同身份会清除旧结果并记录新身份 FAILED/BUSY，不运行替代请求。
 * 配置、独立两点合成校零和速度保持初值的建立不属于 step 计时窗口。
 */
bool observe_compute_probe_start(ObserveComputeProbe *probe, uint32_t request,
    uint32_t clock_hz, uint32_t max_elapsed_cycles, bool permitted);

/*
 * 每次仅计算一例，固定六例轮转共 16 轮；不循环等待、不屏蔽中断、不操作功率。
 * permitted 必须由调用方在当拍核实，失效立即 FAILED/NOT_READY 且不读时钟。
 * 两次 clock_read 包围正式测量、角度、速度和电流算法计算；统计写入在窗口外。
 * uint32 相减支持一次计数回绕；零差值、差值超上界或读取失败均 FAILED/CLOCK。
 * 调用方须保证两次读取间不可能跨越整整 2^32 cycle，否则仅凭读数无法辨别多圈。
 * IRQ 保持开启时 elapsed 包含抢占和测量开销，只是后台算例的观测耗时，
 * 不能作为完整快环 ISR 时长、stage 截止通过或 WCET 的证明。
 * 返回仅表示本次调用 ACTIVE->COMPLETE/FAILED，其他状态无副作用。
 */
bool observe_compute_probe_step(ObserveComputeProbe *probe, bool permitted,
    ObserveComputeProbeClockRead clock_read, void *clock_context);

/*
 * 与旧 step 共用正式算例和轮转输入；trig/context 只替换最终三角函数计算。
 * total 和每个实际执行段分别读取两个 stamp，速度保持段也计时；跳过段不读。
 * 所有统计在 total 末读后一起提交；任意读取失败、零耗时或超上界均 CLOCK，
 * 算法失败为 ALGORITHM，两者都不提交本步的部分统计；此前已接受统计继续保留。
 * uint32 差支持 cycle 和 IRQ 进入计数各自一次回绕；调用方排除整圈计数歧义。
 * IRQ 差为零计入 clean，否则累计抢占次数；clean 只对计数器覆盖的 IRQ 有意义，
 * 不代表完整 ISR WCET 或提交截止验收。顶层 count/min/max/sum 同步为 total 兼容视图。
 * 旧 step 仍只读两次 clock，不填 segments；两个入口不得交错用于同一请求的性能比较。
 */
bool observe_compute_probe_step_profiled(ObserveComputeProbe *probe, bool permitted,
    ElectricalAngleTrig trig, void *trig_context, ObserveComputeProbeStampRead stamp_read,
    void *stamp_context);

/* 只在 ACTIVE 锁存失败并保留已接受统计；无效或 NONE reason 统一为 ABORTED。 */
bool observe_compute_probe_abort(ObserveComputeProbe *probe, ObserveComputeProbeFailure reason);

/*
 * 仅供同一后台调用者在 step 后、结果发布前复核失败时使用；不是异步功率保护。
 * ACTIVE 或刚完成的 COMPLETE 转为 FAILED/NOT_READY，保留本次身份和全部已计统计。
 * IDLE/FAILED 无副作用，不能覆盖首个失败原因；返回是否发生状态变化。
 */
bool observe_compute_probe_revoke(ObserveComputeProbe *probe);

#endif
