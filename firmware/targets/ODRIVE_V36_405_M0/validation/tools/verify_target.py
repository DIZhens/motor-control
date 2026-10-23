"""Static Target configuration/member check; does not replace Rebuild or L1/L2."""

from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return (ROOT / path).read_text(encoding="utf-8-sig")


def check():
    ioc = dict(line.replace("\\", "").split("=", 1)
               for line in read("ODRIVE_V36_405_M0.ioc").splitlines() if "=" in line)
    expected = {
        "Mcu.Name": "STM32F405RGTx",
        "ProjectManager.DefaultFWLocation": "true",
        "ProjectManager.CustomerFirmwarePackage": "",
        "PH0-OSC_IN.Mode": "HSE-External-Oscillator",
        "PH1-OSC_OUT.Mode": "HSE-External-Oscillator",
        "RCC.HSE_VALUE": "8000000",
        "RCC.SYSCLKFreq_VALUE": "168000000",
        "RCC.APB1TimFreq_Value": "84000000",
        "RCC.APB2TimFreq_Value": "168000000",
        "TIM1.CounterMode": "TIM_COUNTERMODE_CENTERALIGNED1",
        "TIM1.Period": "4200",
        "TIM1.DeadTime": "84",
        "TIM1.TIM_MasterOutputTrigger": "TIM_TRGO_OC4REF",
        "TIM1.OffStateIDLEMode": "TIM_OSSI_ENABLE",
        "TIM1.OffStateRunMode": "TIM_OSSR_ENABLE",
        "TIM1.OCMode_PWM-PWM Generation4 No Output": "TIM_OCMODE_PWM2",
        "TIM1.Pulse-PWM Generation4 No Output": "4116",
        "TIM2.Prescaler": "83",
        "TIM2.Period": "4294967295",
        "SPI3.BaudRatePrescaler": "SPI_BAUDRATEPRESCALER_32",
        "SPI3.CLKPolarity": "SPI_POLARITY_HIGH",
        "PA0-WKUP.GPIO_Label": "MT6701_CS",
        "PA0-WKUP.PinState": "GPIO_PIN_SET",
        "ADC1.NbrOfConversion": "2",
        "ADC1.Channel-0#ChannelRegularConversion": "ADC_CHANNEL_6",
        "ADC1.Channel-1#ChannelRegularConversion": "ADC_CHANNEL_VREFINT",
        "Dma.ADC1.0.Instance": "DMA2_Stream0",
        "Dma.ADC1.0.Mode": "DMA_NORMAL",
        "Dma.ADC1.0.MemDataAlignment": "DMA_MDATAALIGN_HALFWORD",
        "Dma.ADC1.0.PeriphDataAlignment": "DMA_PDATAALIGN_HALFWORD",
    }
    for adc, channel in [("ADC2", "ADC_CHANNEL_10"), ("ADC3", "ADC_CHANNEL_11")]:
        expected.update({
            f"{adc}.ClockPrescaler": "ADC_CLOCK_SYNC_PCLK_DIV4",
            f"{adc}.InjNumberOfConversion": "1",
            f"{adc}.Channel-0#ChannelInjectedConversion": channel,
            f"{adc}.SamplingTime-0#ChannelInjectedConversion": "ADC_SAMPLETIME_15CYCLES",
            f"{adc}.ExternalTrigInjecConv": "ADC_EXTERNALTRIGINJECCONV_T1_TRGO",
            f"{adc}.ExternalTrigInjecConvEdge": "ADC_EXTERNALTRIGINJECCONVEDGE_RISING",
        })
    for key, value in expected.items():
        require(ioc.get(key) == value, f"IOC mismatch: {key} = {ioc.get(key)!r}")

    project = ET.fromstring(read("MDK-ARM/ODRIVE_V36_405_M0.uvprojx"))
    require(project.findtext(".//Device") == "STM32F405RGTx", "Wrong Keil MCU")
    require(project.findtext(".//uAC6") == "0", "Expected ARM Compiler 5")
    require("HSE_VALUE=8000000U" in project.findtext(".//Cads/VariousControls/Define").split(","),
            "CMSIS SystemCoreClockUpdate must use the same 8 MHz HSE as HAL")
    for name, start, size in [("IRAM", 0x20000000, 0x20000),
                             ("IROM", 0x08000000, 0x100000),
                             ("OCR_RVCT4", 0x08000000, 0x100000),
                             ("OCR_RVCT9", 0x20000000, 0x20000)]:
        node = project.find(".//OnChipMemories/" + name)
        require(int(node.findtext("StartAddress") or "0", 0) == start
                and int(node.findtext("Size") or "0", 0) == size,
                f"Keil memory region mismatch: {name}")
    require(project.findtext(".//Ir2Chk") == "0", "Keep DMA buffers out of CCM RAM")
    paths = [(ROOT / 'MDK-ARM' / file.findtext("FilePath").replace("\\", "/")).resolve()
             for file in project.findall(".//Groups/Group/Files/File")]
    require(len(paths) == len(set(paths)), "Duplicate Keil source member")
    for path in paths:
        require(path.is_file(), f"Missing Keil source: {path}")
    sources = {p.resolve() for folder in ["Core/Src", "platform", "validation",
                                        "Drivers/STM32F4xx_HAL_Driver/Src"]
               for p in (ROOT / folder).glob("*.c")}
    require(sources == {p for p in paths if p.suffix == ".c"},
            "Keil C membership differs from formal source set")
    includes = project.findtext(".//Cads/VariousControls/IncludePath").split(";")
    for path in includes:
        require((ROOT / 'MDK-ARM' / path.replace("\\", "/")).is_dir(),
                f"Missing Keil include: {path}")
    for path in [ROOT / 'platform', ROOT / 'validation',
                 Path(__file__).resolve().parents[4] / 'platform']:
        require(path in {(ROOT / 'MDK-ARM' / p.replace("\\", "/")).resolve() for p in includes},
                f"Missing include binding: {path}")

    tim = read("Core/Src/tim.c")
    for fragment in ["htim1.Init.RepetitionCounter = 0;",
                     "sConfigOC.OCIdleState = TIM_OCIDLESTATE_RESET;",
                     "sConfigOC.OCNIdleState = TIM_OCNIDLESTATE_RESET;",
                     "sBreakDeadTimeConfig.AutomaticOutput = TIM_AUTOMATICOUTPUT_DISABLE;",
                     "htim2.Init.Period = 4294967295;"]:
        require(fragment in tim, f"Generated TIM configuration missing: {fragment}")
    main = read("Core/Src/main.c")
    require("RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSE;" in main
            and "RCC_OscInitStruct.PLL.PLLN = 336;" in main, "Generated PLL mismatch")
    require(not re.search(r"\brealtime_motor_io_power_enable\s*\(", main),
            "main must not enable power directly")
    require("target_validation_start()" in main, "Missing Target validation entry")
    validation = read("validation/target_validation.c")
    for fragment in ["g_target_validation_request.power_key = 0U;",
                     "g_target_validation_request.sequence = 0U;",
                     "request.power_key != TARGET_VALIDATION_POWER_KEY",
                     "target_validation_update_irq", "realtime_motor_io_power_enable()",
                     "realtime_motor_io_stage_phase_duty(&s_duty)"]:
        require(fragment in validation, f"Missing validation guard/path: {fragment}")
    irq = read("Core/Src/stm32f4xx_it.c")
    require("target_validation_update_irq();" in irq, "Missing independent burst deadline")
    require(not list((ROOT / 'platform').glob("*hardware_trip*")), "Unexpected Hardware Trip")
    print(f"PASS: IOC, generated timing, {len(paths)} Keil members, includes, power-disabled entry")
    print("Static checks only; Keil Rebuild and physical L1/L2 remain separate.")


if __name__ == "__main__":
    try:
        check()
    except (ValueError, OSError, KeyError, AttributeError, ET.ParseError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
