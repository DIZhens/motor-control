#include "system_runtime.h"
#include "system_runtime_target.h"
#include "telemetry_mailbox.h"

#include "FreeRTOS.h"
#include "task.h"
#include "queue.h"

/* 初始预算保留FPU上下文及装配调用链余量；实机高水位另行验证。 */
#define BOOTSTRAP_WORDS 768U
#define COMMUNICATION_WORDS 768U
#define TELEMETRY_WORDS 384U
#define BOOTSTRAP_PRIORITY 3U
#define COMMUNICATION_PRIORITY 2U
#define TELEMETRY_PRIORITY 1U
#define COMMUNICATION_MAX_WAIT_TICKS 1U /* 1 kHz Kernel tick：最长等待 1 ms。 */
#define STACK_WATERMARK_PERIOD_TICKS 1000U
#define TELEMETRY_DEFAULT_PERIOD_TICKS 4U /* 默认 250 Hz。 */
#define BOOTSTRAP_SELF_TEST_TICKS 2U /* HAL 与 Kernel 两时基启动自检窗口。 */
#define TELEMETRY_MAILBOX_DEPTH 1U /* latest value 单槽，不额外缓存待发样本。 */
static StaticTask_t s_bootstrap_tcb, s_communication_tcb, s_telemetry_tcb, s_idle_tcb;
static StackType_t s_bootstrap_stack[BOOTSTRAP_WORDS], s_communication_stack[COMMUNICATION_WORDS];
static StackType_t s_telemetry_stack[TELEMETRY_WORDS], s_idle_stack[configMINIMAL_STACK_SIZE];
static StaticQueue_t s_telemetry_queue;
static uint8_t s_telemetry_storage[sizeof(RuntimeTelemetryValue)];
static QueueHandle_t s_mailbox;
static TaskHandle_t s_communication, s_telemetry, s_idle;
/* 单消费者的订阅身份由本模块分配；生产计数跨订阅连续，身份本启动不重用。 */
static volatile uint32_t s_active_subscription;
static uint32_t s_subscription_counter, s_subscription_first;
/* 未测量与实测耗尽的0分开；各所有者测量后原样覆盖，不在协议映射时猜测。 */
volatile SystemRuntimeStatus g_system_runtime_status = {
    .bootstrap_stack_free = UINT32_MAX, .communication_stack_free = UINT32_MAX,
    .telemetry_stack_free = UINT32_MAX, .idle_stack_free = UINT32_MAX
};

static void fatal(RuntimeInternalFaultReason reason)
{
    g_system_runtime_status.phase = SYSTEM_RUNTIME_FAILED;
    system_runtime_target_fatal(reason);
    /* Target合同不返回；即使集成错误返回，也不能继续装配或重新启动。 */
    for (;;) { }
}

void system_runtime_assert_failed(void) { fatal(RUNTIME_INTERNAL_RTOS); }

void system_runtime_communication_notify(bool from_interrupt)
{
    BaseType_t higher_priority_woken = pdFALSE;
    if (s_communication == NULL) { fatal(RUNTIME_INTERNAL_BOOTSTRAP); }
    if (from_interrupt) {
        vTaskNotifyGiveFromISR(s_communication, &higher_priority_woken);
        portYIELD_FROM_ISR(higher_priority_woken);
    } else { xTaskNotifyGive(s_communication); }
}

void vApplicationStackOverflowHook(TaskHandle_t task, char *name)
{
    (void)task; (void)name;
    fatal(RUNTIME_INTERNAL_STACK_OVERFLOW);
}

void vApplicationGetIdleTaskMemory(StaticTask_t **tcb, StackType_t **stack, uint32_t *depth)
{
    *tcb = &s_idle_tcb; *stack = s_idle_stack; *depth = configMINIMAL_STACK_SIZE;
}

static void communication_task(void *argument)
{
    TickType_t watermark_tick = xTaskGetTickCount();
    (void)argument;
    for (;;) {
        /* ADC只写Completion；后台最长1ms醒来，随后由协议Server接管排空。 */
        (void)ulTaskNotifyTake(pdTRUE, COMMUNICATION_MAX_WAIT_TICKS);
        g_system_runtime_status.communication_passes++;
        system_runtime_target_communication_service();
        if ((TickType_t)(xTaskGetTickCount() - watermark_tick) >= STACK_WATERMARK_PERIOD_TICKS) {
            watermark_tick = xTaskGetTickCount();
            g_system_runtime_status.communication_stack_free = uxTaskGetStackHighWaterMark(NULL);
            g_system_runtime_status.telemetry_stack_free = uxTaskGetStackHighWaterMark(s_telemetry);
            g_system_runtime_status.idle_stack_free = uxTaskGetStackHighWaterMark(s_idle);
        }
    }
}

uint32_t system_runtime_telemetry_begin(void)
{
    uint32_t subscription;
    if (s_mailbox == NULL) { return 0U; }
    taskENTER_CRITICAL();
    s_active_subscription = 0U;
    (void)xQueueReset(s_mailbox);
    if (s_subscription_counter != UINT32_MAX) {
        s_subscription_first = g_system_runtime_status.telemetry_publications + 1U;
        s_active_subscription = ++s_subscription_counter;
    }
    subscription = s_active_subscription;
    taskEXIT_CRITICAL();
    return subscription;
}

void system_runtime_telemetry_end(void)
{
    taskENTER_CRITICAL();
    s_active_subscription = 0U;
    if (s_mailbox != NULL) { (void)xQueueReset(s_mailbox); }
    taskEXIT_CRITICAL();
}

bool system_runtime_telemetry_peek(RuntimeTelemetryValue *value)
{
    return value != NULL && s_active_subscription != 0U
        && xQueuePeek(s_mailbox, value, 0U) == pdPASS;
}

