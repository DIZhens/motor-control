"""真实 ARM Runtime 的电流滤波、原始保护及历史退休；不连接设备。"""
import math
import struct
import csv
import json
import statistics
from pathlib import Path


def run_current_filter_cases(Model, Pmsm, dt, cases):
    d = 1.0 - math.cos(2.0 * math.pi * 1000.0 * dt)
    coefficient = struct.unpack('<f', struct.pack('<f', math.sqrt(d*(d+2.0))-d))[0]

    def set_current(model, alpha, beta):
        b = -alpha/2.0 + math.sqrt(3.0)/2.0*beta
        c = -alpha/2.0 - math.sqrt(3.0)/2.0*beta
        # 与合成平台的独立有符号增益及零偏相反换算，仍走真实Measurement。
        model.write('model_current_b', b/1.25 + 0.02)
        model.write('model_current_c', c/-0.8 - 0.03)

    def active(value=coefficient, extra=None):
        model = Model({'model_filter_coefficient': value, **(extra or {})})
        model.ready()
        assert model.word('model_filter_ready') == 0
        model.start()
        assert model.word('model_filter_ready') == 0
        return model

    for bad in (-0.01, 1.01, float('nan'), float('inf')):
        Model({'model_filter_coefficient': bad}, valid_init=False)
    Model({'model_enabled': 0, 'model_filter_coefficient': float('nan')}).ready()
    cases.append('滤波系数非法则初始化失败；Observe关闭控制时不消费无关滤波配置')

    model = active()
    expected = None
    values = [(0.12, -0.06)]*3 + [(-0.08, 0.09)]*8 + [(0.0, 0.0)]*12
    for alpha, beta in values:
        set_current(model, alpha, beta)
        model.step()
        raw_alpha, raw_beta, filtered_alpha, filtered_beta = model.filter_values()
        if expected is None:
            expected = [raw_alpha, raw_beta]
            assert struct.pack('<2f', filtered_alpha, filtered_beta) == struct.pack('<2f', raw_alpha, raw_beta)
        else:
            expected = [(1-coefficient)*old+coefficient*new for old,new in zip(expected,(raw_alpha,raw_beta))]
        assert all(math.isclose(a,b,abs_tol=8e-8) for a,b in zip((filtered_alpha,filtered_beta),expected))
        assert math.isclose(raw_alpha, alpha, abs_tol=1e-7)
        assert math.isclose(raw_beta, beta, abs_tol=1e-7)
        angle = model.values()[10]*2*math.pi
        expected_dq = (filtered_alpha*math.cos(angle)+filtered_beta*math.sin(angle),
                       -filtered_alpha*math.sin(angle)+filtered_beta*math.cos(angle))
        assert all(math.isclose(a,b,abs_tol=1e-6) for a,b in zip(model.controller_dq(),expected_dq))
        assert struct.pack('<2f', *model.controller_dq()) == struct.pack('<2f', *model.values()[2:4])
    cases.append('滤波首样本直通、两轴阶跃/衰减符合独立递推；原始相电流保持，PI与RUN快照采用同一滤后DQ')

    if math.isclose(dt,0.0001):
        evidence=Path(__file__).resolve().parents[1] / 'fixtures/current-filter'
        rows=list(csv.DictReader((evidence/'raw-adc-12v-static.csv').open(encoding='utf-8-sig')))
        source_b=[float(row['adc2_b']) for row in rows]
        source_c=[float(row['adc1_c']) for row in rows]
        mean_b,mean_c=statistics.mean(source_b),statistics.mean(source_c)
        k=3.3/4095/.7049
        replay=active()
        raw_values,filtered_values=[],[]
        for b,c in zip(source_b,source_c):
            corrected_b,corrected_c=-k*(b-mean_b),-k*(c-mean_c)
            set_current(replay,-corrected_b-corrected_c,(corrected_b-corrected_c)/math.sqrt(3))
            replay.step()
            value=replay.filter_values()
            raw_values.append(value[0]);filtered_values.append(value[2])
        baseline_std=statistics.pstdev(raw_values[128:])*1000
        filtered_std=statistics.pstdev(filtered_values[128:])*1000
        report=json.loads((evidence/'current-filter-evaluation.json').read_text(encoding='utf-8'))
        powered=next(c for c in report['conditions'] if c['condition']=='powered')
        candidate=next(c for c in powered['candidates'] if c['cutoff_hz']==1000)
        assert math.isclose(baseline_std,powered['baseline']['alpha_std_nominal_mA'],abs_tol=0.0003)
        assert math.isclose(filtered_std,candidate['alpha_std_nominal_mA'],abs_tol=0.0003)
        replay.command(2);replay.step();replay.off()
        cases.append('保存的512点12V原码经真实ARM Measurement/滤波/PI回放，与独立Python/MATLAB静态降噪结果一致')

    bypass, direct = active(0.0), active(1.0)
    for alpha,beta in values:
        for instance in (bypass,direct):
            set_current(instance,alpha,beta)
            instance.step()
            observed = instance.filter_values()
            assert struct.pack('<2f', *observed[:2]) == struct.pack('<2f', *observed[2:])
        assert struct.pack('<16f', *bypass.values()) == struct.pack('<16f', *direct.values())
        assert struct.pack('<3f', *bypass.duty()) == struct.pack('<3f', *direct.duty())
    cases.append('系数0旁路与1直通逐位相同，不改变电流输入、PI历史、DQ发布或duty')

    model.command(2)
    model.step()
    model.off()
    assert model.filter_values()[2:] == (0.0,0.0)
    set_current(model,0.07,-0.03)
    model.start()
    model.step()
    observed = model.filter_values()
    assert struct.pack('<2f', *observed[:2]) == struct.pack('<2f', *observed[2:])
    assert model.word('model_enable_count') == 2
    assert bypass.filter_values() != model.filter_values()
    cases.append('Stop清滤波历史；显式再次Start使用新首样本而非上次历史，多Runtime实例无共享滤波状态')

    model = active(0.001, {'model_current_trip_a':0.15})
    set_current(model,0.02,0.0)
    model.step()
    stages = model.word('model_stage_count')
    set_current(model,0.5,0.0)  # 强低通本可掩盖的原始越限，必须先于滤波撤销。
    model.step()
    model.off(1<<12)
    assert model.word('model_stage_count') == stages
    cases.append('强低通不能掩盖原始相电流越限：同拍OVERCURRENT、清历史、无新增stage')

    for settings in ({'model_current_valid':0}, {'model_current_b':float('nan')}, {'model_tick_step':4}):
        model = active()
        model.step()
        stages = model.word('model_stage_count')
        for key,value in settings.items(): model.write(key,value)
        model.step()
        model.off(1<<8)
        assert model.word('model_stage_count') == stages
    cases.append('无效/非有限/跳拍采样不能被滤波旧值替代：FEEDBACK故障、同拍关断并清历史')

    for mode in (1,2,3,4):
        model = active()
        model.step()
        stages = model.word('model_stage_count')
        model.write('model_filter_corruption',mode)
        model.call('model_corrupt_filter')
        model.step()
        model.off(1<<9)
        assert model.word('model_stage_count') == stages
        assert model.filter_values()[2:] == (0.0,0.0)
    cases.append('滤波历史/系数/配置破坏均为CONTROL失败，同拍清DQ/积分/滤波历史并关断，不stage')

    for target in (-60.0,60.0):
        model = Model({'model_filter_coefficient':coefficient})
        model.ready()
        plant = Pmsm()
        model.start(target,plant=plant)
        model.step(round(0.6/dt),plant=plant)
        assert model.diag()[:2] == (2,0)
        assert abs(model.values()[1]-target) < 4.0
        assert abs(plant.x[2]*60/(2*math.pi)-target) < 4.0
        model.command(2)
        model.step(plant=plant)
        model.off()
    cases.append('1kHz滤波在独立合成PMSM的正反速度跟踪及Stop保持有界；不作为真实电机稳定性或性能验收')
