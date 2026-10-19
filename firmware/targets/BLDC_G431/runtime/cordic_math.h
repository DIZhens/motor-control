#ifndef BLDC_G431_CORDIC_MATH_H
#define BLDC_G431_CORDIC_MATH_H

#include <stdbool.h>
#include <stdint.h>

/* 固定次数是故障边界，不表示 CORDIC 的实测周期数。 */
#define G431_CORDIC_READY_POLL_LIMIT (64U)

typedef struct {
    volatile uint32_t busy; /* 使用独占访问指令获取，拒绝重入而不屏蔽中断。 */
    bool initialized; /* 仅启动时验证生成配置并取得唯一外设所有权。 */
    bool quarantined; /* 外设状态异常后锁存，直到 MCU 重启才允许重新使用。 */
} G431CordicMath;

/*
 * 仅在启动阶段、串行且尚无其他使用者时调用；context 必须保持到 MCU 重启。
 * CubeMX 仅开启 CORDIC 时钟；本函数接受复位 CSR 或已经符合本模块的配置，
 * 然后仅在首次 init 配置 COSINE、6 cycles、scale 0、两个 32 位输入与输出，
 * 关闭 DMA/IRQ。调用前外设不得有未读结果或未完成事务。
 * 不写时钟或复位寄存器。同一对象重复 init 不重新配置或清除失败；不同对象
 * 不能取得已被占用的单个 CORDIC。首次验证失败也保持隔离。
 */
bool g431_cordic_math_init(G431CordicMath *context);

/*
 * turns 为已归一化电角度 [0,1) [turn]；成功同时发布 sine/cosine，失败不改输出。
 * 每次显式写角度和 INT32_MAX 模长，再读 cosine、sine；不依赖复位默认模长。
 * 有界轮询 RRDY，异常仅在结果已就绪时排空，并永久隔离，不复位外设或采样时钟。
 * 当前只供后台无功率对照；禁止其他代码、IRQ 或 DMA 绕过本模块访问 CORDIC。
 * busy 只拒绝重入，不提供任务排队或时限保证；不接受两个输出指向同一地址。
 */
bool g431_cordic_sincos(void *context, float turns, float *sine, float *cosine);

#endif
