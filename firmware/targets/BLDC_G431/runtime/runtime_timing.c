#include "runtime_timing.h"

#if defined(__CC_ARM)
#pragma Otime
#endif

#include <stddef.h>
#include <string.h>

#include "stm32g4xx.h"
#include "control_runtime.h"
#include "motor.h"
#include "realtime_motor_io.h"
#include "position_observation.h"
#include "bus_observation.h"
#include "../platform/realtime_motor_io_config.h"

bool g431_runtime_timing_runtime_init(ControlRuntime *runtime, Motor *motor,
    const ControlRuntimeConfig *config);
bool g431_runtime_timing_runtime_step(ControlRuntime *runtime, ControlTime now,
    const PhaseCurrentObservation *current, bool power_disabled,
    uint32_t fault_evidence, const MotorCommand *command, bool has_command);

volatile uint32_t g_target_runtime_timing_sequence;
volatile uint32_t g_target_runtime_timing_active;
volatile G431RuntimeTimingResult g_target_runtime_timing_result;

static Motor s_timing_motor;
static ControlRuntime s_timing_runtime;
static Motor s_timing_initial_motor;
static ControlRuntime s_timing_initial_runtime;
static G431RuntimeTimingResult s_timing_result;
static G431RuntimeTimingStampRead s_timing_stamp_read;
static void *s_timing_stamp_context;
static G431RuntimeTimingStamp s_timing_begin_stamp;
static G431RuntimeTimingStamp s_timing_end_stamp;
static bool s_timing_fixture_ready;
static bool s_timing_in_step;
static bool s_timing_irq_open;
static bool s_timing_pending;
/* 第80拍立即关断，下一真实采样空拍复核并发布，不追加第81次控制。 */
static bool s_timing_terminal_pending;
static G431RuntimeTimingFailure s_timing_terminal_reason;
static bool s_timing_speed_updated;
static bool s_timing_limited;
static uint32_t s_timing_irq_begin;
static uint32_t s_timing_work_begin;
static uint32_t s_timing_work_end;
static uint32_t s_timing_failure;
static uint32_t s_timing_step_sink_calls;
static uint32_t s_timing_step_observation_reads;
static ControlTime s_timing_now;
static float s_timing_position;

static void timing_publish(void)
{
    g_target_runtime_timing_sequence++;
    __DMB();
    g_target_runtime_timing_result = s_timing_result;
    __DMB();
    g_target_runtime_timing_sequence++;
}

static void timing_remember_failure(G431RuntimeTimingFailure reason)
{
    if (s_timing_failure == G431_RUNTIME_TIMING_FAILURE_NONE) {
        s_timing_failure = (uint32_t)reason;
    }
}

static bool timing_read_stamp(G431RuntimeTimingStamp *stamp)
{
    if (s_timing_stamp_read == NULL
        || !s_timing_stamp_read(s_timing_stamp_context, stamp)) {
        timing_remember_failure(G431_RUNTIME_TIMING_FAILURE_CLOCK);
        return false;
    }
    if ((stamp->timer_bdtr & TIM_BDTR_MOE) != 0U) {
        timing_remember_failure(G431_RUNTIME_TIMING_FAILURE_POWER);
        return false;
    }
    /* compute-only允许跨峰/谷和原stage截止；不能用旧1700线阻断计算测量。 */
    if ((stamp->timer_cr1 & (TIM_CR1_CEN | TIM_CR1_CMS | TIM_CR1_UDIS | TIM_CR1_OPM))
            != (TIM_CR1_CEN | TIM_CR1_CMS_0)
        || stamp->timer_count > REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS
        || stamp->callback_entry_count > REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS) {
        timing_remember_failure(G431_RUNTIME_TIMING_FAILURE_CLOCK);
        return false;
    }
    return true;
}

