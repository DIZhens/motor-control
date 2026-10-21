#include "hardware_trip.h"

#include <stddef.h>

#include "comp.h"
#include "dac.h"
#include "tim.h"

#include "hardware_trip_config.h"
#include "realtime_motor_io.h"

#define DAC_FULL_SCALE_CODE (4095.0f)
#define DAC_VREF_NOMINAL_V (3.3f)

#define CURRENT_FRONTEND_BIAS_V (0.1014f)
#define CURRENT_FRONTEND_SHUNT_GAIN (0.8811f)
#define CURRENT_SHUNT_RESISTANCE_OHM (0.05f)

#define VBUS_DIVIDER_RATIO (5.1f / (47.0f + 5.1f))

static HardwareTripHandler s_handler;
static void *s_handler_context;
static bool s_handler_bound;
static volatile bool s_armed;
static volatile HardwareTripCauseFlags s_latched_causes;

static uint32_t voltage_to_dac_code(float voltage_v)
{
    float code = voltage_v * DAC_FULL_SCALE_CODE / DAC_VREF_NOMINAL_V;

    if (code <= 0.0f) {
        return 0U;
    }

    if (code >= DAC_FULL_SCALE_CODE) {
        return (uint32_t)DAC_FULL_SCALE_CODE;
    }

    return (uint32_t)(code + 0.5f);
}

static uint32_t ocp_dac_code(void)
{
    float shunt_voltage_v;
    float comparator_threshold_v;

    shunt_voltage_v =
        HARDWARE_TRIP_OCP_THRESHOLD_A * CURRENT_SHUNT_RESISTANCE_OHM;
    comparator_threshold_v = CURRENT_FRONTEND_BIAS_V
        + CURRENT_FRONTEND_SHUNT_GAIN * shunt_voltage_v;

    return voltage_to_dac_code(comparator_threshold_v);
}

static uint32_t ovp_dac_code(void)
{
    float comparator_threshold_v =
        HARDWARE_TRIP_OVP_THRESHOLD_V * VBUS_DIVIDER_RATIO;

    return voltage_to_dac_code(comparator_threshold_v);
}

__STATIC_FORCEINLINE HardwareTripCauseFlags pending_timer_causes(void)
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

__STATIC_FORCEINLINE HardwareTripCauseFlags current_condition_causes(void)
{
    HardwareTripCauseFlags causes = HARDWARE_TRIP_CAUSE_NONE;

    /* 与 HAL_COMP_GetOutputLevel 的 VALUE 位读取相同；热路径避免四次函数跳转。
     * 保留原短路顺序、每次 volatile 读取和 sticky evidence 的归属。 */
    if ((READ_BIT(hcomp1.Instance->CSR, COMP_CSR_VALUE) != 0U)
        || (READ_BIT(hcomp2.Instance->CSR, COMP_CSR_VALUE) != 0U)
        || (READ_BIT(hcomp4.Instance->CSR, COMP_CSR_VALUE) != 0U)) {
        causes |= HARDWARE_TRIP_CAUSE_OVERCURRENT;
    }

    if (READ_BIT(hcomp3.Instance->CSR, COMP_CSR_VALUE) != 0U) {
        causes |= HARDWARE_TRIP_CAUSE_BUS_OVERVOLTAGE;
    }

    return causes;
}

static void latch_authoritative_evidence(void)
{
    s_latched_causes |= pending_timer_causes();
    s_latched_causes |= current_condition_causes();
}

static void stop_protection_sources(void)
{
    (void)HAL_COMP_Stop(&hcomp4);
    (void)HAL_COMP_Stop(&hcomp3);
    (void)HAL_COMP_Stop(&hcomp2);
    (void)HAL_COMP_Stop(&hcomp1);

    (void)HAL_DAC_Stop(&hdac3, DAC_CHANNEL_2);
    (void)HAL_DAC_Stop(&hdac3, DAC_CHANNEL_1);
    (void)HAL_DAC_Stop(&hdac1, DAC_CHANNEL_1);
}

static bool configure_and_start_dac_thresholds(void)
{
    uint32_t ocp_code = ocp_dac_code();
    uint32_t ovp_code = ovp_dac_code();

    if (HAL_DAC_SetValue(
            &hdac3,
            DAC_CHANNEL_1,
            DAC_ALIGN_12B_R,
            ocp_code) != HAL_OK) {
        return false;
    }

    if (HAL_DAC_SetValue(
            &hdac3,
            DAC_CHANNEL_2,
            DAC_ALIGN_12B_R,
            ocp_code) != HAL_OK) {
        return false;
    }

    if (HAL_DAC_SetValue(
            &hdac1,
            DAC_CHANNEL_1,
            DAC_ALIGN_12B_R,
            ovp_code) != HAL_OK) {
        return false;
    }

    if (HAL_DAC_Start(&hdac3, DAC_CHANNEL_1) != HAL_OK) {
        return false;
    }

    if (HAL_DAC_Start(&hdac3, DAC_CHANNEL_2) != HAL_OK) {
        return false;
    }

    if (HAL_DAC_Start(&hdac1, DAC_CHANNEL_1) != HAL_OK) {
        return false;
    }

    return true;
}

