#ifndef BLDC_G431_PROTECTION_PROBE_H
#define BLDC_G431_PROTECTION_PROBE_H

#include <stdbool.h>
#include <stdint.h>

/* Target 私有的台架注入接口；正式保护路径不调用。 */
typedef enum {
    PROTECTION_PROBE_NONE = 0,
    PROTECTION_PROBE_OVP = 1,
    PROTECTION_PROBE_OCP_C = 2,
    PROTECTION_PROBE_OCP_AB = 3
} ProtectionProbeSource;

typedef enum {
    PROTECTION_PROBE_PREPARED = 1,
    PROTECTION_PROBE_OBSERVED = 2,
    PROTECTION_PROBE_REJECTED = 3,
    PROTECTION_PROBE_NOT_OBSERVED = 4
} ProtectionProbeOutcome;

/* 由 ADC 维护序列锁；读取前后的 sequence 必须相同且为偶数。
 * 证据在软件 safe-off 之前采集；elapsed_cycles 是轮询观测间隔，
 * 不是从比较器到栅极的传播延迟。
 * 比较器位图：COMP1=bit0（C）、COMP2=bit1（B）、COMP3=bit2（VBUS）、COMP4=bit3（A）。
 * 清除注入只更新 held 状态，原有证据仍可读取。 */
typedef struct {
    uint32_t sequence;
    uint32_t request_sequence;
    uint32_t source;
    uint32_t outcome;
    uint32_t held;
    uint32_t original_code;
    uint32_t expected_comp_mask;
    uint32_t expected_break_flag;
    uint32_t bdtr_before;
    uint32_t sr_before;
    uint32_t comp_before;
    uint32_t bdtr_after;
    uint32_t sr_after;
    uint32_t comp_after;
    uint32_t start_cycles;
    uint32_t elapsed_cycles;
    uint32_t poll_count;
} ProtectionProbeCapture;

extern volatile ProtectionProbeCapture g_target_validation_probe;

/* 准备阶段要求 MOE=0；只准备软件状态，不修改硬件。 */
bool protection_probe_prepare(ProtectionProbeSource source, uint32_t request_sequence);
bool protection_probe_is_held(void);
/* 调用方必须先屏蔽中断，并在任何结果下先 safe-off，再恢复中断。
 * 仅将一路 DAC 阈值降为零，硬件 Break 保持使能。
 * 即使测试失败，注入也保持生效，直到在空闲状态下显式清除。 */
ProtectionProbeOutcome protection_probe_fire(void);
/* 要求 MOE=0；仅恢复保存的阈值，不重新布防或使能功率。 */
bool protection_probe_clear(void);

#endif
