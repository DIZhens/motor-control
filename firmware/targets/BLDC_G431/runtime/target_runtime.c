#include "target_runtime.h"

#include <float.h>
#include <stddef.h>
#include <string.h>

#include "main.h"
#include "tim.h"
#include "usart.h"
#include "motor.h"
#include "vofa.h"
#include "realtime_motor_io.h"
#include "hardware_trip.h"
#include "position_observation.h"
#include "bus_observation.h"

#define CYCLES_PER_US (170U)
#define SAMPLE_MIN_US (25U)
#define SAMPLE_MAX_US (75U)
#define OBSERVATION_DIVIDER (20U)
#define PUBLICATION_DIVIDER (200U)
#define BUS_MAX_AGE_TICKS (100U)
#define TELEMETRY_PERIOD_MS (10U)
#define RX_FRAGMENT_TIMEOUT_MS (100U)

typedef struct {
    VofaTelemetry telemetry;
    uint32_t incorporated_guard_faults;
    bool has_sample;
} RuntimePublication;

volatile uint32_t g_target_runtime_guard_faults;

/* Startup writes before enabling timing; afterwards only sample_ready owns Motor. */
static Motor s_motor;
static volatile RuntimePublication s_publication;
static volatile uint32_t s_publication_sequence;
static volatile MotorCommand s_pending_command;
static volatile bool s_command_pending;
static volatile bool s_stop_pending;
static volatile bool s_reject_pending;
static volatile uint32_t s_last_sample_cycles;
static uint32_t s_last_guard_cycles;
static uint32_t s_observation_divider;
static uint32_t s_publication_divider;
static bool s_attempted;
static bool s_has_sample;
static BusObservation s_bus;
static bool s_bus_available;

/* Only the foreground owns parsing and the immutable in-flight TX frame. */
static VofaParser s_parser;
static uint8_t s_tx_frame[VOFA_TELEMETRY_FRAME_SIZE];
static size_t s_tx_cursor;
static size_t s_tx_length;
static uint32_t s_last_telemetry_ms;
static uint32_t s_last_rx_ms;

static void restore_interrupts(uint32_t primask)
{
    if (primask == 0U) {
        __enable_irq();
    }
}

static bool finite_float(float value)
{
    return (value >= -FLT_MAX) && (value <= FLT_MAX);
}

static void publish(ControlTime now, bool has_sample)
{
    RuntimePublication next;

    memset(&next, 0, sizeof(next));
    next.telemetry.state = (uint32_t)s_motor.state;
    next.telemetry.faults = s_motor.faults;
    next.telemetry.target_velocity_rpm = s_motor.target_velocity_rpm;
    next.telemetry.command_result = (uint32_t)s_motor.command_result;
    next.telemetry.valid_flags = VOFA_VALID_STATE | VOFA_VALID_FAULTS
        | VOFA_VALID_TARGET_VELOCITY | VOFA_VALID_COMMAND_RESULT;
    if (has_sample) {
        next.telemetry.time_s = (float)now / 20000.0f;
        next.telemetry.valid_flags |= VOFA_VALID_TIME;
        if (s_bus_available && s_bus.acquisition_valid
            && (s_bus.observation_time <= now)
            && ((now - s_bus.observation_time) <= BUS_MAX_AGE_TICKS)
            && finite_float(s_bus.voltage_v) && (s_bus.voltage_v >= 0.0f)) {
            next.telemetry.bus_voltage_v = s_bus.voltage_v;
            next.telemetry.valid_flags |= VOFA_VALID_BUS_VOLTAGE;
        }
    }
    /* Velocity and d/q current are not implemented: their valid bits stay clear. */
    next.incorporated_guard_faults = s_motor.faults;
    next.has_sample = has_sample;
    s_publication_sequence++;
    __DMB();
    s_publication = next;
    __DMB();
    s_publication_sequence++;
}

/* Three bounded retries; background never masks ADC to copy a telemetry block. */
static bool read_publication(RuntimePublication *out)
{
    uint32_t attempt;

    for (attempt = 0U; attempt < 3U; attempt++) {
        uint32_t sequence = s_publication_sequence;

        if ((sequence & 1U) != 0U) {
            continue;
        }
        __DMB();
        *out = s_publication;
        __DMB();
        if (sequence == s_publication_sequence) {
            return true;
        }
    }
    return false;
}

static bool consume_command(void)
{
    MotorCommand command;

    if (s_stop_pending) {
        s_stop_pending = false;
        s_command_pending = false;
        s_reject_pending = false;
        command.type = MOTOR_COMMAND_STOP;
        command.target_velocity_rpm = 0.0f;
        realtime_motor_io_safe_off();
        (void)motor_handle_command(&s_motor, &command);
        return true;
    }
    if (s_command_pending) {
        __DMB();
        command = s_pending_command;
        s_command_pending = false;
        (void)motor_handle_command(&s_motor, &command);
        return true;
    }
    if (s_reject_pending) {
        s_reject_pending = false;
        (void)motor_handle_command(&s_motor, NULL);
        return true;
    }
    return false;
}

