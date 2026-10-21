#include "cordic_math.h"

#include <stddef.h>

#include "stm32g4xx.h"

/* AN5325 的 COSINE 主结果是 cos、次结果是 sin；RM0440 要求两次完整字访问。 */
#define CORDIC_CONFIGURATION_MASK (CORDIC_CSR_FUNC | CORDIC_CSR_PRECISION | \
    CORDIC_CSR_SCALE | CORDIC_CSR_IEN | CORDIC_CSR_DMAREN | CORDIC_CSR_DMAWEN | \
    CORDIC_CSR_NRES | CORDIC_CSR_NARGS | CORDIC_CSR_RESSIZE | CORDIC_CSR_ARGSIZE)
#define CORDIC_EXPECTED_CONFIGURATION ((6U << CORDIC_CSR_PRECISION_Pos) | \
    CORDIC_CSR_NRES | CORDIC_CSR_NARGS)

/* 物理单元只有一个；不能以第二个软件对象规避超时后的隔离。 */
static G431CordicMath *owner;

static bool configuration_valid(void)
{
    return (RCC->AHB1ENR & RCC_AHB1ENR_CORDICEN) != 0U &&
        (CORDIC->CSR & CORDIC_CONFIGURATION_MASK) == CORDIC_EXPECTED_CONFIGURATION;
}

static void quarantine(G431CordicMath *context)
{
    /* RDATA 提前读会阻塞总线，所以没有 RRDY 就只隔离，不尝试强制读取。 */
    if (configuration_valid() && (CORDIC->CSR & CORDIC_CSR_RRDY) != 0U) {
        (void)CORDIC->RDATA;
        (void)CORDIC->RDATA;
    }
    context->quarantined = true;
    context->initialized = false;
}

static bool acquire(G431CordicMath *context)
{
    uint32_t attempt;
    for (attempt = 0U; attempt < G431_CORDIC_ACQUIRE_ATTEMPTS; attempt++) {
        if (__LDREXW(&context->busy) != 0U) {
            __CLREX();
            return false;
        }
        /*
         * 普通IRQ会清本地exclusive reservation，即使没有其他使用者也可能失败。
         * 仅该失败有限重取；不等待已占用的busy，不屏蔽中断，也不无限spin。
         */
        if (__STREXW(1U, &context->busy) == 0U) {
            __DMB();
            return true;
        }
    }
    return false;
}

static void release(G431CordicMath *context)
{
    __DMB();
    context->busy = 0U;
}

bool g431_cordic_math_init(G431CordicMath *context)
{
    uint32_t configuration;
    if (context == NULL) {
        return false;
    }
    if (owner != NULL) {
        if (owner != context || !context->initialized || context->quarantined ||
            context->busy != 0U) {
            return false;
        }
        if (!configuration_valid() || (CORDIC->CSR & CORDIC_CSR_RRDY) != 0U) {
            quarantine(context);
            return false;
        }
        return true;
    }
    owner = context;
    context->busy = 0U;
    context->initialized = false;
    context->quarantined = false;
    if ((RCC->AHB1ENR & RCC_AHB1ENR_CORDICEN) == 0U) {
        quarantine(context);
        return false;
    }
    configuration = CORDIC->CSR;
    if ((configuration & CORDIC_CSR_RRDY) != 0U ||
        ((configuration & CORDIC_CONFIGURATION_MASK) != 0x50U &&
         (configuration & CORDIC_CONFIGURATION_MASK) != CORDIC_EXPECTED_CONFIGURATION)) {
        quarantine(context);
        return false;
    }
    /* 本机 CubeMX 仅生成 HAL_CORDIC_Init/MSP 时钟代码，功能配置由本适配器拥有。 */
    CORDIC->CSR = CORDIC_EXPECTED_CONFIGURATION;
    if (!configuration_valid() || (CORDIC->CSR & CORDIC_CSR_RRDY) != 0U) {
        quarantine(context);
        return false;
    }
    context->initialized = true;
    return true;
}

bool g431_cordic_sincos(void *raw_context, float turns, float *sine, float *cosine)
{
    G431CordicMath *context = raw_context;
    int32_t angle;
    int32_t result_cosine;
    int32_t result_sine;
    uint32_t polls;
    bool ready = false;
    if (context == NULL || context != owner || !context->initialized ||
        context->quarantined || sine == NULL || cosine == NULL || sine == cosine ||
        !(turns >= 0.0f && turns < 1.0f)) {
        return false;
    }
    if (!acquire(context)) {
        return false;
    }
    /* 检查与获取 busy 之间可能被先行事务抢占；失败锁存必须在取得 busy 后再核对。 */
    if (!context->initialized || context->quarantined) {
        release(context);
        return false;
    }
    if (!configuration_valid() || (CORDIC->CSR & CORDIC_CSR_RRDY) != 0U) {
        quarantine(context);
        release(context);
        return false;
    }
    /*
     * theta/pi 的 Q1.31 等于 turns*2^32，再在半圈处映射到负半区。
     * 先分区可保证两个 cast 都在 int32_t 范围内；不把接近 +1 的 float
     * 先乘 2^31，避免四舍五入到 +2147483648 后发生越界转换。
     */
    angle = turns < 0.5f ? (int32_t)(turns * 4294967296.0f) :
        (int32_t)((turns - 1.0f) * 4294967296.0f);
    CORDIC->WDATA = (uint32_t)angle;
    CORDIC->WDATA = (uint32_t)INT32_MAX;
    for (polls = 0U; polls < G431_CORDIC_READY_POLL_LIMIT; polls++) {
        if ((CORDIC->CSR & CORDIC_CSR_RRDY) != 0U) {
            ready = true;
            break;
        }
    }
    if (!ready) {
        quarantine(context);
        release(context);
        return false;
    }
    result_cosine = (int32_t)CORDIC->RDATA;
    result_sine = (int32_t)CORDIC->RDATA;
    if (!configuration_valid() || (CORDIC->CSR & CORDIC_CSR_RRDY) != 0U) {
        quarantine(context);
        release(context);
        return false;
    }
    /* 单精度舍入可能将 INT32_MAX 对应的值表示为 1.0f，属于输出表示误差。 */
    *sine = (float)result_sine * (1.0f / 2147483648.0f);
    *cosine = (float)result_cosine * (1.0f / 2147483648.0f);
    release(context);
    return true;
}
