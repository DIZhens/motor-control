#ifndef VOFA_H
#define VOFA_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* 命令内容不含终止换行；超长后一直丢弃到下一个换行。 */
#define VOFA_COMMAND_MAX_LENGTH 47U
#define VOFA_TELEMETRY_CHANNEL_COUNT 10U
#define VOFA_TELEMETRY_FRAME_SIZE 44U

/* 这里只表达完整请求；业务门控、范围和功率状态由 Motor 决定。 */
typedef enum {
    VOFA_COMMAND_START_VELOCITY = 0,
    VOFA_COMMAND_UPDATE_VELOCITY,
    VOFA_COMMAND_STOP,
    VOFA_COMMAND_CLEAR_FAULT
} VofaCommandType;

/* 从一行输入中解析得到的完整请求，交由业务层校验与执行。 */
typedef struct {
    VofaCommandType type; /* Start / Update 都明确请求速度模式。 */
    float target_velocity_rpm; /* 机械转速 rpm；Stop / Clear 时为 0。 */
} VofaCommand;

/* 只有完整换行帧返回 COMMAND 或 REJECTED，碎片返回 NONE。 */
typedef enum {
    VOFA_PARSE_NONE = 0,
    VOFA_PARSE_COMMAND,
    VOFA_PARSE_REJECTED
} VofaParseResult;

/* 每条独立字节流独占一个解析器；不保存输入缓冲或 Command 的外部指针。 */
typedef struct {
    char line[VOFA_COMMAND_MAX_LENGTH + 1U];
    size_t length; /* 已缓存内容字节数，不含换行。 */
    bool discarding; /* 超长或非法字节后丢弃当前行。 */
} VofaParser;

/* 有效位随编码校验收紧；未实现、过期或未知的数据不得置位。 */
typedef enum {
    VOFA_VALID_TIME = 1U << 0,
    VOFA_VALID_TARGET_VELOCITY = 1U << 1,
    VOFA_VALID_VELOCITY = 1U << 2,
    VOFA_VALID_ID = 1U << 3,
    VOFA_VALID_IQ = 1U << 4,
    VOFA_VALID_BUS_VOLTAGE = 1U << 5,
    VOFA_VALID_STATE = 1U << 6,
    VOFA_VALID_FAULTS = 1U << 7,
    VOFA_VALID_COMMAND_RESULT = 1U << 8
} VofaTelemetryValidity;

/* 只接受调用方已经取得的一致性快照，编码器不读取 Motor 私有状态。 */
typedef struct {
    float time_s; /* 单调控制时间，s。 */
    float target_velocity_rpm; /* 当前业务接受的机械目标，rpm。 */
    float velocity_rpm; /* 有效机械转速估计，rpm。 */
    float id_a; /* 有效 d 轴电流，A。 */
    float iq_a; /* 有效 q 轴电流，A。 */
    float bus_voltage_v; /* 有效物理母线电压，V。 */
    uint32_t state; /* Motor 生命周期值。 */
    uint32_t faults; /* Motor 故障位图。 */
    uint32_t valid_flags; /* VofaTelemetryValidity 的按位或。 */
    uint32_t command_result; /* 最近一次请求的 Motor 处理结果。 */
} VofaTelemetry;

void vofa_parser_init(VofaParser *parser);

/* 每次消费恰好一个字节；仅 COMMAND 时写 command，调用方逐帧交给 Motor。 */
VofaParseResult vofa_parser_feed(VofaParser *parser, uint8_t byte, VofaCommand *command);

/* 成功写入完整 44 字节并返回 44；空指针或空间不足返回 0 且不写 frame。 */
size_t vofa_encode_telemetry(const VofaTelemetry *telemetry, uint8_t *frame, size_t capacity);

#endif
