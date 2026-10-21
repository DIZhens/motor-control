#ifndef BLDC_G431_TARGET_RUNTIME_H
#define BLDC_G431_TARGET_RUNTIME_H

#include <stdint.h>

#include "motor_snapshot.h"
#if defined(BLDC_G431_FINITE_RUN)
#include "runtime_parameters.h"
#include "speed_commissioning.h"
#endif

#if defined(BLDC_G431_DIAGNOSTICS)
#include "current_capture.h"
#include "compute_comparison.h"
#include "stage_budget.h"
#endif

/* Target入口的锁存诊断；Clear不绕过恢复条件，Run另有独立有限守卫。 */
enum {
    TARGET_RUNTIME_FAULT_STARTUP = 1U << 0, /* 装配或能力准备失败。 */
    TARGET_RUNTIME_FAULT_CURRENT = 1U << 1, /* 同步电流采集结果无效。 */
    TARGET_RUNTIME_FAULT_CADENCE = 1U << 2, /* 采样间隔异常或采样丢失。 */
    TARGET_RUNTIME_FAULT_TRIP = 1U << 3, /* 保护快照不可用，或存在保护条件、锁存原因。 */
    TARGET_RUNTIME_FAULT_UNEXPECTED_POWER = 1U << 4, /* 功率应关闭时检测到 MOE 使能。 */
    TARGET_RUNTIME_FAULT_CLOCK = 1U << 5, /* 时钟配置异常或 DWT 计数未推进。 */
    TARGET_RUNTIME_FAULT_RUN_MOTION = 1U << 6, /* 有限运行入口的净位移越界。 */
    TARGET_RUNTIME_FAULT_INTERNAL = 1U << 7 /* 内部可信性丢失；不授予Clear恢复能力。 */
};

/* TIM6 独立写入；ADC 不再执行时，不能靠旧 Motor 快照判断健康。 */
extern volatile uint32_t g_target_runtime_guard_faults;
#if defined(BLDC_G431_DIAGNOSTICS)
/* 当前 Observe 入口所有可返回异常的进入计数，用于后台计时的抢占分类。 */
extern volatile uint32_t g_target_runtime_irq_entries[8];
#endif

/* Run Target私有只读记录；不作为协议或主机写入许可。停止后再读取完整结果。 */
typedef struct {
    uint32_t accepted_starts, active, limit_reason, started_cycles;
    uint64_t last_position_time;
    float displacement_turns, last_position_turns;
} TargetRuntimeRunStatus;
extern volatile TargetRuntimeRunStatus g_target_runtime_run_status;

#if defined(BLDC_G431_DIAGNOSTICS)
/*
 * Observe 专用无功率诊断，不属于 Motor 命令或 UART 协议。
 * 主机仅写 request 一个非零新序号；ADC 边界消费，重复序号不重放。
 * capture 仅在开始/完成/失败时锁存；读取前后 sequence 相同且为偶数才一致。
 * ACTIVE 发布不代表完整窗口；只有 COMPLETE 的 2048 点统计可用于电流对照。
 */
extern volatile uint32_t g_target_runtime_current_request;
extern volatile uint32_t g_target_runtime_current_sequence;
extern volatile ObserveCurrentCaptureResult g_target_runtime_current_capture;

/* 后台独立计算计时：仅写一次非零新 request，不使用真实控制状态或功率许可。
 * result 使用奇偶 sequence 发布；只有 COMPLETE 才含完整固定次数统计。
 * CPU cycle 读数包含抢占与测量开销，不代表完整 ISR 或 stage 截止验收。 */
extern volatile uint32_t g_target_runtime_compute_request;
extern volatile uint32_t g_target_runtime_compute_sequence;
extern volatile ObserveComputeComparisonResult g_target_runtime_compute_result;

/* 独立试具的有限提交预算窗口；只写一次新身份，不表示实物标定或真实RUN。 */
extern volatile uint32_t g_target_runtime_stage_request;
#endif

/* 后台到 ADC 邮箱的交接结果，不是 Motor 的业务处理结果或进入 RUN 的应答。 */
typedef enum {
    TARGET_RUNTIME_SUBMIT_QUEUED = 0, /* 完整请求已复制，等待实时边界处理。 */
    TARGET_RUNTIME_SUBMIT_BUSY = 1, /* 邮箱或 Stop 已占用；不覆盖原普通请求。 */
    TARGET_RUNTIME_SUBMIT_REJECTED = 2 /* 非法请求只记录拒绝事件，不产生运动意图。 */
} TargetRuntimeSubmitResult;

/*
 * 仅由同一后台所有者调用；复制完整 MotorCommand，不保留调用者指针。
 * Stop 覆盖普通请求并优先交接；后到普通请求不能越过待处理 Stop。
 * NULL 仅代表通信层已经完整接收但判为非法的拒绝事件，不是 MotorCommand；
 * 返回 REJECTED。BUSY/非法事件若遇到待处理 Stop，随后随该 Stop 一起退休。
 */
TargetRuntimeSubmitResult target_runtime_submit_command(const MotorCommand *command);

/*
 * 后台最多尝试三次复制一致快照，不屏蔽控制中断；失败时不改写输出。
 * 成功表示取得完整副本，不保证各观测有效。未合入的 guard、时钟异常或
 * ADC 心跳过期会交付全无效快照，调用者必须遵守快照内各有效位。
 */
bool target_runtime_read_snapshot(MotorSnapshot *snapshot);

#if defined(BLDC_G431_FINITE_RUN)
/* 唯一后台所有者；成功返回本启动非零ticket和被Stop取消的旧ticket。
 * 普通请求只允许一个待消费槽；不同Stop不覆盖原Stop。结果只从Completion取得。
 * 参数写也在ADC边界判定并应用，后台不能直接改Profile或PI实例。
 */
RuntimeExchangeResult target_runtime_submit_request(const RuntimeRequest *request,
    uint32_t *ticket, uint32_t *cancelled_ticket);
bool target_runtime_pop_completion(RuntimeCommandCompletion *completion);
/* 三次有界一致读取；失败不改输出。内部故障/过期观测时收紧使用资格。 */
bool target_runtime_read_parameters(RuntimeParameters *parameters);
bool target_runtime_read_commissioning(SpeedCommissioningSample *sample);
/* 幂等关断，无内核/等待；立即撤销逻辑Session资格，下一采样合并fault bit7。 */
void target_runtime_fail_closed(RuntimeInternalFaultReason reason);
bool target_runtime_session_permitted(void);
bool target_runtime_read_internal_fault(RuntimeInternalFaultStatus *status);
uint32_t target_runtime_completion_high_water(void);
#endif

/* 一次装配，默认关断；Run仅经完整Start请求执行受控启动，不提供保护注入。 */
void target_runtime_start(void);
/* 主循环至多收/发一个 UART 字节、计算一个诊断算例，不等待外设。 */
void target_runtime_service(void);
/* 复用已配置的 TIM6_DAC IRQ，仅处理 TIM6 update。 */
void target_runtime_guard_irq(void);
#if defined(BLDC_G431_DIAGNOSTICS)
/* 仅由 ADC IRQ 所有者读取当拍无功率诊断条件，不作为后台授权接口。 */
bool target_runtime_timing_permitted(void);
#endif

#endif
