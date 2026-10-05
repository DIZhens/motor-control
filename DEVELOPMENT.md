# 开发与验证规则

本文档是本仓库开发方式的唯一正式合同。它只回答“我们怎样开发、分工和验证”，不定义 Motor、Host 或协议本身的业务架构；系统长期设计以 `ARCHITECTURE.md` 为准。

## 0. AI 会话启动协议

本文件是本仓库开发与协作方式的事实来源。新的 ChatGPT 聊天在首次处理本仓库工程任务前，必须先读取当前活动分支上的 `DEVELOPMENT.md`，不能只依据长期记忆、历史聊天总结或过去的协作方式继续工作。

如果任务涉及系统架构、模块边界、公共接口、源码实现或长期约束，同时读取根目录 `ARCHITECTURE.md`。

规则和事实的使用优先级：

```text
当前仓库正式文档
    ↓
当前活动分支源码与配置
    ↓
已经形成的验证事实
    ↓
聊天历史与长期记忆
```

记忆用于恢复上下文和减少重复讨论，不作为当前工程规则的唯一依据。活动分支发生变化，或 `DEVELOPMENT.md` / `ARCHITECTURE.md` 更新后，应重新读取相关文件。读取完成后无需向用户复述整份规则，直接按当前规则工作。

## 1. 文档职责

```text
README.md
    仓库入口和导航

ARCHITECTURE.md
    系统长期架构合同

DEVELOPMENT.md
    Git、工具链、协作分工、验证与代码风格

hardware/*/HARDWARE_BASELINE.md
    物理硬件事实

interface/
    Firmware / Host 共享的 Device / Protocol Contract

各主要目录 README.md
    对应区域的职责、边界、构建入口和已冻结基础能力
```

一次性的调试过程、完整日志、临时错误和测试流水账不进入长期文档。只有具有明确复用价值的验证流程，才优先固化为脚本、测试应用或一条明确的 verify 命令；一次性本地自动执行不要求为了形式留下脚本。

不维护持续增长的 `STATUS.md`、`ROADMAP.md`、`CHANGELOG.md` 或重复状态文档。阶段变化由 Git 分支、提交、合并检查点和可重复验证保存。

## 2. 协作分工

当前 BLDC/PMSM 阶段只要求两个主要工作面能够稳定并行，不把“所有模块都能独立并行”作为架构目标：

```text
                  Shared Contract
          Architecture + interface/
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
        Motor Firmware             PC Host
        ChatGPT 主责               Codex 主责
```

共享契约包含设备公共语义、线协议合同以及会同时影响 Firmware / Host 的长期接口约束。共享契约由 ChatGPT 负责最终冻结和维护，但不要求由 ChatGPT 单方面闭门设计；涉及 Host 实现可行性、事务模型、异步 Telemetry、重连、mock/test 等问题时，应主动让 Codex 从 Host 侧参与评审并提出修改建议。

Device Contract / Protocol Contract 不属于 Motor 内部实现，也不属于 Host 私有实现。它们位于 Firmware ↔ Host 的共享边界：

```text
Motor business semantics
        ↓
Device Contract
        ↓
Protocol Contract
        ↓
Firmware Communication / Host Protocol Client
```

两端不需要等整个共享契约一次性全部冻结后才开始实现。当前采用按功能纵向切片（vertical slice）推进：某个 Host 功能所依赖的 Device / Protocol Contract 已经明确到足以实现后，该切片即可由 Codex 主导 Host 侧实现；后续切片继续按相同方式冻结和推进。

固件与 Host 内部如何继续拆分，以各自最自然、最容易维护为准，不为了并行开发人为制造接口。

### 2.1 ChatGPT

ChatGPT 主要负责：

