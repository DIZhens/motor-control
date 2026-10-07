#ifndef BLDC_G431_HARDWARE_TRIP_CONFIG_H
#define BLDC_G431_HARDWARE_TRIP_CONFIG_H

/*
 * G431 V1 首板 commissioning 的保护起始值，尚未完成实机资格化。
 *
 * 这些参数属于当前 Target Configuration，不属于 Hardware Capability Contract。
 * 后续只有实测确认后，才能把它们视为该 Target 的已验证保护阈值。
 */
#define HARDWARE_TRIP_OCP_THRESHOLD_A (4.0f)
#define HARDWARE_TRIP_OVP_THRESHOLD_V (17.0f)

#endif /* BLDC_G431_HARDWARE_TRIP_CONFIG_H */
