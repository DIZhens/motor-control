#ifndef CORE_PROTOCOL_H
#define CORE_PROTOCOL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* 与 interface/CORE_PROTOCOL_V1.md 对应；不依赖传输、时钟或调度器。 */
#define CORE_HEADER_SIZE 16U
#define CORE_PAYLOAD_MAX 110U
#define CORE_DECODED_MAX 128U
#define CORE_ENCODED_MAX 129U
#define CORE_WIRE_MAX 130U
#define CORE_PARTIAL_TIMEOUT_MS 20U

enum {
    CORE_REQUEST = 1, CORE_RESPONSE = 2, CORE_TELEMETRY = 3, CORE_EVENT = 4
};
enum {
    CORE_OPEN_SESSION = 0x0001, CORE_READ_OBJECT = 0x0002,
    CORE_WRITE_OBJECT = 0x0003, CORE_EXECUTE_COMMAND = 0x0004,
    CORE_SPEED_SAMPLE = 0x1001, CORE_STATE_CHANGED = 0x2001,
    CORE_FAULT_LATCHED = 0x2002
};

/* FRAME_* 表示完整性可信但 Header 语义不受支持，调用方不得作为正常请求执行。 */
typedef enum {
    CORE_OK = 0, CORE_BAD_ARGUMENT, CORE_CAPACITY, CORE_COBS_ERROR,
    CORE_LENGTH_ERROR, CORE_CRC_ERROR, CORE_FRAME_VERSION,
    CORE_FRAME_FLAGS, CORE_FRAME_KIND
} CoreStatus;

typedef struct {
    uint8_t version, kind, flags, header_size;
    uint16_t payload_length, message_id;
    uint32_t session_id, sequence;
    uint8_t payload[CORE_PAYLOAD_MAX];
} CoreFrame;

uint16_t core_crc16(const uint8_t *data, size_t length);
/* COBS 接口不含 delimiter；失败不写输出及 length，允许编码空输入。不可原地操作。 */
CoreStatus core_cobs_encode(const uint8_t *data, size_t length,
    uint8_t *output, size_t capacity, size_t *output_length);
CoreStatus core_cobs_decode(const uint8_t *data, size_t length,
    uint8_t *output, size_t capacity, size_t *output_length);
/* 编码包含 delimiter；只接受当前版本 Header。失败不写输出及 length。 */
CoreStatus core_frame_encode(const CoreFrame *frame, uint8_t *output,
    size_t capacity, size_t *output_length);
/* 输入为不含 delimiter 的候选。仅 OK/FRAME_* 写 frame，其余保持原值。 */
CoreStatus core_frame_decode(const uint8_t *candidate, size_t length, CoreFrame *frame);

typedef struct {
    uint32_t cobs_errors, crc_errors, length_errors, version_errors;
    uint32_t flags_errors, message_errors, timeouts, discontinuities;
} CoreParserStats;

typedef struct {
    uint8_t candidate[CORE_ENCODED_MAX];
    size_t length;
    uint32_t last_byte_ms;
    bool discarding;
    CoreParserStats stats;
} CoreParser;

/* frame 仅在回调期间有效；FRAME_* 供后续 Server 返回错误，不代表业务通过。
 * 单一后台所有者调用，不可重入。分片边界及空 chunk 不充当帧边界或延长超时。
 */
typedef void (*CoreFrameSink)(void *context, CoreStatus status, const CoreFrame *frame);
void core_parser_init(CoreParser *parser);
void core_parser_poll(CoreParser *parser, uint32_t now_ms);
void core_parser_discontinuity(CoreParser *parser);
void core_parser_feed(CoreParser *parser, const uint8_t *data, size_t length,
    uint32_t now_ms, CoreFrameSink sink, void *context);

#endif