- 需求澄清、外部调研、方案判断和总体架构；
- `control/` 第一方源码；
- `firmware/` 第一方源码与 Firmware 架构，包括 Motor、ControlRuntime、Communication 固件端、SystemRuntime、Platform contract 与 Target 集成设计；
- `interface/` 中跨 Firmware / Host 的 Device / Protocol Contract，并负责最终冻结；
- 在 Protocol Contract 冻结前主动吸收 Codex 的 Host 侧实现评审，不把“最终冻结权”理解为单方面设计；
- 根目录正式文档；
- GitHub 远端分支、文件、PR、Issue 等仓库操作；
- Firmware 与共享接口的代码审查；
- 测试设计和验收标准；
- 判断某次修改需要 L0 / L1 / L2 中哪一级验证；
- 根据用户或 Codex 返回的本地结果继续修改设计或源码；
- 审查 Codex 的 Host 实现是否违反共享合同或形成第二套设备语义。

能由仓库内容、代码推理和已有验证事实确定的 Firmware 问题，直接由 ChatGPT 完成，不因为 Codex 也能写 C 代码就把 Firmware 的普通第一方源码实现转交 Codex。

ChatGPT 不因为能够编写 GUI 就默认接管 `host/` 的日常实现；当前阶段 Host 是 Codex 的主要开发工作面。

### 2.2 Codex

Codex 当前有两个职责。

第一，**主责 `host/` 的设计、实现和本地验证**。这里的“开始主导”按功能切片判断，而不是等待整个 Host / Protocol 全部设计完成：只要某个 Host 功能依赖的 Device / Protocol Contract 已经冻结到足以实现，该切片之后的 Host 内部架构、代码、测试和本地闭环就由 Codex 主导。Codex 可以直接推进：

- Python / PySide6 / Qt6 工程组织；
- Qt Widgets GUI；
- Host Device API；
- Protocol Client；
- Transport 实现；
- Telemetry、绘图和数据保存；
- mock device / fake transport；
- pytest 和 Host 自动测试；
- Host 本地依赖、虚拟环境和运行入口；
- Host 内部必要的重构和实现细节。

Host 内部实现不需要等待 ChatGPT 逐文件编写。Codex 应主动利用本地源码、解释器、测试框架、CLI/API 等能力形成“修改 → 测试 → 修正”的闭环，而不是退化成只执行 ChatGPT 给出的机械步骤。

在 Protocol Contract 尚未冻结时，Codex 不是被动等待者。ChatGPT 可以把 Device / Protocol 草案交给 Codex 做 Host-side design review；Codex 应从 Host 实现角度主动检查：

```text
codec 是否自然
request / response / error transaction 是否清楚
async telemetry 是否与同步事务冲突
timeout / reconnect 语义是否有歧义
stream framing 是否容易 resync
mock / fake / pytest 是否容易建立
是否存在只为 Firmware 方便而给 Host 增加的不必要复杂度
```

Codex 可以提出共享合同修改建议，但不能把这些建议直接作为 Host 私有事实落地。如果建议会改变 Firmware 与 Host 都必须遵守的业务语义、消息、单位、状态、事务或 framing，应停止扩大实现并交回 ChatGPT 决策；合同冻结后再同步两端。

第二，Codex 继续作为本地执行代理。凡是任务必须依赖用户本地环境、又不属于用户熟练操作白名单，并且可以通过可靠机器接口完成，默认交给 Codex。可用入口包括但不限于：

- 命令行；
- API / SDK / DLL；
- CLI；
- 调试器；
- MCP；
- 编译器和构建工具；
- 串口、网络或测试工具；
- 临时 PowerShell / Python 命令；
- 已存在或新建立的自动化脚本。

Codex 不单方面改变：

- `ARCHITECTURE.md` 中的长期架构；
- Firmware 公共业务语义；
- Hardware Capability Contract；
- `interface/` 的 Device / Protocol Contract。

判断边界时使用一个简单规则：

```text
如果一个决定改变后 Firmware 与 Host 都必须一起修改
    → Shared Contract，由 ChatGPT 最终冻结

如果一个决定只改变 Host 内部实现
    → Host internal，由 Codex 自主决定
```

