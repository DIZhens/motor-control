"""按 MCU 控制时间分析已解码的有限运行日志；离线指标不构成性能验收。"""
import argparse
import json
import math
import statistics


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def window_metrics(rows, ticks_per_second):
    """RUN 内同一目标窗口；5% 带与尾部 20% 是描述约定，不是 PASS 门槛。"""
    first, last = rows[0], rows[-1]
    target = first['target_velocity_rpm']
    duration = (last['control_time_ticks'] - first['control_time_ticks']) / ticks_per_second
    velocity = [(row['control_time_ticks'], row['velocity_rpm']) for row in rows
                if row['valid_flags'] & 8 and finite(row['velocity_rpm'])]
    result = dict(target_rpm=target, first_control_tick=first['control_time_ticks'],
                  last_control_tick=last['control_time_ticks'], observed_duration_s=duration,
                  samples=len(rows), valid_speed_samples=len(velocity),
                  rise_10_90_s=None, settling_5_percent_s=None,
                  terminal_window_error_mean_rpm=None, terminal_window_speed_std_rpm=None)
    gaps = sum(max(0, b['_wire_sequence'] - a['_wire_sequence'] - 1) for a, b in zip(rows, rows[1:]))
    result['wire_gaps_inside_window'] = gaps
    if velocity:
        initial = velocity[0][1]
        step = target - initial
        times = [(tick - first['control_time_ticks']) / ticks_per_second for tick, _ in velocity]
        values = [v for _, v in velocity]
        if step:
            normalized = [(v - initial) / step for v in values]
            t10 = next((t for t, v in zip(times, normalized) if v >= .1), None)
            t90 = next((t for t, v in zip(times, normalized) if v >= .9), None)
            if t10 is not None and t90 is not None:
                result['rise_10_90_s'] = t90 - t10
            result['overshoot_rpm'] = max(0., max((v - target) * math.copysign(1., step) for v in values))
        else:
            result['overshoot_rpm'] = None
        # 缺失/无效速度时不声称窗口持续处于误差带内。
        band = .05 * abs(target)
        outside = [i for i, value in enumerate(values) if abs(value - target) > band]
        inside_start = outside[-1] + 1 if outside else 0
        if not gaps and len(velocity) == len(rows) and inside_start < len(values) - 1:
            result['settling_5_percent_s'] = times[inside_start]
        cutoff = first['control_time_ticks'] + .8 * (last['control_time_ticks'] - first['control_time_ticks'])
        tail = [v for tick, v in velocity if tick >= cutoff]
        if tail:
            result['terminal_window_error_mean_rpm'] = statistics.fmean(v - target for v in tail)
            result['terminal_window_speed_std_rpm'] = statistics.pstdev(tail)
            result['terminal_window_speed_peak_to_peak_rpm'] = max(tail) - min(tail)
        result['terminal_window_is_not_proof_of_steady_state'] = True
    currents = [row for row in rows if row['valid_flags'] & 0x70 == 0x70
                and all(finite(row[k]) for k in ('iq_reference_a', 'iq_feedback_a', 'id_feedback_a'))]
    result['current_samples'] = len(currents)
    if currents:
        errors = [row['iq_feedback_a'] - row['iq_reference_a'] for row in currents]
        result.update(iq_reference_abs_peak_a=max(abs(row['iq_reference_a']) for row in currents),
                      iq_tracking_rmse_a=math.sqrt(statistics.fmean(e * e for e in errors)),
                      iq_tracking_abs_peak_a=max(map(abs, errors)),
                      id_mean_a=statistics.fmean(row['id_feedback_a'] for row in currents),
                      id_abs_peak_a=max(abs(row['id_feedback_a']) for row in currents))
    result['limit_sample_fractions'] = {name: sum(bool(row['limit_flags'] & bit) for row in rows) / len(rows)
        for name, bit in [('speed_iq', 1), ('d_integral', 2), ('q_integral', 4), ('voltage', 8)]}
    return result


def analyze(records, ticks_per_second=20000):
    if not finite(ticks_per_second) or ticks_per_second <= 0:
        raise ValueError('控制时基须为有限正数')
    segments, current = [], []
    previous_domain = None
    sample_count = 0
    for record in records:
        frame, value = record.get('frame', {}), record.get('value')
        if (record.get('direction') != 'rx' or record.get('status') != 0
                or frame.get('kind') != 3 or frame.get('message_id') != 0x1001 or not value):
            continue
        sample_count += 1
        domain = (frame['session_id'], value['telemetry_config_generation'], value['active_profile_generation'])
        usable = (value['valid_flags'] & 0x305 == 0x305 and value['motor_state'] == 2
                  and finite(value['target_velocity_rpm']))
        split = (not usable or domain != previous_domain or current and (
            value['target_velocity_rpm'] != current[-1]['target_velocity_rpm']
            or value['control_time_ticks'] <= current[-1]['control_time_ticks']))
        if split and current:
            segments.append(window_metrics(current, ticks_per_second))
            current = []
        if usable:
            current.append(dict(value, _wire_sequence=frame['sequence']))
        previous_domain = domain
    if current:
        segments.append(window_metrics(current, ticks_per_second))
    return dict(status='DESCRIPTIVE_ONLY', samples=sample_count, control_ticks_per_second=ticks_per_second,
                conventions='Observed samples only; first valid velocity is step origin; 5% target band; last20% terminal window; no interpolation or invented PASS threshold',
                limitations='Not 10kHz current-loop bandwidth/WCET proof; settling after window end is unobserved; consult raw Session/sequence gaps and independent stop/fault evidence',
                windows=segments)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log')
    parser.add_argument('--ticks-per-second', type=float, default=20000)
    parser.add_argument('--output', required=True, help='新建汇总JSON')
    args = parser.parse_args()
    with open(args.log, encoding='utf-8') as stream:
        result = analyze([json.loads(line) for line in stream], args.ticks_per_second)
    with open(args.output, 'x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
