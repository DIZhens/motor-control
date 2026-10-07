#ifndef HARDWARE_TRIP_H
#define HARDWARE_TRIP_H

#include <stdbool.h>
#include <stdint.h>

typedef uint32_t HardwareTripCauseFlags;

enum
{
    HARDWARE_TRIP_CAUSE_NONE = 0U,
    HARDWARE_TRIP_CAUSE_UNKNOWN = (1UL << 0),
    HARDWARE_TRIP_CAUSE_OVERCURRENT = (1UL << 1),
    HARDWARE_TRIP_CAUSE_BUS_OVERVOLTAGE = (1UL << 2)
};

/*
 * Hardware Trip authoritative snapshot。
 *
 * condition_active 表示当前受该能力覆盖的物理保护条件是否仍 active。
 * causes 表示尚未被显式 re-arm 退休的 observable protection episode 的
 * sticky cause evidence。
 *
 * 允许 condition_active == false 且 causes != NONE：
 * 物理条件已经消失，但旧 protection episode 仍等待受控恢复。
 *
 * 本合同不承诺无损记录每一次物理 Trip；Target 无法区分的同类重复事件
 * 可以合并在同一个 observable protection episode 中。
 */
typedef struct
{
    bool condition_active;
    HardwareTripCauseFlags causes;
} HardwareTripSnapshot;

/*
 * urgent rescan notification，不是无损 fault event。
 *
 * callback 可以延迟、重复，甚至在旧 protection episode 已经 re-arm 后才到达。
 * consumer 收到通知后必须重新读取 authoritative snapshot，不能只凭 callback
 * 到达次数建立新的 Fault evidence。
 */
typedef void (*HardwareTripHandler)(void *context);

/*
 * 绑定唯一 urgent-rescan consumer。
 * 必须在 hardware_trip_arm() 之前完成；V1 不支持运行期动态 rebind。
 */
bool hardware_trip_bind(
    HardwareTripHandler handler,
    void *context);

/*
 * 准备 CPU-independent protection path 与软件 evidence/notification 路径，
 * 同时保持 power disabled。
 */
bool hardware_trip_arm(void);

/*
 * coherent、non-blocking、bounded-time 地读取 authoritative snapshot。
 * true 表示 out_snapshot 得到完整结果；false 时调用方不得读取输出对象。
 */
bool hardware_trip_get_snapshot(
    HardwareTripSnapshot *out_snapshot);

/*
 * 显式恢复当前 observable protection episode。
 *
 * true 必须同时保证：
 * - 当前 protection condition 允许恢复；
 * - 旧 observable protection episode 已受控退休；
 * - CPU-independent protection path 已重新 armed；
 * - power 仍保持 disabled；
 * - 已建立新的 observable protection episode boundary；
 * - 该 boundary 之后形成的新 observable episode 不会被本次 re-arm 清除。
 *
 * 如果 Target 无法证明这些后置条件，必须 fail-closed 并返回 false。
 * re-arm 不等价于 Motor Clear Fault、Power Enable 或 Restart。
 */
bool hardware_trip_rearm(void);

#endif /* HARDWARE_TRIP_H */
