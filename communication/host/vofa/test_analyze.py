"""独立合成阶跃的描述指标，不把期望值建立在控制算法实现上。"""
import copy
import unittest

from communication.host.vofa.analyze import analyze
from communication.host.vofa.test_bridge import sample


def records(target, velocities, session=1):
    output = []
    for index, velocity in enumerate(velocities):
        value = sample()
        value.update(control_time_ticks=index * 100, target_velocity_rpm=target,
                     velocity_rpm=velocity, iq_reference_a=.06, iq_feedback_a=.05,
                     id_feedback_a=.002, limit_flags=(1 if index == 0 else 0))
        output.append(dict(direction='rx', status=0, frame=dict(kind=3, message_id=0x1001,
                                                              session_id=session, sequence=index + 1), value=value))
    return output


class AnalyzeTest(unittest.TestCase):
    def test_signed_step_and_tracking(self):
        for sign in (1, -1):
            result = analyze(records(sign * 40, [sign * v for v in (0, 4, 20, 36, 40, 40, 40)]), 1000)
            window, = result['windows']
            self.assertEqual(result['status'], 'DESCRIPTIVE_ONLY')
            self.assertAlmostEqual(window['rise_10_90_s'], .2)
            self.assertAlmostEqual(window['settling_5_percent_s'], .4)
            self.assertEqual(window['overshoot_rpm'], 0)
            self.assertEqual(window['terminal_window_error_mean_rpm'], 0)
            self.assertAlmostEqual(window['iq_tracking_rmse_a'], .01)
            self.assertAlmostEqual(window['limit_sample_fractions']['speed_iq'], 1/7)

    def test_down_step(self):
        window, = analyze(records(40, [60, 58, 50, 42, 40, 40]), 1000)['windows']
        self.assertAlmostEqual(window['rise_10_90_s'], .2)
        self.assertEqual(window['overshoot_rpm'], 0)

    def test_censored_invalid_and_lost_samples(self):
        data = records(40, [0, 4, 20, 30])
        window, = analyze(data, 1000)['windows']
        self.assertIsNone(window['rise_10_90_s'])
        self.assertIsNone(window['settling_5_percent_s'])
        data[-1]['value']['velocity_rpm'] = None
        data[-1]['value']['valid_flags'] &= ~8
        self.assertIsNone(analyze(data, 1000)['windows'][0]['terminal_window_error_mean_rpm'])
        data = records(40, [0, 40, 40, 40])
        for row in data[1:]:
            row['frame']['sequence'] += 1
        window, = analyze(data, 1000)['windows']
        self.assertEqual(window['wire_gaps_inside_window'], 1)
        self.assertIsNone(window['settling_5_percent_s'])

    def test_session_and_target_boundaries(self):
        a = records(40, [0, 20, 40])
        b = records(40, [0, 20, 40], session=2)
        c = copy.deepcopy(b)
        for row in c:
            row['value']['control_time_ticks'] += 300
            row['value']['target_velocity_rpm'] = 60
            row['frame']['sequence'] += 3
        result = analyze(a + b + c, 1000)
        self.assertEqual([w['target_rpm'] for w in result['windows']], [40, 40, 60])
        self.assertEqual(result['samples'], 9)


if __name__ == '__main__':
    unittest.main()
