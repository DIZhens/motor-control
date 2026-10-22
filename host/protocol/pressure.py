"""功率关闭时的遥测/RPC压力检查；不包含Start、Update或故障恢复。"""
import argparse
import json
import math

from . import codec
from .client import Client, JsonlLogger, json_value


def checked(response):
    if response['result_code'] != codec.Result.OK:
        raise RuntimeError(response)
    return response


def pressure(client, rate, seconds, request_hz=1.0):
    """先关闭遥测再建新Session，给本工作点独立序号域；统计覆盖包含首帧前缺口。"""
    checked(client.connect())
    snapshot = client.last_recovery['motor_snapshot']['value']
    if (snapshot['valid_flags'] & 0xC0) != 0xC0 or snapshot['motor_state'] != 1 or snapshot['fault_flags']:
        raise RuntimeError('只允许有效、无故障的IDLE；还需操作者确认功率关闭')
    checked(client.configure_telemetry(False, rate))
    checked(client.connect())
    baseline = checked(client.read_object(codec.COMMUNICATION_STATUS))['value']
    parser_before = dict(client.parser.stats)
    retries_before, async_before = client.retransmissions, client.async_errors
    configured = checked(client.configure_telemetry(True, rate))['value']
    domain = (client.session, configured['generation'])
    start = client.clock()
    deadline, next_request = start + seconds, start + 1 / request_hz
    requests, latencies = 0, []
    while client.clock() < deadline:
        if client.clock() >= next_request:
            before = client.clock()
            checked(client.read_object(codec.COMMUNICATION_STATUS))
            latencies.append(client.clock() - before)
            requests += 1
            next_request += 1 / request_hz
        else:
            client.poll()
    elapsed = client.clock() - start
    checked(client.configure_telemetry(False, rate))
    # 排空USB/Host已在途帧，不把配置切换后的样本并入原域。
    drain = client.clock() + 0.100
    while client.clock() < drain:
        client.poll()
    final = checked(client.read_object(codec.COMMUNICATION_STATUS))['value']
    delta = {key: (final[key] - baseline[key]) & 0xFFFFFFFF for key in codec.STATUS_FIELDS
             if key not in ('session_id', 'communication_stack_min_words', 'telemetry_stack_min_words',
                            'internal_fault_reason', 'response_fifo_high_water',
                            'completion_fifo_high_water', 'event_fifo_high_water')}
    stats = client.telemetry_domains.get(domain, dict(received=0, gaps=0, order_errors=0, first=0, last=0))
    missing = stats['gaps'] + max(0, stats['first'] - 1)
    loss = missing / stats['last'] if stats['last'] else 1.0
    parser_delta = {key: value - parser_before[key] for key, value in client.parser.stats.items()}
    errors = ['rx_discontinuities', 'rx_dma_overruns', 'uart_pe', 'uart_fe', 'uart_ne', 'uart_ore',
              'cobs_errors', 'crc_errors', 'length_errors', 'version_errors', 'message_errors',
              'parser_timeouts', 'internal_fault_count', 'tx_errors']
    passed = (all(delta[key] == 0 for key in errors) and not any(parser_delta.values())
              and final['internal_fault_reason'] == 0 and client.async_errors == async_before
              and client.retransmissions == retries_before and stats['received'] > 0
              and stats['order_errors'] == 0 and missing == delta['telemetry_overwrites']
              and final['communication_stack_min_words'] >= 192 and final['telemetry_stack_min_words'] >= 96
              and (loss < .01 if rate == 500 else missing == 0))
    return dict(status='PASS' if passed else 'FAIL', motion_commands=0, rate_hz=rate,
                requested_seconds=seconds, elapsed_seconds=elapsed, request_hz=request_hz,
                periodic_status_requests=requests, response_max_ms=max(latencies, default=0) * 1000,
                retransmissions=client.retransmissions-retries_before, domain=domain, telemetry=stats,
                missing_including_prefix=missing, loss_fraction=loss, counters_delta=delta,
                host_parser_delta=parser_delta, async_errors=client.async_errors-async_before,
                baseline=baseline, final=final)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', required=True)
    parser.add_argument('--rate', type=int, choices=[50, 250, 500], required=True)
    parser.add_argument('--seconds', type=float, required=True)
    parser.add_argument('--request-hz', type=float, default=1.0)
    parser.add_argument('--log', required=True, help='新建逐帧JSONL')
    parser.add_argument('--report', required=True, help='新建汇总JSON')
    args = parser.parse_args()
    if not all(math.isfinite(x) and x > 0 for x in (args.seconds, args.request_hz)):
        parser.error('seconds/request-hz须为有限正数')
    import serial
    # 先独占新报告，拒绝覆盖已有证据。
    with open(args.report, 'x', encoding='utf-8') as report, JsonlLogger(args.log) as logger:
        with serial.Serial(args.port, 460800, timeout=.005, write_timeout=.100) as stream:
            try:
                result = pressure(Client(stream, logger=logger), args.rate, args.seconds, args.request_hz)
            except Exception as error:
                result = dict(status='FAIL', motion_commands=0, rate_hz=args.rate,
                              error_type=type(error).__name__, error=str(error))
        report.write(json.dumps(json_value(result), ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps(json_value(result), ensure_ascii=False, indent=2, allow_nan=False))
    raise SystemExit(0 if result['status'] == 'PASS' else 1)


if __name__ == '__main__':
    main()
