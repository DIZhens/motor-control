#ifndef BLDC_G431_BUS_OBSERVATION_INTERNAL_H
#define BLDC_G431_BUS_OBSERVATION_INTERNAL_H

#include <stdbool.h>
#include <stdint.h>
#include "control_time.h"

/* 与同一份母线publication一起交付的原始采集诊断，不定义电压保护策略。 */
typedef enum {
    G431_BUS_ACQUISITION_OK = 0,
    G431_BUS_ACQUISITION_START_FAILED = 1,
    G431_BUS_ACQUISITION_RANK_MISMATCH = 2,
    G431_BUS_ACQUISITION_VREF_ZERO = 3,
    G431_BUS_ACQUISITION_ADC_ERROR = 4
} G431BusAcquisitionCause;

typedef struct {
    ControlTime observation_time;
    uint32_t cause, hal_status, rank_count, vbus_raw, vrefint_raw;
    uint32_t adc_error, adc_state, adc_isr, adc_ier, adc_cr;
} G431BusAcquisitionDiagnostic;

bool g431_bus_acquisition_get_diagnostic(G431BusAcquisitionDiagnostic *diagnostic);

/*
 * 由 G431 唯一 fast tick 驱动 Bus regular ADC 采集调度。
 * 该接口仅供当前 Target 内部组合使用，不属于 Hardware Capability Contract。
 */
void bus_observation_fast_tick(ControlTime fast_tick_time);

#endif /* BLDC_G431_BUS_OBSERVATION_INTERNAL_H */
