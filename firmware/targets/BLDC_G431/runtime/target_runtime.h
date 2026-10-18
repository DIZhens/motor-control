#ifndef BLDC_G431_TARGET_RUNTIME_H
#define BLDC_G431_TARGET_RUNTIME_H

#include <stdint.h>

/* Observe 入口的锁存诊断；当前没有清除或功率使能路径。 */
enum {
    TARGET_RUNTIME_FAULT_STARTUP = 1U << 0,
    TARGET_RUNTIME_FAULT_CURRENT = 1U << 1,
    TARGET_RUNTIME_FAULT_CADENCE = 1U << 2,
    TARGET_RUNTIME_FAULT_TRIP = 1U << 3,
    TARGET_RUNTIME_FAULT_UNEXPECTED_POWER = 1U << 4,
    TARGET_RUNTIME_FAULT_CLOCK = 1U << 5
};

/* TIM6 独立写入；ADC 不再执行时，不能靠旧 Motor 快照判断健康。 */
extern volatile uint32_t g_target_runtime_guard_faults;

/* 一次装配，默认关断；不包含 stage、enable 或保护注入命令。 */
void target_runtime_start(void);
/* 主循环轮询 USART1，一次至多收/发一个字节，不等待外设。 */
void target_runtime_service(void);
/* 复用已配置的 TIM6_DAC IRQ，仅处理 TIM6 update。 */
void target_runtime_guard_irq(void);

#endif
