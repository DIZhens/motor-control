#include "hardware_trip.h"

#include <stddef.h>

#include "comp.h"
#include "tim.h"

#include "realtime_motor_io.h"

static HardwareTripHandler s_handler;
static void *s_handler_context;
static bool s_handler_bound;
static volatile bool s_armed;
static volatile HardwareTripCauseFlags s_latched_causes;

static HardwareTripCauseFlags pending_timer_causes(void)
{
    HardwareTripCauseFlags causes = HARDWARE_TRIP_CAUSE_NONE;

    if (__HAL_TIM_GET_FLAG(&htim1, TIM_FLAG_BREAK) != RESET) {
        causes |= HARDWARE_TRIP_CAUSE_BUS_OVERVOLTAGE;
    }

    if (__HAL_TIM_GET_FLAG(&htim1, TIM_FLAG_BREAK2) != RESET) {
        causes |= HARDWARE_TRIP_CAUSE_OVERCURRENT;
    }

    return causes;
}

static bool protection_condition_active(void)
{
    bool overcurrent_active;
    bool overvoltage_active;

    overcurrent_active =
        (HAL_COMP_GetOutputLevel(&hcomp1) == COMP_OUTPUT_LEVEL_HIGH)
        || (HAL_COMP_GetOutputLevel(&hcomp2) == COMP_OUTPUT_LEVEL_HIGH)
        || (HAL_COMP_GetOutputLevel(&hcomp4) == COMP_OUTPUT_LEVEL_HIGH);

    overvoltage_active =
        (HAL_COMP_GetOutputLevel(&hcomp3) == COMP_OUTPUT_LEVEL_HIGH);

    return overcurrent_active || overvoltage_active;
}

/*
 * TIM HAL 会在 Break callback 之前清 BIF / B2IF。
 * 因此 Platform 必须把尚未被 HAL 消费的硬件 flag 合并进自己的 sticky episode。
 */
static void latch_pending_timer_causes(void)
{
    s_latched_causes |= pending_timer_causes();
}

static void notify_urgent_rescan(void)
{
    if (s_handler_bound) {
        s_handler(s_handler_context);
    }
}

bool hardware_trip_bind(
    HardwareTripHandler handler,
    void *context)
{
    if ((handler == NULL) || s_handler_bound || s_armed) {
        return false;
    }

    s_handler = handler;
    s_handler_context = context;
    s_handler_bound = true;

    return true;
}

bool hardware_trip_arm(void)
{
    /*
     * G431 的 OCP / OVP DAC threshold 仍属于未冻结、未实机资格化的 Target 参数。
     * 在阈值明确之前不能启动 COMP protection path，否则无法声称该能力满足
     * Hardware Trip Contract。保持 fail-closed，避免把任意 DAC code 当成安全事实。
     */
    realtime_motor_io_safe_off();
    return false;
}

bool hardware_trip_get_snapshot(HardwareTripSnapshot *out_snapshot)
{
    uint32_t primask;

    if ((out_snapshot == NULL) || !s_armed) {
        return false;
    }

    primask = __get_PRIMASK();
    __disable_irq();

    latch_pending_timer_causes();
    out_snapshot->condition_active = protection_condition_active();
    out_snapshot->causes = s_latched_causes;

    if (primask == 0U) {
        __enable_irq();
    }

    return true;
}

bool hardware_trip_rearm(void)
{
    uint32_t primask;
    bool success;

    if (!s_armed) {
        return false;
    }

    realtime_motor_io_safe_off();

    primask = __get_PRIMASK();
    __disable_irq();

    latch_pending_timer_causes();
    if (protection_condition_active()) {
        success = false;
    } else {
        /*
         * 这里是旧 observable protection episode 的退休边界。
         * 清硬件 flag 后先清旧软件 latch；随后只要产生新的可观测 Trip，
         * BIF/B2IF 或 COMP 当前状态就会使本次 re-arm fail-closed。
         */
        __HAL_TIM_CLEAR_FLAG(&htim1, TIM_FLAG_BREAK | TIM_FLAG_BREAK2);
        s_latched_causes = HARDWARE_TRIP_CAUSE_NONE;
        __DSB();

        latch_pending_timer_causes();
        success = (s_latched_causes == HARDWARE_TRIP_CAUSE_NONE)
            && !protection_condition_active();
    }

    if (primask == 0U) {
        __enable_irq();
    }

    return success;
}

void HAL_TIMEx_BreakCallback(TIM_HandleTypeDef *htim)
{
    if ((htim != &htim1) || !s_armed) {
        return;
    }

    /*
     * HAL 在进入 callback 前已清 BIF；BRK1 的来源在当前 Target 中只有 COMP3。
     * hardware trip 已先于本 ISR 清除 MOE，这里再执行 software safe-off，
     * 使 Firmware 的 actuation epoch 同步失效。
     */
    realtime_motor_io_safe_off();
    s_latched_causes |= HARDWARE_TRIP_CAUSE_BUS_OVERVOLTAGE;
    notify_urgent_rescan();
}

void HAL_TIMEx_Break2Callback(TIM_HandleTypeDef *htim)
{
    if ((htim != &htim1) || !s_armed) {
        return;
    }

    /*
     * BRK2 只汇聚 COMP1 / COMP2 / COMP4，因此公共 cause 只记录 OVERCURRENT，
     * 不把具体 comparator / phase 泄漏到 Hardware Capability Contract。
     */
    realtime_motor_io_safe_off();
    s_latched_causes |= HARDWARE_TRIP_CAUSE_OVERCURRENT;
    notify_urgent_rescan();
}
