#include "target_communication_config.h"
#if BLDC_G431_COMMUNICATION == BLDC_G431_COMMUNICATION_CAN_V1
#include "target_can_server.h"
#include "can_server.h"
#include "../runtime/target_runtime.h"
#include "main.h"
#if BLDC_G431_CAN_LOOPBACK_VALIDATION
#include "../validation/communication/can_loopback.h"
#endif

static CanServer s_can_server;
void target_can_server_start(void) { can_server_init(&s_can_server, BLDC_G431_CAN_NODE_ID); }

void target_can_server_service(void)
{
    CanFrame frame;
    CanFrameResult result;
    uint32_t batch;
    MotorSnapshot snapshot;
#if BLDC_G431_CAN_LOOPBACK_VALIDATION
    g431_can_loopback_service();
#endif
    can_server_poll(&s_can_server, HAL_GetTick());
    /* 每轮有界排空；硬件FIFO只有3项，持续输入由IRQ及1ms后台轮询继续推进。 */
    for (batch = 0U; batch < 6U && !s_can_server.failed; ++batch) {
        result = can_frame_pop(&frame);
        if (result == CAN_FRAME_EMPTY || result == CAN_FRAME_BUSY) { break; }
        if (result != CAN_FRAME_ACCEPTED) { target_runtime_fail_closed(RUNTIME_INTERNAL_TRANSPORT); break; }
        can_server_feed(&s_can_server, &frame, HAL_GetTick());
#if BLDC_G431_CAN_LOOPBACK_VALIDATION
        g431_can_loopback_server_progress(&s_can_server);
#endif
    }
    can_server_poll(&s_can_server, HAL_GetTick());
    if (s_can_server.suspended && !s_can_server.failed
        && !s_can_server.stop.active && !s_can_server.ordinary.active) {
        /* Stop已准确退休，还须当前业务及功率事实证明关断；不以链路恢复替代Motor恢复。 */
        if (!target_runtime_read_snapshot(&snapshot) || !snapshot.status_valid
            || snapshot.state == MOTOR_STATE_RUN || snapshot.target_velocity_rpm != 0.0F
            || (TIM1->BDTR & TIM_BDTR_MOE) != 0U) {
            target_runtime_fail_closed(RUNTIME_INTERNAL_TRANSACTION_CORRUPT); return;
        }
        result = can_frame_request_recovery();
        if (result == CAN_FRAME_ACCEPTED) { (void)can_server_recovered(&s_can_server); }
        else if (result != CAN_FRAME_BUSY) { target_runtime_fail_closed(RUNTIME_INTERNAL_TRANSPORT); }
    }
    can_server_flush(&s_can_server);
    can_server_observe(&s_can_server);
#if BLDC_G431_CAN_LOOPBACK_VALIDATION
    g431_can_loopback_server_progress(&s_can_server);
#endif
}
#endif