例如 message ID、packet header、CRC、transaction ID、error code、Telemetry message layout、公共单位和枚举属于 Shared Contract；Python class 划分、Qt Signal 组织、reader thread / asyncio 选择、Host buffer、重连实现细节属于 Host internal。

如果 Host 实现发现共享合同不足、存在歧义或必须改变，先形成最小问题描述并交回 ChatGPT 决策；合同确定后再同步两端实现。

Codex 是否接手一次本地任务，不以“是否已有脚本”为条件。一次性任务可以临时组合命令、API 或工具完成。只有当流程会重复、步骤较多且容易出错、需要统一验收入口，或多个阶段会复用时，才进一步固化为长期脚本或 verify 命令。

### 2.2.1 Codex 任务下发方式

ChatGPT 给 Codex 下发工程任务时，优先描述目标、必要事实、修改边界和验收结果，不把 Codex 当作机械命令执行器。除非某个具体操作本身属于工程合同，否则不预先规定不必要的逐命令、逐行或逐点击步骤。

Codex 当前具备较强的本地工程推理与执行能力。任务边界明确后，允许 Codex 根据本机实际环境自行：

```text
搜索和理解相关源码 / 配置
查询 CLI / API / SDK / MCP / 工具帮助
选择具体实现与自动化方式
处理普通执行层错误
完成 修改 → 检查 → 测试 → 修正 的本地闭环
```

任务说明优先包含：

```text
目标
当前必要事实
允许修改的范围
必须保持的冻结边界
明确不做的范围
PASS 验收条件
需要停止并回报的条件
```

以下情况应停止扩大修改并回报，不自行改变设计：

```text
与 ARCHITECTURE.md / DEVELOPMENT.md / 硬件事实冲突
需要改变公共接口、Hardware Capability Contract 或长期架构
必须突破本次冻结边界
存在无法安全处理的用户本地修改
涉及真实硬件风险
工具实际能力无法满足任务目标
```

普通 CLI、路径、脚本语法、构建调用等执行层问题由 Codex 自行诊断和处理，不因这类问题退化为逐步等待 ChatGPT 指令。

成功时只要求简洁汇报结果、关键修改和 PASS / FAIL，不默认回传完整命令流水账和长日志；失败时提供第一个关键失败点和最小必要证据。

为了便于用户直接复制，ChatGPT 在聊天中给出的单个 Codex 任务正文统一放在**一个完整的可复制代码块**中；代码块外只保留必要的简短说明。

### 2.3 用户

用户负责两类工作。

第一类是当前无法可靠机器化的操作：

- 接线、供电、换板和机械操作；
- 示波器、万用表等需要人工参与的测量；
- 涉及真实硬件风险的安全确认；
- 没有可靠 CLI / API / 调试接口、只能人工完成的 GUI 操作。

不能仅因为某个工具表现为 GUI 就直接判定必须由用户操作。对于尚未确认能力的工具，应先确认是否存在 CLI、API、SDK、DLL、配置文件接口或其他可靠自动化入口。

第二类是“用户熟练操作白名单”。这些流程即使技术上可以自动执行，也继续由用户负责，因为用户已经能够低成本、可靠地完成。

当前白名单：

- **本地 Git** 日常操作，包括 `status`、`diff`、`add`、`commit`、普通 `pull / push` 等；
- Keil 普通 Rebuild，以及 Error / Warning 检查；
- 日常固件烧录 / download；
- 日常运行已有脚本、verify 命令并查看 PASS / FAIL 或最小必要结果；
- 日常串口 monitor / 日志读取；
- 用户以后明确加入的其他熟练流程。

这里的 Git 白名单只约束用户本机上的日常 Git 工作流，不限制 ChatGPT 通过 GitHub 维护远端仓库，也不限制 Codex 在 Host 开发时修改、测试本地工作树。

用户可以随项目推进扩大白名单。当某个原本由 Codex 执行的流程已经稳定、简单且用户认为不会因为人工操作产生实际风险时，可以转为用户日常操作。

