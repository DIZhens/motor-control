/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file    stm32g4xx_it.c
  * @brief   Interrupt Service Routines.
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
#include "stm32g4xx_it.h"
/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include "../../platform/realtime_motor_io_internal.h"
#include "../../platform/position_observation_internal.h"
#if defined(BLDC_G431_TIMING_ENTRY)
#include "timing_entry.h"
#include "runtime_timing.h"
#elif defined(BLDC_G431_OBSERVE_ENTRY)
#include "target_runtime.h"
#include "irq_timing.h"
#else
#include "target_validation.h"
#endif
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN TD */

/* USER CODE END TD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/
/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/
/* USER CODE BEGIN PV */

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
/* USER CODE BEGIN PFP */

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */

/* USER CODE END 0 */

/* External variables --------------------------------------------------------*/
extern DMA_HandleTypeDef hdma_adc1;
extern ADC_HandleTypeDef hadc1;
extern ADC_HandleTypeDef hadc2;
extern DAC_HandleTypeDef hdac1;
extern DAC_HandleTypeDef hdac3;
extern SPI_HandleTypeDef hspi1;
extern TIM_HandleTypeDef htim1;
extern TIM_HandleTypeDef htim6;
/* USER CODE BEGIN EV */

/* USER CODE END EV */

/******************************************************************************/
/*           Cortex-M4 Processor Interruption and Exception Handlers          */
/******************************************************************************/
/**
  * @brief This function handles Non maskable interrupt.
  */
void NMI_Handler(void)
{
  /* USER CODE BEGIN NonMaskableInt_IRQn 0 */

  /* USER CODE END NonMaskableInt_IRQn 0 */
  /* USER CODE BEGIN NonMaskableInt_IRQn 1 */
   while (1)
  {
  }
  /* USER CODE END NonMaskableInt_IRQn 1 */
}

/**
  * @brief This function handles Hard fault interrupt.
  */
void HardFault_Handler(void)
{
  /* USER CODE BEGIN HardFault_IRQn 0 */

  /* USER CODE END HardFault_IRQn 0 */
  while (1)
  {
    /* USER CODE BEGIN W1_HardFault_IRQn 0 */
    /* USER CODE END W1_HardFault_IRQn 0 */
  }
}

/**
  * @brief This function handles Memory management fault.
  */
void MemManage_Handler(void)
{
  /* USER CODE BEGIN MemoryManagement_IRQn 0 */

  /* USER CODE END MemoryManagement_IRQn 0 */
  while (1)
  {
    /* USER CODE BEGIN W1_MemoryManagement_IRQn 0 */
    /* USER CODE END W1_MemoryManagement_IRQn 0 */
  }
}

/**
  * @brief This function handles Prefetch fault, memory access fault.
  */
void BusFault_Handler(void)
{
  /* USER CODE BEGIN BusFault_IRQn 0 */

  /* USER CODE END BusFault_IRQn 0 */
  while (1)
  {
    /* USER CODE BEGIN W1_BusFault_IRQn 0 */
    /* USER CODE END W1_BusFault_IRQn 0 */
  }
}

/**
  * @brief This function handles Undefined instruction or illegal state.
  */
void UsageFault_Handler(void)
{
  /* USER CODE BEGIN UsageFault_IRQn 0 */

  /* USER CODE END UsageFault_IRQn 0 */
  while (1)
  {
    /* USER CODE BEGIN W1_UsageFault_IRQn 0 */
    /* USER CODE END W1_UsageFault_IRQn 0 */
  }
}

/**
  * @brief This function handles System service call via SWI instruction.
  */
void SVC_Handler(void)
{
  /* USER CODE BEGIN SVCall_IRQn 0 */
#if defined(BLDC_G431_OBSERVE_ENTRY)
  g_target_runtime_irq_entries[0]++;
#endif

  /* USER CODE END SVCall_IRQn 0 */
  /* USER CODE BEGIN SVCall_IRQn 1 */

  /* USER CODE END SVCall_IRQn 1 */
}

/**
  * @brief This function handles Debug monitor.
  */
