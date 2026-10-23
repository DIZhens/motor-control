#ifndef CAN_FRAME_PORT_H
#define CAN_FRAME_PORT_H

#include <stdbool.h>
#include <stdint.h>

/* Classical CAN硬件帧事实；扩展帧/RTR可被表示以明确拒绝，不支持CAN FD。 */
typedef struct {
    uint16_t id;
    uint8_t dlc;
    bool extended, remote;
    uint8_t data[8];
} CanFrame;

typedef enum {
    CAN_FRAME_ACCEPTED = 0, CAN_FRAME_EMPTY, CAN_FRAME_BUSY,
    CAN_FRAME_NOT_STARTED, CAN_FRAME_HARDWARE_ERROR, CAN_FRAME_BAD_ARGUMENT
} CanFrameResult;

typedef struct {
    uint32_t rx_frames, tx_frames, rx_lost, tx_errors, bus_off_count;
    bool started, tx_in_flight, error_warning, error_passive, bus_off, recovering;
} CanFrameStatus;

enum {
    CAN_FRAME_RX_AVAILABLE = 1U, CAN_FRAME_TX_COMPLETE = 2U,
    CAN_FRAME_ERROR_CHANGED = 4U, CAN_FRAME_RX_LOST = 8U
};

/* 通知不携带协议意图；sink只唤醒唯一Communication所有者。 */
typedef void (*CanFrameEventSink)(void *context, uint32_t events, bool from_interrupt);
CanFrameResult can_frame_init(void);
CanFrameResult can_frame_bind_event_sink(CanFrameEventSink sink, void *context);
CanFrameResult can_frame_start(void);
CanFrameResult can_frame_pop(CanFrame *frame);
/* 接受后完整复制到硬件；不得覆盖在途帧，也不承诺已经取得真实ACK。 */
CanFrameResult can_frame_try_send(const CanFrame *frame);
bool can_frame_read_status(CanFrameStatus *status);
CanFrameResult can_frame_request_recovery(void);

#endif
