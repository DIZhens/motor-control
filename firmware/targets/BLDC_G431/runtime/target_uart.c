#include "target_uart.h"

#include <stddef.h>

#include "usart.h"

uint32_t target_uart_receive(uint8_t *byte)
{
    uint32_t status;
    uint32_t errors;
    uint32_t result = 0U;

    if (byte == NULL) {
        return 0U;
    }
    status = USART1->ISR;
    errors = status & (USART_ISR_PE | USART_ISR_FE | USART_ISR_NE | USART_ISR_ORE);
    if (errors != 0U) {
        /* PE/FE/NE/ORE 的状态位与清除位位置一一对应。 */
        USART1->ICR = errors;
        result |= TARGET_UART_RX_STREAM_ERROR;
    }
    if ((status & USART_ISR_RXNE_RXFNE) != 0U) {
        *byte = (uint8_t)USART1->RDR;
        result |= TARGET_UART_RX_BYTE;
    }
    return result;
}

bool target_uart_transmit(uint8_t byte)
{
    if ((USART1->ISR & USART_ISR_TXE_TXFNF) == 0U) {
        return false;
    }
    USART1->TDR = byte;
    return true;
}