static void timing_freeze(G431RuntimeTimingFailure reason)
{
    G431RuntimeTimingStamp cleanup;

    if (s_timing_failure != G431_RUNTIME_TIMING_FAILURE_NONE) {
        reason = (G431RuntimeTimingFailure)s_timing_failure;
    }
    realtime_motor_io_safe_off();
    if (s_timing_stamp_read == NULL
        || !s_timing_stamp_read(s_timing_stamp_context, &cleanup)) {
        if (reason == G431_RUNTIME_TIMING_FAILURE_NONE) {
            reason = G431_RUNTIME_TIMING_FAILURE_CLOCK;
        }
    } else if ((cleanup.timer_bdtr & TIM_BDTR_MOE) != 0U
        && reason == G431_RUNTIME_TIMING_FAILURE_NONE) {
        reason = G431_RUNTIME_TIMING_FAILURE_POWER;
    }
    s_timing_result.last_shadow_faults = s_timing_motor.faults;
    s_timing_result.failure = (uint32_t)reason;
    s_timing_result.state = reason == G431_RUNTIME_TIMING_FAILURE_NONE
        ? G431_RUNTIME_TIMING_COMPLETE : G431_RUNTIME_TIMING_FAILED;
    s_timing_in_step = false;
    s_timing_pending = false;
    s_timing_irq_open = false;
    s_timing_terminal_pending = false;
    g_target_runtime_timing_active = 0U;
    timing_publish();
}

void g431_runtime_timing_init(ElectricalAngleTrig trig, void *trig_context,
    G431RuntimeTimingStampRead stamp_read, void *stamp_context)
{
    const ControlRuntimeConfig config = {
        20000U, 2U, 10U, 100U, 100U, {3000.0f, 0.01f},
        {CURRENT_MEASUREMENT_PHASE_BC, 2U, 0.2f, 0.01f,
            true, {0.0f, 1.25f, -0.8f}},
        2U, 5.0f, false,
        {7U, 1, 0.125f, 0.01f, 1000.0f}, true,
        {true, true, true,
            {20.0f, 20.0f, 1000.0f, 1000.0f, 200.0f, 4.0f, 0.6f, 0.45f, 0.0002f},
            {0.006f, 0.04f, 20.0f, 0.4f, 0.4f, 200.0f, 0.002f},
            10U, 6.0f, 16.0f, 2.0f, 0.000125f},
        trig, trig_context
    };
    const CurrentMeasurementSample zero = {
        {0.0f, 0.02f, -0.03f}, CURRENT_MEASUREMENT_PHASE_BC
    };
    const SpeedControllerInput speed = {90.0f, 60.0f, 0.001f, true, true};

    memset(&s_timing_result, 0, sizeof(s_timing_result));
    s_timing_result.schema_version = 2U;
    s_timing_stamp_read = stamp_read;
    s_timing_stamp_context = stamp_context;
    s_timing_failure = G431_RUNTIME_TIMING_FAILURE_NONE;
    s_timing_fixture_ready = false;
    s_timing_in_step = false;
    s_timing_irq_open = false;
    s_timing_pending = false;
    s_timing_terminal_pending = false;
    s_timing_terminal_reason = G431_RUNTIME_TIMING_FAILURE_NONE;
    g_target_runtime_timing_active = 0U;
    g_target_runtime_timing_sequence = 0U;
    motor_init(&s_timing_motor);
    motor_finish_boot(&s_timing_motor);
    /* 仅启动时建立合成标定/历史；没有真实功率动作或现场标定声明。 */
    if (g431_runtime_timing_runtime_init(&s_timing_runtime, &s_timing_motor, &config)
        && current_measurement_start(&s_timing_runtime.current_measurement, true)
        && !current_measurement_calibrate(&s_timing_runtime.current_measurement, &zero, true)
        && current_measurement_calibrate(&s_timing_runtime.current_measurement, &zero, true)) {
        (void)position_estimator_update(&s_timing_runtime.position_estimator, 0.04f, 0.001f);
        if (position_estimator_update(&s_timing_runtime.position_estimator, 0.041f, 0.001f)
            && speed_controller_update(&s_timing_runtime.speed_controller, &speed,
                &s_timing_runtime.speed_output)) {
            s_timing_runtime.phase = CONTROL_RUNTIME_RUNNING;
            s_timing_runtime.position_time_available = true;
            s_timing_runtime.mechanical_position_turns = 0.041f;
            s_timing_runtime.bus_available = true;
            s_timing_runtime.bus.voltage_v = 12.0f;
            s_timing_runtime.bus.acquisition_valid = true;
            s_timing_motor.state = MOTOR_STATE_RUN;
            s_timing_motor.target_velocity_rpm = 90.0f;
            s_timing_initial_motor = s_timing_motor;
            s_timing_initial_runtime = s_timing_runtime;
            s_timing_initial_runtime.motor = &s_timing_initial_motor;
            s_timing_fixture_ready = true;
        }
    }
    timing_publish();
}

