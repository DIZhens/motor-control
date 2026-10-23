/* 执行正式开环模块的离线入口；无硬件、生命周期或功率授权。 */
#include "voltage_open_loop.h"

#include <math.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

VoltageOpenLoopConfig model_config;
VoltageOpenLoop model_loop;
ControlTime model_now;
ControlTime model_current_time;
ControlTime model_bus_time;
float model_inputs[4];
float model_values[8];
uint32_t model_flags;
uint32_t model_result;
uint32_t model_diag[4];
uint32_t model_trig_mode;

static VoltageOpenLoopInput snapshot(void)
{
    VoltageOpenLoopInput input;
    memset(&input, 0, sizeof(input));
    input.now = model_now;
    input.current_time = model_current_time;
    input.bus_time = model_bus_time;
    input.phase_current_a[0] = model_inputs[0];
    input.phase_current_a[1] = model_inputs[1];
    input.phase_current_a[2] = model_inputs[2];
    input.bus_voltage_v = model_inputs[3];
    input.permitted = (model_flags & 1U) != 0U;
    input.current_valid = (model_flags & 2U) != 0U;
    input.bus_valid = (model_flags & 4U) != 0U;
    input.fault_active = (model_flags & 8U) != 0U;
    input.stop_requested = (model_flags & 16U) != 0U;
    return input;
}

static void publish(void)
{
    model_diag[0] = (uint32_t)model_loop.state;
    model_diag[1] = (uint32_t)model_loop.reason;
    model_diag[2] = (uint32_t)model_loop.configured;
    model_diag[3] = (uint32_t)model_loop.output.output_requested;
    model_values[0] = model_loop.output.duty_a;
    model_values[1] = model_loop.output.duty_b;
    model_values[2] = model_loop.output.duty_c;
    model_values[3] = model_loop.output.alpha_voltage_v;
    model_values[4] = model_loop.output.beta_voltage_v;
    model_values[5] = model_loop.output.electrical_turns;
    model_values[6] = model_loop.output.electrical_hz;
    model_values[7] = model_loop.electrical_turns;
}

void model_init(void)
{
    model_result = (uint32_t)voltage_open_loop_init(&model_loop, &model_config);
    publish();
}

void model_start(void)
{
    VoltageOpenLoopInput input = snapshot();
    model_result = (uint32_t)voltage_open_loop_start(&model_loop, &input);
    publish();
}

void model_update(void)
{
    VoltageOpenLoopInput input = snapshot();
    model_result = (uint32_t)voltage_open_loop_update(&model_loop, &input);
    publish();
}

static bool test_trig(void *context, float turns, float *sine, float *cosine)
{
    uint32_t mode = *(uint32_t *)context;
    if (mode == 1U) {
        return false;
    }
    if (mode == 2U) {
        *sine = NAN;
        *cosine = 1.0f;
        return true;
    }
    if (mode == 3U) {
        *sine = 1.0f;
        *cosine = 1.0f;
        return true;
    }
    return electrical_angle_sincos_software(NULL, turns, sine, cosine);
}

void model_update_trig(void)
{
    VoltageOpenLoopInput input = snapshot();
    model_result = (uint32_t)voltage_open_loop_update_with_trig(&model_loop, &input,
        model_trig_mode == 4U ? NULL : test_trig, &model_trig_mode);
    publish();
}

void model_stop(void)
{
    voltage_open_loop_stop(&model_loop);
    model_result = 0U;
    publish();
}

void model_alias_init(void)
{
    model_result = (uint32_t)voltage_open_loop_init(&model_loop, &model_loop.config);
    publish();
}

void model_null_init(void)
{
    model_result = (uint32_t)voltage_open_loop_init(&model_loop, NULL);
    publish();
}

void model_null_start(void)
{
    model_result = (uint32_t)voltage_open_loop_start(&model_loop, NULL);
    publish();
}

void model_null_update(void)
{
    model_result = (uint32_t)voltage_open_loop_update(&model_loop, NULL);
    publish();
}

void model_nulls(void)
{
    model_result = (uint32_t)voltage_open_loop_init(NULL, &model_config);
    model_result |= (uint32_t)voltage_open_loop_start(NULL, NULL);
    model_result |= (uint32_t)voltage_open_loop_update(NULL, NULL);
    voltage_open_loop_stop(NULL);
    publish();
}
