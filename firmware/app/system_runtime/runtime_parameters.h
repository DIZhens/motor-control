#ifndef RUNTIME_PARAMETERS_H
#define RUNTIME_PARAMETERS_H

#include "runtime_exchange.h"

typedef struct {
    RuntimeSpeedPi speed_default;
    RuntimeSpeedProfile speed_staged, speed_active;
    RuntimeTelemetryConfig telemetry;
} RuntimeParameters;

/* 启动时在开放采样前初始化，之后仅实时所有者修改；读者取得Target发布的副本。 */
void runtime_parameters_init(RuntimeParameters *parameters, float default_kp, float default_ki);
RuntimeParameterResult runtime_parameters_write_pi(RuntimeParameters *parameters,
    const RuntimeSpeedPi *value, bool healthy_idle);
RuntimeParameterResult runtime_parameters_write_telemetry(RuntimeParameters *parameters,
    bool enabled, uint16_t rate_hz, bool healthy_idle);
/* 仅在Motor已接受Start且PI实例装配成功后锁存；Stop/Fault只撤销in_use。 */
void runtime_parameters_activate(RuntimeParameters *parameters);
void runtime_parameters_deactivate(RuntimeParameters *parameters);

#endif