static void sample_ready(void *context, const PhaseCurrentObservation *current)
{
    HardwareTripSnapshot trip;
    uint32_t now_cycles = DWT->CYCCNT;
    uint32_t faults = g_target_runtime_guard_faults;
    uint32_t previous_faults = s_motor.faults;
    bool command_consumed;

    (void)context;
    if (s_has_sample && (((uint32_t)(now_cycles - s_last_sample_cycles)
        < SAMPLE_MIN_US * CYCLES_PER_US)
        || ((uint32_t)(now_cycles - s_last_sample_cycles)
        > SAMPLE_MAX_US * CYCLES_PER_US))) {
        faults |= TARGET_RUNTIME_FAULT_CADENCE;
    }
    s_last_sample_cycles = now_cycles;
    s_has_sample = true;
    if (!current->acquisition_valid
        || (current->measured_phases != (PHASE_MASK_B | PHASE_MASK_C))
        || !finite_float(current->phase_b_a) || !finite_float(current->phase_c_a)) {
        faults |= TARGET_RUNTIME_FAULT_CURRENT;
    }
    /* A notification itself is not fault evidence; rescan authoritative state. */
    if (!hardware_trip_get_snapshot(&trip)
        || trip.condition_active || (trip.causes != HARDWARE_TRIP_CAUSE_NONE)) {
        faults |= TARGET_RUNTIME_FAULT_TRIP;
    }
    if ((TIM1->BDTR & TIM_BDTR_MOE) != 0U) {
        faults |= TARGET_RUNTIME_FAULT_UNEXPECTED_POWER;
    }
    if (faults != 0U) {
        realtime_motor_io_safe_off();
        motor_latch_fault(&s_motor, faults);
    }
    command_consumed = consume_command();
    s_observation_divider++;
    if (s_observation_divider >= OBSERVATION_DIVIDER) {
        s_observation_divider = 0U;
        s_bus_available = bus_observation_get_latest(&s_bus);
    }
    s_publication_divider++;
    if (command_consumed || (previous_faults != s_motor.faults)
        || (s_publication_divider >= PUBLICATION_DIVIDER)) {
        s_publication_divider = 0U;
        publish(current->observation_time, true);
    }
}

static void trip_notify(void *context)
{
    (void)context;
    /* Platform has already cut power. The next sample reads its sticky snapshot. */
}

void target_runtime_guard_irq(void)
{
    uint32_t primask;
    uint32_t now_cycles;
    uint32_t last_sample_cycles;

    if (((TIM6->SR & TIM_SR_UIF) == 0U) || ((TIM6->DIER & TIM_DIER_UIE) == 0U)) {
        return;
    }
    __HAL_TIM_CLEAR_FLAG(&htim6, TIM_FLAG_UPDATE);
    /* ADC has higher priority: the pair must not straddle a newer sample. */
    primask = __get_PRIMASK();
    __disable_irq();
    last_sample_cycles = s_last_sample_cycles;
    now_cycles = DWT->CYCCNT;
    restore_interrupts(primask);
    if (now_cycles == s_last_guard_cycles) {
        g_target_runtime_guard_faults |= TARGET_RUNTIME_FAULT_CLOCK;
    }
    if ((uint32_t)(now_cycles - last_sample_cycles) > SAMPLE_MAX_US * CYCLES_PER_US) {
        g_target_runtime_guard_faults |= TARGET_RUNTIME_FAULT_CADENCE;
    }
    s_last_guard_cycles = now_cycles;
    if (g_target_runtime_guard_faults != 0U) {
        realtime_motor_io_safe_off();
    }
}

void target_runtime_start(void)
{
    RealtimeMotorIoFacts facts;
    HardwareTripSnapshot trip;
    uint32_t primask;
    bool success;

    if (s_attempted) {
        return;
    }
    s_attempted = true;
    motor_init(&s_motor);
    vofa_parser_init(&s_parser);
    realtime_motor_io_safe_off();
    if ((SystemCoreClock != 170000000U) || (TIM6->PSC != 169U)) {
        motor_latch_fault(&s_motor, TARGET_RUNTIME_FAULT_CLOCK);
        publish(0U, false);
        return;
    }
    if (!realtime_motor_io_get_facts(&facts)
        || (facts.control_time_ticks_per_second != 20000U)
        || (facts.fast_tick_period_ticks != 1U)
        || !realtime_motor_io_bind_sample_ready(sample_ready, NULL)
        || !hardware_trip_bind(trip_notify, NULL)
        || !position_observation_arm() || !bus_observation_arm()
        || !realtime_motor_io_arm() || !hardware_trip_arm()
        || !hardware_trip_get_snapshot(&trip)
        || trip.condition_active || (trip.causes != HARDWARE_TRIP_CAUSE_NONE)) {
        realtime_motor_io_safe_off();
        motor_latch_fault(&s_motor, TARGET_RUNTIME_FAULT_STARTUP);
        publish(0U, false);
        return;
    }

    /* Finish startup ownership before the first ADC callback can run. */
    primask = __get_PRIMASK();
    __disable_irq();
    motor_finish_boot(&s_motor);
    success = realtime_motor_io_start_drive_timing();
    if (!success) {
        realtime_motor_io_safe_off();
        motor_latch_fault(&s_motor, TARGET_RUNTIME_FAULT_STARTUP);
    }
    publish(0U, false);
    if (success) {
        s_last_sample_cycles = DWT->CYCCNT;
        s_last_guard_cycles = s_last_sample_cycles;
        TIM6->ARR = 49U;
        TIM6->CNT = 0U;
        TIM6->EGR = TIM_EGR_UG;
        __HAL_TIM_CLEAR_FLAG(&htim6, TIM_FLAG_UPDATE);
        SET_BIT(TIM6->DIER, TIM_DIER_UIE);
        SET_BIT(TIM6->CR1, TIM_CR1_CEN);
    }
    restore_interrupts(primask);
}

