#ifndef CONTROL_TIME_H
#define CONTROL_TIME_H

#include <stdint.h>

/*
 * ControlTime（控制时间）是 Firmware 内部统一的单调控制时间坐标。
 *
 * 它不是 UTC、系统墙上时间、HAL Tick 或 RTOS Tick。
 * Phase Current / Position / Bus 的 observation_time 必须属于同一个
 * 可比较的 Control Time Domain。
 *
 * tick 的实际分辨率由当前 Target 声明，本类型不固定为 us / ns。
 */
typedef uint64_t ControlTime;

#endif /* CONTROL_TIME_H */
