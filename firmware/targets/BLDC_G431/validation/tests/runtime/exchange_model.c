/* 真实Target交接入口的模型包装；不进入Keil，不调用设备或内核。 */
#if defined(BLDC_G431_FINITE_RUN)
#include "target_runtime.h"
#else
#include "target_observe_runtime.h"
#endif
#include "control_runtime.h"
#include <stddef.h>
#include <string.h>

uint32_t exchange_kind, exchange_command, exchange_rate = 250U, exchange_enabled = 1U;
float exchange_target, exchange_kp = 0.001f, exchange_ki = 0.004f;
uint32_t exchange_result[16], exchange_reason;
RuntimeRequest exchange_source;
RuntimeCommandCompletion exchange_completion;
RuntimeParameters exchange_parameters;
MotorTelemetrySample exchange_sample;
RuntimeExchange exchange_unit;
RuntimeCommandEnvelope exchange_envelope;
RuntimeInternalFaultStatus exchange_fault;
float exchange_profile_values[6];
uint32_t exchange_profile_meta[6];

const uint32_t exchange_layout[] = {
    sizeof(RuntimeParameters), offsetof(RuntimeParameters, speed_staged) + offsetof(RuntimeSpeedProfile, generation),
    offsetof(RuntimeExchange, head), offsetof(RuntimeExchange, tail), offsetof(RuntimeExchange, last_ticket),
    offsetof(RuntimeExchange, completions), sizeof(RuntimeCommandCompletion),
    sizeof(MotorTelemetrySample), offsetof(MotorTelemetrySample, sample_sequence),
    offsetof(MotorTelemetrySample, control_time_ticks), offsetof(MotorTelemetrySample, position_observation_time),
    offsetof(MotorTelemetrySample, target_velocity_rpm), offsetof(MotorTelemetrySample, valid_flags),
    offsetof(MotorTelemetrySample, active_profile_generation), offsetof(MotorTelemetrySample, telemetry_config_generation),
    offsetof(MotorTelemetrySample, limit_flags),
    offsetof(ControlRuntime, speed_controller) + offsetof(SpeedController, config)
        + offsetof(SpeedControllerConfig, kp_a_per_rpm),
    offsetof(RuntimeInternalFaultStatus, count), offsetof(RuntimeExchange, internal_stop)
};

static void source(void)
{
    memset(&exchange_source, 0, sizeof(exchange_source));
    exchange_source.kind = (RuntimeRequestKind)exchange_kind;
    if (exchange_kind == RUNTIME_REQUEST_MOTOR) {
        exchange_source.value.motor.type = (MotorCommandType)exchange_command;
        exchange_source.value.motor.target_velocity_rpm = exchange_target;
    } else if (exchange_kind == RUNTIME_REQUEST_SPEED_PI) {
        exchange_source.value.speed_pi.kp = exchange_kp; exchange_source.value.speed_pi.ki = exchange_ki;
    } else {
        exchange_source.value.telemetry.enabled = exchange_enabled != 0U;
        exchange_source.value.telemetry.rate_hz = (uint16_t)exchange_rate;
    }
}

static void result(const RuntimeCommandCompletion *completion)
{
    exchange_result[4] = completion->ticket; exchange_result[5] = completion->kind;
    exchange_result[6] = completion->outcome; exchange_result[7] = completion->motor_result;
    exchange_result[8] = completion->parameter_result;
    exchange_result[9] = completion->value.speed_pi.generation;
    exchange_result[10] = completion->value.telemetry.rate_hz;
    exchange_result[11] = completion->value.telemetry.generation;
}

void exchange_submit(void)
{
    source(); exchange_result[1] = 0xA5A5A5A5U; exchange_result[2] = 0xA5A5A5A5U;
    exchange_result[0] = target_runtime_submit_request(&exchange_source, &exchange_result[1], &exchange_result[2]);
}

void exchange_change_source(void) { source(); }

void exchange_pop(void)
{
    exchange_result[3] = target_runtime_pop_completion(&exchange_completion);
    if (exchange_result[3] != 0U) { result(&exchange_completion); }
}

void exchange_read(void)
{
    exchange_result[12] = target_runtime_read_parameters(&exchange_parameters);
    exchange_result[13] = target_runtime_read_motor_telemetry(&exchange_sample);
    exchange_result[14] = target_runtime_read_internal_fault(&exchange_fault);
    exchange_result[15] = target_runtime_integrity_ok();
    if (exchange_result[12]) {
        exchange_profile_values[0] = exchange_parameters.speed_default.kp;
        exchange_profile_values[1] = exchange_parameters.speed_default.ki;
        exchange_profile_values[2] = exchange_parameters.speed_staged.value.kp;
        exchange_profile_values[3] = exchange_parameters.speed_staged.value.ki;
        exchange_profile_values[4] = exchange_parameters.speed_active.value.kp;
        exchange_profile_values[5] = exchange_parameters.speed_active.value.ki;
        exchange_profile_meta[0] = exchange_parameters.speed_staged.generation;
        exchange_profile_meta[1] = exchange_parameters.speed_active.generation;
        exchange_profile_meta[2] = exchange_parameters.speed_active.in_use;
        exchange_profile_meta[3] = exchange_parameters.telemetry.enabled;
        exchange_profile_meta[4] = exchange_parameters.telemetry.rate_hz;
        exchange_profile_meta[5] = exchange_parameters.telemetry.generation;
    }
}

void exchange_fail(void) { target_runtime_fail_closed((RuntimeInternalFaultReason)exchange_reason); }

void exchange_unit_init(void) { runtime_exchange_init(&exchange_unit); }
void exchange_unit_submit(void)
{
    source(); exchange_result[1] = 0xA5A5A5A5U; exchange_result[2] = 0xA5A5A5A5U;
    exchange_result[0] = runtime_exchange_submit(&exchange_unit, &exchange_source,
        &exchange_result[1], &exchange_result[2]);
}
void exchange_unit_take(void)
{
    exchange_result[3] = runtime_exchange_take(&exchange_unit, &exchange_envelope, &exchange_result[2]);
    exchange_result[4] = exchange_envelope.ticket;
}
void exchange_unit_complete(void)
{
    memset(&exchange_completion, 0, sizeof(exchange_completion));
    exchange_completion.ticket = exchange_reason; exchange_completion.motor_result = (MotorCommandResult)exchange_command;
    exchange_result[3] = runtime_exchange_complete(&exchange_unit, &exchange_completion);
}
void exchange_unit_pop(void)
{
    exchange_result[3] = runtime_exchange_pop(&exchange_unit, &exchange_completion);
    if (exchange_result[3] != 0U) { result(&exchange_completion); }
}

void exchange_nulls(void)
{
    uint32_t ticket = 7U, cancelled = 8U;
    exchange_result[0] = runtime_exchange_submit(NULL, &exchange_source, &ticket, &cancelled);
    exchange_result[1] = (ticket == 7U && cancelled == 8U
        && !runtime_exchange_take(NULL, &exchange_envelope, &cancelled)
        && !runtime_exchange_complete(NULL, &exchange_completion)
        && !runtime_exchange_pop(NULL, &exchange_completion)
        && !target_runtime_read_parameters(NULL) && !target_runtime_read_motor_telemetry(NULL)
        && !target_runtime_read_internal_fault(NULL));
    runtime_exchange_init(NULL); runtime_exchange_request_internal_stop(NULL);
}
