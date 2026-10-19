#ifndef BLDC_G431_IRQ_TIMING_H
#define BLDC_G431_IRQ_TIMING_H

#include <stdint.h>
#include <stdbool.h>

#define G431_IRQ_TIMING_SAMPLES (256U)
#define G431_IRQ_TIMING_MAX_CYCLES (34000U)
#define G431_IRQ_TIMING_METRICS (4U)

enum {
    G431_IRQ_TIMING_IDLE = 0,
    G431_IRQ_TIMING_ACTIVE = 1,
    G431_IRQ_TIMING_COMPLETE = 2,
    G431_IRQ_TIMING_FAILED = 3
};
enum {
    G431_IRQ_TIMING_FAILURE_NONE = 0,
    G431_IRQ_TIMING_FAILURE_BUSY = 1,
    G431_IRQ_TIMING_FAILURE_ORDER = 2,
    G431_IRQ_TIMING_FAILURE_MISSING = 3,
    G431_IRQ_TIMING_FAILURE_CLOCK = 4,
    G431_IRQ_TIMING_FAILURE_NOT_READY = 5
};
enum {
    G431_IRQ_TIMING_IRQ = 0, /* IRQ USER CODE 的首尾之间，不含异常入栈/出栈及观测尾部提交。 */
    G431_IRQ_TIMING_PREFIX = 1, /* 既有 Platform cycle 锚点到 consumer 调用前。 */
    G431_IRQ_TIMING_CONSUMER = 2, /* Platform 同步调用 consumer 的首尾。 */
    G431_IRQ_TIMING_OBSERVE = 3 /* Observe sample_ready 入口到管理发布结束。 */
};

typedef struct {
    uint32_t count; /* ADC行每个合法IRQ增加；其他行仅在完整的injected嵌套出现时增加。 */
    uint32_t min_cycles;
    uint32_t max_cycles;
    uint32_t sum_cycles; /* 256*34000 <= 8704000，固定上界内不会发生32位溢出。 */
} G431IrqTimingMetric;

typedef struct {
    uint32_t schema_version; /* 当前为1；全部字段固定32位，不依赖枚举存储宽度。 */
    uint32_t request_sequence;
    uint32_t state;
    uint32_t failure;
    uint32_t irq_count;
    G431IrqTimingMetric rows[G431_IRQ_TIMING_METRICS];
} G431IrqTimingResult;

/* 调用方从共用请求邮箱传入身份；这里不再建立独立可写请求地址。 */
extern volatile uint32_t g_target_irq_timing_sequence;
extern volatile G431IrqTimingResult g_target_irq_timing_result;
/* 可在调用点先检查此标志，非活动时免除 mark 调用和额外DWT读取。 */
extern volatile uint32_t g_target_irq_timing_active;

/* 仅在ADC时基启动前调用；不读取、配置或清零任何硬件时钟。 */
void g431_irq_timing_init(void);

/*
 * 所有运行期调用属于同一个ADC IRQ单写者，不可重入，也不能由其他IRQ调用。
 * 入口接受新nonce后跳过本拍准备开销，下一IRQ开始256点；ACTIVE遇新nonce记BUSY。
 * 相同nonce不重开；0不启动也不取消。失败不自动重试；IRQ停顿时主机须有限等待。
 * cycles由调用点读取连续DWT；Platform必须传已有锚点，不得移动或重置安全时基。
 * 序列严格为irq_enter/platform_enter/consumer_enter/observe_enter/observe_exit/
 * consumer_exit/irq_exit；无injected callback的regular ADC IRQ只记录total。
 * 若出现Platform边界则要求完整嵌套，缺失、错误顺序或异常时钟失败，部分行不提交。
 * permitted仅由调用方按当拍条件判断；入口或出口撤销许可均失败，不发布COMPLETE。
 * 末尾只做4行有界整数统计；不屏蔽中断，不操作功率/采样/配置，结果仅供诊断。
 * 差值包含抢占及部分插桩开销，不等同于未插桩WCET或stage截止验收。
 */
void g431_irq_timing_irq_enter(uint32_t cycles, uint32_t request, bool permitted);
void g431_irq_timing_platform_enter(uint32_t existing_anchor_cycles);
void g431_irq_timing_consumer_enter(uint32_t cycles);
void g431_irq_timing_observe_enter(uint32_t cycles);
void g431_irq_timing_observe_exit(uint32_t cycles);
void g431_irq_timing_consumer_exit(uint32_t cycles);
void g431_irq_timing_irq_exit(uint32_t cycles, bool permitted);

#endif
