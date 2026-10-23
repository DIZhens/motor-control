#ifndef PROTOCOL_DEVICE_PORT_H
#define PROTOCOL_DEVICE_PORT_H

#include "motor_node_profile.h"

/* 设备意图与完成信息不包含Session、Sequence、链路ID或Firmware/内核类型。 */
typedef enum {
    PROTOCOL_REQUEST_MOTOR = 0, PROTOCOL_REQUEST_SPEED_PI, PROTOCOL_REQUEST_TELEMETRY
} ProtocolRequestKind;

typedef struct {
    ProtocolRequestKind kind;
    union {
        struct { uint16_t command_id; float target_velocity_rpm; } motor;
        struct { float kp, ki; } speed_pi;
        struct { bool enabled; uint16_t rate_hz; } telemetry;
    } value;
} ProtocolDeviceRequest;

typedef enum {
    PROTOCOL_COMPLETED = 0, PROTOCOL_CANCELLED_BY_STOP, PROTOCOL_PRECONDITION_FAILED
} ProtocolCompletionOutcome;

typedef struct {
    uint32_t ticket; /* 本启动不重用的内部关联，不是线上序号。 */
    ProtocolRequestKind kind;
    ProtocolCompletionOutcome outcome;
    CoreResult result_code;
    uint16_t motor_result; /* 未进入Motor时为0xFFFF。 */
    union { CoreSpeedPi speed_pi; CoreTelemetryConfig telemetry; } value;
} ProtocolDeviceCompletion;

/* 协议检测到的内部不变量失败；Firmware绑定负责映射到本机首因域。 */
typedef enum {
    PROTOCOL_FAULT_RESPONSE_OVERFLOW = 0,
    PROTOCOL_FAULT_TRANSACTION_CORRUPT,
    PROTOCOL_FAULT_TRANSPORT,
    PROTOCOL_FAULT_TIMEOUT
} ProtocolDeviceFault;

typedef enum {
    PROTOCOL_SUBMIT_QUEUED = 0, PROTOCOL_SUBMIT_BUSY, PROTOCOL_SUBMIT_BAD_REQUEST,
    PROTOCOL_SUBMIT_FAILED, PROTOCOL_SUBMIT_PRECONDITION_FAILED
} ProtocolSubmitResult;

typedef struct {
    CoreSpeedSample sample;
    uint32_t subscription_id, produced, subscription_first; /* Mailbox产生域；只有后台读取，不由协议生产样本。 */
} ProtocolTelemetryValue;

/* 链接期固定绑定；读取完整副本，提交完整意图，不暴露Motor/ControlRuntime实例。 */
CoreResult protocol_device_read_object(uint16_t object, MotorNodeValue *value);
ProtocolSubmitResult protocol_device_submit(const ProtocolDeviceRequest *request,
    uint32_t *ticket, uint32_t *cancelled_ticket);
bool protocol_device_completion(ProtocolDeviceCompletion *completion);
bool protocol_device_permitted(void);
void protocol_device_fail_closed(ProtocolDeviceFault reason);
/* 单消费者内部订阅，不携带Session/Sequence。begin替换旧域，返回非零身份；
 * 0表示未能建立可信订阅，不能继续开放会话。end撤销旧域并清槽。 */
uint32_t protocol_device_telemetry_begin(void);
void protocol_device_telemetry_end(void);
bool protocol_device_telemetry_peek(ProtocolTelemetryValue *value);
void protocol_device_telemetry_retire(void);

#endif
