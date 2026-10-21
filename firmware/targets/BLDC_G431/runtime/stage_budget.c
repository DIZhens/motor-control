#include "stage_budget.h"

/* 原始锚点至 stage 的开销也包含本诊断分派；与正式热路径同样按速度优化。 */
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

/* 只对私有同源编译副本可见，不向 Motor/Control 公共合同增加诊断模式。 */
bool g431_stage_budget_runtime_init(ControlRuntime *runtime, Motor *motor,
    const ControlRuntimeConfig *config);
bool g431_stage_budget_runtime_step(ControlRuntime *runtime, ControlTime now,
    const PhaseCurrentObservation *current, bool power_disabled,
    uint32_t fault_evidence, const MotorCommand *command, bool has_command);

volatile uint32_t g_target_stage_budget_sequence;
volatile uint32_t g_target_stage_budget_active;
volatile G431StageBudgetResult g_target_stage_budget_result;

/* 初始化后仅 ADC consumer 修改；合成实例从未交给真实业务快照或通信层。 */
static Motor s_stage_motor;
static ControlRuntime s_stage_runtime;
/* 启动时建立一次，此后只读；新窗口不在 ADC 中重新校零、配参或计算初值。 */
static Motor s_stage_initial_motor;
static ControlRuntime s_stage_initial_runtime;
static bool s_stage_fixture_ready;
static G431StageBudgetResult s_stage_result;
static G431StageBudgetStampRead s_stamp_read;
static void *s_stamp_context;
static ElectricalAngleTrig s_trig;
static void *s_trig_context;
static G431StageBudgetStamp s_step_begin;
static G431StageBudgetStamp s_stage_end;
static ControlTime s_now;
static float s_position;
static uint32_t s_step_stage_calls;
static uint32_t s_step_observation_reads;
static uint32_t s_step_failure;
static bool s_in_step;
static bool s_have_stage_end;

static bool prepare_fixture(void);

static void publish(void)
{
    g_target_stage_budget_sequence++;
    __DMB();
    g_target_stage_budget_result = s_stage_result;
    __DMB();
    g_target_stage_budget_sequence++;
}

static void remember_failure(G431StageBudgetFailure reason)
{
    if (s_step_failure == G431_STAGE_BUDGET_FAILURE_NONE) {
        s_step_failure = (uint32_t)reason;
    }
}

static bool stamp_valid(const G431StageBudgetStamp *stamp)
{
    return (stamp->timer_bdtr & TIM_BDTR_MOE) == 0U
        && (stamp->timer_cr1 & (TIM_CR1_CEN | TIM_CR1_DIR | TIM_CR1_CMS
            | TIM_CR1_UDIS | TIM_CR1_OPM))
            == (TIM_CR1_CEN | TIM_CR1_DIR | TIM_CR1_CMS_0)
        && stamp->callback_entry_count > REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS
        && stamp->callback_entry_count <= REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS
        && stamp->timer_count > REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS
        && stamp->timer_count <= stamp->callback_entry_count
        && (stamp->cycles - stamp->callback_start_cycles)
            < (stamp->callback_entry_count - REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS);
}

static bool read_stamp(G431StageBudgetStamp *stamp)
{
    if (s_stamp_read == NULL || !s_stamp_read(s_stamp_context, stamp)) {
        remember_failure(G431_STAGE_BUDGET_FAILURE_CLOCK);
        return false;
    }
    if ((stamp->timer_bdtr & TIM_BDTR_MOE) != 0U) {
        remember_failure(G431_STAGE_BUDGET_FAILURE_POWER);
        return false;
    }
    if (!stamp_valid(stamp)) {
        remember_failure(G431_STAGE_BUDGET_FAILURE_CLOCK);
        return false;
    }
    return true;
}

