#ifndef POSITION_OBSERVATION_H
#define POSITION_OBSERVATION_H

#include <stdbool.h>

#include "control_time.h"

typedef enum
{
    POSITION_TOPOLOGY_PERIODIC = 0,
    POSITION_TOPOLOGY_CONTINUOUS
} PositionTopology;

/*
 * encoder-derived mechanical position observation，单位为 mechanical turn。
 *
 * PERIODIC Target 的 mechanical_position_turns 使用规范化 [0, 1) 周期坐标。
 * CONTINUOUS 只用于位置源真实提供的累计机械位置；Platform 不把单圈绝对位置
 * 自行 unwrap 后伪装成 CONTINUOUS。
 *
 * referenced 是动态 reference 状态，不表达 pole pairs、motor direction
 * 或 electrical offset。
 */
typedef struct
{
    float mechanical_position_turns;
    ControlTime observation_time;
    bool referenced;
    bool acquisition_valid;
} PositionObservation;

/* 返回当前 Target/source 的 bind-time immutable position topology。 */
PositionTopology position_observation_get_topology(void);

/*
 * 准备位置采集和 publication，同时保持 power disabled。
 * fast-control ISR 不得因为本能力阻塞等待 SPI / SSI / I2C 等采集完成。
 */
bool position_observation_arm(void);

/*
 * 读取一份 coherent、non-blocking、bounded-time latest snapshot。
 *
 * true  表示 out_observation 得到完整快照；
 * false 表示当前没有可交付的 coherent publication，调用方不得读取输出对象。
 *
 * false 不等价于一次传感器采集失败；真正 acquisition failure 由成功读取到的
 * observation.acquisition_valid == false 表达。
 */
bool position_observation_get_latest(
    PositionObservation *out_observation);

#endif /* POSITION_OBSERVATION_H */
