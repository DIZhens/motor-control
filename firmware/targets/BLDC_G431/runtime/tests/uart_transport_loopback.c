/* 仅供纯逻辑供电的临时回环镜像，不加入正式Run/Validation工程，不处理任何电机命令。
 * 临时链接时以本文件替代Communication的Target service，其余装配与驱动使用正式源码。
 * request由一次性SWD工具写入：action先于非零新sequence，完成后不得自动重放。 */
#include "byte_stream_transport.h"
#include "main.h"

typedef struct { uint32_t sequence, action; } UartTransportTestRequest;
typedef struct {
    uint32_t sequence, action, phase, echoed_bytes, discontinuities, last_tx_result, last_rx_result;
} UartTransportTestStatus;
volatile UartTransportTestRequest g_uart_transport_test_request;
volatile UartTransportTestStatus g_uart_transport_test_status;
static uint8_t s_pending[130];
static size_t s_length;
static uint32_t s_seen, s_pause_cycles;
static bool s_paused;

void system_runtime_target_communication_service(void)
{
    uint32_t request = g_uart_transport_test_request.sequence;
    uint32_t action, epoch, batch, index;
    size_t count;
    ByteStreamResult result;
    if (request != 0U && request != s_seen) {
        s_seen = request; action = g_uart_transport_test_request.action;
        g_uart_transport_test_status.sequence = request;
        g_uart_transport_test_status.action = action;
        if (action == 1U) {
            s_paused = true; s_pause_cycles = DWT->CYCCNT;
            g_uart_transport_test_status.phase = 1U;
        } else if (action == 2U) {
            /* 仅测试构建故意冻结TX，验证正式驱动的10ms独立关断路径。 */
            for (index = 0U; index < sizeof(s_pending); ++index) { s_pending[index] = (uint8_t)index; }
            result = byte_stream_try_send(s_pending, sizeof(s_pending));
            g_uart_transport_test_status.last_tx_result = (uint32_t)result;
            if (result == BYTE_STREAM_ACCEPTED) {
                USART1->CR3 &= ~USART_CR3_DMAT;
                DMA1_Channel3->CCR &= ~DMA_CCR_EN;
                g_uart_transport_test_status.phase = 3U;
            } else { g_uart_transport_test_status.phase = 4U; }
        }
    }
    if (s_paused) {
        if ((uint32_t)(DWT->CYCCNT - s_pause_cycles) < 40U * 170000U) { return; }
        s_paused = false; g_uart_transport_test_status.phase = 2U;
    }
    for (batch = 0U; batch < 4U; ++batch) {
        if (s_length != 0U) {
            result = byte_stream_try_send(s_pending, s_length);
            g_uart_transport_test_status.last_tx_result = (uint32_t)result;
            if (result != BYTE_STREAM_ACCEPTED) { return; }
            g_uart_transport_test_status.echoed_bytes += (uint32_t)s_length;
            s_length = 0U;
        }
        result = byte_stream_read(s_pending, sizeof(s_pending), &count, &epoch);
        g_uart_transport_test_status.last_rx_result = (uint32_t)result;
        if (result == BYTE_STREAM_DISCONTINUITY) {
            g_uart_transport_test_status.discontinuities++;
        } else if (result == BYTE_STREAM_OK) { s_length = count; }
        else { return; }
    }
}
