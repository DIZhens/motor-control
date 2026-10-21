#ifndef BLDC_G431_CONTROL_TIME_SOURCE_H
#define BLDC_G431_CONTROL_TIME_SOURCE_H

#include "control_time.h"

/*
 * G431 Target 的 Control Time 由同步相电流 fast tick 推进。
 * time domain 固定 20000 tick/s；每个 10 kHz 硬件 slot 推进 2 tick。
 * 漏 slot 必须保留缺口，不能按收到的 callback 次数伪造连续时间。
 */
void control_time_source_reset(void);
ControlTime control_time_source_advance_fast_tick(void);
ControlTime control_time_source_advance_slots(uint32_t slots);
ControlTime control_time_source_now(void);

#endif /* BLDC_G431_CONTROL_TIME_SOURCE_H */
