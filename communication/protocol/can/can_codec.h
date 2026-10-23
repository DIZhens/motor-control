#ifndef CAN_CODEC_H
#define CAN_CODEC_H

#include "can_frame_port.h"
#include "motor_node_profile.h"

enum {
    CAN_V1_BROADCAST_STOP = 0x000, CAN_V1_NODE_STOP_BASE = 0x000,
    CAN_V1_FAULT_BASE = 0x080, CAN_V1_REQUEST_BASE = 0x100,
    CAN_V1_RESPONSE_BASE = 0x180, CAN_V1_STATE_BASE = 0x200, CAN_V1_MOTION_BASE = 0x280
};
enum {
    CAN_V1_OPEN_SESSION = 0, CAN_V1_START = 1, CAN_V1_UPDATE = 2,
    CAN_V1_STOP = 3, CAN_V1_CLEAR = 4
};
enum {
    CAN_V1_TARGET_VALID = 1U, CAN_V1_VELOCITY_VALID = 2U,
    CAN_V1_LIMIT_SHIFT = 8U, CAN_V1_MOTION_FLAGS_MASK = 0x0F03U
};

typedef struct {
    uint16_t session, sequence;
    uint8_t service;
    int16_t target_x100;
    bool sessionless_stop;
    CanFrame original; /* 完整规范化请求用于冲突判定，不以硬件CRC替代内容比较。 */
} CanRequest;

/* 只检查正确Node/标准数据帧/DLC=8；不符合时不提取可应答身份。 */
bool can_v1_request_envelope(uint8_t node, const CanFrame *frame);
/* 已可信的完整外壳才写request；失败时仅session/sequence/original可用于关联错误。 */
CoreResult can_v1_request_decode(uint8_t node, const CanFrame *frame, CanRequest *request);
CoreResult can_v1_request_encode(uint8_t node, uint16_t session, uint16_t sequence,
    uint8_t service, int16_t target_x100, CanFrame *frame);
CoreResult can_v1_stop_encode(uint8_t node, bool broadcast, uint16_t sequence, CanFrame *frame);
void can_v1_response(uint8_t node, uint16_t session, uint16_t sequence,
    CoreResult result, uint16_t motor_result, CanFrame *frame);
/* IEEE binary32先乘100，最近取整，半值远离0；非有限/舍入越界失败且不写输出。 */
bool can_v1_rpm_x100(float rpm, int16_t *value);
void can_v1_state(uint8_t node, uint16_t counter, uint8_t state,
    uint8_t flags, uint32_t faults, CanFrame *frame);
void can_v1_motion(uint8_t node, uint16_t sequence, const CoreSpeedSample *sample, CanFrame *frame);
void can_v1_fault(uint8_t node, uint32_t newly_latched, uint32_t all, CanFrame *frame);

#endif
