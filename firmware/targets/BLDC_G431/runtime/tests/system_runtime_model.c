#include "system_runtime_target.h"
#include "system_runtime.h"
#include "telemetry_mailbox.h"
#include "FreeRTOS.h"
#include "task.h"

uint32_t SystemCoreClock = 170000000U;
volatile uint32_t model_hal_tick = 7U;
volatile uint32_t model_pre_ok = 1U, model_boot_ok = 1U;
volatile uint32_t model_pre_count, model_boot_count, model_fatal_reason;
volatile uint32_t model_sample_sequence = 42U;
volatile uint32_t model_sample_available = 1U, model_config_available = 1U;
volatile uint32_t model_enabled = 1U, model_rate_hz = 250U;
volatile uint32_t model_transport_ok = 1U, model_transport_count, model_service_count;
volatile uint32_t model_epoch = 1U, model_generation = 1U;
volatile uint32_t model_change_epoch_during_copy;
uint32_t system_runtime_target_telemetry_epoch(void) { return model_epoch; }
uint32_t model_mailbox_epoch(void)
{ RuntimeTelemetryValue value; return system_runtime_telemetry_peek(&value) ? value.epoch : 0U; }

uint32_t system_runtime_target_hal_tick(void) { return model_hal_tick; }
bool system_runtime_target_pre_scheduler(void) { model_pre_count++; return model_pre_ok != 0U; }
bool system_runtime_target_bootstrap(void)
{
    model_boot_count++;
    return model_boot_ok != 0U;
}
bool system_runtime_target_start_transport(void)
{
    model_transport_count++;
    return model_transport_ok != 0U;
}
void system_runtime_target_communication_service(void)
{
    RuntimeTelemetryValue value;
    model_service_count++;
    if (system_runtime_telemetry_peek(&value)) { system_runtime_telemetry_retire(); }
}
bool system_runtime_target_read_sample(SpeedCommissioningSample *sample)
{
    if (model_sample_available == 0U) { return false; }
    *sample = (SpeedCommissioningSample){0};
    sample->sample_sequence = model_sample_sequence;
    if (model_change_epoch_during_copy) { model_epoch++; model_change_epoch_during_copy = 0U; }
    return true;
}
bool system_runtime_target_read_telemetry(RuntimeTelemetryConfig *config)
{
    if (model_config_available == 0U) { return false; }
    config->enabled = model_enabled != 0U;
    config->rate_hz = (uint16_t)model_rate_hz;
    config->generation = model_generation;
    return true;
}
void system_runtime_target_fatal(RuntimeInternalFaultReason reason)
{
    model_fatal_reason = reason;
    for (;;) { }
}
void model_start(void) { system_runtime_start(); }