static bool start_comparators(void)
{
    if (HAL_COMP_Start(&hcomp1) != HAL_OK) {
        return false;
    }

    if (HAL_COMP_Start(&hcomp2) != HAL_OK) {
        return false;
    }

    if (HAL_COMP_Start(&hcomp3) != HAL_OK) {
        return false;
    }

    if (HAL_COMP_Start(&hcomp4) != HAL_OK) {
        return false;
    }

    return true;
}

static void notify_urgent_rescan(void)
{
    if (s_handler_bound) {
        s_handler(s_handler_context);
    }
}

static void latch_trip_and_notify(HardwareTripCauseFlags cause)
{
    /*
     * active Break 的 BIF/B2IF 不能清除，继续开 BIE 会反复进入最高优先级 ISR。
     * 本 episode 只暂停软件通知；BKE/BK2E 和 comparator 仍持续硬件关断。
     * 期间的新 cause 由 timer flag / condition 保留，snapshot 继续收集 evidence。
     */
    __HAL_TIM_DISABLE_IT(&htim1, TIM_IT_BREAK);
    realtime_motor_io_safe_off();
    s_latched_causes |= cause;
    latch_authoritative_evidence();
    notify_urgent_rescan();
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
    if (!s_handler_bound || s_armed) {
        return false;
    }

    realtime_motor_io_safe_off();

    /*
     * Break hardware 本身不依赖 BIE 即可清 MOE。
     * Arm 期间先关闭软件 Break interrupt source，防止 comparator 逐个启动时
     * 产生半配置状态 callback；硬件 safe-off 始终有效。
     */
    __HAL_TIM_DISABLE_IT(&htim1, TIM_IT_BREAK);
    __HAL_TIM_CLEAR_FLAG(&htim1, TIM_FLAG_BREAK | TIM_FLAG_BREAK2);
    s_latched_causes = HARDWARE_TRIP_CAUSE_NONE;

    if (!configure_and_start_dac_thresholds()) {
        stop_protection_sources();
        return false;
    }

    if (!start_comparators()) {
        stop_protection_sources();
        __HAL_TIM_CLEAR_FLAG(&htim1, TIM_FLAG_BREAK | TIM_FLAG_BREAK2);
        return false;
    }

    /*
     * Comparator 全部 active 后才建立正式 episode boundary。
     * 若 arm 当下就存在保护条件，当前 condition / Break flag 会成为第一份 evidence。
     */
    s_armed = true;
    latch_authoritative_evidence();
    __HAL_TIM_ENABLE_IT(&htim1, TIM_IT_BREAK);

    return true;
}

bool hardware_trip_get_snapshot(HardwareTripSnapshot *out_snapshot)
{
    HardwareTripCauseFlags active_causes;
    uint32_t primask;

    if ((out_snapshot == NULL) || !s_armed) {
        return false;
    }

    primask = __get_PRIMASK();
    __disable_irq();

    active_causes = current_condition_causes();
    s_latched_causes |= pending_timer_causes();
    s_latched_causes |= active_causes;

    out_snapshot->condition_active =
        (active_causes != HARDWARE_TRIP_CAUSE_NONE);
    out_snapshot->causes = s_latched_causes;

    if (primask == 0U) {
        __enable_irq();
    }

    return true;
}

bool hardware_trip_rearm(void)
{
    HardwareTripCauseFlags active_causes;
    uint32_t primask;
    bool success;

    if (!s_armed) {
        return false;
    }

    realtime_motor_io_safe_off();

    primask = __get_PRIMASK();
    __disable_irq();

    __HAL_TIM_DISABLE_IT(&htim1, TIM_IT_BREAK);
    latch_authoritative_evidence();
    active_causes = current_condition_causes();

    if (active_causes != HARDWARE_TRIP_CAUSE_NONE) {
        success = false;
    } else {
        /*
         * 旧 episode 的 retirement boundary：
         * 只清 boundary 之前已经观察到的 timer flag 和 sticky cause。
         * boundary 之后出现的新 Break 会重新设置 BIF/B2IF，且不会被再次清除。
         */
        __HAL_TIM_CLEAR_FLAG(&htim1, TIM_FLAG_BREAK | TIM_FLAG_BREAK2);
        s_latched_causes = HARDWARE_TRIP_CAUSE_NONE;
        __DSB();

        latch_authoritative_evidence();
        success = (s_latched_causes == HARDWARE_TRIP_CAUSE_NONE)
            && (current_condition_causes() == HARDWARE_TRIP_CAUSE_NONE);
    }

    if (success) {
        /* 新 boundary 之后的 Break 保持 pending，恢复 BIE 后仍会通知。 */
        __HAL_TIM_ENABLE_IT(&htim1, TIM_IT_BREAK);
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
     * HAL 在 callback 前尝试清 BIF；active 条件下 flag 会保持。
     * 当前 BRK1 唯一外部来源为 COMP3。
     * 硬件 Break 已先于本 ISR 清 MOE，这里同步失效 Firmware actuation epoch。
     */
    latch_trip_and_notify(HARDWARE_TRIP_CAUSE_BUS_OVERVOLTAGE);
}

void HAL_TIMEx_Break2Callback(TIM_HandleTypeDef *htim)
{
    if ((htim != &htim1) || !s_armed) {
        return;
    }

    /*
     * BRK2 汇聚 COMP1 / COMP2 / COMP4。
     * 公共 cause 只记录 OVERCURRENT，不暴露具体 comparator / phase。
     */
    latch_trip_and_notify(HARDWARE_TRIP_CAUSE_OVERCURRENT);
}
