"""正式Run装配、UART和独立有限守卫；由verify_runtime --finite-run加载。"""
import struct

TIM1, TIM6, DWT = 0x40012C00, 0x40001000, 0xE0001004
IDLE, RUN, FAULT = 1, 2, 3


def verify(Model, symbols, passed):
    def tick(m, count=1):
        m.phase = 'sample'
        for _ in range(count):
            m.call('model_sample')
        m.phase = ''

    def status(m):
        return struct.unpack('<4IQ2f',m.cpu.mem_read(symbols['g_target_runtime_run_status'],32))

    def ready(wrap=False):
        m=Model(start=False)
        if wrap: m.memory_write(DWT,0xF8000000)
        m.call('model_start')
        assert m.motor()[0:2]==(IDLE,0) and m.word('model_enable_count')==0
        m.write('model_bus_voltage',12.0,'f')
        m.write('model_position_available',1);m.write('model_position_use_fixed_time',0)
        m.write('model_current_b',-0.012,'f');m.write('model_current_c',-0.001,'f')
        tick(m,4250)
        assert m.current()['state']==3 and m.current()['count']==4096,m.current()
        assert m.current()['valid'] and m.motor()[0:2]==(IDLE,0)
        return m

    def start(m):
        m.receive(b'start:60\n');tick(m)
        assert m.motor()[0]==IDLE and status(m)[0:3]==(1,1,0)
        assert not m.memory_word(TIM1+0x44)&0x8000
        tick(m);assert m.word('model_enable_count')==0
        tick(m);assert m.motor()[0]==RUN and m.word('model_enable_count')==1

    m=Model({'model_cordic_ok':0})
    assert m.motor()[0:2]==(FAULT,1) and m.word('model_enable_count')==0
    passed('Run所选CORDIC装配失败锁存启动故障且保持关断')
    m=Model();m.receive(b'start:60\n');tick(m,3)
    assert m.motor()[0]==IDLE and m.motor()[2]==2 and m.word('model_enable_count')==0
    passed('Run校零或反馈未就绪时UART Start仍拒绝，不借用配置许可启动')

    # Platform每10拍在consumer之后发起采集；新publication下拍才可读取。
    # latest读取不启动采集，重复时间戳不作为新的速度测量。
    m=Model();m.write('model_bus_voltage',12.0,'f')
    m.write('model_position_use_fixed_time',1)
    for index in range(4250):
        tick(m)
        if index % 10 == 9:
            m.write('model_position_available',1)
            m.write('model_position_time',m.control_time(),'Q')
    assert m.current()['state']==3 and m.current()['count']==4096,m.current()
    assert m.angle()['valid'] and m.motor()[0:2]==(IDLE,0)
    start(m)
    for index in range(100):
        tick(m)
        if index % 10 == 9:
            m.write('model_position_time',m.control_time(),'Q')
        assert m.motor()[0:2]==(RUN,0),m.motor()
    passed('真实1kHz位置publication在consumer之后更新，10kHz读取缓存不重复估速且持续角度有效')
    m=ready();r=m.runtime_status()
    assert all(r[k] for k in ['control_enabled','current_confirmed','timing_confirmed','angle_confirmed']),r
    assert status(m)==(0,0,0,0,0,0.0,0.0)
    m.receive(b'start:81\n');tick(m)
    assert m.motor()[2]==3 and m.word('model_enable_count')==0
    passed('Run真实装配自动校零且默认关断，越界UART目标不能使能')
    start(m);stamp=status(m)[3]
    tick(m,100);m.receive(b'speed:40\n');tick(m)
    assert m.motor()[0]==RUN and m.motor()[4]==40.0
    assert status(m)[3]==stamp and status(m)[0]==1
    frame=m.frame();assert frame[1]==40.0 and frame[6]==RUN and frame[8]==511.0,frame
    m.receive(b'speed:50\nstop\n');tick(m)
    assert m.motor()[0:2]==(IDLE,0) and m.motor()[4]==0
    assert not m.memory_word(TIM1+0x44)&0x8000 and status(m)[1]==0
    assert m.current()['state']==3 and not m.fast_uart_accesses
    passed('UART完整启动跨两拍使能、Update不续期、Stop优先，VOFA发布RUN及有效Id/Iq')

    m=ready(wrap=True);start(m);stamp=status(m)[3]
    while ((m.memory_word(DWT)-stamp)&0xffffffff)<255000000: tick(m)
    m.receive(b'speed:30\n') # 超时必须撤销已排队的旧运动请求
    m.memory_write(TIM6+0x10,1);m.call('model_guard')
    assert not m.memory_word(TIM1+0x44)&0x8000 and status(m)[1:3]==(0,1)
    tick(m);assert m.motor()[0:2]==(IDLE,0) and m.motor()[4]==0
    tick(m,20);assert m.word('model_enable_count')==1 and status(m)[0]==1
    passed('独立1.5秒守卫跨DWT回绕先关断，再Stop并退休旧目标，无自动重启')

    m=ready();start(m)
    m.write('model_position_use_fixed_time',1)
    m.write('model_position_time',m.control_time(),'Q')
    for i in range(1,520):
        tick(m,10)
        m.write('model_position_turns',(i*0.004)%1,'f')
        m.write('model_position_time',m.control_time(),'Q');tick(m)
        if m.motor()[0]==FAULT: break
    assert m.motor()[0]==FAULT and m.motor()[1]&64,m.motor()
    assert not m.memory_word(TIM1+0x44)&0x8000 and status(m)[2]==2
    assert 2<=abs(status(m)[5])<2.01,status(m)
    tick(m,20);assert m.word('model_enable_count')==1
    passed('有限Run累计净位移跨编码器回绕，2圈边界锁故障并关断')

    m=ready();start(m);m.write('model_current_valid',0);tick(m)
    assert m.motor()[0]==FAULT and not m.memory_word(TIM1+0x44)&0x8000
    assert status(m)[1]==0
    m.receive(b'clear\nstart:60\n');tick(m,5)
    assert m.motor()[0]==FAULT and m.word('model_enable_count')==1
    passed('真实采样错误关断；Clear/新Start不绕过已有故障与校准失效')