bool g431_runtime_timing_prepare(uint32_t request_word, bool permitted)
{
    uint32_t mode = request_word & 3U;

    if (g_target_runtime_timing_active != 0U
        || (request_word != 0U && request_word == s_timing_result.request_word)) {
        return false;
    }
    memset(&s_timing_result, 0, sizeof(s_timing_result));
    s_timing_result.schema_version = 2U;
    s_timing_result.request_word = request_word;
    s_timing_result.mode = mode;
    s_timing_result.expected_samples = G431_RUNTIME_TIMING_SAMPLES;
    s_timing_failure = G431_RUNTIME_TIMING_FAILURE_NONE;
    s_timing_irq_open = false;
    s_timing_pending = false;
    s_timing_terminal_pending = false;
    s_timing_terminal_reason = G431_RUNTIME_TIMING_FAILURE_NONE;
    s_timing_in_step = false;
    if (!permitted || (request_word >> 2U) == 0U
        || (mode != G431_RUNTIME_TIMING_STAGE && mode != G431_RUNTIME_TIMING_COMPUTE)
        || s_timing_stamp_read == NULL) {
        timing_freeze(G431_RUNTIME_TIMING_FAILURE_NOT_READY);
        return false;
    }
    if (!s_timing_fixture_ready) {
        timing_freeze(G431_RUNTIME_TIMING_FAILURE_CONTROL);
        return false;
    }
    s_timing_motor = s_timing_initial_motor;
    s_timing_runtime = s_timing_initial_runtime;
    s_timing_runtime.motor = &s_timing_motor;
    s_timing_position = 0.041f;
    s_timing_now = 0U;
    s_timing_result.state = G431_RUNTIME_TIMING_READY;
    timing_publish();
    __DMB();
    g_target_runtime_timing_active = 1U;
    return true;
}

PositionTopology g431_runtime_timing_position_topology(void)
{
    return POSITION_TOPOLOGY_PERIODIC;
}

bool g431_runtime_timing_position_latest(PositionObservation *observation)
{
    PositionObservation physical;

    if (!s_timing_in_step || observation == NULL) {
        return false;
    }
    s_timing_step_observation_reads++;
    /* 保留真实coherent getter开销，只替换其数据，不阻塞等待SSI。 */
    if (!position_observation_get_latest(&physical) || !physical.acquisition_valid
        || !physical.referenced) {
        timing_remember_failure(G431_RUNTIME_TIMING_FAILURE_NOT_READY);
        return false;
    }
    s_timing_position += 0.001f;
    observation->mechanical_position_turns = s_timing_position;
    observation->observation_time = s_timing_now;
    observation->referenced = true;
    observation->acquisition_valid = true;
    return true;
}

bool g431_runtime_timing_bus_latest(BusObservation *observation)
{
    BusObservation physical;

    if (!s_timing_in_step || observation == NULL) {
        return false;
    }
    if (!bus_observation_get_latest(&physical) || !physical.acquisition_valid) {
        timing_remember_failure(G431_RUNTIME_TIMING_FAILURE_NOT_READY);
        return false;
    }
    observation->voltage_v = 12.0f;
    observation->observation_time = s_timing_now;
    observation->acquisition_valid = true;
    return true;
}

