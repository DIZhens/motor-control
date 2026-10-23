#ifndef MOTOR_SNAPSHOT_H
#define MOTOR_SNAPSHOT_H

#include "motor.h"

#include <stdbool.h>
#include <stdint.h>

/*
 * Motor 对外发布的业务值快照，不包含协议格式或实时私有状态指针。
 * 发布者负责一次性取得同一版本；读者不能用该结构直接修改运行状态。
 */
typedef struct {
    uint64_t control_time_ticks; /* 单调控制时间；单位由下列频率说明。 */
    uint32_t control_time_ticks_per_second; /* 控制时间单位，tick/s。 */
    MotorState state; /* 对外业务生命周期。 */
    uint32_t faults; /* Motor 已合入的锁存故障位图。 */
    MotorCommandResult command_result; /* 最近一次完整请求的处理结果。 */
    float target_velocity_rpm; /* 已接受的机械速度目标，rpm。 */
    float velocity_rpm; /* 机械速度 rpm；电角度方向确认后统一正向，否则保留编码器坐标。 */
    float id_a; /* d 轴电流，A；尚无有效实现时保持无效。 */
    float iq_a; /* q 轴电流，A；尚无有效实现时保持无效。 */
    float bus_voltage_v; /* 物理母线电压，V。 */
    bool status_valid; /* state / faults / target / command_result 同组有效。 */
    bool control_time_valid;
    bool velocity_valid;
    bool id_valid;
    bool iq_valid;
    bool bus_voltage_valid;
} MotorSnapshot;

#endif
