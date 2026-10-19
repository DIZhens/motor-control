/* 编译正式适配器，以寄存器替身验证协议；不把替身当成实体 CORDIC 精度。 */
#include "cordic_math.h"

#include <stddef.h>

G431CordicMath model_context;
G431CordicMath model_second;
float model_turns;
float model_outputs[2];
uint32_t model_argument_kind; /* 0 正常；1 空 context；2 空 sin；3 空 cos；4 同地址输出。 */
uint32_t model_result;
uint32_t model_words[3]; /* initialized / quarantined / busy。 */

static void publish(void)
{
    model_words[0] = (uint32_t)model_context.initialized;
    model_words[1] = (uint32_t)model_context.quarantined;
    model_words[2] = model_context.busy;
}

void model_init(void)
{
    model_outputs[0] = 123.0f;
    model_outputs[1] = -456.0f;
    model_result = (uint32_t)g431_cordic_math_init(&model_context);
    publish();
}

void model_init_null(void)
{
    model_result = (uint32_t)g431_cordic_math_init(NULL);
    publish();
}

void model_init_second(void)
{
    model_result = (uint32_t)g431_cordic_math_init(&model_second);
    publish();
}

void model_compute(void)
{
    void *context = model_argument_kind == 1U ? NULL : &model_context;
    float *sine = model_argument_kind == 2U ? NULL : &model_outputs[0];
    float *cosine = model_argument_kind == 3U ? NULL :
        (model_argument_kind == 4U ? &model_outputs[0] : &model_outputs[1]);
    model_result = (uint32_t)g431_cordic_sincos(context, model_turns, sine, cosine);
    publish();
}

void model_compute_second(void)
{
    model_result = (uint32_t)g431_cordic_sincos(&model_second, model_turns,
        &model_outputs[0], &model_outputs[1]);
    publish();
}

void model_mark_busy(void)
{
    model_context.busy = 1U;
    publish();
}

void model_clear_busy(void)
{
    model_context.busy = 0U;
    publish();
}