void system_runtime_telemetry_retire(void)
{
    RuntimeTelemetryValue value;
    if (xQueueReceive(s_mailbox, &value, 0U) == pdPASS) {
        g_system_runtime_status.latest_sample_sequence = value.sample.sample_sequence;
        g_system_runtime_status.telemetry_consumed++;
    }
}

static void telemetry_task(void *argument)
{
    TickType_t wake = xTaskGetTickCount();
    RuntimeTelemetryConfig config;
    RuntimeTelemetryValue value, old;
    TickType_t period;
    bool published;
    (void)argument;
    for (;;) {
        period = TELEMETRY_DEFAULT_PERIOD_TICKS;
        published = false;
        if (system_runtime_target_read_telemetry(&config)) {
            if (config.rate_hz != 50U && config.rate_hz != 250U && config.rate_hz != 500U) {
                fatal(RUNTIME_INTERNAL_TRANSACTION_CORRUPT);
            }
            period = 1000U / config.rate_hz;
            value.subscription_id = s_active_subscription;
            if (config.enabled && value.subscription_id != 0U
                && system_runtime_target_read_motor_telemetry(&value.sample)) {
                value.sample.telemetry_config_generation = config.generation;
                /* 短后台临界区使“检查旧槽+覆盖”不可被消费者穿插；高优先级控制不受屏蔽。 */
                taskENTER_CRITICAL();
                /* 复制期间允许消费者替换/结束订阅；迟到旧样本不占槽、不推进产生计数。 */
                if (value.subscription_id == s_active_subscription) {
                    value.produced = g_system_runtime_status.telemetry_publications + 1U;
                    value.subscription_first = s_subscription_first;
                    if (xQueuePeek(s_mailbox, &old, 0U) == pdPASS
                        && old.subscription_id == value.subscription_id
                        && old.sample.telemetry_config_generation == config.generation) {
                        g_system_runtime_status.telemetry_overwrites++;
                    }
                    if (xQueueOverwrite(s_mailbox, &value) != pdPASS) { fatal(RUNTIME_INTERNAL_RTOS); }
                    g_system_runtime_status.telemetry_publications++;
                    published = true;
                }
                taskEXIT_CRITICAL();
                if (published) { xTaskNotifyGive(s_communication); }
            }
        }
        vTaskDelayUntil(&wake, period);
    }
}

static void bootstrap_task(void *argument)
{
    uint32_t hal_before = system_runtime_target_hal_tick();
    TickType_t tick_before = xTaskGetTickCount();
    (void)argument;
    g_system_runtime_status.phase = SYSTEM_RUNTIME_BOOTSTRAP;
    /* 这是两个独立时基的启动自检，不是操作者准备倒计时。 */
    vTaskDelay(BOOTSTRAP_SELF_TEST_TICKS);
    g_system_runtime_status.hal_tick_bootstrap = system_runtime_target_hal_tick();
    g_system_runtime_status.kernel_tick_bootstrap = xTaskGetTickCount();
    if ((uint32_t)(g_system_runtime_status.hal_tick_bootstrap - hal_before) == 0U
        || (TickType_t)(xTaskGetTickCount() - tick_before) < BOOTSTRAP_SELF_TEST_TICKS) { fatal(RUNTIME_INTERNAL_BOOTSTRAP); }
    s_mailbox = xQueueCreateStatic(TELEMETRY_MAILBOX_DEPTH, sizeof(RuntimeTelemetryValue),
        s_telemetry_storage, &s_telemetry_queue);
    if (s_mailbox == NULL) { fatal(RUNTIME_INTERNAL_BOOTSTRAP); }
    s_communication = xTaskCreateStatic(communication_task, "Communication", COMMUNICATION_WORDS,
        NULL, COMMUNICATION_PRIORITY, s_communication_stack, &s_communication_tcb);
    s_telemetry = xTaskCreateStatic(telemetry_task, "Telemetry", TELEMETRY_WORDS,
        NULL, TELEMETRY_PRIORITY, s_telemetry_stack, &s_telemetry_tcb);
    /* Idle已由vTaskStartScheduler静态创建。Bootstrap仍最高优先级，长期任务尚未执行。 */
    s_idle = xTaskGetIdleTaskHandle();
    if (s_communication == NULL || s_telemetry == NULL || s_idle == NULL
        || !system_runtime_target_bootstrap() || !system_runtime_target_start_transport()) {
        fatal(RUNTIME_INTERNAL_BOOTSTRAP);
    }
    g_system_runtime_status.phase = SYSTEM_RUNTIME_READY;
    g_system_runtime_status.bootstrap_stack_free = uxTaskGetStackHighWaterMark(NULL);
    vTaskDelete(NULL);
    fatal(RUNTIME_INTERNAL_RTOS);
}

void system_runtime_start(void)
{
    TaskHandle_t bootstrap;
    if (g_system_runtime_status.phase != SYSTEM_RUNTIME_RESET) { fatal(RUNTIME_INTERNAL_BOOTSTRAP); }
    if (!system_runtime_target_pre_scheduler()) { fatal(RUNTIME_INTERNAL_BOOTSTRAP); }
    g_system_runtime_status.hal_tick_before_scheduler = system_runtime_target_hal_tick();
    bootstrap = xTaskCreateStatic(bootstrap_task, "Bootstrap", BOOTSTRAP_WORDS, NULL, BOOTSTRAP_PRIORITY,
        s_bootstrap_stack, &s_bootstrap_tcb);
    if (bootstrap == NULL) { fatal(RUNTIME_INTERNAL_BOOTSTRAP); }
    g_system_runtime_status.phase = SYSTEM_RUNTIME_SCHEDULER_START;
    vTaskStartScheduler();
    fatal(RUNTIME_INTERNAL_RTOS);
}
