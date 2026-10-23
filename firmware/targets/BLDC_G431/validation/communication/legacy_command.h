#ifndef G431_VALIDATION_LEGACY_COMMAND_H
#define G431_VALIDATION_LEGACY_COMMAND_H

#include "motor.h"

/* 旧裸机Validation的提交结果，不是Motor执行结果或正式协议应答。 */
typedef enum {
    TARGET_RUNTIME_SUBMIT_QUEUED = 0,
    TARGET_RUNTIME_SUBMIT_BUSY = 1,
    TARGET_RUNTIME_SUBMIT_REJECTED = 2
} TargetRuntimeSubmitResult;

/* Observe保留旧命令入口；正式Run仅由测试侧适配到RuntimeRequest。
 * NULL/非法请求不产生运动意图，提交成功不等于已启转。
 */
TargetRuntimeSubmitResult target_runtime_submit_command(const MotorCommand *command);

#endif
