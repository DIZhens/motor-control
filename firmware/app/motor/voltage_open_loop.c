#include "voltage_open_loop.h"

#include <math.h>
#include <stddef.h>
#include <string.h>

#include "../../../control/foc_math.h"

#define OPEN_LOOP_INV_SQRT_THREE 0.5773502691896258f

static void clear_output(VoltageOpenLoop *loop)
{
    memset(&loop->output, 0, sizeof(loop->output));
    loop->output.duty_a = 0.5f;
    loop->output.duty_b = 0.5f;
    loop->output.duty_c = 0.5f;
}

static bool terminate(VoltageOpenLoop *loop, VoltageOpenLoopState state,
                      VoltageOpenLoopReason reason)
{
    clear_output(loop);
    loop->state = state;
    loop->reason = reason;
    return false;
}

static uint64_t duration(const VoltageOpenLoopConfig *config)
{
    if (config->profile == VOLTAGE_OPEN_LOOP_STATIC_SURVEY) {
        return (uint64_t)config->voltage_ramp_ticks + 3ULL * config->alignment_hold_ticks +
            2ULL * config->frequency_ramp_ticks + config->voltage_ramp_down_ticks;
    }
    return (uint64_t)config->voltage_ramp_ticks + config->alignment_hold_ticks +
        config->frequency_ramp_ticks + config->rotation_ticks + config->voltage_ramp_down_ticks;
}

static bool config_valid(const VoltageOpenLoopConfig *config)
{
    float step_turns;
    if (config->ticks_per_second == 0U || !isfinite(config->voltage_v) ||
        config->voltage_v <= 0.0f || !isfinite(config->target_electrical_hz) ||
        !isfinite(config->max_voltage_ratio) ||
        config->max_voltage_ratio <= 0.0f || config->max_voltage_ratio > OPEN_LOOP_INV_SQRT_THREE ||
        !isfinite(config->min_bus_voltage_v) || config->min_bus_voltage_v <= 0.0f ||
        !isfinite(config->max_bus_voltage_v) || config->max_bus_voltage_v < config->min_bus_voltage_v ||
        config->voltage_v > config->max_voltage_ratio * config->min_bus_voltage_v ||
        !isfinite(config->nominal_current_trip_a) || config->nominal_current_trip_a <= 0.0f ||
        config->voltage_ramp_ticks == 0U || config->alignment_hold_ticks == 0U ||
        config->frequency_ramp_ticks == 0U || config->rotation_ticks == 0U ||
        config->voltage_ramp_down_ticks == 0U || config->max_step_ticks == 0U ||
        config->max_step_ticks > config->voltage_ramp_ticks ||
        config->max_step_ticks > config->frequency_ramp_ticks ||
        config->max_step_ticks > config->voltage_ramp_down_ticks ||
        duration(config) >= (uint64_t)config->timeout_ticks) {
        return false;
    }
    if (config->profile == VOLTAGE_OPEN_LOOP_STATIC_SURVEY) {
        step_turns = config->excursion_turns *
            ((float)config->max_step_ticks / (float)config->frequency_ramp_ticks);
        return config->target_electrical_hz == 0.0f && isfinite(config->excursion_turns) &&
            config->excursion_turns > 0.0f && config->excursion_turns <= 1.0f &&
            isfinite(step_turns) && step_turns <= 0.25f;
    }
    if (config->profile != VOLTAGE_OPEN_LOOP_ROTATING || config->excursion_turns != 0.0f ||
        config->target_electrical_hz == 0.0f) {
        return false;
    }
    /* 单步相位增量有界，避免跳过多圈或对任意大浮点做整数取模。 */
    step_turns = fabsf(config->target_electrical_hz) *
        ((float)config->max_step_ticks / (float)config->ticks_per_second);
    return isfinite(step_turns) && step_turns <= 0.25f;
}

bool voltage_open_loop_init(VoltageOpenLoop *loop, const VoltageOpenLoopConfig *config)
{
    VoltageOpenLoopConfig requested;
    if (loop == NULL) {
        return false;
    }
    if (config != NULL) {
        requested = *config;
    } else {
        memset(&requested, 0, sizeof(requested));
    }
    memset(loop, 0, sizeof(*loop));
    clear_output(loop);
    if (!config_valid(&requested)) {
        return false;
    }
    loop->config = requested;
    loop->configured = true;
    return true;
}

static bool fresh(ControlTime now, ControlTime observed, uint32_t maximum_age)
{
    return observed <= now && now - observed <= (ControlTime)maximum_age;
}

