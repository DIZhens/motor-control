#include "system_runtime_target.h"
#include "target_runtime.h"
#include "main.h"
#include "tim.h"
#include "realtime_motor_io.h"
#include "system_runtime.h"
#include "byte_stream_transport.h"
#include "uart_dma_transport.h"
#include "target_core_server.h"

uint32_t system_runtime_target_hal_tick(void) { return HAL_GetTick(); }

bool system_runtime_target_pre_scheduler(void)
{
    uint32_t before, cycles, remaining = 1000000U;
    realtime_motor_io_safe_off();
    if (SystemCoreClock != 170000000U || __get_PRIMASK() != 0U || __get_BASEPRI() != 0U
        || TIM7->PSC != 169U || TIM7->ARR != 999U
        || (TIM7->CR1 & TIM_CR1_CEN) == 0U || (TIM7->DIER & TIM_DIER_UIE) == 0U
        || NVIC_GetPriority(TIM7_IRQn) != 15U || NVIC_GetEnableIRQ(TIM7_IRQn) == 0U
        || (SysTick->CTRL & SysTick_CTRL_ENABLE_Msk) != 0U) { return false; }
    CoreDebug->DEMCR |= CoreDebug_DEMCR_TRCENA_Msk;
    DWT->CTRL |= DWT_CTRL_CYCCNTENA_Msk;
    before = HAL_GetTick(); cycles = DWT->CYCCNT;
    /* 只验证HAL时基在调度器前真实推进；有限CPU循环避免失去tick时永久等待。 */
    while (HAL_GetTick() == before) {
        if (--remaining == 0U || (uint32_t)(DWT->CYCCNT - cycles) > 5U * 170000U) { return false; }
    }
    return true;
}

bool system_runtime_target_bootstrap(void)
{
    MotorSnapshot snapshot;
    if (NVIC_GetPriorityGrouping() != NVIC_PRIORITYGROUP_4
        || NVIC_GetPriority(TIM1_UP_TIM16_IRQn) != 0U
        || NVIC_GetPriority(TIM1_BRK_TIM15_IRQn) != 0U
        || NVIC_GetPriority(ADC1_2_IRQn) != 1U
        || NVIC_GetPriority(DMA1_Channel1_IRQn) != 1U
        || NVIC_GetPriority(SPI1_IRQn) != 2U
        || NVIC_GetPriority(TIM6_DAC_IRQn) != 15U
        || NVIC_GetPriority(TIM7_IRQn) != 15U
        || NVIC_GetPriority(SysTick_IRQn) != 15U
        || NVIC_GetPriority(PendSV_IRQn) != 15U
        || NVIC_GetPriority(SVCall_IRQn) != 0U
        || TIM6->PSC != 169U || TIM7->PSC != 169U || TIM7->ARR != 999U
        || SysTick->LOAD != 169999U) { return false; }
    target_runtime_start();
    return target_runtime_read_snapshot(&snapshot) && snapshot.state == MOTOR_STATE_IDLE
        && snapshot.faults == 0U && target_runtime_session_permitted()
        && TIM6->ARR == 49U && (TIM6->CR1 & TIM_CR1_CEN) != 0U;
}

bool system_runtime_target_read_sample(SpeedCommissioningSample *sample)
{
    return target_runtime_read_commissioning(sample);
}

static void transport_event(void *context, uint32_t events, bool from_interrupt)
{
    (void)context; (void)events;
    /* 通知只唤醒唯一所有者，不以通知次数推断生产字节或完成状态。 */
    system_runtime_communication_notify(from_interrupt);
}

bool system_runtime_target_start_transport(void)
{
    target_core_server_start();
    return byte_stream_init() == BYTE_STREAM_OK
        && byte_stream_bind_event_sink(transport_event, NULL) == BYTE_STREAM_OK
        && byte_stream_start_rx() == BYTE_STREAM_OK;
}

void system_runtime_target_communication_service(void)
{
    target_core_server_service();
}

void g431_uart_transport_fail(bool timeout)
{
    target_runtime_fail_closed(timeout ? RUNTIME_INTERNAL_TIMEOUT : RUNTIME_INTERNAL_TRANSPORT);
}

bool system_runtime_target_read_telemetry(RuntimeTelemetryConfig *config)
{
    RuntimeParameters parameters;
    if (!target_runtime_read_parameters(&parameters)) { return false; }
    *config = parameters.telemetry;
    return true;
}

void system_runtime_target_fatal(RuntimeInternalFaultReason reason)
{
    target_runtime_fail_closed(reason);
    /* 内部可信性失败后只保留调试读回，不尝试调度器或电机重启。 */
    __disable_irq();
    for (;;) { __NOP(); }
}