白名单针对一个明确的熟练流程，不自动覆盖多个工具组合起来的复杂自动化。例如用户分别熟悉 build、flash 和串口 monitor，并不意味着“自动 build → flash → 抓日志 → 执行测试 → 分析结果”的组合流程也必须人工完成。

活动开发分支首次创建、切换或发生阶段性变化后，如果需要核对用户本地状态，可以让 Codex 做一次本地分支和工作区检查；日常本地工作区干净检查继续由用户自己完成。

正常情况下用户只需要回传 PASS / FAIL；失败时再保留关键错误和最小必要日志。

## 3. Git 规则

- `main` 表示已经形成明确验证边界、可以作为下一阶段起点的稳定检查点。
- 一个阶段只维护一条活动开发分支。
- 活动分支对应**阶段级能力闭环**，而不是单个功能、单个对象、一次重构或小里程碑。
- 创建新的活动分支前，先明确三个边界：目标能力、完成条件、明确不做的范围。
- 子任务和中间里程碑默认通过 commit 与针对性验证推进；单个节点 PASS 不作为合并 `main` 的理由。
- 如果后续工作仍服务于同一阶段目标，原则上继续使用当前活动分支，不因为进入下一个子任务就新建分支。
- 只有当分支最初定义的阶段目标已经完成，并形成可作为后续开发基础的稳定能力后，才默认合回 `main`。
- 如果项目方向发生明显变化、后续工作已属于另一个工程阶段，或继续推进会混入明显无关工作，可以提前结束当前分支并重新定义阶段。
- 不使用 Git worktree 维护多个并行本地工程环境。
- Firmware 与 Host 可以在同一活动阶段分支上并行修改各自目录；不因为两边并行就强制拆成两个长期分支。
- 只有存在明确“修改 → 影响边界 → 风险”时，才要求重测已经通过的能力。
- Git 提交标题和正文、PR 标题与说明统一以中文描述改动及原因；代码标识符、路径、命令和必要术语保留原文。不因风格调整重写历史提交。

活动分支名称不写入长期开发合同，以实际 Git 状态为准；长期规则只要求一个阶段维护一条活动开发分支。

## 4. 验证分层

### 4.1 L0：构建与静态验证

适用于普通代码修改、重构、目录整理和接口调整。

默认只做：

- 受影响正式工程的全量构建；
- 必要静态检查；
- 生成物、依赖或工程成员检查；
- Host 对应的快速自动测试。

执行者继续遵守第 2 节职责边界：

- 用户熟练操作白名单内的普通 Keil Rebuild、本地 Git 检查由用户完成；
- Host 的 pytest、静态检查、依赖与运行验证由 Codex 主责；
- 其他必须依赖本地环境且能够可靠机器化的 L0 检查由 Codex 执行。

### 4.2 L1：针对性验证

只有修改直接影响某个行为或模块时，才验证本次受影响部分。

优先复用已有自动测试，例如：

- `control/` 的 host-side algorithm test；
- Protocol codec / test vector；
- Host mock/fake tests；
- UART HIL；
- Firmware 针对性实机验证。

执行者继续遵守第 2 节职责边界：用户熟练操作白名单内的烧录、运行已有 verify、查看 PASS / FAIL 等流程由用户完成；其他需要本地环境且可以机器化的验证由 Codex 完成。

### 4.3 L2：完整实机回归

只用于：

- 阶段里程碑；
- 关键硬件路径变化；
- 电流采样、PWM、保护等安全/功率路径变化；
- 重要接口或阶段准备冻结。

L2 不作为每次提交默认步骤。

已经通过的测试不会因为任意代码变化自动失效。要求重测时必须能说明具体的“修改 → 影响边界 → 风险”链路，不因“理论上可能有问题”就扩大测试范围。

## 5. STM32 配置与工具链权威

