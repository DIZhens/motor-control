#ifndef RUNTIME_EXCHANGE_H
#define RUNTIME_EXCHANGE_H

#include <stdbool.h>
#include <stdint.h>
#include "motor.h"

#define RUNTIME_COMPLETION_CAPACITY 4U

/* 固定运行时交接，不含线上Session、Sequence、对象编号或内核对象。 */
typedef enum {
    RUNTIME_REQUEST_MOTOR = 0, RUNTIME_REQUEST_SPEED_PI, RUNTIME_REQUEST_TELEMETRY
} RuntimeRequestKind;
typedef struct { float kp, ki; } RuntimeSpeedPi;
typedef struct { RuntimeSpeedPi value; uint32_t generation; bool in_use; } RuntimeSpeedProfile;
typedef struct { bool enabled; uint16_t rate_hz; uint32_t generation; } RuntimeTelemetryConfig;
typedef struct {
    RuntimeRequestKind kind;
    union {
        MotorCommand motor;
        RuntimeSpeedPi speed_pi;
        struct { bool enabled; uint16_t rate_hz; } telemetry;
    } value;
} RuntimeRequest;
typedef struct { uint32_t ticket; RuntimeRequest request; } RuntimeCommandEnvelope;

typedef enum {
    RUNTIME_PARAMETER_APPLIED = 0, RUNTIME_PARAMETER_BAD_VALUE, RUNTIME_PARAMETER_WRONG_STATE
} RuntimeParameterResult;
typedef enum {
    RUNTIME_COMPLETED = 0, RUNTIME_CANCELLED_BY_STOP, RUNTIME_PRECONDITION_FAILED
} RuntimeCompletionOutcome;
typedef struct {
    uint32_t ticket;
    RuntimeRequestKind kind; /* 仅COMPLETED有效，取消仅以ticket关联原事务。 */
    RuntimeCompletionOutcome outcome;
    MotorCommandResult motor_result; /* 仅MOTOR且COMPLETED有效，忠实保留Motor结果。 */
    RuntimeParameterResult parameter_result;
    union { RuntimeSpeedProfile speed_pi; RuntimeTelemetryConfig telemetry; } value;
} RuntimeCommandCompletion;

typedef enum {
    RUNTIME_EXCHANGE_QUEUED = 0, RUNTIME_EXCHANGE_BUSY, RUNTIME_EXCHANGE_BAD_REQUEST,
    RUNTIME_EXCHANGE_TICKET_EXHAUSTED, RUNTIME_EXCHANGE_FAILED,
    RUNTIME_EXCHANGE_PRECONDITION_FAILED
} RuntimeExchangeResult;
typedef struct {
    volatile RuntimeCommandEnvelope ordinary, stop;
    volatile bool ordinary_pending, stop_pending, internal_stop;
    uint32_t last_ticket; /* 后台独占；本启动不重用，用尽后由Target Fail-closed。 */
    volatile RuntimeCommandCompletion completions[RUNTIME_COMPLETION_CAPACITY];
    volatile uint32_t head, tail; /* ADC独占head，后台独占tail，无符号差允许自然回绕。 */
    volatile uint32_t completion_high_water;
} RuntimeExchange;

/* 链接期由Target提供真正的CPU/编译器内存屏障，不能用空替身代替。 */
void runtime_exchange_barrier(void);
void runtime_exchange_init(RuntimeExchange *exchange);
/* 唯一后台调用，Target须用短临界区与ADC排序；完整复制，成功才写两个输出。
 * Stop返回尚未消费的普通ticket，调用方立即为旧事务形成取消终态；不向FIFO再投一次取消。
 */
RuntimeExchangeResult runtime_exchange_submit(RuntimeExchange *exchange, const RuntimeRequest *request,
    uint32_t *ticket, uint32_t *cancelled_ticket);
/* 安全守卫仅置位；不从守卫生产Completion，也不覆盖外部Stop身份。 */
void runtime_exchange_request_internal_stop(RuntimeExchange *exchange);
/* ADC唯一消费者；内部Stop取消普通请求时返回cancelled_ticket，ADC须明确发布其取消结果。
 * ticket=0只用于内部Stop，绝不返回给后台作为已接受的请求身份。
 */
bool runtime_exchange_take(RuntimeExchange *exchange, RuntimeCommandEnvelope *envelope,
    uint32_t *cancelled_ticket);
/* 静态SPSC，slot先于head、复制先于tail。满/空失败不改队列或输出，满必须Fail-closed。 */
bool runtime_exchange_complete(RuntimeExchange *exchange, const RuntimeCommandCompletion *completion);
bool runtime_exchange_pop(RuntimeExchange *exchange, RuntimeCommandCompletion *completion);

typedef enum {
    RUNTIME_INTERNAL_NONE = 0, RUNTIME_INTERNAL_BOOTSTRAP, RUNTIME_INTERNAL_RTOS,
    RUNTIME_INTERNAL_STACK_OVERFLOW, RUNTIME_INTERNAL_COMPLETION_OVERFLOW,
    RUNTIME_INTERNAL_RESPONSE_OVERFLOW, RUNTIME_INTERNAL_TRANSACTION_CORRUPT,
    RUNTIME_INTERNAL_TRANSPORT, RUNTIME_INTERNAL_TIMEOUT
} RuntimeInternalFaultReason;
typedef struct { RuntimeInternalFaultReason first_reason; uint32_t count; } RuntimeInternalFaultStatus;

#endif