bool g431_runtime_timing_sink(const PhaseDutyRequest *request)
{
    if (!s_timing_in_step || request == NULL || s_timing_step_sink_calls != 0U
        || s_timing_result.mode != G431_RUNTIME_TIMING_COMPUTE) {
        timing_remember_failure(G431_RUNTIME_TIMING_FAILURE_CONTROL);
        return false;
    }
    /* 有界比较同时拒绝NaN/Inf；只验证计算数值，不执行真实stage检查或提交。 */
    if (!(request->phase_a_duty >= REALTIME_MOTOR_IO_DUTY_MIN
            && request->phase_a_duty <= REALTIME_MOTOR_IO_DUTY_MAX)
        || !(request->phase_b_duty >= REALTIME_MOTOR_IO_DUTY_MIN
            && request->phase_b_duty <= REALTIME_MOTOR_IO_DUTY_MAX)
        || !(request->phase_c_duty >= REALTIME_MOTOR_IO_DUTY_MIN
            && request->phase_c_duty <= REALTIME_MOTOR_IO_DUTY_MAX)) {
        timing_remember_failure(G431_RUNTIME_TIMING_FAILURE_CONTROL);
        return false;
    }
    s_timing_step_sink_calls++;
    s_timing_result.sink_calls++;
    /* 只接收隔离Runtime算出的数值，使同源函数走完；绝无真实stage或硬件accepted。 */
    return true;
}

bool g431_runtime_timing_enable(void)
{
    s_timing_result.enable_attempts++;
    timing_remember_failure(G431_RUNTIME_TIMING_FAILURE_POWER);
    return false;
}

void g431_runtime_timing_safe_off(void)
{
    realtime_motor_io_safe_off();
}

