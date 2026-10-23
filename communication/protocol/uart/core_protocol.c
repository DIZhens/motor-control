#include "core_protocol.h"

#include <string.h>

static uint16_t read16(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[0] | ((uint16_t)p[1] << 8));
}

static uint32_t read32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
        ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void write16(uint8_t *p, uint16_t value)
{
    p[0] = (uint8_t)value; p[1] = (uint8_t)(value >> 8);
}

static void write32(uint8_t *p, uint32_t value)
{
    p[0] = (uint8_t)value; p[1] = (uint8_t)(value >> 8);
    p[2] = (uint8_t)(value >> 16); p[3] = (uint8_t)(value >> 24);
}

uint16_t core_crc16(const uint8_t *data, size_t length)
{
    uint16_t crc = 0xFFFFU;
    size_t i;
    unsigned int bit;
    if (data == NULL && length != 0U) { return 0U; }
    for (i = 0U; i < length; ++i) {
        crc ^= (uint16_t)((uint16_t)data[i] << 8);
        for (bit = 0U; bit < 8U; ++bit) {
            crc = (uint16_t)((crc & 0x8000U) != 0U ? (crc << 1) ^ 0x1021U : crc << 1);
        }
    }
    return crc;
}

CoreStatus core_cobs_encode(const uint8_t *data, size_t length,
    uint8_t *output, size_t capacity, size_t *output_length)
{
    size_t i, code_index = 0U, next = 1U;
    uint8_t code = 1U;
    if ((data == NULL && length != 0U) || output == NULL || output_length == NULL) {
        return CORE_BAD_ARGUMENT;
    }
    if (length > CORE_DECODED_MAX) { return CORE_LENGTH_ERROR; }
    /* 长度小于254时，每个输入都恰好增加一个 code 字节。先检查容量以保证原子失败。 */
    if (capacity < length + 1U) { return CORE_CAPACITY; }
    for (i = 0U; i < length; ++i) {
        if (data[i] == 0U) {
            output[code_index] = code;
            code_index = next++;
            code = 1U;
        } else {
            output[next++] = data[i];
            ++code;
        }
    }
    output[code_index] = code;
    *output_length = next;
    return CORE_OK;
}

CoreStatus core_cobs_decode(const uint8_t *data, size_t length,
    uint8_t *output, size_t capacity, size_t *output_length)
{
    size_t i = 0U, decoded = 0U, j, next = 0U;
    uint8_t code;
    if (data == NULL || output == NULL || output_length == NULL) { return CORE_BAD_ARGUMENT; }
    if (length == 0U || length > CORE_ENCODED_MAX) { return CORE_COBS_ERROR; }
    /* 先校验完整候选，避免坏 COBS 或容量不足时交付半个结果。 */
    while (i < length) {
        code = data[i++];
        if (code == 0U || (size_t)(code - 1U) > length - i) { return CORE_COBS_ERROR; }
        for (j = 0U; j < (size_t)(code - 1U); ++j) {
            if (data[i++] == 0U) { return CORE_COBS_ERROR; }
            ++decoded;
        }
        if (code != 255U && i < length) { ++decoded; }
    }
    if (decoded > CORE_DECODED_MAX) { return CORE_LENGTH_ERROR; }
    if (capacity < decoded) { return CORE_CAPACITY; }
    i = 0U;
    while (i < length) {
        code = data[i++];
        for (j = 0U; j < (size_t)(code - 1U); ++j) { output[next++] = data[i++]; }
        if (code != 255U && i < length) { output[next++] = 0U; }
    }
    *output_length = next;
    return CORE_OK;
}

static CoreStatus header_status(const CoreFrame *frame)
{
    if (frame->version != 1U) { return CORE_FRAME_VERSION; }
    if (frame->flags != 0U) { return CORE_FRAME_FLAGS; }
    if (frame->kind < CORE_REQUEST || frame->kind > CORE_EVENT) { return CORE_FRAME_KIND; }
    return CORE_OK;
}

