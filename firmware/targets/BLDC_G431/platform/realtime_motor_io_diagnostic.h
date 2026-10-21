#ifndef BLDC_G431_REALTIME_MOTOR_IO_DIAGNOSTIC_H
#define BLDC_G431_REALTIME_MOTOR_IO_DIAGNOSTIC_H

#include <stdint.h>

typedef enum {
    G431_TIMING_FAILURE_EARLY_UPDATE = 1,
    G431_TIMING_FAILURE_UPDATE_STATE = 2,
    G431_TIMING_FAILURE_DELIVERY = 3,
    G431_TIMING_FAILURE_ARM_WINDOW = 4,
    G431_TIMING_FAILURE_ADC_ENTRY = 5,
    G431_TIMING_FAILURE_CONSUMER_RETURN = 6,
    G431_TIMING_FAILURE_IRQ_EXIT = 7
} G431TimingFailureReason;

/* 只保存本次启动的首次时序故障；先撤销输出，再发布偶数版本。
 * 不读有清标志副作用的ADC DR，Stop和后续采样不覆盖记录。
 * observed_cycles为故障分支内采集的时刻，不是未插桩固件WCET。 */
typedef struct {
    uint32_t sequence, reason, slot_sequence, callback_slot;
    uint32_t observed_cycles, slot_start_cycles, irq_entry_cycles, callback_start_cycles;
    uint32_t consumer_entry_cycles, consumer_exit_cycles, auxiliary_exit_cycles, reserved;
    uint32_t tim_count, tim_status, tim_control, state_bits;
    uint32_t adc1_status, adc2_status, actuation_epoch, callback_epoch;
    uint64_t observation_time; /* ControlTime单位：50 us。 */
} G431TimingFailure;

extern volatile G431TimingFailure g_g431_timing_failure;

/* Target 私有计时入口；仅在同步 ADC consumer 内读取本拍原有锚点。
 * 不创建窗口、不延长截止、不改变 DWT 或定时器。两个输出必须非空。 */
void realtime_motor_io_stage_anchor(uint32_t *cycles, uint32_t *entry_count);

#endif