bool g431_runtime_timing_step(ControlTime now, bool permitted)
{
    PhaseCurrentObservation current;
    PhaseDutyRequest duty;
    ControlTime previous_speed;
    bool accepted = false;

    if (g_target_runtime_timing_active == 0U) {
        return false;
    }
    if (!permitted) {
        timing_freeze(G431_RUNTIME_TIMING_FAILURE_NOT_READY);
        return true;
    }
    if (!s_timing_irq_open || s_timing_pending || s_timing_in_step) {
        timing_freeze(G431_RUNTIME_TIMING_FAILURE_CLOCK);
        return true;
    }
    if (s_timing_terminal_pending) {
        /* 第80拍Platform完整IRQ出口已执行；当拍许可先由上方检查。
         * 保留第80拍work/IRQ/stamp，收尾只做关断复核与结果发布。 */
        timing_freeze(s_timing_terminal_reason);
        return true;
    }
    s_timing_failure = G431_RUNTIME_TIMING_FAILURE_NONE;
    if (!timing_read_stamp(&s_timing_begin_stamp)) {
        timing_freeze((G431RuntimeTimingFailure)s_timing_failure);
        return true;
    }
    if ((s_timing_begin_stamp.cycles - s_timing_irq_begin) >= REALTIME_MOTOR_IO_CONTROL_PERIOD_CYCLES
        || (s_timing_begin_stamp.callback_start_cycles - s_timing_irq_begin) >= REALTIME_MOTOR_IO_CONTROL_PERIOD_CYCLES) {
        timing_freeze(G431_RUNTIME_TIMING_FAILURE_CLOCK);
        return true;
    }
    if (s_timing_result.state == G431_RUNTIME_TIMING_READY) {
        if (now < 10U) {
            timing_freeze(G431_RUNTIME_TIMING_FAILURE_NOT_READY);
            return true;
        }
        s_timing_runtime.last_position_time = now;
        s_timing_runtime.bus.observation_time = now;
        s_timing_runtime.last_speed_time = now - 2U;
        s_timing_result.state = G431_RUNTIME_TIMING_ACTIVE;
    }
    s_timing_now = now;
    s_timing_step_sink_calls = 0U;
    s_timing_step_observation_reads = 0U;
    s_timing_speed_updated = false;
    s_timing_limited = false;
    if (s_timing_result.mode == G431_RUNTIME_TIMING_STAGE) {
        if ((s_timing_begin_stamp.stage_deadline_cycles - s_timing_begin_stamp.cycles)
                >= REALTIME_MOTOR_IO_CONTROL_PERIOD_CYCLES) {
            timing_freeze(G431_RUNTIME_TIMING_FAILURE_CLOCK);
            return true;
        }
        duty.phase_a_duty = (s_timing_result.completed_samples & 1U) == 0U ? 0.25f : 0.75f;
        duty.phase_b_duty = 0.50f;
        duty.phase_c_duty = (s_timing_result.completed_samples & 1U) == 0U ? 0.75f : 0.25f;
        s_timing_result.real_stage_calls++;
        /* 两个直接DWT读之间只有单次真实HCC调用；许可/合成输入/MMIO stamp在外。 */
        s_timing_work_begin = DWT->CYCCNT;
        accepted = realtime_motor_io_stage_phase_duty(&duty);
        s_timing_work_end = DWT->CYCCNT;
        if (accepted) {
            s_timing_result.real_stage_accepted++;
        } else {
            timing_remember_failure(G431_RUNTIME_TIMING_FAILURE_STAGE);
        }
    } else {
        if (s_timing_result.completed_samples == G431_RUNTIME_TIMING_SAMPLES / 2U) {
            s_timing_motor.target_velocity_rpm = 200.0f;
        }
        current.phase_a_a = 0.0f;
        current.phase_b_a = 0.10f;
        current.phase_c_a = 0.12f;
        current.measured_phases = PHASE_MASK_B | PHASE_MASK_C;
        current.observation_time = now;
        current.acquisition_valid = true;
        previous_speed = s_timing_runtime.last_speed_time;
        s_timing_in_step = true;
        /* power_disabled=false仅为合成RUN输入，真实MOE由独立stamp证明为0。 */
        s_timing_work_begin = DWT->CYCCNT;
        (void)g431_runtime_timing_runtime_step(&s_timing_runtime, now, &current,
            false, 0U, NULL, false);
        s_timing_work_end = DWT->CYCCNT;
        s_timing_in_step = false;
        s_timing_speed_updated = previous_speed != s_timing_runtime.last_speed_time;
        s_timing_limited = s_timing_runtime.current_output.voltage_limited;
        if (s_timing_motor.faults != 0U || s_timing_motor.state != MOTOR_STATE_RUN
            || s_timing_runtime.phase != CONTROL_RUNTIME_RUNNING
            || !s_timing_runtime.current_output.valid || !s_timing_runtime.current_dq.valid
            || s_timing_step_sink_calls != 1U) {
            timing_remember_failure(G431_RUNTIME_TIMING_FAILURE_CONTROL);
        }
    }
    s_timing_result.last_work_cycles = s_timing_work_end - s_timing_work_begin;
    s_timing_result.last_anchor_cycles = s_timing_work_end - s_timing_begin_stamp.callback_start_cycles;
    s_timing_result.last_stage_budget_cycles = s_timing_begin_stamp.stage_deadline_cycles
        - s_timing_begin_stamp.callback_start_cycles;
    s_timing_result.last_irq_budget_cycles = s_timing_begin_stamp.stage_deadline_cycles
        - s_timing_irq_begin;
    /* 读端口失败不能把上一拍寄存器伪装成本拍证据；零值表示本拍未取得读数。 */
    memset(&s_timing_end_stamp, 0, sizeof(s_timing_end_stamp));
    (void)timing_read_stamp(&s_timing_end_stamp);
    s_timing_result.last_timer_count = s_timing_end_stamp.timer_count;
    s_timing_result.last_entry_count = s_timing_begin_stamp.callback_entry_count;
    s_timing_result.last_cr1 = s_timing_end_stamp.timer_cr1;
    s_timing_result.last_bdtr = s_timing_end_stamp.timer_bdtr;
    if (s_timing_end_stamp.callback_start_cycles != s_timing_begin_stamp.callback_start_cycles
        || s_timing_end_stamp.callback_entry_count != s_timing_begin_stamp.callback_entry_count
        || s_timing_end_stamp.stage_deadline_cycles != s_timing_begin_stamp.stage_deadline_cycles
        || s_timing_result.last_work_cycles == 0U
        || s_timing_result.last_anchor_cycles >= REALTIME_MOTOR_IO_CONTROL_PERIOD_CYCLES
        || (s_timing_work_begin - s_timing_begin_stamp.callback_start_cycles)
            > s_timing_result.last_anchor_cycles) {
        timing_remember_failure(G431_RUNTIME_TIMING_FAILURE_CLOCK);
    }
    s_timing_pending = true;
    /* 测量后出错立即实际关断；结果等本IRQ末标记后冻结，保留失败拍的总时长。 */
    if (s_timing_failure != G431_RUNTIME_TIMING_FAILURE_NONE) {
        realtime_motor_io_safe_off();
    }
    return true;
}

