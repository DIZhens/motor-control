#ifndef BLDC_G431_TARGET_UART_H
#define BLDC_G431_TARGET_UART_H

#include <stdbool.h>
#include <stdint.h>

/* 本板 USART1 单次轮询结果；流错误使上层当前半帧失效。 */
enum {
    TARGET_UART_RX_BYTE = 1U << 0,
    TARGET_UART_RX_STREAM_ERROR = 1U << 1
};

/* 至多读取一个字节并清除本次已见错误；byte 为空时不访问外设。 */
uint32_t target_uart_receive(uint8_t *byte);

/* 仅发送寄存器可写时写入一个字节；不等待、不排队、不解释业务。 */
bool target_uart_transmit(uint8_t byte);

#endif
