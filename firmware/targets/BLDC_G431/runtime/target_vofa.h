#ifndef BLDC_G431_TARGET_VOFA_H
#define BLDC_G431_TARGET_VOFA_H

#include <stdint.h>

/* 既有 VOFA 兼容通道；由同一后台所有者初始化和周期调用。 */
void target_vofa_init(void);

/* 至多收发一个字节；now_ms 是后台超时基准，不参与控制时基。 */
void target_vofa_service(uint32_t now_ms);

#endif
