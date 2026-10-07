#ifndef BUS_OBSERVATION_H
#define BUS_OBSERVATION_H

#include <stdbool.h>

#include "control_time.h"

/*
 * DC Bus 的物理电压观测，单位为 V。
 * freshness、软件 over/undervoltage policy 不属于 Platform。
 */
typedef struct
{
    float voltage_v;
    ControlTime observation_time;
    bool acquisition_valid;
} BusObservation;

/* 准备 Bus acquisition / publication，同时保持 power disabled。 */
bool bus_observation_arm(void);

/*
 * 读取一份 coherent、non-blocking、bounded-time latest snapshot。
 *
 * true  表示 out_observation 得到完整快照；
 * false 表示当前没有可交付的 coherent publication，调用方不得读取输出对象。
 *
 * false 不等价于一次采集失败；真正 acquisition failure 由成功读取到的
 * observation.acquisition_valid == false 表达。
 */
bool bus_observation_get_latest(
    BusObservation *out_observation);

#endif /* BUS_OBSERVATION_H */
