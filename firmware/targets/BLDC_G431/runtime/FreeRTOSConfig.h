#ifndef G431_FREERTOS_CONFIG_H
#define G431_FREERTOS_CONFIG_H

#include <stdint.h>
extern uint32_t SystemCoreClock;
void system_runtime_assert_failed(void);

#define configUSE_PREEMPTION 1
#define configUSE_TIME_SLICING 0
#define configUSE_TICKLESS_IDLE 0
#define configCPU_CLOCK_HZ SystemCoreClock
#define configTICK_RATE_HZ 1000U
#define configMAX_PRIORITIES 4U
#define configMINIMAL_STACK_SIZE 128U
#define configMAX_TASK_NAME_LEN 16U
#define configUSE_16_BIT_TICKS 0
#define configIDLE_SHOULD_YIELD 0
#define configSUPPORT_STATIC_ALLOCATION 1
#define configSUPPORT_DYNAMIC_ALLOCATION 0
#define configUSE_TIMERS 0
#define configUSE_TASK_NOTIFICATIONS 1
#define configCHECK_FOR_STACK_OVERFLOW 2
#define configUSE_IDLE_HOOK 0
#define configUSE_TICK_HOOK 0
#define configUSE_MUTEXES 0
#define configUSE_RECURSIVE_MUTEXES 0
#define configUSE_COUNTING_SEMAPHORES 0
#define configQUEUE_REGISTRY_SIZE 0
#define configUSE_CO_ROUTINES 0
#define configUSE_TRACE_FACILITY 0
#define configGENERATE_RUN_TIME_STATS 0
#define configUSE_PORT_OPTIMISED_TASK_SELECTION 1

/* STM32G431实现4位优先级；只有5..15允许调用FromISR。 */
#define configPRIO_BITS 4U
#define configLIBRARY_LOWEST_INTERRUPT_PRIORITY 15U
#define configLIBRARY_MAX_SYSCALL_INTERRUPT_PRIORITY 5U
/* RVDS内联汇编直接引用此常量，使用汇编器可识别的字面量。 */
#define configKERNEL_INTERRUPT_PRIORITY 0xF0
#define configMAX_SYSCALL_INTERRUPT_PRIORITY 0x50
#define configASSERT(expression) do { if (!(expression)) system_runtime_assert_failed(); } while (0)

#define INCLUDE_vTaskDelete 1
#define INCLUDE_vTaskDelay 1
#define INCLUDE_vTaskDelayUntil 1
#define INCLUDE_uxTaskGetStackHighWaterMark 1
#define INCLUDE_xTaskGetSchedulerState 1
#define INCLUDE_xTaskGetIdleTaskHandle 1
#define INCLUDE_vTaskSuspend 0
#define INCLUDE_vTaskPrioritySet 0
#define INCLUDE_uxTaskPriorityGet 0
#define INCLUDE_xTaskGetCurrentTaskHandle 0

/* Run中三个异常直接由未修改的Port导出，无第二层空Handler。 */
#define vPortSVCHandler SVC_Handler
#define xPortPendSVHandler PendSV_Handler
#define xPortSysTickHandler SysTick_Handler

#endif
