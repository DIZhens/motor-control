/* 仅供有限Run模型复用旧VOFA输入；不进入Keil，不保存第二套命令状态。 */
#include "target_runtime.h"
#include "legacy_command.h"
#include <stddef.h>
#include <string.h>

TargetRuntimeSubmitResult target_runtime_submit_command(const MotorCommand *command)
{
    RuntimeRequest request;
    RuntimeExchangeResult result;
    uint32_t ticket, cancelled_ticket;

    if (command == NULL) { return TARGET_RUNTIME_SUBMIT_REJECTED; }
    memset(&request, 0, sizeof(request));
    request.kind = RUNTIME_REQUEST_MOTOR;
    request.value.motor = *command;
    result = target_runtime_submit_request(&request, &ticket, &cancelled_ticket);
    /* 旧输入夹具没有协议事务；ticket/取消的完整验证由Exchange模型承担。 */
    if (result == RUNTIME_EXCHANGE_QUEUED) { return TARGET_RUNTIME_SUBMIT_QUEUED; }
    if (result == RUNTIME_EXCHANGE_BUSY) { return TARGET_RUNTIME_SUBMIT_BUSY; }
    return TARGET_RUNTIME_SUBMIT_REJECTED;
}
