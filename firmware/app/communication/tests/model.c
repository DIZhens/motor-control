/* 直接调用正式编解码 C；不连接 HAL、串口或 Motor。 */
#include "vofa.h"

#include <string.h>

uint8_t model_input[512];
uint32_t model_input_length;
uint32_t model_command_count;
uint32_t model_rejected_count;
uint32_t model_none_count;
uint32_t model_command_types[64];
float model_command_targets[64];
uint32_t model_output_changed_on_reject;
uint32_t model_encode_capacity = VOFA_TELEMETRY_FRAME_SIZE;
uint32_t model_encoded_size;
uint32_t model_null_result;
VofaTelemetry model_telemetry;
uint8_t model_frame[VOFA_TELEMETRY_FRAME_SIZE + 8U];
static VofaParser s_parser;

void model_init(void)
{
    vofa_parser_init(&s_parser);
}

void model_feed(void)
{
    uint32_t index;
    VofaCommand command;
    VofaCommand before;
    VofaParseResult result;

    for (index = 0U; index < model_input_length; index++) {
        memset(&command, 0xA5, sizeof(command));
        before = command;
        result = vofa_parser_feed(&s_parser, model_input[index], &command);
        if (result == VOFA_PARSE_COMMAND) {
            if (model_command_count < 64U) {
                model_command_types[model_command_count] = (uint32_t)command.type;
                model_command_targets[model_command_count] = command.target_velocity_rpm;
            }
            model_command_count++;
        } else {
            if (memcmp(&command, &before, sizeof(command)) != 0) {
                model_output_changed_on_reject++;
            }
            if (result == VOFA_PARSE_REJECTED) {
                model_rejected_count++;
            } else {
                model_none_count++;
            }
        }
    }
}

void model_encode(void)
{
    memset(model_frame, 0xA5, sizeof(model_frame));
    model_encoded_size = (uint32_t)vofa_encode_telemetry(
        &model_telemetry, model_frame + 4U, model_encode_capacity);
}

void model_null_arguments(void)
{
    VofaCommand command;

    vofa_parser_init(NULL);
    model_null_result = (uint32_t)vofa_parser_feed(NULL, '\n', &command);
    model_null_result |= (uint32_t)vofa_parser_feed(&s_parser, '\n', NULL);
    model_null_result |= (uint32_t)vofa_encode_telemetry(NULL, model_frame, sizeof(model_frame));
    model_null_result |= (uint32_t)vofa_encode_telemetry(&model_telemetry, NULL,
                                                      sizeof(model_frame));
}