static void freeze(G431StageBudgetFailure reason)
{
    G431StageBudgetStamp cleanup;

    realtime_motor_io_safe_off();
    /* 退出阶段不再要求同一 staging 半周，只保留真实 MOE 必须关闭的不变量。 */
    if (s_stamp_read == NULL || !s_stamp_read(s_stamp_context, &cleanup)) {
        if (reason == G431_STAGE_BUDGET_FAILURE_NONE) {
            reason = G431_STAGE_BUDGET_FAILURE_CLOCK;
        }
    } else if ((cleanup.timer_bdtr & TIM_BDTR_MOE) != 0U) {
        if (reason == G431_STAGE_BUDGET_FAILURE_NONE) {
            reason = G431_STAGE_BUDGET_FAILURE_POWER;
        }
    }
    s_stage_result.last_shadow_faults = s_stage_motor.faults;
    s_stage_result.failure = (uint32_t)reason;
    s_stage_result.state = reason == G431_STAGE_BUDGET_FAILURE_NONE
        ? G431_STAGE_BUDGET_COMPLETE : G431_STAGE_BUDGET_FAILED;
    s_in_step = false;
    g_target_stage_budget_active = 0U;
    publish();
}

void g431_stage_budget_init(ElectricalAngleTrig trig, void *trig_context,
    G431StageBudgetStampRead stamp_read, void *stamp_context)
{
    memset(&s_stage_result, 0, sizeof(s_stage_result));
    memset(&s_stage_motor, 0, sizeof(s_stage_motor));
    memset(&s_stage_runtime, 0, sizeof(s_stage_runtime));
    s_stage_result.schema_version = 1U;
    s_stamp_read = stamp_read;
    s_stamp_context = stamp_context;
    s_trig = trig;
    s_trig_context = trig_context;
    s_in_step = false;
    s_have_stage_end = false;
    g_target_stage_budget_active = 0U;
    g_target_stage_budget_sequence = 0U;
    /* Drive Timing 尚未启动：只读 HCC facts，不 stage、enable 或 safe_off。 */
    s_stage_fixture_ready = prepare_fixture();
    if (s_stage_fixture_ready) {
        s_stage_initial_motor = s_stage_motor;
        s_stage_initial_runtime = s_stage_runtime;
        s_stage_initial_runtime.motor = &s_stage_initial_motor;
    }
    publish();
}

static bool prepare_fixture(void)
{
    /* 所有数值与 confirmed 标志只描述合成试具，不是 2804 电机标定或启动许可。 */
    const ControlRuntimeConfig config = {
        20000U, 1U, 20U, 100U, 100U, {3000.0f, 0.01f},
        {CURRENT_MEASUREMENT_PHASE_BC, 2U, 0.2f, 0.01f,
            true, {0.0f, 1.25f, -0.8f}},
        2U, 5.0f, false,
        {7U, 1, 0.125f, 0.01f, 1000.0f}, true,
        {true, true, true,
            {20.0f, 20.0f, 1000.0f, 1000.0f, 200.0f, 4.0f, 0.6f, 0.45f, 0.0001f},
            {0.006f, 0.04f, 20.0f, 0.4f, 0.4f, 200.0f, 0.002f},
            10U, 6.0f, 16.0f, 2.0f, 0.00005f},
        s_trig, s_trig_context
    };
    const CurrentMeasurementSample zero = {
        {0.0f, 0.02f, -0.03f}, CURRENT_MEASUREMENT_PHASE_BC
    };
    const SpeedControllerInput initial_speed = {90.0f, 60.0f, 0.0005f, true, true};

    motor_init(&s_stage_motor);
    motor_finish_boot(&s_stage_motor);
    if (!g431_stage_budget_runtime_init(&s_stage_runtime, &s_stage_motor, &config)
        || !current_measurement_start(&s_stage_runtime.current_measurement, true)
        || current_measurement_calibrate(&s_stage_runtime.current_measurement, &zero, true)
        || !current_measurement_calibrate(&s_stage_runtime.current_measurement, &zero, true)) {
        return false;
    }
    (void)position_estimator_update(&s_stage_runtime.position_estimator, 0.04f, 0.001f);
    if (!position_estimator_update(&s_stage_runtime.position_estimator, 0.041f, 0.001f)
        || !speed_controller_update(&s_stage_runtime.speed_controller, &initial_speed,
            &s_stage_runtime.speed_output)) {
        return false;
    }
    /* 直接建立隔离模拟 RUN，绕过真实 Start/enable；此状态不进入业务 publication。 */
    s_stage_runtime.phase = CONTROL_RUNTIME_RUNNING;
    s_stage_runtime.position_time_available = true;
    s_stage_runtime.mechanical_position_turns = 0.041f;
    s_stage_runtime.bus_available = true;
    s_stage_runtime.bus.voltage_v = 12.0f;
    s_stage_runtime.bus.acquisition_valid = true;
    s_stage_motor.state = MOTOR_STATE_RUN;
    s_stage_motor.target_velocity_rpm = 90.0f;
    s_position = 0.041f;
    return true;
}

