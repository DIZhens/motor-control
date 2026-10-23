#include "target_vofa.h"

#include <stddef.h>

#include "motor_vofa.h"
#include "legacy_command.h"
#include "../../runtime/target_runtime.h"
#include "target_uart.h"

#define TELEMETRY_PERIOD_MS (10U)
#define RX_FRAGMENT_TIMEOUT_MS (100U)

/* 解析器和在途整帧仅由后台维护；发送完成前不改写帧内容。 */
static VofaParser s_parser;
static uint8_t s_tx_frame[VOFA_TELEMETRY_FRAME_SIZE];
static size_t s_tx_cursor;
static size_t s_tx_length;
static uint32_t s_last_telemetry_ms;
static uint32_t s_last_rx_ms;

void target_vofa_init(void)
{
    vofa_parser_init(&s_parser);
    s_tx_cursor = 0U;
    s_tx_length = 0U;
    s_last_telemetry_ms = 0U;
    s_last_rx_ms = 0U;
}

void target_vofa_service(uint32_t now_ms)
{
    uint8_t byte;
    uint32_t received;

    if (s_parser.length != 0U && !s_parser.discarding
        && (uint32_t)(now_ms - s_last_rx_ms) >= RX_FRAGMENT_TIMEOUT_MS) {
        s_parser.length = 0U;
        s_parser.discarding = true;
    }
    received = target_uart_receive(&byte);
    if ((received & TARGET_UART_RX_STREAM_ERROR) != 0U) {
        s_parser.length = 0U;
        s_parser.discarding = true;
    }
    if ((received & TARGET_UART_RX_BYTE) != 0U) {
        VofaCommand request;
        VofaParseResult result = vofa_parser_feed(&s_parser, byte, &request);

        s_last_rx_ms = now_ms;
        if (result == VOFA_PARSE_COMMAND) {
            MotorCommand command;

            if (motor_vofa_command(&request, &command)) {
                (void)target_runtime_submit_command(&command);
            } else {
                (void)target_runtime_submit_command(NULL);
            }
        } else if (result == VOFA_PARSE_REJECTED) {
            (void)target_runtime_submit_command(NULL);
        }
    }
    if ((uint32_t)(now_ms - s_last_telemetry_ms) >= TELEMETRY_PERIOD_MS
        && s_tx_cursor == s_tx_length) {
        MotorSnapshot snapshot;

        s_last_telemetry_ms = now_ms;
        if (target_runtime_read_snapshot(&snapshot)) {
            VofaTelemetry telemetry;

            motor_vofa_telemetry(&snapshot, &telemetry);
            s_tx_length = vofa_encode_telemetry(&telemetry, s_tx_frame, sizeof(s_tx_frame));
            s_tx_cursor = 0U;
        }
    }
    if (s_tx_cursor < s_tx_length && target_uart_transmit(s_tx_frame[s_tx_cursor])) {
        s_tx_cursor++;
    }
}
