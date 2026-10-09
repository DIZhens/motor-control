/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"
#include "adc.h"
#include "comp.h"
#include "dac.h"
#include "fdcan.h"
#include "opamp.h"
#include "spi.h"
#include "tim.h"
#include "usart.h"
#include "usb.h"
#include "gpio.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include <stddef.h>

#include "bus_observation.h"
#include "position_observation.h"
#include "realtime_motor_io.h"
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */
/* 首板无功率诊断快照：仅供 Keil Watch 观察，不作为正式 Firmware API。 */
typedef struct
{
    volatile uint32_t status;                /* 启动结果：1=运行；101~106=初始化失败。 */
    volatile uint32_t fast_ticks;            /* 实际收到的同步采样 callback 数量。 */
    volatile uint32_t current_valid_ticks;   /* 本次 B/C 电流采集完整的次数。 */
    volatile uint32_t current_invalid_ticks; /* 本次采集未完整的次数。 */
    volatile uint32_t measured_phases;       /* 最近采集实际测得的物理相位位图。 */
    volatile float phase_b_a;                /* 最近实际测得的 B 相电流，A。 */
    volatile float phase_c_a;                /* 最近实际测得的 C 相电流，A。 */
    volatile uint32_t position_samples;      /* 不同 ControlTime 的位置 publication 数。 */
    volatile float position_turns;           /* 最近位置，机械圈。 */
    volatile bool position_valid;            /* 最近位置采集是否有效。 */
    volatile uint32_t bus_samples;           /* 不同 ControlTime 的母线 publication 数。 */
    volatile float bus_voltage_v;            /* 最近母线电压，V。 */
    volatile bool bus_valid;                 /* 最近母线采集是否有效。 */
} PlatformBringupDebug;
/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/

/* USER CODE BEGIN PV */
volatile PlatformBringupDebug g_platform_bringup;
static ControlTime s_last_position_time;
static ControlTime s_last_bus_time;
/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
/* USER CODE BEGIN PFP */
static void platform_bringup_sample_ready(
    void *context,
    const PhaseCurrentObservation *observation);
static void platform_bringup_start(void);
static void platform_bringup_poll(void);
/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */
/*
 * 临时无功率 Bring-up：只观测 ADC / SPI / ControlTime，
 * 不提交 phase duty，不调用 realtime_motor_io_power_enable()。
 * 上板前仍需断开电机，并确认安全供电与功率路径。
 */
static void platform_bringup_sample_ready(
    void *context,
    const PhaseCurrentObservation *observation)
{
    (void)context;

    g_platform_bringup.fast_ticks++;
    g_platform_bringup.measured_phases = observation->measured_phases;

    if (observation->acquisition_valid) {
        g_platform_bringup.current_valid_ticks++;
    } else {
        g_platform_bringup.current_invalid_ticks++;
    }

    if ((observation->measured_phases & PHASE_MASK_B) != 0U) {
        g_platform_bringup.phase_b_a = observation->phase_b_a;
    }

    if ((observation->measured_phases & PHASE_MASK_C) != 0U) {
        g_platform_bringup.phase_c_a = observation->phase_c_a;
    }
}

static void platform_bringup_start(void)
{
    RealtimeMotorIoFacts facts;

    /* 所有启动阶段都禁止功率输出；失败时保持 safe-off。 */
    realtime_motor_io_safe_off();

    if (!realtime_motor_io_get_facts(&facts)) {
        g_platform_bringup.status = 101U;
        return;
    }

    if (!realtime_motor_io_bind_sample_ready(
            platform_bringup_sample_ready, NULL)) {
        g_platform_bringup.status = 102U;
        return;
    }

    if (!position_observation_arm()) {
        g_platform_bringup.status = 103U;
        return;
    }

    if (!bus_observation_arm()) {
        g_platform_bringup.status = 104U;
        return;
    }

    if (!realtime_motor_io_arm()) {
        g_platform_bringup.status = 105U;
        realtime_motor_io_safe_off();
        return;
    }

    if (!realtime_motor_io_start_drive_timing()) {
        g_platform_bringup.status = 106U;
        realtime_motor_io_safe_off();
        return;
    }

    g_platform_bringup.status = 1U;
}

static void platform_bringup_poll(void)
{
    PositionObservation position;
    BusObservation bus;

    if (position_observation_get_latest(&position)) {
        if (position.observation_time != s_last_position_time) {
            s_last_position_time = position.observation_time;
            g_platform_bringup.position_samples++;
        }

        g_platform_bringup.position_turns = position.mechanical_position_turns;
        g_platform_bringup.position_valid = position.acquisition_valid;
    }

    if (bus_observation_get_latest(&bus)) {
        if (bus.observation_time != s_last_bus_time) {
            s_last_bus_time = bus.observation_time;
            g_platform_bringup.bus_samples++;
        }

        g_platform_bringup.bus_voltage_v = bus.voltage_v;
        g_platform_bringup.bus_valid = bus.acquisition_valid;
    }
}
/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{

  /* USER CODE BEGIN 1 */

  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_ADC1_Init();
  MX_ADC2_Init();
  MX_FDCAN1_Init();
  MX_OPAMP1_Init();
  MX_OPAMP2_Init();
  MX_OPAMP3_Init();
  MX_SPI1_Init();
  MX_TIM1_Init();
  MX_USART1_UART_Init();
  MX_COMP1_Init();
  MX_COMP2_Init();
  MX_COMP4_Init();
  MX_DAC3_Init();
  MX_COMP3_Init();
  MX_DAC1_Init();
  MX_USB_PCD_Init();
  MX_TIM6_Init();
  /* USER CODE BEGIN 2 */
  platform_bringup_start();
  /* USER CODE END 2 */

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  while (1)
  {
    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
    platform_bringup_poll();
    HAL_Delay(10);
  }
  /* USER CODE END 3 */
}

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /** Configure the main internal regulator output voltage
  */
  HAL_PWREx_ControlVoltageScaling(PWR_REGULATOR_VOLTAGE_SCALE1_BOOST);

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSI48|RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState = RCC_HSE_ON;
  RCC_OscInitStruct.HSI48State = RCC_HSI48_ON;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSE;
  RCC_OscInitStruct.PLL.PLLM = RCC_PLLM_DIV2;
  RCC_OscInitStruct.PLL.PLLN = 85;
  RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV2;
  RCC_OscInitStruct.PLL.PLLQ = RCC_PLLQ_DIV2;
  RCC_OscInitStruct.PLL.PLLR = RCC_PLLR_DIV2;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV1;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_4) != HAL_OK)
  {
    Error_Handler();
  }
}

/* USER CODE BEGIN 4 */

/* USER CODE END 4 */

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* User can add his own implementation to report the HAL error return state */
  __disable_irq();
  while (1)
  {
  }
  /* USER CODE END Error_Handler_Debug */
}
#ifdef USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
