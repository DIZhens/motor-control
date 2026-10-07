#ifndef BLDC_G431_CONTROL_TIME_SOURCE_H
#define BLDC_G431_CONTROL_TIME_SOURCE_H

#include "control_time.h"

/*
 * G431 Target 的 Control Time 由同步相电流 fast tick 推进。
 * 当前 time domain 使用 20 kHz fast tick 作为 1 tick。
 */
void control_time_source_reset(void);
ControlTime control_time_source_advance_fast_tick(void);
ControlTime control_time_source_now(void);

#endif /* BLDC_G431_CONTROL_TIME_SOURCE_H */
