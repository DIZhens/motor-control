#ifndef G431_UART_DMA_TRANSPORT_H
#define G431_UART_DMA_TRANSPORT_H

#include <stdbool.h>

/* 仅由相应Core向量分发；不解析协议或处理Motor命令。 */
void g431_uart_rx_dma_irq(void);
void g431_uart_tx_dma_irq(void);
void g431_uart_irq(void);
/* Target提供独立关断，不依赖队列、协议或ADC继续运行。 */
void g431_uart_transport_fail(bool timeout);

#endif