void DebugMon_Handler(void)
{
  /* USER CODE BEGIN DebugMonitor_IRQn 0 */
#if defined(BLDC_G431_OBSERVE_ENTRY)
  g_target_runtime_irq_entries[1]++;
#endif

  /* USER CODE END DebugMonitor_IRQn 0 */
  /* USER CODE BEGIN DebugMonitor_IRQn 1 */

  /* USER CODE END DebugMonitor_IRQn 1 */
}

/**
  * @brief This function handles Pendable request for system service.
  */
void PendSV_Handler(void)
{
  /* USER CODE BEGIN PendSV_IRQn 0 */
#if defined(BLDC_G431_OBSERVE_ENTRY)
  g_target_runtime_irq_entries[2]++;
#endif

  /* USER CODE END PendSV_IRQn 0 */
  /* USER CODE BEGIN PendSV_IRQn 1 */

  /* USER CODE END PendSV_IRQn 1 */
}

/**
  * @brief This function handles System tick timer.
  */
void SysTick_Handler(void)
{
  /* USER CODE BEGIN SysTick_IRQn 0 */
#if defined(BLDC_G431_OBSERVE_ENTRY)
  g_target_runtime_irq_entries[3]++;
#endif

  /* USER CODE END SysTick_IRQn 0 */
  HAL_IncTick();
  /* USER CODE BEGIN SysTick_IRQn 1 */

  /* USER CODE END SysTick_IRQn 1 */
}

/******************************************************************************/
/* STM32G4xx Peripheral Interrupt Handlers                                    */
/* Add here the Interrupt Handlers for the used peripherals.                  */
/* For the available peripheral interrupt handler names,                      */
/* please refer to the startup file (startup_stm32g4xx.s).                    */
/******************************************************************************/

/**
  * @brief This function handles DMA1 channel1 global interrupt.
  */
void DMA1_Channel1_IRQHandler(void)
{
  /* USER CODE BEGIN DMA1_Channel1_IRQn 0 */

  /* USER CODE END DMA1_Channel1_IRQn 0 */
  HAL_DMA_IRQHandler(&hdma_adc1);
  /* USER CODE BEGIN DMA1_Channel1_IRQn 1 */

  /* USER CODE END DMA1_Channel1_IRQn 1 */
}

/**
  * @brief This function handles ADC1 and ADC2 global interrupt.
  */
void ADC1_2_IRQHandler(void)
{
  /* USER CODE BEGIN ADC1_2_IRQn 0 */
  realtime_motor_io_adc_irq_enter();
#if defined(BLDC_G431_TIMING_ENTRY)
  g431_runtime_timing_irq_enter(DWT->CYCCNT);
#elif defined(BLDC_G431_OBSERVE_ENTRY)
  uint32_t timing_request = g_target_runtime_compute_request;
  bool timing_ready = (g_target_runtime_compute_sequence & 1U) == 0U &&
      g_target_runtime_compute_result.state == OBSERVE_COMPUTE_PROBE_COMPLETE &&
      g_target_runtime_compute_result.request_sequence == timing_request;
  bool timing_window = (g_target_irq_timing_result.state == G431_IRQ_TIMING_ACTIVE) ||
      (timing_ready && timing_request != 0U &&
      (timing_request != g_target_irq_timing_result.request_sequence ||
       g_target_irq_timing_result.state == G431_IRQ_TIMING_ACTIVE));
  g_target_runtime_irq_entries[4]++;
  if (timing_window) {
    uint32_t timing_begin = DWT->CYCCNT;
    g431_irq_timing_irq_enter(timing_begin, timing_ready ? timing_request : 0U,
        target_runtime_timing_permitted());
  }
#endif

  /* USER CODE END ADC1_2_IRQn 0 */
  HAL_ADC_IRQHandler(&hadc1);
  HAL_ADC_IRQHandler(&hadc2);
  /* USER CODE BEGIN ADC1_2_IRQn 1 */
#if defined(BLDC_G431_TIMING_ENTRY)
  if (g_target_runtime_timing_active != 0U) {
    g431_runtime_timing_irq_exit(DWT->CYCCNT, timing_entry_permitted());
  }
#elif defined(BLDC_G431_OBSERVE_ENTRY)
  if (timing_window) {
    uint32_t timing_end = DWT->CYCCNT;
    g431_irq_timing_irq_exit(timing_end, target_runtime_timing_permitted());
  }
#endif
  /* 覆盖 HAL 与诊断尾部；UP IRQ 不得在旧 ADC 服务未退出时重新开放采样。 */
  realtime_motor_io_adc_irq_exit();

  /* USER CODE END ADC1_2_IRQn 1 */
}

