#ifndef BYTE_STREAM_TRANSPORT_H
#define BYTE_STREAM_TRANSPORT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* 字节流合同由Target在链接期提供；不含协议、Motor或调度器类型。 */
typedef enum {
    BYTE_STREAM_OK = 0, /* 初始化/读取成功；try_send表示整帧已复制接受，尚未完成TX。 */
    BYTE_STREAM_ACCEPTED = BYTE_STREAM_OK,
    BYTE_STREAM_EMPTY,
    BYTE_STREAM_BUSY,
    BYTE_STREAM_TOO_LARGE,
    BYTE_STREAM_NOT_STARTED,
    BYTE_STREAM_HARDWARE_ERROR,
    BYTE_STREAM_DISCONTINUITY,
    BYTE_STREAM_BAD_ARGUMENT
} ByteStreamResult;

enum {
    BYTE_STREAM_RX_AVAILABLE = 1U << 0,
    BYTE_STREAM_RX_DISCONTINUITY = 1U << 1,
    BYTE_STREAM_TX_COMPLETE = 1U << 2,
    BYTE_STREAM_TX_ERROR = 1U << 3
};

/* 通知可合并/重复/延迟，不传送字节或协议结果；sink在对应上下文只作有界通知。 */
typedef void (*ByteStreamEventSink)(void *context, uint32_t events, bool from_interrupt);

typedef struct {
    uint32_t rx_bytes, tx_bytes;
    uint32_t rx_discontinuities, buffer_overruns;
    uint32_t parity_errors, framing_errors, noise_errors, hardware_rx_overruns;
    uint32_t tx_busy_count, tx_errors;
    uint32_t produced_bytes, consumed_bytes, continuity_epoch;
    bool rx_started, tx_in_flight;
} ByteStreamStats;

/* 硬件已配置、调度器已启动且尚无使用者时初始化软件所有权；不启动RX或功率。 */
ByteStreamResult byte_stream_init(void);
/* RX启动前绑定一次；上下文寿命覆盖传输实例，活动期间不能替换sink。 */
ByteStreamResult byte_stream_bind_event_sink(ByteStreamEventSink sink, void *context);
/* 只能在任务句柄、sink和业务装配均就绪后调用；不在调度器前开放RX。 */
ByteStreamResult byte_stream_start_rx(void);
/*
 * 唯一后台所有者批量读取。OK返回完整副本及其连续性epoch，EMPTY返回0字节。
 * 覆盖/硬件错误使当前半帧失效；DISCONTINUITY返回0字节，调用者忽略目标内容。
 * 实现须在复制前后复核生产进度，不得把可能已被覆盖的副本当作连续字节交付。
 * produced-consumed的无符号差须有界；通知次数不充当权威生产位置。
 */
ByteStreamResult byte_stream_read(uint8_t *destination, size_t capacity,
    size_t *bytes_read, uint32_t *continuity_epoch);
/* 接受后完整复制，调用者可立刻复用输入；在途内部缓冲归传输所有，不排队第二帧。 */
ByteStreamResult byte_stream_try_send(const uint8_t *frame, size_t length);
/* 有界一致读取；false不改输出。计数自然u32回绕，不经本接口清零。 */
bool byte_stream_read_stats(ByteStreamStats *stats);

#endif