STM32 静态 MCU 配置以 `*.ioc` 为权威，由本地 CubeMX 修改和重新生成。CubeMX 自动生成区域不由第一方代码直接改写，USER CODE 区可以正常维护。

当前正式 STM32 开发方式继续使用已经长期验证且用户熟悉的：

```text
STM32CubeMX
Keil MDK
ST-Link
```

STM32CubeCLT 保留为命令行检查、自动化和后续 verify 能力的辅助工具，不替代当前 Keil 主开发链。

当前不推进 CubeMX + VS Code / CMake 的开发环境迁移，也不因为工具现代化而搬动已经稳定工作的 CubeMX / Keil 工程目录。现阶段电机控制功能推进优先级高于开发环境迁移。

每块 Target 保留完整、正常的 CubeMX / Keil 工程：

```text
firmware/targets/<target>/
├── *.ioc
├── Core/
├── Drivers/
├── MDK-ARM/
└── platform/
```

文件系统与 Git 是第一方源码的事实来源；`MDK-ARM/*.uvprojx` 等 Keil 工程文件只承担当前构建所需的工程成员、Include Path、Target 和调试配置。Keil 左侧 Group 是构建工程的虚拟组织，不代表软件架构。

新增参与 Firmware 构建的 `.c` 文件时，需要在本地 Keil 工程中同步加入并完成真实 Rebuild。

Keil 工程文件的版本控制边界沿用旧工程已验证规则：

- `*.uvprojx` 必须跟踪，它承载正式构建配置；
- `*.uvoptx` 默认也继续跟踪，因为实际使用中它可能承载 ST-Link Download / Debug 所需配置；
- `*.uvoptx` 同时包含探针 ID、窗口、树展开等本机状态，提交前只审查是否混入无意义会话变化，不因噪声而整体忽略；
- 根目录 `.gitattributes` 固定 `*.uvprojx` / `*.uvoptx` 为 LF，避免 Windows `core.autocrlf` 与 Keil 重写造成“status 显示修改但 diff 为空”的假 dirty。

未来只有当当前工具链明显阻碍开发效率，或进入适合集中做基础设施升级的阶段时，再单独重新评估 VS Code / CMake 迁移。

### 5.1 Keil 生成物与 Git 边界

Keil / ARMCC 的编译产物和可由工程重新生成的设备包辅助目录不进入 Git。原因不仅是减少仓库噪声，也避免 Git、索引器或其他后台工具扫描这些正在被编译器覆盖写入的文件，干扰本地构建。

默认忽略并保持不跟踪：

```text
Objects/
Listings/
MDK-ARM/<target build output>/
MDK-ARM/build*.log
MDK-ARM/*.lst
MDK-ARM/*.uvguix.*
MDK-ARM/EventRecorderStub.scvd
MDK-ARM/DebugConfig/
MDK-ARM/RTE/
```

继续跟踪正式源码和构建配置，包括：

```text
Core/
Drivers/
*.ioc
*.uvprojx
*.uvoptx
startup_*.s
```

如果历史提交已经跟踪了后来应忽略的生成物，应使用“取消 Git 跟踪但保留本地文件”的方式清理，而不是删除用户本地仍被 Keil 使用的文件。

### 5.2 ST-Link 独占访问

同一时刻只允许一个工具进程占用同一只 ST-Link。

在运行以下任一操作前：

```text
STM32CubeProgrammer GUI
STM32_Programmer_CLI
Keil Download / Debug
其他 ST-Link CLI / debugger
```

先确保其他可能持有该探针的程序已经断开或关闭。若 CLI 出现连接失败，应先排查探针是否被 CubeProgrammer GUI、Keil Debug Session 或其他后台工具占用，再判断 SWD 硬件故障。

该规则只约束探针资源占用，不改变日常烧录仍由用户熟练操作白名单负责的分工。

## 6. Host 工具链与开发规则

当前首选技术基线：

```text
Python
PySide6 / Qt6
Qt Widgets
pyqtgraph
pytest
```

