#ifndef MOTOR_VOFA_H
#define MOTOR_VOFA_H

#include "motor_snapshot.h"
#include "vofa.h"

#include <stdbool.h>

/* 完整映射请求；未知类型或空指针返回 false，且不改写输出。 */
bool motor_vofa_command(const VofaCommand *source, MotorCommand *destination);

/*
 * 将已取得的一致性业务快照映射到既有十通道格式。
 * destination 为空时不操作；source 为空时将非空输出清零并保持全部无效。
 * 数值有限性及整数精度校验仍由 vofa_encode_telemetry 收紧。
 */
void motor_vofa_telemetry(const MotorSnapshot *source, VofaTelemetry *destination);

#endif