/* Only a complete, bounded command crosses from background to the fast owner. */
static void enqueue(const VofaCommand *request, bool rejected)
{
    uint32_t primask = __get_PRIMASK();

    __disable_irq();
    if (!rejected && (request->type == VOFA_COMMAND_STOP)) {
        s_command_pending = false;
        s_stop_pending = true;
    } else if (rejected || s_command_pending || s_stop_pending) {
        s_reject_pending = true;
    } else {
        switch (request->type) {
        case VOFA_COMMAND_START_VELOCITY:
            s_pending_command.type = MOTOR_COMMAND_START_VELOCITY;
            break;
        case VOFA_COMMAND_UPDATE_VELOCITY:
            s_pending_command.type = MOTOR_COMMAND_UPDATE_VELOCITY;
            break;
        case VOFA_COMMAND_CLEAR_FAULT:
            s_pending_command.type = MOTOR_COMMAND_CLEAR_FAULT;
            break;
        default:
            s_pending_command.type = (MotorCommandType)UINT32_MAX;
            break;
        }
        s_pending_command.target_velocity_rpm = request->target_velocity_rpm;
        __DMB();
        s_command_pending = true;
    }
    restore_interrupts(primask);
}

void target_runtime_service(void)
{
    uint32_t now_ms = HAL_GetTick();
    uint32_t uart_status = USART1->ISR;
    uint32_t errors = uart_status & (USART_ISR_PE | USART_ISR_FE | USART_ISR_NE | USART_ISR_ORE);

    if ((s_parser.length != 0U) && !s_parser.discarding
        && ((uint32_t)(now_ms - s_last_rx_ms) >= RX_FRAGMENT_TIMEOUT_MS)) {
        s_parser.length = 0U;
        s_parser.discarding = true;
    }
    if (errors != 0U) {
        USART1->ICR = errors; /* PE/FE/NE/ORE clear bits have the same positions. */
        s_parser.length = 0U;
        s_parser.discarding = true;
    }
    if ((uart_status & USART_ISR_RXNE_RXFNE) != 0U) {
        VofaCommand request;
        VofaParseResult result = vofa_parser_feed(&s_parser, (uint8_t)USART1->RDR, &request);

        s_last_rx_ms = now_ms;
        if (result == VOFA_PARSE_COMMAND) {
            enqueue(&request, false);
        } else if (result == VOFA_PARSE_REJECTED) {
            enqueue(NULL, true);
        }
    }
    if (((uint32_t)(now_ms - s_last_telemetry_ms) >= TELEMETRY_PERIOD_MS)
        && (s_tx_cursor == s_tx_length)) {
        RuntimePublication snapshot;

        s_last_telemetry_ms = now_ms;
        if (read_publication(&snapshot)) {
            uint32_t faults = g_target_runtime_guard_faults;
            uint32_t last_sample_cycles = s_last_sample_cycles;
            uint32_t now_cycles = DWT->CYCCNT;

            /* A stalled ADC leaves an old coherent snapshot. Do not report it healthy. */
            if (((faults & ~snapshot.incorporated_guard_faults) != 0U)
                || ((faults & TARGET_RUNTIME_FAULT_CLOCK) != 0U)
                || (snapshot.has_sample
                    && ((uint32_t)(now_cycles - last_sample_cycles)
                        > SAMPLE_MAX_US * CYCLES_PER_US))) {
                snapshot.telemetry.valid_flags = 0U;
            }
            s_tx_length = vofa_encode_telemetry(&snapshot.telemetry, s_tx_frame, sizeof(s_tx_frame));
            s_tx_cursor = 0U;
        }
    }
    if ((s_tx_cursor < s_tx_length) && ((USART1->ISR & USART_ISR_TXE_TXFNF) != 0U)) {
        USART1->TDR = s_tx_frame[s_tx_cursor++];
    }
}
