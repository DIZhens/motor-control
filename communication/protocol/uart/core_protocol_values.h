#ifndef CORE_PROTOCOL_VALUES_H
#define CORE_PROTOCOL_VALUES_H

#include "core_protocol.h"
#include "motor_node_profile.h"

/* 与传输/协议统计装配时按名称赋值，线上顺序由序列化器明确给出。 */
typedef struct {
    uint32_t session_id;
    uint32_t rx_bytes;
    uint32_t tx_bytes;
    uint32_t event_drop_count;
    uint32_t rx_discontinuities;
    uint32_t rx_dma_overruns;
    uint32_t uart_pe;
    uint32_t uart_fe;
    uint32_t uart_ne;
    uint32_t uart_ore;
    uint32_t cobs_errors;
    uint32_t crc_errors;
    uint32_t length_errors;
    uint32_t version_errors;
    uint32_t message_errors;
    uint32_t parser_timeouts;
    uint32_t response_fifo_high_water;
    uint32_t completion_fifo_high_water;
    uint32_t event_fifo_high_water;
    uint32_t telemetry_overwrites;
    uint32_t communication_stack_min_words;
    uint32_t telemetry_stack_min_words;
    uint32_t internal_fault_reason;
    uint32_t internal_fault_count;
    uint32_t tx_busy_count;
    uint32_t tx_errors;
} CoreCommunicationStatus;
/* 仅为编解码值容器，不是对象存储、参数权威或 Motor 私有状态。 */
typedef union {
    CoreDeviceInfo device_info;
    CoreMotorSnapshot motor_snapshot;
    CoreCommunicationStatus communication_status;
    CoreSpeedPi speed_pi;
    CoreTelemetryConfig telemetry_config;
} CoreObject;

/* write_value=true 时使用精确写布局；只校验格式/固定值域，不判定状态或执行写入。 */
size_t core_object_size(uint16_t object_id, bool write_value);
CoreResult core_object_encode(uint16_t object_id, bool write_value, const CoreObject *value,
    uint8_t *output, size_t capacity, size_t *length);
/* 失败不修改 value；只读对象用写布局返回 READ_ONLY。 */
CoreResult core_object_decode(uint16_t object_id, bool write_value,
    const uint8_t *data, size_t length, CoreObject *value);

typedef struct {
    uint16_t message_id, subject_id;
    uint32_t proposed_session_id;
    float target_velocity_rpm;
    CoreObject value;
} CoreRequest;

/* 仅接受 Header 语义正确的 REQUEST。失败仍保留可提取的 message/subject，
 * 其余字段不可使用。无会话、Sequence、去重、权限或 Motor 调用。
 */
CoreResult core_request_decode(const CoreFrame *frame, CoreRequest *request);
CoreResult core_request_encode(const CoreRequest *request, uint8_t *output,
    size_t capacity, size_t *length);

typedef struct {
    uint16_t result_code, subject_id, motor_result;
    uint32_t accepted_session_id;
    CoreObject value;
} CoreResponse;
CoreResult core_response_encode(uint16_t message_id, const CoreResponse *response,
    uint8_t *output, size_t capacity, size_t *length);
CoreResult core_response_decode(uint16_t message_id, const uint8_t *data,
    size_t length, CoreResponse *response);

/* 缺有效位或非有限浮点在编码时规范为 NaN 并清位；解码严格核对规范值。 */
CoreResult core_sample_encode(const CoreSpeedSample *sample, uint8_t *output,
    size_t capacity, size_t *length);
CoreResult core_sample_decode(const uint8_t *data, size_t length, CoreSpeedSample *sample);

/* 两种固定事件共用布局，但两个u32的业务含义由 message_id 决定。 */
typedef struct { uint32_t first, second; uint64_t control_time_ticks; } CoreEvent;
CoreResult core_event_encode(uint16_t message_id, const CoreEvent *event,
    uint8_t *output, size_t capacity, size_t *length);
CoreResult core_event_decode(uint16_t message_id, const uint8_t *data,
    size_t length, CoreEvent *event);

#endif
