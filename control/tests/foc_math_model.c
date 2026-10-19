/* 在 Cortex-M4 模型中执行正式 FOC 数学 C；不包含硬件与功率操作。 */
#include "foc_math.h"

#include <stddef.h>
#include <stdint.h>

float model_input[4];
float model_output[5];
uint32_t model_result;
uint32_t model_valid;
uint32_t model_limited;
FocDq model_dq;
FocAlphaBeta model_ab;
FocModulation model_modulation;

void model_init(void)
{
    model_result = 0U;
}

void model_park(void)
{
    model_result = (uint32_t)foc_park(model_input[0], model_input[1], model_input[2],
                                      model_input[3], &model_dq);
    model_output[0] = model_dq.d;
    model_output[1] = model_dq.q;
    model_valid = (uint32_t)model_dq.valid;
}

void model_inverse_park(void)
{
    model_result = (uint32_t)foc_inverse_park(model_input[0], model_input[1], model_input[2],
                                              model_input[3], &model_ab);
    model_output[0] = model_ab.alpha;
    model_output[1] = model_ab.beta;
    model_valid = (uint32_t)model_ab.valid;
}

void model_svpwm(void)
{
    model_result = (uint32_t)foc_svpwm(model_input[0], model_input[1], model_input[2],
                                      model_input[3], &model_modulation);
    model_output[0] = model_modulation.applied_alpha_v;
    model_output[1] = model_modulation.applied_beta_v;
    model_output[2] = model_modulation.duty_a;
    model_output[3] = model_modulation.duty_b;
    model_output[4] = model_modulation.duty_c;
    model_limited = (uint32_t)model_modulation.limited;
    model_valid = (uint32_t)model_modulation.valid;
}

void model_null_arguments(void)
{
    model_result = (uint32_t)foc_park(0.0f, 0.0f, 0.0f, 1.0f, NULL);
    model_result |= (uint32_t)foc_inverse_park(0.0f, 0.0f, 0.0f, 1.0f, NULL);
    model_result |= (uint32_t)foc_svpwm(0.0f, 0.0f, 12.0f, 0.45f, NULL);
}