bool g431_stage_budget_prepare(uint32_t request, bool permitted)
{
    if (g_target_stage_budget_active != 0U
        || (request != 0U && request == s_stage_result.request_sequence)) {
        return false;
    }
    memset(&s_stage_result, 0, sizeof(s_stage_result));
    s_stage_result.schema_version = 1U;
    s_stage_result.request_sequence = request;
    s_step_failure = G431_STAGE_BUDGET_FAILURE_NONE;
    if (!permitted || request == 0U || s_stamp_read == NULL) {
        freeze(G431_STAGE_BUDGET_FAILURE_NOT_READY);
        return false;
    }
    if (!s_stage_fixture_ready) {
        freeze(G431_STAGE_BUDGET_FAILURE_CONTROL);
        return false;
    }
    /* ADC 准备拍只复制固定模板；每个显式窗口均获得独立的原始积分与历史。 */
    s_stage_motor = s_stage_initial_motor;
    s_stage_runtime = s_stage_initial_runtime;
    s_stage_runtime.motor = &s_stage_motor;
    s_position = 0.041f;
    s_now = 0U;
    s_step_stage_calls = 0U;
    s_step_observation_reads = 0U;
    s_in_step = false;
    s_have_stage_end = false;
    s_stage_result.state = G431_STAGE_BUDGET_READY;
    publish();
    __DMB();
    g_target_stage_budget_active = 1U;
    return true;
}

PositionTopology g431_stage_budget_position_topology(void)
{
    return POSITION_TOPOLOGY_PERIODIC;
}

bool g431_stage_budget_position_latest(PositionObservation *observation)
{
    PositionObservation physical;

    if (!s_in_step || observation == NULL) {
        return false;
    }
    s_step_observation_reads++;
    /* 真正执行 HCC coherent latest 的读取成本；只替换返回数值，不阻塞等待 SSI。 */
    if (!position_observation_get_latest(&physical) || !physical.acquisition_valid
        || !physical.referenced) {
        remember_failure(G431_STAGE_BUDGET_FAILURE_NOT_READY);
        return false;
    }
    s_position += 0.001f;
    observation->mechanical_position_turns = s_position;
    observation->observation_time = s_now;
    observation->referenced = true;
    observation->acquisition_valid = true;
    return true;
}

bool g431_stage_budget_bus_latest(BusObservation *observation)
{
    BusObservation physical;

    if (!s_in_step || observation == NULL) {
        return false;
    }
    if (!bus_observation_get_latest(&physical) || !physical.acquisition_valid) {
        remember_failure(G431_STAGE_BUDGET_FAILURE_NOT_READY);
        return false;
    }
    observation->voltage_v = 12.0f;
    observation->observation_time = s_now;
    observation->acquisition_valid = true;
    return true;
}

