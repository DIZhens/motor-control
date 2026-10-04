# 开发与验证规则

本文档是本仓库开发方式的正式合同。系统架构以 `ARCHITECTURE.md` 为准。

## 1. 会话启动

新的 ChatGPT 工程聊天首次处理本仓库任务前，读取当前活动分支上的 `DEVELOPMENT.md`。

如果任务涉及架构、模块边界、公共接口、源码实现或长期约束，同时读取 `ARCHITECTURE.md`。

事实优先级：

```text
当前分支正式文档
    ↓
当前分支源码与配置
    ↓
已经形成的验证事实
    ↓
聊天历史与记忆
```

活动分支变化，或上述正式文档更新后，重新读取相关文件。

## 2. 协作分工

当前阶段只要求两个主要工作面能够并行：

```text
                Shared Contract
          interface/ + architecture
                       │
           ┌───────────┴───────────┐
           ▼                       ▼
       Firmware                  PC Host
     ChatGPT 主责               Codex 主责
```

### ChatGPT

主要负责：

- 需求澄清、外部调研和总体架构；
- `control/`；
- `firmware/` 第一方源码与 Firmware 架构；
- `interface/` 共享 Device / Protocol Contract；
- 正式文档、远端 GitHub 操作、代码审查；
- 测试设计和验收标准；
- 审查 Host 是否违反共享合同。

### Codex

主要负责：

- `host/` 的设计、实现和本地验证；
- Qt / PySide6 GUI；
- Host Device API；
- Host Protocol Client；
- Transport 实现；
- Telemetry / plotting / data save；
- mock device、pytest 和其他 Host 本地自动测试；
- 其他必须依赖用户本机、且能通过 CLI / API / SDK / debugger / MCP / script 可靠执行的任务。

Codex 不单方面修改总体架构或共享 Device / Protocol Contract。发现合同不充分或冲突时，交回 ChatGPT 决策。

### 用户

负责：

- 接线、供电、换板和机械操作；
- 示波器、万用表等人工测量；
- 真实硬件风险确认；
- 当前无法可靠机器化的 GUI 操作；
- 日常本地 Git；
- Keil Rebuild 与 Error / Warning 检查；
- 日常固件下载；
- 运行已经存在的 verify / test 并反馈 PASS / FAIL；
- 日常串口日志查看。

不要求为了并行开发给每个小模块都建立接口。只在真实独立变化边界建立接口。

## 3. Git

- `main` 只保存已经形成清晰验证边界的稳定检查点；
- 一个阶段维护一条活动开发分支；
- 阶段分支对应能力闭环，不对应单个函数或小重构；
- 创建阶段分支前明确：目标、完成条件、明确不做的范围；
- 子任务通过 commit 推进；
- 只有阶段目标形成稳定基础后才合回 `main`；
- 不为并行 Firmware / Host 使用多个 worktree；
- Firmware 与 Host 可以在同一阶段分支并行修改各自目录；
- 提交标题和 PR 说明使用中文，代码标识符、路径和必要术语保留原文。

## 4. 验证分层

### L0：构建与静态验证

普通代码修改、重构、接口整理默认执行。

### L1：针对性验证

修改直接影响某个行为时，只验证受影响路径。优先自动化。

### L2：完整实机回归

只用于阶段里程碑、关键硬件路径变化、安全/功率路径变化或重要冻结点。

已经通过的测试不会因为任意代码变化自动失效；要求重测必须存在明确影响链。

## 5. STM32 工具链

正式开发链：

```text
STM32CubeMX
Keil MDK
ST-Link
```

规则：

- `*.ioc` 是 MCU 静态配置事实来源；
- CubeMX 自动生成区域只由 CubeMX 维护；
- USER CODE 区可以维护第一方代码；
- 每个板 Target 保存完整 CubeMX / Keil 工程；
- Git 真实目录决定软件职责，Keil Group 不代表软件架构；
- 新增参与构建的 `.c` 文件后，需要同步加入实际 Keil 工程并真实 Rebuild；
- 当前不为了工具现代化迁移到 VS Code / CMake 主开发链。

## 6. Host 工具链

当前首选：

```text
Python
PySide6 / Qt6
Qt Widgets
pyqtgraph
pytest
```

Host 依赖与虚拟环境由 `host/pyproject.toml` 等 Host 自身工程文件管理。

GUI 外观可以人工确认，但 Device API、Protocol、Transport 和主要业务行为应尽量可自动测试。

## 7. 文档

长期文档只保留：

```text
README.md
ARCHITECTURE.md
DEVELOPMENT.md
hardware/*/HARDWARE_BASELINE.md
interface/ 中的 Device / Protocol Contract
各主要目录 README.md
```

不维护持续增长的调试流水账、STATUS、ROADMAP 或重复状态文档。

## 8. 第一方 C 风格

- 4 空格缩进，不使用 Tab；
- Allman 大括号；
- 文件、函数、变量使用 `lower_snake_case`；
- 项目类型使用 `PascalCase`；
- 宏与枚举常量使用 `UPPER_SNAKE_CASE`；
- 公共函数使用明确模块前缀，私有实现优先 `static`；
- 中文注释解释语义、原因、单位、状态和约束；
- 公共头文件不无必要泄漏 HAL/CMSIS 等具体框架类型；
- CubeMX / HAL / CMSIS / 第三方源码保持上游风格；
- 不为了形式制造 Manager、万能 HAL、RTOS wrapper 或无真实职责的抽象。
