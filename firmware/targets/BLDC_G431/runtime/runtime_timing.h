#ifndef BLDC_G431_RUNTIME_TIMING_H
#define BLDC_G431_RUNTIME_TIMING_H

#include <stdbool.h>
#include <stdint.h>

#include "control_time.h"
#include "electrical_angle.h"

#define G431_RUNTIME_TIMING_SAMPLES (80U)
#define G431_RUNTIME_TIMING_ROWS (9U)

typedef enum {
    G431_RUNTIME_TIMING_STAGE = 1,
    G431_RUNTIME_TIMING_COMPUTE = 2
} G431RuntimeTimingMode;

typedef enum {
    G431_RUNTIME_TIMING_IDLE = 0,
    G431_RUNTIME_TIMING_READY = 1,
    G431_RUNTIME_TIMING_ACTIVE = 2,
    G431_RUNTIME_TIMING_COMPLETE = 3,
    G431_RUNTIME_TIMING_FAILED = 4
} G431RuntimeTimingState;

typedef enum {
    G431_RUNTIME_TIMING_FAILURE_NONE = 0,
    G431_RUNTIME_TIMING_FAILURE_NOT_READY = 1,
    G431_RUNTIME_TIMING_FAILURE_ABORTED = 2,
    G431_RUNTIME_TIMING_FAILURE_BUSY = 3,
    G431_RUNTIME_TIMING_FAILURE_CLOCK = 4,
    G431_RUNTIME_TIMING_FAILURE_CONTROL = 5,
    G431_RUNTIME_TIMING_FAILURE_STAGE = 6,
    G431_RUNTIME_TIMING_FAILURE_POWER = 7,
    G431_RUNTIME_TIMING_FAILURE_INCOMPLETE = 8,
    G431_RUNTIME_TIMING_FAILURE_IRQ_BUDGET = 9
} G431RuntimeTimingFailure;

/* 与原 Platform 同拍锚点一致；只读，不能移动安全计时起点。 */
typedef struct {
    uint32_t cycles;
    uint32_t timer_count;
    uint32_t timer_cr1;
    uint32_t timer_bdtr;
    uint32_t callback_start_cycles;
    uint32_t callback_entry_count;
} G431RuntimeTimingStamp;

typedef bool (*G431RuntimeTimingStampRead)(void *context, G431RuntimeTimingStamp *stamp);

typedef struct {
    uint32_t count;
    uint32_t min_cycles;
    uint32_t max_cycles;
    uint32_t sum_cycles;
} G431RuntimeTimingMetric;

/* 全部字段固定32位，布局248字节；发布序列为相同偶数才可读取。
 * request_word=(非零nonce<<2)|mode；同一完整身份不会自动重开。
 * 合成RUN、功率输入、标定值只属于隔离试具，不是现场Motor状态或许可。 */
typedef struct {
    uint32_t schema_version;
    uint32_t request_word;
    uint32_t mode;
    uint32_t state;
    uint32_t failure;
    uint32_t expected_samples;
    uint32_t completed_samples;
    uint32_t real_stage_calls;
    uint32_t real_stage_accepted;
    uint32_t sink_calls;
    uint32_t enable_attempts;
    uint32_t speed_updates;
    uint32_t observation_refreshes;
    uint32_t fast_samples;
    uint32_t slow_samples;
    uint32_t limited_samples;
    uint32_t last_shadow_faults;
    uint32_t last_work_cycles;
    uint32_t last_anchor_cycles;
    uint32_t last_irq_cycles;
    uint32_t last_timer_count;
    uint32_t last_entry_count;
    uint32_t last_cr1;
    uint32_t last_bdtr;
    uint32_t over_stage_deadline_samples;
    /* work返回点距原锚点已>=4250的拍数；不是末端consumer保护触发次数。 */
    uint32_t over_consumer_budget_samples;
    /* 0直接DWT包围单次HCC/Runtime调用；1原锚点至该调用返回；
     * 2 IRQ首末USER标记；3原锚点至调用入口；4调用返回至IRQ末标记；
     * 5速度拍/6观测刷新拍/7普通快拍/8慢拍的直接调用耗时。
     * 末标记后的统计、发布和异常入出栈不在行2内；这些行不是未插桩WCET。
     * stage模式只使用行0..4；compute模式真实HCC stage次数必须为0。 */
    G431RuntimeTimingMetric rows[G431_RUNTIME_TIMING_ROWS];
} G431RuntimeTimingResult;

extern volatile uint32_t g_target_runtime_timing_sequence;
extern volatile uint32_t g_target_runtime_timing_active;
extern volatile G431RuntimeTimingResult g_target_runtime_timing_result;

/* 时基启动前构建一次只读模板；不能stage、enable、safe_off。 */
void g431_runtime_timing_init(ElectricalAngleTrig trig, void *trig_context,
    G431RuntimeTimingStampRead stamp_read, void *stamp_context);
/* 以下运行期入口均属于ADC单写者。prepare拍仅复制模板，不计入80点。 */
bool g431_runtime_timing_prepare(uint32_t request_word, bool permitted);
bool g431_runtime_timing_step(ControlTime now, bool permitted);
void g431_runtime_timing_cancel(G431RuntimeTimingFailure reason);
/* 每个ADC IRQ首末USER区调用；仅包含已配对consumer的IRQ才提交一点。
 * regular-only IRQ不计数，缺失首/末标记不能发布COMPLETE。 */
void g431_runtime_timing_irq_enter(uint32_t cycles);
void g431_runtime_timing_irq_exit(uint32_t cycles, bool permitted);

#endif
