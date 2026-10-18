#include "vofa.h"

#include <float.h>
#include <string.h>

#if FLT_RADIX != 2 || FLT_MANT_DIG != 24 || FLT_MAX_EXP != 128
#error "VOFA JustFloat requires IEEE 754 binary32 float"
#endif

#define VOFA_FLOAT_EXPONENT_MASK 0x7F800000UL
#define VOFA_FLOAT_INVALID_BITS 0x7FC00000UL
#define VOFA_VALID_ALL 0x1FFU
#define VOFA_MAX_EXACT_INTEGER 0xFFFFFFUL

static uint32_t vofa_float_bits(float value)
{
    uint32_t bits;
    memcpy(&bits, &value, sizeof(bits));
    return bits;
}

static bool vofa_is_finite(float value)
{
    return (vofa_float_bits(value) & VOFA_FLOAT_EXPONENT_MASK) != VOFA_FLOAT_EXPONENT_MASK;
}

static bool vofa_is_digit(char value)
{
    return value >= '0' && value <= '9';
}

static bool vofa_parse_velocity(const char *text, float *velocity)
{
    const char *cursor = text;
    double mantissa = 0.0;
    double factor = 10.0;
    int decimal_exponent = 0;
    unsigned int exponent_magnitude = 0U;
    bool has_digit = false;
    bool negative = false;
    bool exponent_negative = false;
    float value;

    if (*cursor == '+' || *cursor == '-') {
        negative = *cursor == '-';
        cursor++;
    }
    while (vofa_is_digit(*cursor)) {
        has_digit = true;
        mantissa = mantissa * 10.0 + (double)(*cursor - '0');
        cursor++;
    }
    if (*cursor == '.') {
        cursor++;
        while (vofa_is_digit(*cursor)) {
            has_digit = true;
            mantissa = mantissa * 10.0 + (double)(*cursor - '0');
            decimal_exponent--;
            cursor++;
        }
    }
    if (!has_digit) {
        return false;
    }
    if (*cursor == 'e' || *cursor == 'E') {
        cursor++;
        if (*cursor == '+' || *cursor == '-') {
            exponent_negative = *cursor == '-';
            cursor++;
        }
        if (!vofa_is_digit(*cursor)) {
            return false;
        }
        while (vofa_is_digit(*cursor)) {
            /* 已足以判定 binary32 上溢或下溢，饱和以防指数整数溢出。 */
            if (exponent_magnitude < 400U) {
                exponent_magnitude = exponent_magnitude * 10U + (unsigned int)(*cursor - '0');
                if (exponent_magnitude > 400U) {
                    exponent_magnitude = 400U;
                }
            }
            cursor++;
        }
        decimal_exponent += exponent_negative ? -(int)exponent_magnitude : (int)exponent_magnitude;
    }
    if (*cursor != '\0') {
        return false;
    }
    exponent_negative = decimal_exponent < 0;
    exponent_magnitude = (unsigned int)(exponent_negative ? -decimal_exponent : decimal_exponent);
    /* 行长限制保证尾数不超过 41 位；double 累加和最多九次缩放都有界。 */
    while (exponent_magnitude != 0U) {
        if ((exponent_magnitude & 1U) != 0U) {
            mantissa = exponent_negative ? mantissa / factor : mantissa * factor;
        }
        exponent_magnitude >>= 1U;
        if (exponent_magnitude != 0U) {
            factor *= factor;
        }
    }
    value = (float)(negative ? -mantissa : mantissa);
    if (!vofa_is_finite(value)) {
        return false;
    }
    *velocity = value;
    return true;
}

static bool vofa_parse_line(const char *line, VofaCommand *command)
{
    VofaCommand parsed;

    parsed.target_velocity_rpm = 0.0f;
    if (strcmp(line, "stop") == 0) {
        parsed.type = VOFA_COMMAND_STOP;
    } else if (strcmp(line, "clear") == 0) {
        parsed.type = VOFA_COMMAND_CLEAR_FAULT;
    } else if (strncmp(line, "start:", 6U) == 0) {
        parsed.type = VOFA_COMMAND_START_VELOCITY;
        if (!vofa_parse_velocity(line + 6U, &parsed.target_velocity_rpm)) {
            return false;
        }
    } else if (strncmp(line, "speed:", 6U) == 0) {
        parsed.type = VOFA_COMMAND_UPDATE_VELOCITY;
        if (!vofa_parse_velocity(line + 6U, &parsed.target_velocity_rpm)) {
            return false;
        }
    } else {
        return false;
    }
    *command = parsed;
    return true;
}