CoreStatus core_frame_encode(const CoreFrame *frame, uint8_t *output,
    size_t capacity, size_t *output_length)
{
    uint8_t raw[CORE_DECODED_MAX];
    size_t length, encoded;
    CoreStatus status;
    if (frame == NULL || output == NULL || output_length == NULL) { return CORE_BAD_ARGUMENT; }
    if (frame->payload_length > CORE_PAYLOAD_MAX || frame->header_size != CORE_HEADER_SIZE) {
        return CORE_LENGTH_ERROR;
    }
    status = header_status(frame);
    if (status != CORE_OK) { return status; }
    length = CORE_HEADER_SIZE + frame->payload_length;
    if (capacity < length + 4U) { return CORE_CAPACITY; }
    raw[0] = frame->version; raw[1] = frame->kind;
    raw[2] = frame->flags; raw[3] = frame->header_size;
    write16(raw + 4U, frame->payload_length); write16(raw + 6U, frame->message_id);
    write32(raw + 8U, frame->session_id); write32(raw + 12U, frame->sequence);
    memcpy(raw + CORE_HEADER_SIZE, frame->payload, frame->payload_length);
    write16(raw + length, core_crc16(raw, length));
    status = core_cobs_encode(raw, length + 2U, output, capacity - 1U, &encoded);
    if (status != CORE_OK) { return status; }
    output[encoded] = 0U;
    *output_length = encoded + 1U;
    return CORE_OK;
}

CoreStatus core_frame_decode(const uint8_t *candidate, size_t length, CoreFrame *frame)
{
    uint8_t raw[CORE_DECODED_MAX];
    size_t decoded;
    uint16_t payload_length;
    CoreFrame next;
    CoreStatus status;
    if (frame == NULL) { return CORE_BAD_ARGUMENT; }
    status = core_cobs_decode(candidate, length, raw, sizeof(raw), &decoded);
    if (status != CORE_OK) { return status; }
    if (decoded < CORE_HEADER_SIZE + 2U) { return CORE_LENGTH_ERROR; }
    payload_length = read16(raw + 4U);
    if (payload_length > CORE_PAYLOAD_MAX || decoded != CORE_HEADER_SIZE + payload_length + 2U) {
        return CORE_LENGTH_ERROR;
    }
    if (read16(raw + decoded - 2U) != core_crc16(raw, decoded - 2U)) { return CORE_CRC_ERROR; }
    if (raw[3] != CORE_HEADER_SIZE) { return CORE_LENGTH_ERROR; }
    memset(&next, 0, sizeof(next));
    next.version = raw[0]; next.kind = raw[1]; next.flags = raw[2]; next.header_size = raw[3];
    next.payload_length = payload_length; next.message_id = read16(raw + 6U);
    next.session_id = read32(raw + 8U); next.sequence = read32(raw + 12U);
    memcpy(next.payload, raw + CORE_HEADER_SIZE, payload_length);
    *frame = next;
    return header_status(&next);
}

void core_parser_init(CoreParser *parser)
{
    if (parser != NULL) { memset(parser, 0, sizeof(*parser)); }
}

void core_parser_poll(CoreParser *parser, uint32_t now_ms)
{
    if (parser != NULL && parser->length != 0U &&
        (uint32_t)(now_ms - parser->last_byte_ms) >= CORE_PARTIAL_TIMEOUT_MS) {
        parser->length = 0U;
        parser->discarding = true;
        ++parser->stats.timeouts;
    }
}

void core_parser_discontinuity(CoreParser *parser)
{
    if (parser != NULL) {
        parser->length = 0U;
        parser->discarding = true;
        ++parser->stats.discontinuities;
    }
}

void core_parser_feed(CoreParser *parser, const uint8_t *data, size_t length,
    uint32_t now_ms, CoreFrameSink sink, void *context)
{
    size_t i;
    CoreFrame frame;
    CoreStatus status;
    if (parser == NULL || (data == NULL && length != 0U)) { return; }
    core_parser_poll(parser, now_ms);
    for (i = 0U; i < length; ++i) {
        if (data[i] == 0U) {
            if (parser->length != 0U && !parser->discarding) {
                status = core_frame_decode(parser->candidate, parser->length, &frame);
                switch (status) {
                case CORE_COBS_ERROR: ++parser->stats.cobs_errors; break;
                case CORE_LENGTH_ERROR: ++parser->stats.length_errors; break;
                case CORE_CRC_ERROR: ++parser->stats.crc_errors; break;
                case CORE_FRAME_VERSION: ++parser->stats.version_errors; break;
                case CORE_FRAME_FLAGS: ++parser->stats.flags_errors; break;
                case CORE_FRAME_KIND: ++parser->stats.message_errors; break;
                default: break;
                }
                if ((status == CORE_OK || status == CORE_FRAME_VERSION ||
                     status == CORE_FRAME_FLAGS || status == CORE_FRAME_KIND) && sink != NULL) {
                    sink(context, status, &frame);
                }
            }
            parser->length = 0U;
            parser->discarding = false;
        } else if (!parser->discarding) {
            if (parser->length == CORE_ENCODED_MAX) {
                parser->length = 0U;
                parser->discarding = true;
                ++parser->stats.length_errors;
            } else {
                parser->candidate[parser->length++] = data[i];
                parser->last_byte_ms = now_ms;
            }
        }
    }
}
