#include "byte_stream_transport.h"
#include "uart_dma_transport.h"
#include "main.h"
#include "usart.h"
#include <stddef.h>

/* 模型只替代C库复制：原生ARMCC库在当前Unicorn的27字节用例中出现尾2字节偏差，
 * 同一长度的MCU原生回环逐字节一致；不据模拟执行结果修改正式C库。
 * volatile逐字节复制同时保留“复制中DMA推进”的注入位置，不替换适配器逻辑。 */
void __aeabi_memcpy(void *destination, const void *source, size_t size)
{
    volatile uint8_t *to = destination;
    const volatile uint8_t *from = source;
    while (size != 0U) { *to++ = *from++; --size; }
}
void __aeabi_memcpy4(void *destination, const void *source, size_t size)
{
    __aeabi_memcpy(destination, source, size);
}

UART_HandleTypeDef huart1;
uint32_t SystemCoreClock = 170000000U;
volatile uint32_t model_ms, model_events, model_irq_events, model_task_events;
volatile uint32_t model_failure, model_failure_count;
uint32_t HAL_GetTick(void) { return model_ms; }
void g431_uart_transport_fail(bool timeout)
{
    model_failure = timeout ? 2U : 1U;
    model_failure_count++;
}
static void event(void *context, uint32_t events, bool irq)
{
    (void)context;
    model_events |= events;
    if (irq) { model_irq_events++; } else { model_task_events++; }
}
uint32_t model_init(void)
{
    huart1.Init.BaudRate = 460800U;
    return (uint32_t)byte_stream_init();
}
uint32_t model_bind(void) { return (uint32_t)byte_stream_bind_event_sink(event, NULL); }
uint32_t model_start(void) { return (uint32_t)byte_stream_start_rx(); }
uint8_t model_destination[1024], model_source[256];
size_t model_count;
uint32_t model_epoch;
ByteStreamStats model_stats;
uint32_t model_read(uint32_t capacity)
{
    return (uint32_t)byte_stream_read(model_destination, capacity, &model_count, &model_epoch);
}
uint32_t model_send(uint32_t length)
{
    return (uint32_t)byte_stream_try_send(model_source, length);
}
uint32_t model_read_stats(void) { return byte_stream_read_stats(&model_stats); }
/* 对齐、布局由正式ARM编译器提供，不在Python中猜测bool padding。 */
const uint32_t model_stats_layout[] = {sizeof(ByteStreamStats),
    offsetof(ByteStreamStats, rx_bytes), offsetof(ByteStreamStats, tx_bytes),
    offsetof(ByteStreamStats, rx_discontinuities), offsetof(ByteStreamStats, buffer_overruns),
    offsetof(ByteStreamStats, parity_errors), offsetof(ByteStreamStats, framing_errors),
    offsetof(ByteStreamStats, noise_errors), offsetof(ByteStreamStats, hardware_rx_overruns),
    offsetof(ByteStreamStats, tx_busy_count), offsetof(ByteStreamStats, tx_errors),
    offsetof(ByteStreamStats, produced_bytes), offsetof(ByteStreamStats, consumed_bytes),
    offsetof(ByteStreamStats, continuity_epoch), offsetof(ByteStreamStats, rx_started),
    offsetof(ByteStreamStats, tx_in_flight)};