bool g431_stage_budget_stage(const PhaseDutyRequest *request)
{
    bool accepted;
    G431StageBudgetStamp before;

    if (!s_in_step || s_step_stage_calls != 0U
        || s_step_failure != G431_STAGE_BUDGET_FAILURE_NONE) {
        remember_failure(G431_STAGE_BUDGET_FAILURE_STAGE);
        return false;
    }
    if (s_stamp_read == NULL || !s_stamp_read(s_stamp_context, &before)) {
        remember_failure(G431_STAGE_BUDGET_FAILURE_CLOCK);
        return false;
    }
    if ((before.timer_bdtr & TIM_BDTR_MOE) != 0U) {
        remember_failure(G431_STAGE_BUDGET_FAILURE_POWER);
        return false;
    }
    if (before.callback_start_cycles != s_step_begin.callback_start_cycles
        || before.callback_entry_count != s_step_begin.callback_entry_count) {
        remember_failure(G431_STAGE_BUDGET_FAILURE_CLOCK);
        return false;
    }
    /* 算法已耗尽窗口时仍调用真实 HCC，让原截止门控决定拒绝并保留返回点证据。 */
    s_step_stage_calls++;
    s_stage_result.stage_calls++;
    accepted = realtime_motor_io_stage_phase_duty(request);
    if (accepted) {
        s_stage_result.stage_accepted++;
    } else {
        remember_failure(G431_STAGE_BUDGET_FAILURE_STAGE);
    }
    if (s_stamp_read == NULL || !s_stamp_read(s_stamp_context, &s_stage_end)) {
        remember_failure(G431_STAGE_BUDGET_FAILURE_CLOCK);
        return false;
    }
    s_stage_result.last_elapsed_cycles = s_stage_end.cycles - s_stage_end.callback_start_cycles;
    s_stage_result.last_step_cycles = s_stage_end.cycles - s_step_begin.cycles;
    s_stage_result.last_timer_count = s_stage_end.timer_count;
    s_stage_result.last_entry_count = s_stage_end.callback_entry_count;
    s_stage_result.last_cr1 = s_stage_end.timer_cr1;
    s_stage_result.last_bdtr = s_stage_end.timer_bdtr;
    if ((s_stage_end.timer_bdtr & TIM_BDTR_MOE) != 0U) {
        remember_failure(G431_STAGE_BUDGET_FAILURE_POWER);
        return false;
    }
    if (!stamp_valid(&s_stage_end)) {
        remember_failure(G431_STAGE_BUDGET_FAILURE_CLOCK);
        return false;
    }
    s_have_stage_end = true;
    if (s_stage_end.callback_start_cycles != s_step_begin.callback_start_cycles
        || s_stage_end.callback_entry_count != s_step_begin.callback_entry_count
        || (s_stage_end.cycles - s_step_begin.cycles) == 0U
        || (s_stage_end.cycles - s_step_begin.cycles) >= REALTIME_MOTOR_IO_PWM_PERIOD_COUNTS) {
        remember_failure(G431_STAGE_BUDGET_FAILURE_CLOCK);
        return false;
    }
    return accepted;
}

bool g431_stage_budget_enable(void)
{
    s_stage_result.enable_attempts++;
    remember_failure(G431_STAGE_BUDGET_FAILURE_POWER);
    return false;
}

void g431_stage_budget_safe_off(void)
{
    realtime_motor_io_safe_off();
}

static void add_timing(G431StageBudgetTiming *timing, uint32_t cycles)
{
    if (timing->count == 0U || cycles < timing->min_cycles) {
        timing->min_cycles = cycles;
    }
    if (cycles > timing->max_cycles) {
        timing->max_cycles = cycles;
    }
    timing->count++;
    timing->sum_cycles += cycles;
}