static VoltageOpenLoopReason check_input(const VoltageOpenLoop *loop,
                                         const VoltageOpenLoopInput *input)
{
    uint32_t index;
    if (input->fault_active) {
        return VOLTAGE_OPEN_LOOP_REASON_FAULT;
    }
    if (!input->permitted) {
        return VOLTAGE_OPEN_LOOP_REASON_PREREQUISITE;
    }
    if (!input->current_valid || !input->bus_valid ||
        !fresh(input->now, input->current_time, loop->config.max_current_age_ticks) ||
        !fresh(input->now, input->bus_time, loop->config.max_bus_age_ticks)) {
        return VOLTAGE_OPEN_LOOP_REASON_FEEDBACK;
    }
    if (!isfinite(input->bus_voltage_v) || input->bus_voltage_v < loop->config.min_bus_voltage_v ||
        input->bus_voltage_v > loop->config.max_bus_voltage_v) {
        return VOLTAGE_OPEN_LOOP_REASON_BUS;
    }
    for (index = 0U; index < 3U; ++index) {
        if (!isfinite(input->phase_current_a[index])) {
            return VOLTAGE_OPEN_LOOP_REASON_FEEDBACK;
        }
        if (fabsf(input->phase_current_a[index]) >= loop->config.nominal_current_trip_a) {
            return VOLTAGE_OPEN_LOOP_REASON_NOMINAL_CURRENT;
        }
    }
    return VOLTAGE_OPEN_LOOP_REASON_NONE;
}

void voltage_open_loop_stop(VoltageOpenLoop *loop)
{
    if (loop != NULL) {
        (void)terminate(loop, VOLTAGE_OPEN_LOOP_STOPPED, VOLTAGE_OPEN_LOOP_REASON_STOP);
    }
}

bool voltage_open_loop_start(VoltageOpenLoop *loop, const VoltageOpenLoopInput *input)
{
    VoltageOpenLoopReason reason;
    if (loop == NULL || !loop->configured || loop->state != VOLTAGE_OPEN_LOOP_IDLE) {
        return false;
    }
    if (input == NULL) {
        return terminate(loop, VOLTAGE_OPEN_LOOP_FAILED, VOLTAGE_OPEN_LOOP_REASON_FEEDBACK);
    }
    if (input->stop_requested) {
        voltage_open_loop_stop(loop);
        return false;
    }
    reason = check_input(loop, input);
    if (reason != VOLTAGE_OPEN_LOOP_REASON_NONE) {
        return terminate(loop, VOLTAGE_OPEN_LOOP_FAILED, reason);
    }
    if (input->now > UINT64_MAX - (ControlTime)loop->config.timeout_ticks) {
        return terminate(loop, VOLTAGE_OPEN_LOOP_FAILED, VOLTAGE_OPEN_LOOP_REASON_TIME);
    }
    loop->started_at = input->now;
    loop->last_update_time = input->now;
    loop->last_current_time = input->current_time;
    loop->last_bus_time = input->bus_time;
    loop->last_bus_voltage_v = input->bus_voltage_v;
    loop->electrical_turns = 0.0f;
    loop->state = VOLTAGE_OPEN_LOOP_RAMP;
    return true;
}

/* 在线性升频区间用梯形积分；跨边界时分段，不能把最新频率用于整个旧时间段。 */
static float phase_increment(const VoltageOpenLoopConfig *config, uint64_t first,
                              uint64_t last)
{
    uint64_t begin = (uint64_t)config->voltage_ramp_ticks + config->alignment_hold_ticks;
    uint64_t end = begin + config->frequency_ramp_ticks;
    uint64_t low, high;
    float turns = 0.0f;
    if (last <= begin) {
        return 0.0f;
    }
    low = first > begin ? first : begin;
    high = last < end ? last : end;
    if (high > low) {
        float fraction_first = (float)(low - begin) / (float)config->frequency_ramp_ticks;
        float fraction_last = (float)(high - begin) / (float)config->frequency_ramp_ticks;
        turns = config->target_electrical_hz * (0.5f * (fraction_first + fraction_last)) *
            ((float)(high - low) / (float)config->ticks_per_second);
    }
    low = first > end ? first : end;
    if (last > low) {
        turns += config->target_electrical_hz *
            ((float)(last - low) / (float)config->ticks_per_second);
    }
    return turns;
}

