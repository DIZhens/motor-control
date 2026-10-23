#include "target_communication.h"
#include "target_communication_config.h"
#include "system_runtime.h"
#include "main.h"
#include "core_server.h"
#include "byte_stream_transport.h"
#include "can_frame_port.h"

#if BLDC_G431_COMMUNICATION == BLDC_G431_COMMUNICATION_UART_V1
#include "usart.h"
#include "byte_stream_transport.h"
#include "target_core_server.h"
#else
#include "fdcan.h"
#include "can_frame_port.h"
#include "target_can_server.h"
#if BLDC_G431_CAN_LOOPBACK_VALIDATION
#include "../validation/can_loopback.h"
#endif
#endif

/* ARM链接器在移除未使用节之前解析全部输入对象的外部符号。未选库的窄端口只提供
 * 无状态拒绝定义，最终镜像会连同未选Server全部移除；不创建硬件/事务状态或通知。 */
#if BLDC_G431_COMMUNICATION == BLDC_G431_COMMUNICATION_UART_V1
bool can_frame_read_status(CanFrameStatus *status) { (void)status; return false; }
CanFrameResult can_frame_try_send(const CanFrame *frame) { (void)frame; return CAN_FRAME_NOT_STARTED; }
#else
CoreResult core_server_port_read_object(uint16_t id, CoreObject *value)
{ (void)id; (void)value; return CORE_INTERNAL_ERROR; }
bool byte_stream_read_stats(ByteStreamStats *stats) { (void)stats; return false; }
ByteStreamResult byte_stream_try_send(const uint8_t *bytes, size_t length)
{ (void)bytes; (void)length; return BYTE_STREAM_NOT_STARTED; }
#endif

void target_communication_hardware_init(void)
{
#if BLDC_G431_COMMUNICATION == BLDC_G431_COMMUNICATION_UART_V1
    MX_USART1_UART_Init();
    /* CubeMX不在全局DMA初始化中开放UART中断；仅选中的链路激活它们。
     * 优先级继承CubeMX生成的USART配置，Transport启动还会核对其为5。 */
    HAL_NVIC_SetPriority(DMA1_Channel2_IRQn, NVIC_GetPriority(USART1_IRQn), 0U);
    HAL_NVIC_SetPriority(DMA1_Channel3_IRQn, NVIC_GetPriority(USART1_IRQn), 0U);
    HAL_NVIC_EnableIRQ(DMA1_Channel2_IRQn);
    HAL_NVIC_EnableIRQ(DMA1_Channel3_IRQn);
#else
    MX_FDCAN1_Init();
#if BLDC_G431_CAN_LOOPBACK_VALIDATION
    g431_can_loopback_configure();
#endif
#endif
}

static void transport_event(void *context, uint32_t events, bool from_interrupt)
{
    (void)context; (void)events;
    system_runtime_communication_notify(from_interrupt);
}

bool target_communication_start(void)
{
#if BLDC_G431_COMMUNICATION == BLDC_G431_COMMUNICATION_UART_V1
    if (NVIC_GetEnableIRQ(FDCAN1_IT0_IRQn) != 0U || NVIC_GetEnableIRQ(FDCAN1_IT1_IRQn) != 0U) { return false; }
    target_core_server_start();
    return byte_stream_init() == BYTE_STREAM_OK
        && byte_stream_bind_event_sink(transport_event, NULL) == BYTE_STREAM_OK
        && byte_stream_start_rx() == BYTE_STREAM_OK;
#else
    if (NVIC_GetEnableIRQ(USART1_IRQn) != 0U || NVIC_GetEnableIRQ(DMA1_Channel2_IRQn) != 0U
        || NVIC_GetEnableIRQ(DMA1_Channel3_IRQn) != 0U) { return false; }
    target_can_server_start();
    return can_frame_init() == CAN_FRAME_ACCEPTED
        && can_frame_bind_event_sink(transport_event, NULL) == CAN_FRAME_ACCEPTED
        && can_frame_start() == CAN_FRAME_ACCEPTED;
#endif
}

void target_communication_service(void)
{
#if BLDC_G431_COMMUNICATION == BLDC_G431_COMMUNICATION_UART_V1
    target_core_server_service();
#else
    target_can_server_service();
#endif
}

uint32_t target_communication_telemetry_epoch(void)
{
#if BLDC_G431_COMMUNICATION == BLDC_G431_COMMUNICATION_UART_V1
    return target_core_server_telemetry_epoch();
#else
    return target_can_server_telemetry_epoch();
#endif
}