Host 依赖与虚拟环境由 `host/pyproject.toml` 等 Host 自身工程文件管理，不把 Python/Qt 依赖扩散到 Firmware。

Codex 主导 Host 时遵守以下规则：

- GUI 只通过 Host Device API 使用设备，不直接拥有串口、CAN 或协议状态机；
- Protocol Client 不依赖具体 Qt Widget；
- Transport 不理解 Start、Target、Fault、Telemetry 等 Motor 业务语义；
- `interface/` 是 Firmware 与 Host 的共同事实来源，Host 不私自复制一套不同的枚举、单位或状态语义；
- Device API、Protocol、Transport 和主要业务行为优先做到可 headless / mock / pytest 验证；
- GUI 外观和交互中必须人工判断的部分再交给用户确认；
- 不为了“工程化”预建 Manager、Repository、插件系统、通用 Dashboard 编辑器、通用协议编辑器或无真实职责的框架层；
- Host 内部设计允许 Codex自主调整，只要不突破 `ARCHITECTURE.md` 与 `interface/` 的长期边界。

第一方 Python 基础风格：

- 4 空格缩进；
- 模块、函数、变量使用 `lower_snake_case`；
- 类使用 `PascalCase`；
- 常量使用 `UPPER_SNAKE_CASE`；
- 公共 API 优先有明确类型标注；
- 注释 / docstring 解释语义、单位、状态约束和原因，不重复代码表面含义；
- 不把 Qt Widget、Signal 或具体 Transport 类型泄漏进不需要知道它们的 Device/Protocol 核心接口；
- 测试优先验证行为与契约，不为了覆盖率数字制造无价值测试。

## 7. 本地与云端修改边界

- 根目录长期架构合同、`interface/` 共享 Device / Protocol Contract、`control/` 和 Firmware 第一方源码由 ChatGPT 负责维护与审查。
- `host/` 的第一方源码、测试和本地工程配置由 Codex 主责设计、实现和验证。
- Host 如果需要改变共享契约，先交回 ChatGPT 决策，不在 Host 内形成第二套设备语义。
- 用户熟练操作白名单中的 Git 规则只针对用户本机日常 Git 工作流，不限制 ChatGPT 的 GitHub 远端操作，也不限制 Codex 在 Host 开发中修改本地工作树、运行测试和使用本地工具。
- 必须依赖本地工具生成或修改的文件，如果存在可靠的命令、API、CLI、调试器、MCP 或脚本入口，且不属于用户熟练操作白名单，默认由 Codex 执行；只有不能可靠机器化时才交给用户人工操作。
- `*.ioc` 仍以 CubeMX 为唯一配置事实来源；不得因为自动化方便绕过 CubeMX 的配置语义。
- CubeMX 自动生成区只能通过 CubeMX 正常生成。
- `MDK-ARM` 工程文件作为正式 STM32 构建配置保留；由实际 Keil / CubeMX 工具维护，ChatGPT 负责审查必要变化。
- 不因为 GitHub 上 Firmware 源码已经完成，就宣称真实 Keil build / 实机行为已通过。
- 不因为 Host 源码已经提交，就宣称用户本机 Qt 环境、GUI 行为或真实设备联调已通过。

## 8. 验证证据

长期验证证据优先级：

```text
可重复自动测试 / verify
    ↓
明确的 Git commit / merge checkpoint
    ↓
必要的简短验收结论
```

原始串口日志、完整 Hex 报文、一次性 GUI 截图和临时诊断过程只在定位问题时保留，不持续追加到 Markdown。

正常验证只汇报 PASS / FAIL 和必要摘要；失败时再展开最小关键日志。

已经通过的测试不会因为任何代码变化自动作废；要求重测时必须说明具体影响链。

## 9. Firmware 第一方 C 代码风格

Firmware 与 `control/` 第一方 C 代码采用紧凑、主流、便于审查的风格。注释规则继续沿用旧仓库已经形成的写作方式。

