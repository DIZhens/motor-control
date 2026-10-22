#include "runtime_parameters.h"
#include <stddef.h>
#include <string.h>

void runtime_parameters_init(RuntimeParameters *p, float kp, float ki)
{
    if (p == NULL) { return; }
    memset(p, 0, sizeof(*p));
    p->speed_default.kp = kp; p->speed_default.ki = ki;
    p->speed_staged.value = p->speed_default; p->speed_staged.generation = 1U;
    p->speed_active.value = p->speed_default;
    p->telemetry.enabled = true; p->telemetry.rate_hz = 250U; p->telemetry.generation = 1U;
}

RuntimeParameterResult runtime_parameters_write_pi(RuntimeParameters *p, const RuntimeSpeedPi *v, bool idle)
{
    RuntimeSpeedProfile next;
    if (p == NULL || v == NULL || !(v->kp >= 0.0005f && v->kp <= 0.005f)
        || !(v->ki >= 0.0f && v->ki <= 0.02f)) { return RUNTIME_PARAMETER_BAD_VALUE; }
    if (!idle) { return RUNTIME_PARAMETER_WRONG_STATE; }
    next.value = *v; next.generation = p->speed_staged.generation + 1U; next.in_use = false;
    p->speed_staged = next;
    return RUNTIME_PARAMETER_APPLIED;
}

RuntimeParameterResult runtime_parameters_write_telemetry(RuntimeParameters *p,
    bool enabled, uint16_t rate, bool idle)
{
    RuntimeTelemetryConfig next;
    if (p == NULL || (rate != 50U && rate != 250U && rate != 500U)) { return RUNTIME_PARAMETER_BAD_VALUE; }
    if (!idle) { return RUNTIME_PARAMETER_WRONG_STATE; }
    next.enabled = enabled; next.rate_hz = rate; next.generation = p->telemetry.generation + 1U;
    p->telemetry = next;
    return RUNTIME_PARAMETER_APPLIED;
}

void runtime_parameters_activate(RuntimeParameters *p)
{
    if (p != NULL) { p->speed_active = p->speed_staged; p->speed_active.in_use = true; }
}

void runtime_parameters_deactivate(RuntimeParameters *p)
{
    if (p != NULL) { p->speed_active.in_use = false; }
}
