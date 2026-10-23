#ifndef BLDC_G431_RAW_ADC_CAPTURE_H
#define BLDC_G431_RAW_ADC_CAPTURE_H

#include <stdint.h>

/* Target 私有、仅诊断构建启用；不是 HCC 或通信对象。主机只写 request。
 * 每次启动最多接受一次请求；终态后整个记录不再写入，读回须复核终态和镜像身份。
 * 不改变功率状态；若时基停止，主机超时退出，禁止自动重试或重启。
 * first_slot 起每拍递增 1，ControlTime 每拍递增 2（100 us）。当前 SSI/Bus
 * 分频器从启动的第 10 拍开始调度；slot % 10 是调度相位，不是物理 SPI 边沿。
 */
#define G431_RAW_ADC_CAPTURE_REQUEST (0x52415731U)
#define G431_RAW_ADC_CAPTURE_PAIRS (512U)

typedef enum {
    G431_RAW_ADC_IDLE = 0,
    G431_RAW_ADC_COLLECTING = 1,
    G431_RAW_ADC_COMPLETE = 2,
    G431_RAW_ADC_REJECTED = 3,
    G431_RAW_ADC_ABORTED = 4
} G431RawAdcCaptureState;

typedef enum {
    G431_RAW_ADC_OK = 0,
    G431_RAW_ADC_POWER = 1,
    G431_RAW_ADC_INVALID_PAIR = 2,
    G431_RAW_ADC_EPOCH = 3,
    G431_RAW_ADC_DISCONTINUITY = 4,
    G431_RAW_ADC_TIMING = 5,
    G431_RAW_ADC_BOUNDS = 6
} G431RawAdcCaptureReason;

typedef struct {
    uint32_t state, count, reason, first_slot, last_slot, epoch, max_copy_cycles, reserved;
    uint64_t first_time, last_time;
    uint32_t first_entry_cycles, last_entry_cycles;
    uint16_t raw[G431_RAW_ADC_CAPTURE_PAIRS][2]; /* [B=ADC2, C=ADC1]，未换算/校零。 */
} G431RawAdcCapture;

#if defined(BLDC_G431_RAW_ADC_CAPTURE)
extern volatile uint32_t g_g431_raw_adc_request;
extern volatile G431RawAdcCapture g_g431_raw_adc_capture;
#endif

#endif
