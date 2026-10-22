"""执行真实 Core UP/Break IRQ，验证通用 HAL 不能窃取消费 Platform 的 UIF。"""
from pathlib import Path
import tempfile
from unicorn.arm_const import UC_ARM_REG_R0

from verify_platform import (Model, ROOT, compile_image, CR1, SR, CNT, DIER, CR2,
                             DWT_CYCCNT, DIR, GATE)


def run(image, runtime=False, rtos=False):
    cases = 0
    for entry in ["platform_model_core_up", "platform_model_core_break"]:
        for break_flag in [0, 1 << 7, 1 << 8]:
            m = Model(image)
            start = m.get(DWT_CYCCNT)
            m.put(DIER, m.get(DIER) | 1 | (1 << 7))
            m.put(SR, break_flag)
            injected = []

            def arrive_at_hal(model, address):
                if not injected and address == model.symbols["HAL_TIM_IRQHandler"] & ~1:
                    # Platform 首读无 UIF 已返回；同优先级 ISR 中的新 UEV 不能抢占。
                    model.put(DWT_CYCCNT, start + 8500)
                    model.put(CNT, 0)
                    model.put(CR1, model.get(CR1) & ~DIR)
                    model.put(SR, model.get(SR) | 1)
                    injected.append(True)

            m.on_instruction = arrive_at_hal
            m.call(entry)
            m.on_instruction = None
            assert injected and m.get(SR) & 1 and m.get(DIER) & 1
            assert m.word("model_core_hal_updates") == 0
            assert m.word("model_core_hal_source") & 1 == 0
            assert m.word("model_control_time") == 0 and not m.moe()
            if break_flag:
                assert m.word("model_core_hal_breaks") == 1
                assert m.get(DIER) & (1 << 7) == 0
            # 模拟恢复 UIE 后的 NVIC 尾链：仅 Platform 消费该真实 pending UEV。
            m.call("platform_model_core_up")
            assert m.get(SR) & 1 == 0 and m.get(DIER) & 1
            assert m.word("model_control_time") == 2 and m.get(CR2) & GATE == GATE
            assert m.word("model_core_hal_updates") == 0 and not m.moe()
            assert m.word("model_core_unexpected_irqs") == 0
            cases += 1
    m = Model(image)
    m.put(DIER, 0)
    m.call("platform_model_core_break")
    assert m.get(DIER) & 1 == 0 and m.word("model_core_hal_updates") == 0
    cases += 1
    # 负对照：直接让通用 HAL 看到 UIE+UIF，确实会清旗且不推进 Platform 时基。
    m = Model(image)
    m.put(SR, 1)
    m.cpu.reg_write(UC_ARM_REG_R0, m.symbols["htim1"])
    m.call("HAL_TIM_IRQHandler")
    assert m.get(SR) & 1 == 0 and m.word("model_core_hal_updates") == 1
    assert m.word("model_control_time") == 0
    cases += 1
    # 真实 SPI1 向量只分发私有有限事务，生成区 HAL 等待路径必须不可达。
    m = Model(image)
    before = (m.get(SR), m.get(DIER), m.word("model_control_time"))
    m.call("SPI1_IRQHandler")
    assert m.word("model_core_position_irq_calls") == 1
    assert m.word("model_core_spi_hal_calls") == m.word("model_core_unexpected_irqs") == 0
    assert (m.get(SR), m.get(DIER), m.word("model_control_time")) == before and not m.moe()
    cases += 1
    m = Model(image)
    before = (m.get(SR), m.get(DIER), m.word("model_control_time"))
    m.call("DMA1_Channel1_IRQHandler")
    assert m.word("model_core_dma_hal_calls") == 1 and m.word("model_core_unexpected_irqs") == 0
    assert (m.get(SR), m.get(DIER), m.word("model_control_time")) == before and not m.moe()
    cases += 1
    if runtime:
        m = Model(image)
        m.call("TIM6_DAC_IRQHandler")
        assert m.word("model_core_guard_calls") == 1
        assert m.word("model_core_tim6_hal_calls") == 1 and m.word("model_core_dac_hal_calls") == 2
        assert m.word("model_core_unexpected_irqs") == 0 and not m.moe()
        assert not any(name.startswith(("g_target_runtime_compute", "g_target_runtime_irq_entries",
            "g_target_irq_timing", "g431_irq_timing")) for name in m.symbols)
        cases += 1
    m = Model(image)
    m.call('TIM7_IRQHandler')
    assert m.word('model_core_tim7_hal_calls') == int(rtos)
    assert m.word('model_control_time') == 0 and not m.moe()
    cases += 1
    if rtos:
        assert not {'SVC_Handler', 'PendSV_Handler', 'SysTick_Handler'} & m.symbols.keys()
    for entry,counter in [('DMA1_Channel2_IRQHandler','model_core_uart_rx_calls'),
                          ('DMA1_Channel3_IRQHandler','model_core_uart_tx_calls'),
                          ('USART1_IRQHandler','model_core_uart_calls')]:
        m=Model(image);m.call(entry)
        assert m.word(counter)==int(rtos) and m.word('model_core_unexpected_irqs')==0
        assert m.word('model_control_time')==0 and not m.moe()
        cases+=1
    label = 'Run RTOS Core' if rtos else ("Run兼容试具" if runtime else "Validation")
    print(f"G431 real Core IRQ ownership ({label}): {cases} cases PASS")
    print("Core/Platform ARM code with faithful UPDATE/BREAK HAL subset; no board, NVIC latency or electrical proof.")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="g431-core-irq-") as temporary:
        run(compile_image(Path(temporary), [ROOT / "Core/Src/stm32g4xx_it.c",
            Path(__file__).with_name("core_irq_model.c")], ["PLATFORM_MODEL_CORE_IRQ"]))
    with tempfile.TemporaryDirectory(prefix="g431-run-core-irq-") as temporary:
        run(compile_image(Path(temporary), [ROOT / "Core/Src/stm32g4xx_it.c",
            Path(__file__).with_name("core_irq_model.c")], ["PLATFORM_MODEL_CORE_IRQ",
            "BLDC_G431_RUNTIME_ENTRY", "BLDC_G431_FINITE_RUN"]), runtime=True)
    with tempfile.TemporaryDirectory(prefix="g431-rtos-core-irq-") as temporary:
        run(compile_image(Path(temporary), [ROOT / "Core/Src/stm32g4xx_it.c",
            Path(__file__).with_name("core_irq_model.c")], ["PLATFORM_MODEL_CORE_IRQ",
            "BLDC_G431_RUNTIME_ENTRY", "BLDC_G431_FINITE_RUN", "BLDC_G431_FREERTOS"]),
            runtime=True, rtos=True)