void vofa_parser_init(VofaParser *parser)
{
    if (parser != NULL) {
        parser->length = 0U;
        parser->discarding = false;
    }
}

VofaParseResult vofa_parser_feed(VofaParser *parser, uint8_t byte, VofaCommand *command)
{
    bool accepted;

    if (parser == NULL || command == NULL) {
        return VOFA_PARSE_NONE;
    }
    if (byte == '\n') {
        accepted = false;
        if (!parser->discarding) {
            if (parser->length > 0U && parser->line[parser->length - 1U] == '\r') {
                parser->length--;
            }
            parser->line[parser->length] = '\0';
            accepted = vofa_parse_line(parser->line, command);
        }
        parser->length = 0U;
        parser->discarding = false;
        return accepted ? VOFA_PARSE_COMMAND : VOFA_PARSE_REJECTED;
    }
    if (parser->discarding) {
        return VOFA_PARSE_NONE;
    }
    if (parser->length >= VOFA_COMMAND_MAX_LENGTH || byte < 0x20U || byte > 0x7EU) {
        if (byte != '\r' || parser->length >= VOFA_COMMAND_MAX_LENGTH) {
            parser->discarding = true;
            return VOFA_PARSE_NONE;
        }
    }
    parser->line[parser->length++] = (char)byte;
    return VOFA_PARSE_NONE;
}

static void vofa_write_word(uint8_t *output, uint32_t value)
{
    output[0] = (uint8_t)value;
    output[1] = (uint8_t)(value >> 8U);
    output[2] = (uint8_t)(value >> 16U);
    output[3] = (uint8_t)(value >> 24U);
}

static void vofa_write_measurement(uint8_t *output, float value, uint32_t flag,
                                   uint32_t *valid_flags)
{
    if ((*valid_flags & flag) == 0U || !vofa_is_finite(value)) {
        *valid_flags &= ~flag;
        vofa_write_word(output, VOFA_FLOAT_INVALID_BITS);
    } else {
        vofa_write_word(output, vofa_float_bits(value));
    }
}

static void vofa_write_integer(uint8_t *output, uint32_t value, uint32_t flag,
                              uint32_t *valid_flags)
{
    if ((*valid_flags & flag) == 0U || value > VOFA_MAX_EXACT_INTEGER) {
        *valid_flags &= ~flag;
        vofa_write_word(output, VOFA_FLOAT_INVALID_BITS);
    } else {
        vofa_write_word(output, vofa_float_bits((float)value));
    }
}

size_t vofa_encode_telemetry(const VofaTelemetry *telemetry, uint8_t *frame, size_t capacity)
{
    uint32_t valid_flags;

    if (telemetry == NULL || frame == NULL || capacity < VOFA_TELEMETRY_FRAME_SIZE) {
        return 0U;
    }
    valid_flags = telemetry->valid_flags & VOFA_VALID_ALL;
    vofa_write_measurement(frame, telemetry->time_s, VOFA_VALID_TIME, &valid_flags);
    vofa_write_measurement(frame + 4U, telemetry->target_velocity_rpm,
                           VOFA_VALID_TARGET_VELOCITY, &valid_flags);
    vofa_write_measurement(frame + 8U, telemetry->velocity_rpm,
                           VOFA_VALID_VELOCITY, &valid_flags);
    vofa_write_measurement(frame + 12U, telemetry->id_a, VOFA_VALID_ID, &valid_flags);
    vofa_write_measurement(frame + 16U, telemetry->iq_a, VOFA_VALID_IQ, &valid_flags);
    vofa_write_measurement(frame + 20U, telemetry->bus_voltage_v,
                           VOFA_VALID_BUS_VOLTAGE, &valid_flags);
    vofa_write_integer(frame + 24U, telemetry->state, VOFA_VALID_STATE, &valid_flags);
    vofa_write_integer(frame + 28U, telemetry->faults, VOFA_VALID_FAULTS, &valid_flags);
    vofa_write_integer(frame + 36U, telemetry->command_result,
                       VOFA_VALID_COMMAND_RESULT, &valid_flags);
    vofa_write_word(frame + 32U, vofa_float_bits((float)valid_flags));
    vofa_write_word(frame + 40U, VOFA_FLOAT_EXPONENT_MASK);
    return VOFA_TELEMETRY_FRAME_SIZE;
}
