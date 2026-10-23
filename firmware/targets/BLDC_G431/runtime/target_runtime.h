#ifndef BLDC_G431_TARGET_RUNTIME_H
#define BLDC_G431_TARGET_RUNTIME_H

#include <stdint.h>

#include "motor_snapshot.h"
#if defined(BLDC_G431_FINITE_RUN)
#include "runtime_parameters.h"
#include "motor_telemetry.h"
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

/* Run Target私有只读记录；不作为协议或主机写入许可。停止后再读取完整结果。 */
typedef struct {
    uint32_t accepted_starts, active, limit_reason, started_cycles;
    uint64_t last_position_time;
    float displacement_turns, last_position_turns;
} TargetRuntimeRunStatus;
extern volatile TargetRuntimeRunStatus g_target_runtime_run_status;


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
bool target_runtime_read_motor_telemetry(MotorTelemetrySample *sample);
/* 幂等关断，无内核/等待；立即锁内部故障，下一采样合并fault bit7。 */
void target_runtime_fail_closed(RuntimeInternalFaultReason reason);
/* 仅表示未锁内部故障；不代表已建会话、可Start、无Motor/Guard故障或已订阅。 */
bool target_runtime_integrity_ok(void);
bool target_runtime_read_internal_fault(RuntimeInternalFaultStatus *status);
uint32_t target_runtime_completion_high_water(void);
#endif

/* 一次装配，默认关断；Run仅经完整Start请求执行受控启动，不提供保护注入。 */
void target_runtime_start(void);
/* 裸机验证兼容入口：至多收/发一个UART字节或计算一个诊断算例；RTOS Run不调用。 */
void target_runtime_service(void);
/* 复用已配置的 TIM6_DAC IRQ，仅处理 TIM6 update。 */
void target_runtime_guard_irq(void);

#endif