void g431_runtime_timing_cancel(G431RuntimeTimingFailure reason)
{
    if (g_target_runtime_timing_active != 0U) {
        timing_freeze(reason == G431_RUNTIME_TIMING_FAILURE_NONE
            ? G431_RUNTIME_TIMING_FAILURE_ABORTED : reason);
    }
}

void g431_runtime_timing_irq_enter(uint32_t cycles)
{
    if (g_target_runtime_timing_active == 0U) {
        return;
    }
    if (s_timing_irq_open) {
        timing_freeze(G431_RUNTIME_TIMING_FAILURE_CLOCK);
        return;
    }
    s_timing_irq_begin = cycles;
    s_timing_irq_open = true;
    s_timing_pending = false;
}

static void timing_add_metric(G431RuntimeTimingMetric *metric, uint32_t cycles)
{
    if (metric->count == 0U || cycles < metric->min_cycles) {
        metric->min_cycles = cycles;
    }
    if (cycles > metric->max_cycles) {
        metric->max_cycles = cycles;
    }
    metric->count++;
    metric->sum_cycles += cycles;
}

void g431_runtime_timing_irq_exit(uint32_t cycles, bool permitted)
{
    uint32_t work;
    uint32_t anchor;
    uint32_t irq;
    uint32_t front;
    uint32_t tail;

    if (g_target_runtime_timing_active == 0U) {
        return;
    }
    /* prepare在consumer内使active从0变1，此准备IRQ没有enter，不纳入测量。 */
    if (!s_timing_irq_open) {
        if (s_timing_result.state != G431_RUNTIME_TIMING_READY) {
            timing_freeze(G431_RUNTIME_TIMING_FAILURE_CLOCK);
        }
        return;
    }
    s_timing_irq_open = false;
    /* 尾部撤销许可也保留本拍原始IRQ区间，不能把上一成功拍的读数
     * 与本拍work/anchor拼成一个返回点；失败拍仍不计入成功统计。 */
    if (s_timing_pending) {
        s_timing_result.last_irq_cycles = cycles - s_timing_irq_begin;
    }
    if (!permitted) {
        timing_freeze(G431_RUNTIME_TIMING_FAILURE_NOT_READY);
        return;
    }
    if (!s_timing_pending) {
        return; /* regular-only ADC IRQ不充当一个控制采样。 */
    }
    work = s_timing_work_end - s_timing_work_begin;
    anchor = s_timing_work_end - s_timing_begin_stamp.callback_start_cycles;
    irq = cycles - s_timing_irq_begin;
    front = s_timing_work_begin - s_timing_begin_stamp.callback_start_cycles;
    tail = cycles - s_timing_work_end;
    s_timing_result.last_irq_cycles = irq;
    /* 使用Platform实际slot的绝对截止；中间谷底不重置预算，末标记后仍留异常返回预留。 */
    if (irq >= s_timing_result.last_irq_budget_cycles
        || s_timing_result.last_irq_budget_cycles >= REALTIME_MOTOR_IO_CONTROL_PERIOD_CYCLES) {
        timing_remember_failure(G431_RUNTIME_TIMING_FAILURE_IRQ_BUDGET);
    } else if (irq == 0U || work == 0U || anchor > irq || tail > irq
        || work > anchor || front > anchor || anchor != work + front
        || (s_timing_begin_stamp.callback_start_cycles - s_timing_irq_begin) > irq) {
        timing_remember_failure(G431_RUNTIME_TIMING_FAILURE_CLOCK);
    }
    if (s_timing_failure != G431_RUNTIME_TIMING_FAILURE_NONE) {
        timing_freeze((G431RuntimeTimingFailure)s_timing_failure);
        return;
    }
    timing_add_metric(&s_timing_result.rows[0], work);
    timing_add_metric(&s_timing_result.rows[1], anchor);
    timing_add_metric(&s_timing_result.rows[2], irq);
    timing_add_metric(&s_timing_result.rows[3], front);
    timing_add_metric(&s_timing_result.rows[4], tail);
    if (s_timing_result.mode == G431_RUNTIME_TIMING_COMPUTE) {
        if (s_timing_speed_updated) {
            s_timing_result.speed_updates++;
            timing_add_metric(&s_timing_result.rows[5], work);
        }
        if (s_timing_step_observation_reads != 0U) {
            s_timing_result.observation_refreshes++;
            timing_add_metric(&s_timing_result.rows[6], work);
        }
        if (s_timing_speed_updated || s_timing_step_observation_reads != 0U) {
            s_timing_result.slow_samples++;
            timing_add_metric(&s_timing_result.rows[8], work);
        } else {
            s_timing_result.fast_samples++;
            timing_add_metric(&s_timing_result.rows[7], work);
        }
        if (s_timing_limited) {
            s_timing_result.limited_samples++;
        }
    }
    if (anchor >= s_timing_result.last_stage_budget_cycles) {
        s_timing_result.over_stage_deadline_samples++;
    }
    /* 同一绝对截止由Platform再复核；此处统计不能代替其末端保护。 */
    if (anchor >= s_timing_result.last_stage_budget_cycles) {
        s_timing_result.over_consumer_budget_samples++;
    }
    s_timing_pending = false;
    s_timing_result.completed_samples++;
    if (s_timing_result.completed_samples == G431_RUNTIME_TIMING_SAMPLES) {
        bool complete = s_timing_result.enable_attempts == 0U;
        if (s_timing_result.mode == G431_RUNTIME_TIMING_STAGE) {
            complete = complete && s_timing_result.real_stage_calls == G431_RUNTIME_TIMING_SAMPLES
                && s_timing_result.real_stage_accepted == G431_RUNTIME_TIMING_SAMPLES
                && s_timing_result.sink_calls == 0U;
        } else {
            complete = complete && s_timing_result.real_stage_calls == 0U
                && s_timing_result.real_stage_accepted == 0U
                && s_timing_result.sink_calls == G431_RUNTIME_TIMING_SAMPLES
                && s_timing_result.speed_updates == 8U && s_timing_result.observation_refreshes == 8U
                && s_timing_result.fast_samples == 72U && s_timing_result.slow_samples == 8U
                && s_timing_result.limited_samples > 0U
                && s_timing_result.limited_samples < G431_RUNTIME_TIMING_SAMPLES;
        }
        /* 不在最慢控制拍追加stamp读取和256B发布；active持续到下一采样收尾。
         * 立即关断并锁存首因，取消/BUSY仍可按原语义终止待收尾窗口。 */
        realtime_motor_io_safe_off();
        s_timing_terminal_reason = complete ? G431_RUNTIME_TIMING_FAILURE_NONE
            : G431_RUNTIME_TIMING_FAILURE_INCOMPLETE;
        timing_remember_failure(s_timing_terminal_reason);
        s_timing_terminal_pending = true;
    }
}