### 9.1 基础格式

- 4 空格缩进，不使用 Tab；
- 建议行宽不超过 100 列；超过时按语义拆分，不为了强行压到单行牺牲可读性；
- 文件、函数和变量使用 `lower_snake_case`；
- 项目类型使用 `PascalCase`；
- 宏与枚举常量使用 `UPPER_SNAKE_CASE`；
- 公共函数使用清晰的模块前缀，私有实现优先 `static`。

### 9.2 大括号与控制语句

函数定义的左大括号单独一行：

```c
static void motor_update(void)
{
    ...
}
```

函数内部的控制语句采用紧凑 K&R 风格，左大括号与条件同行：

```c
if (condition) {
    ...
} else {
    ...
}

for (...) {
    ...
}

while (...) {
    ...
}

switch (state) {
    ...
}
```

`struct`、`enum` 等类型定义同样采用紧凑写法：

```c
typedef struct {
    ...
} MotorCommand;

typedef enum {
    MOTOR_STATE_IDLE,
    MOTOR_STATE_RUN,
    MOTOR_STATE_FAULT
} MotorState;
```

所有 `if / else / for / while / do / switch` 分支即使只有一条语句也保留大括号，避免后续修改引入作用域错误。

布尔条件遵循主流 C 写法，不强制写 `== true` / `== false`：

```c
if (current.valid) {
    ...
}

if (!current.valid) {
    ...
}
```

变量命名应尽量避免双重否定，例如不写 `!no_fault`、`!not_ready`；优先使用可以直接正向阅读的 `fault_active`、`ready`、`valid` 等名称。

位标志和数值比较应保持语义明确，例如：

```c
if ((faults & MOTOR_FAULT_OVERCURRENT) != 0U) {
    ...
}
```

### 9.3 空行与排版密度

空行只用于表达真实逻辑段落，例如“声明 → 获取输入 → 校验 → 执行控制”，不因为大括号存在而机械插入空行。

不在 `{` 后或 `}` 前自动增加空行，不为了视觉对齐制造大量无意义空格，也不对变量、赋值号或注释做容易产生大面积 diff 的强制列对齐。

### 9.4 注释规则

注释规则保持不变：

- 注释以中文解释语义、原因和约束，代码标识符、API 和约定俗成的缩写保持英文；
- 第一方 `struct`、`enum` 及其非显然成员必须有中文注释：说明用途，必要时注明单位、状态含义、有效条件或所有权；
- 简单成员优先沿用成员行尾中文注释；复杂约束使用结构体、枚举或函数上方的块注释；
- 不写“保存一个值”“当前状态”这类重复类型或变量名表面含义的空注释；
- 注释用于解释代码本身无法自然表达的工程语义，不用于逐行翻译代码。

第一方公共头文件不无必要泄漏 HAL、CMSIS、FreeRTOS 或具体板级 Peripheral Handle。

CubeMX 生成代码、CMSIS、HAL、ThirdParty 保持上游风格，不为了统一项目风格批量格式化。

不为了风格统一额外制造 Manager、万能 HAL、通用 PlatformOps、RTOS wrapper 或其他没有真实职责的抽象。

Firmware 写作原则不是追求“层数多”或“格式漂亮”，而是让职责、所有权、单位、状态语义和硬件边界从代码本身可读，同时避免无信息量的纵向空白。

## 10. 新模块与自动化产生规则

新模块只有在至少出现以下一种真实条件时建立：

```text
独立职责
独立长期状态
独立变化原因
真实替换边界
```

不因为某个概念重要就自动建立顶层模块，也不因为测试方便就提前制造抽象。

自动化遵守同样原则：

- 一次性操作允许 Codex 临时执行；
- 会反复使用、步骤多且容易出错、多个阶段共享或需要统一 PASS / FAIL 入口时，再固化成脚本或 verify；
- 已有成熟通用工具可以解决的问题，不为了“项目完整”自行重造。