bool g431_stage_budget_step(ControlTime now, bool permitted)
{
    PhaseCurrentObservation current;
    ControlTime previous_speed_time;
    uint32_t callback_cycles;
    uint32_t remaining;
    bool speed_updated;

    if (g_target_stage_budget_active == 0U) {
        return false;
    }
    if (!permitted) {
        freeze(G431_STAGE_BUDGET_FAILURE_NOT_READY);
        return true;
    }
    s_step_failure = G431_STAGE_BUDGET_FAILURE_NONE;
    if (!read_stamp(&s_step_begin)) {
        freeze((G431StageBudgetFailure)s_step_failure);
        return true;
    }
    if (s_stage_result.state == G431_STAGE_BUDGET_READY) {
        if (now < 10U) {
            freeze(G431_STAGE_BUDGET_FAILURE_NOT_READY);
            return true;
        }
        /* 首拍只把已建立的合成历史绑定到当前时域，不读取或改写真实 Runtime。 */
        s_stage_runtime.last_position_time = now;
        s_stage_runtime.bus.observation_time = now;
        s_stage_runtime.last_speed_time = now - 1U;
        s_stage_result.state = G431_STAGE_BUDGET_ACTIVE;
    }
    s_now = now;
    /* 前后半窗使用不同合成目标，覆盖未限幅与电压圆限幅；不是运行期业务命令。 */
    if (s_stage_result.completed_samples == G431_STAGE_BUDGET_SAMPLES / 2U) {
        s_stage_motor.target_velocity_rpm = 200.0f;
    }
    s_step_stage_calls = 0U;
    s_step_observation_reads = 0U;
    s_have_stage_end = false;
    current.phase_a_a = 0.0f;
    current.phase_b_a = 0.10f;
    current.phase_c_a = 0.12f;
    current.measured_phases = PHASE_MASK_B | PHASE_MASK_C;
    current.observation_time = now;
    current.acquisition_valid = true;
    previous_speed_time = s_stage_runtime.last_speed_time;
    s_in_step = true;
    /* false 是模拟功率输入，真实 MOE 已由 stamp 读取单独证明为零。 */
    (void)g431_stage_budget_runtime_step(&s_stage_runtime, now, &current, false, 0U, NULL, false);
    s_in_step = false;
    speed_updated = s_stage_runtime.last_speed_time != previous_speed_time;
    if (s_step_failure != G431_STAGE_BUDGET_FAILURE_NONE) {
        freeze((G431StageBudgetFailure)s_step_failure);
        return true;
    }
    if (s_stage_motor.faults != 0U || s_stage_motor.state != MOTOR_STATE_RUN
        || s_stage_runtime.phase != CONTROL_RUNTIME_RUNNING || !s_stage_runtime.current_output.valid
        || !s_stage_runtime.current_dq.valid || !s_have_stage_end || s_step_stage_calls != 1U) {
        freeze(G431_STAGE_BUDGET_FAILURE_CONTROL);
        return true;
    }
    callback_cycles = s_stage_end.cycles - s_stage_end.callback_start_cycles;
    remaining = s_stage_end.timer_count - REALTIME_MOTOR_IO_STAGE_DEADLINE_COUNTS;
    add_timing(&s_stage_result.rows[0], callback_cycles);
    add_timing(&s_stage_result.rows[1], s_stage_end.cycles - s_step_begin.cycles);
    if (speed_updated) {
        s_stage_result.speed_updates++;
        add_timing(&s_stage_result.rows[2], callback_cycles);
    }
    if (s_step_observation_reads != 0U) {
        s_stage_result.observation_refreshes++;
        add_timing(&s_stage_result.rows[3], callback_cycles);
    }
    if (speed_updated || s_step_observation_reads != 0U) {
        s_stage_result.slow_samples++;
        add_timing(&s_stage_result.rows[5], callback_cycles);
    } else {
        s_stage_result.fast_samples++;
        add_timing(&s_stage_result.rows[4], callback_cycles);
    }
    if (s_stage_runtime.current_output.voltage_limited) {
        s_stage_result.limited_samples++;
    }
    if (s_stage_result.completed_samples == 0U || remaining < s_stage_result.min_remaining_counts) {
        s_stage_result.min_remaining_counts = remaining;
    }
    s_stage_result.completed_samples++;
    if (s_stage_result.completed_samples == G431_STAGE_BUDGET_SAMPLES) {
        if (s_stage_result.stage_calls != G431_STAGE_BUDGET_SAMPLES
            || s_stage_result.stage_accepted != G431_STAGE_BUDGET_SAMPLES
            || s_stage_result.enable_attempts != 0U || s_stage_result.speed_updates != 8U
            || s_stage_result.observation_refreshes != 4U || s_stage_result.fast_samples != 72U
            || s_stage_result.slow_samples != 8U || s_stage_result.limited_samples == 0U
            || s_stage_result.limited_samples == G431_STAGE_BUDGET_SAMPLES) {
            freeze(G431_STAGE_BUDGET_FAILURE_INCOMPLETE);
        } else {
            freeze(G431_STAGE_BUDGET_FAILURE_NONE);
        }
    }
    return true;
}

void g431_stage_budget_cancel(G431StageBudgetFailure reason)
{
    if (g_target_stage_budget_active != 0U) {
        freeze(reason == G431_STAGE_BUDGET_FAILURE_NONE
            ? G431_STAGE_BUDGET_FAILURE_ABORTED : reason);
    }
}