bool voltage_open_loop_update(VoltageOpenLoop *loop, const VoltageOpenLoopInput *input)
{
    return voltage_open_loop_update_with_trig(loop, input, electrical_angle_sincos_software, NULL);
}

/* 有限静态往返采用绝对时间解析角度，避免保持段漂移或返回时累计误差。 */
static void static_survey_trajectory(VoltageOpenLoop *loop, uint64_t elapsed,
    float *voltage, float *frequency, float *phase)
{
    const VoltageOpenLoopConfig *c = &loop->config;
    uint64_t ramp = c->voltage_ramp_ticks;
    uint64_t hold0 = ramp + c->alignment_hold_ticks;
    uint64_t move_out = hold0 + c->frequency_ramp_ticks;
    uint64_t hold_out = move_out + c->alignment_hold_ticks;
    uint64_t move_back = hold_out + c->frequency_ramp_ticks;
    uint64_t hold_return = move_back + c->alignment_hold_ticks;
    float speed = c->excursion_turns * (float)c->ticks_per_second / (float)c->frequency_ramp_ticks;

    *voltage = c->voltage_v;
    *frequency = 0.0f;
    *phase = 0.0f;
    if (elapsed < ramp) {
        loop->state = VOLTAGE_OPEN_LOOP_RAMP;
        *voltage *= (float)elapsed / (float)c->voltage_ramp_ticks;
    } else if (elapsed < hold0) {
        loop->state = VOLTAGE_OPEN_LOOP_HOLD;
    } else if (elapsed < move_out) {
        loop->state = VOLTAGE_OPEN_LOOP_MOVE_OUT;
        *phase = c->excursion_turns * (float)(elapsed - hold0) / (float)c->frequency_ramp_ticks;
        *frequency = speed;
    } else if (elapsed < hold_out) {
        loop->state = VOLTAGE_OPEN_LOOP_HOLD_OUT;
        *phase = c->excursion_turns;
    } else if (elapsed < move_back) {
        loop->state = VOLTAGE_OPEN_LOOP_MOVE_BACK;
        *phase = c->excursion_turns * (1.0f - (float)(elapsed - hold_out) / (float)c->frequency_ramp_ticks);
        *frequency = -speed;
    } else if (elapsed < hold_return) {
        loop->state = VOLTAGE_OPEN_LOOP_HOLD_RETURN;
    } else {
        loop->state = VOLTAGE_OPEN_LOOP_RAMP_DOWN;
        *voltage *= 1.0f - (float)(elapsed - hold_return) / (float)c->voltage_ramp_down_ticks;
    }
}