/**
  * @brief This function handles TIM1 break interrupt and TIM15 global interrupt.
  */
void TIM1_BRK_TIM15_IRQHandler(void)
{
  /* USER CODE BEGIN TIM1_BRK_TIM15_IRQn 0 */
  /* HAL 的 TIM 总入口也会清 UIF；先将同时到达的 UEV 交回 Platform 所有者。 */
  realtime_motor_io_update_irq();
  uint32_t update_irq_enabled = TIM1->DIER & TIM_DIER_UIE;
  /* 保留新到 UIF，避免通用 HAL 在两次读取间吞掉下一次更新。 */
  CLEAR_BIT(TIM1->DIER, TIM_DIER_UIE);
#if defined(BLDC_G431_OBSERVE_ENTRY)
  g_target_runtime_irq_entries[5]++;
#endif

  /* USER CODE END TIM1_BRK_TIM15_IRQn 0 */
  HAL_TIM_IRQHandler(&htim1);
  /* USER CODE BEGIN TIM1_BRK_TIM15_IRQn 1 */
  if (update_irq_enabled != 0U) {
    SET_BIT(TIM1->DIER, TIM_DIER_UIE);
  }
  /* USER CODE END TIM1_BRK_TIM15_IRQn 1 */
}

/**
  * @brief This function handles TIM1 update interrupt and TIM16 global interrupt.
  */
void TIM1_UP_TIM16_IRQHandler(void)
{
  /* USER CODE BEGIN TIM1_UP_TIM16_IRQn 0 */
  realtime_motor_io_update_irq();
  uint32_t update_irq_enabled = TIM1->DIER & TIM_DIER_UIE;
  CLEAR_BIT(TIM1->DIER, TIM_DIER_UIE);
  /* USER CODE END TIM1_UP_TIM16_IRQn 0 */
  HAL_TIM_IRQHandler(&htim1);
  /* USER CODE BEGIN TIM1_UP_TIM16_IRQn 1 */
  if (update_irq_enabled != 0U) {
    SET_BIT(TIM1->DIER, TIM_DIER_UIE);
  }
  /* USER CODE END TIM1_UP_TIM16_IRQn 1 */
}

/**
  * @brief This function handles SPI1 global interrupt.
  */
void SPI1_IRQHandler(void)
{
  /* USER CODE BEGIN SPI1_IRQn 0 */
#if defined(BLDC_G431_OBSERVE_ENTRY)
  g_target_runtime_irq_entries[6]++;
#endif
  /* 私有有限长度 SSI 事务独占 SPI1，中断内不得进入 HAL 的等待收尾路径。 */
  position_observation_irq();
  /* CubeMX 保留通用入口；生成区由条件编译排除，避免再次分发同一 IRQ。 */
#if 0
  /* USER CODE END SPI1_IRQn 0 */
  HAL_SPI_IRQHandler(&hspi1);
  /* USER CODE BEGIN SPI1_IRQn 1 */
#endif
  /* USER CODE END SPI1_IRQn 1 */
}

/**
  * @brief This function handles TIM6 global interrupt, DAC1 and DAC3 channel underrun error interrupts.
  */
void TIM6_DAC_IRQHandler(void)
{
  /* USER CODE BEGIN TIM6_DAC_IRQn 0 */
#if defined(BLDC_G431_OBSERVE_ENTRY)
  g_target_runtime_irq_entries[7]++;
#endif
#if defined(BLDC_G431_TIMING_ENTRY)
  timing_entry_guard_irq();
#elif defined(BLDC_G431_OBSERVE_ENTRY)
  target_runtime_guard_irq();
#else
  target_validation_guard_irq();
#endif
  /* USER CODE END TIM6_DAC_IRQn 0 */
  HAL_TIM_IRQHandler(&htim6);
  HAL_DAC_IRQHandler(&hdac1);
  HAL_DAC_IRQHandler(&hdac3);
  /* USER CODE BEGIN TIM6_DAC_IRQn 1 */

  /* USER CODE END TIM6_DAC_IRQn 1 */
}

/* USER CODE BEGIN 1 */

/* USER CODE END 1 */