bool voltage_open_loop_update_with_trig(VoltageOpenLoop *loop,
                                       const VoltageOpenLoopInput *input,
                                       ElectricalAngleTrig trig, void *context)
{
    VoltageOpenLoopReason reason;
    uint64_t elapsed, previous, ramp_end, hold_end, frequency_end, rotation_end;
    float voltage, frequency, phase, increment, rounding_error, sine = 0.0f, cosine = 0.0f;
    FocAlphaBeta vector;
    FocModulation modulation;
    if (loop == NULL) {
        return false;
    }
    clear_output(loop);
    if (!loop->configured || !((loop->state >= VOLTAGE_OPEN_LOOP_RAMP &&
        loop->state <= VOLTAGE_OPEN_LOOP_RAMP_DOWN) || (loop->state >= VOLTAGE_OPEN_LOOP_MOVE_OUT &&
        loop->state <= VOLTAGE_OPEN_LOOP_HOLD_RETURN))) {
        return false;
    }
    if (input == NULL) {
        return terminate(loop, VOLTAGE_OPEN_LOOP_FAILED, VOLTAGE_OPEN_LOOP_REASON_FEEDBACK);
    }
    if (input->stop_requested) {
        voltage_open_loop_stop(loop);
        return false;
    }
    reason = check_input(loop, input);
    if (reason != VOLTAGE_OPEN_LOOP_REASON_NONE) {
        return terminate(loop, VOLTAGE_OPEN_LOOP_FAILED, reason);
    }
    if (input->now <= loop->last_update_time ||
        input->now - loop->last_update_time > loop->config.max_step_ticks ||
        input->current_time <= loop->last_current_time || input->bus_time < loop->last_bus_time ||
        (input->bus_time == loop->last_bus_time && input->bus_voltage_v != loop->last_bus_voltage_v)) {
        return terminate(loop, VOLTAGE_OPEN_LOOP_FAILED, VOLTAGE_OPEN_LOOP_REASON_TIME);
    }
    elapsed = input->now - loop->started_at;
    if (elapsed >= loop->config.timeout_ticks) {
        return terminate(loop, VOLTAGE_OPEN_LOOP_FAILED, VOLTAGE_OPEN_LOOP_REASON_TIMEOUT);
    }
    if (elapsed >= duration(&loop->config)) {
        return terminate(loop, VOLTAGE_OPEN_LOOP_COMPLETE, VOLTAGE_OPEN_LOOP_REASON_NONE);
    }
    if (loop->config.profile == VOLTAGE_OPEN_LOOP_STATIC_SURVEY) {
        static_survey_trajectory(loop, elapsed, &voltage, &frequency, &phase);
        /* 轨迹可移出完整一电角圈；三角后端和公共相位始终使用[0,1)。 */
        if (phase >= 1.0f) {
            phase -= 1.0f;
        }
        rounding_error = 0.0f;
    } else {
        previous = loop->last_update_time - loop->started_at;
        increment = phase_increment(&loop->config, previous, elapsed) - loop->phase_rounding_error;
        phase = loop->electrical_turns + increment;
        rounding_error = (phase - loop->electrical_turns) - increment;
        if (!isfinite(phase) || phase < -0.25f || phase > 1.25f) {
            return terminate(loop, VOLTAGE_OPEN_LOOP_FAILED, VOLTAGE_OPEN_LOOP_REASON_NUMERIC);
        }
        if (phase < 0.0f) {
            float wrapped = phase + 1.0f;
            rounding_error += (wrapped - 1.0f) - phase;
            phase = wrapped;
        } else if (phase >= 1.0f) {
            phase -= 1.0f;
        }
        /* 极小负数加一可能舍入为 1，归一化周期零点。 */
        if (phase >= 1.0f) {
            phase = 0.0f;
        }
        ramp_end = loop->config.voltage_ramp_ticks;
        hold_end = ramp_end + loop->config.alignment_hold_ticks;
        frequency_end = hold_end + loop->config.frequency_ramp_ticks;
        rotation_end = frequency_end + loop->config.rotation_ticks;
        voltage = loop->config.voltage_v;
        frequency = 0.0f;
        if (elapsed < ramp_end) {
            loop->state = VOLTAGE_OPEN_LOOP_RAMP;
            voltage *= (float)elapsed / (float)loop->config.voltage_ramp_ticks;
        } else if (elapsed < hold_end) {
            loop->state = VOLTAGE_OPEN_LOOP_HOLD;
        } else if (elapsed < frequency_end) {
            loop->state = VOLTAGE_OPEN_LOOP_ACCELERATE;
            frequency = loop->config.target_electrical_hz *
                ((float)(elapsed - hold_end) / (float)loop->config.frequency_ramp_ticks);
        } else if (elapsed < rotation_end) {
            loop->state = VOLTAGE_OPEN_LOOP_ROTATE;
            frequency = loop->config.target_electrical_hz;
        } else {
            loop->state = VOLTAGE_OPEN_LOOP_RAMP_DOWN;
            frequency = loop->config.target_electrical_hz;
            voltage *= 1.0f - (float)(elapsed - rotation_end) /
                (float)loop->config.voltage_ramp_down_ticks;
        }
    }
    if (!isfinite(voltage) || !isfinite(frequency) || trig == NULL ||
        !trig(context, phase, &sine, &cosine) ||
        !foc_inverse_park(voltage, 0.0f, sine, cosine, &vector) ||
        !foc_svpwm(vector.alpha, vector.beta, input->bus_voltage_v,
                    loop->config.max_voltage_ratio, &modulation) || modulation.limited) {
        return terminate(loop, VOLTAGE_OPEN_LOOP_FAILED, VOLTAGE_OPEN_LOOP_REASON_NUMERIC);
    }
    loop->last_update_time = input->now;
    loop->last_current_time = input->current_time;
    loop->last_bus_time = input->bus_time;
    loop->last_bus_voltage_v = input->bus_voltage_v;
    loop->electrical_turns = phase;
    loop->phase_rounding_error = rounding_error;
    loop->output.duty_a = modulation.duty_a;
    loop->output.duty_b = modulation.duty_b;
    loop->output.duty_c = modulation.duty_c;
    loop->output.alpha_voltage_v = modulation.applied_alpha_v;
    loop->output.beta_voltage_v = modulation.applied_beta_v;
    loop->output.electrical_turns = phase;
    loop->output.electrical_hz = frequency;
    loop->output.output_requested = true;
    return true;
}
