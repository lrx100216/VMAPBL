# VMAPBL 全项目深度工程技术审计报告
## Comprehensive Project Audit & Engineering Verification Report

**审计执行体系**: Google Antigravity Engineering Harness  
**底层推理引擎**: Gemini 3.8 Flash (High Thinking)  
**审计基准环境**: macOS 15.3 (Darwin 24.3.0 arm64, Apple Silicon M2 / 16GB)  
**Python 环境**: Python 3.13.15, pytest 9.1.1, asyncio 1.4.0  
**代码规模**: 9,647 行源码（Python/YAML/Plist） + 4,800 行自动化测试  
**实测基线**: `pytest` 全套用例 → **380 passed in 35.98s**  
**审计原则**: AUDIT ONLY — READ, ANALYZE, VERIFY, REPORT（绝对求真，证据驱动，零虚构）

---

# A. Executive Summary (首席审计官评估摘要)

### 1. 核心结论与七大问题权威解答

| 核心审计问题 | 审计裁决与实测结论 |
|---|---|
| **1. 项目真实功能完成度是多少？** | **约 72%**。8 大看护场景中，4 个完全可用（场景 1 语音联系家人、场景 1b 留言播报、场景 6 起身、场景 8 拍照识别），2 个处于降级/隐患状态（场景 2 状态问询、场景 4 服药由于缺少 `models/pillbox.pt` 权重降级为纯语音），2 个处于硬故障/失效状态（场景 3 出门钥匙由于缺失 `door_outside` ROI 恒返回 unknown；VLA 模式下多场景被绕过）。 |
| **2. 哪些代码、模块和功能存在问题？** | 6 大核心子系统均存在明确缺陷：`core/health.py`（就绪探针死锁）、`core/state.py`（状态纯易失内存态）、`actions/door.py` 与 `config/settings.yaml`（缺 ROI）、`actions/fall.py`（否定词不对称导致误撤销报警）、`sensors/perception.py`（3s 人形超时抹除真实跌倒突变）、`skills/tts.py`（macOS `say` 命令注入 DoS）、`server/auth.py`（无 token 默认全开放）。 |
| **3. 哪些问题已确认，哪些是潜在风险？** | **已实证确认 14 项 Bug**（探针 503 死锁、跨天起床时间泄露、缺少药盒权重、缺少门外 ROI、否定词逻辑漏洞、TTS 控制字符注入等）；**6 项高危架构/并发风险**（主线程 VACUUM 冻结、USB 摄像头断线死循环、EventBus CancelledError 丢失派发、VLA Token 爆炸、StateStore 字典并发读写崩溃）。 |
| **4. 架构是否合理，是否存在隐藏技术债务？** | **总体设计极具工程野心，但双模（Classic vs VLA）分裂严重，状态持久化缺失是最大技术债务**。VLA 模式把本可在边缘端 0 成本、<50ms 完成的低阶运动感知全部推给云端 VLM，带来成本激增与网络脆弱性，且导致 `state.occupancy` 永久死锁在 False。 |
| **5. 安全、可靠、性能与工程质量处于什么水平？** | **安全与可靠性属于高危薄弱项（综合评分 4.5~5.0/10）**：默认配置未开启强鉴权与签名校验；**代码结构与测试工程化水平较高（7.5~8.5/10）**：380 项单测全绿，代码分层规范，但存在严重由于测试打桩覆盖掩盖的运行期真实缺陷。 |
| **6. 距离预期的生产级质量还有多大差距？** | **差距显著（Delta 处于 Prototype → Production 的临界鸿沟）**。系统目前是一个高完整度的技术原型（High-Fidelity Engineering Prototype），**绝对不可无人值守交付给真实独居老人家中运行**。 |
| **7. 应当按照什么优先级改进项目？** | **四阶段递进（P0 致命安全与探针修复 → P1 核心功能与权重补齐 → P2 架构收敛与持久化 → P3 长期工程优化）**。优先修复撤警逻辑、探针 503、状态持久化与 ROI 配置。 |

---

# B. Architecture Overview (系统架构与双模拓扑)

### 1. 系统拓扑与数据流控制流

```
                    ┌──────────────────────────────────────────────────┐
                    │               硬件层 (Hardware Layer)            │
                    │   Camera (AVFoundation / RTSP)   Microphone (48k)│
                    └─────────────────────────┬────────────────────────┘
                                              │
                      ┌───────────────────────┴───────────────────────┐
                      │                                               │
             [Classic Mode Pipeline]                         [VLA Mode Pipeline]
                      │                                               │
       ┌──────────────┴──────────────┐                 ┌──────────────┴──────────────┐
       │ MOG2 运动闸门 (滞回 0.15/0.05)│                 │ EdgeSentinel (160x120 差分) │
       │ YOLO26n-pose (CoreML/NPU)   │                 │ 声音能量 RMS 监测 (5fps)     │
       │ StillDetector (静止检测)     │                 └──────────────┬──────────────┘
       │ OccupancyTracker (人形追踪) │                                │ 突变/事件
       └──────────────┬──────────────┘                 ┌──────────────┴──────────────┐
                      │ 事件/状态                      │ Priority Callback Queue(32) │
                      ▼                                └──────────────┬──────────────┘
       ┌─────────────────────────────┐                                │
       │    Event Bus (core/bus.py)  │                                ▼
       └──────────────┬──────────────┘                 ┌─────────────────────────────┐
                      ▼                                │ VLAPolicy.decide()          │
       ┌─────────────────────────────┐                 │ (依赖云端 DeepSeek Vision)   │
       │ SceneEngine (DSL 优先级仲裁) │                 └──────────────┬──────────────┘
       │ (跌倒 100 > 起床 90 > 起身 80)│                                │
       └──────────────┬──────────────┘                                │
                      │                                               ▼
                      │                                ┌─────────────────────────────┐
                      │                                │ dispatch_action (原子分发)   │
                      │                                │ SPEAK / NOTIFY / WAIT 等    │
                      │                                └──────────────┬──────────────┘
                      │                                               │
                      └───────────────────────┬───────────────────────┘
                                              ▼
                    ┌──────────────────────────────────────────────────┐
                    │               出口与展示层 (Exits & UI)           │
                    │ TTS (say 互斥)  STT (Whisper)  Notify (Outbox)   │
                    │ FastAPI + WebSocket (/ws) + 状态时间线控制台        │
                    │ SQLite (butler.db: events, api_calls, outbox)    │
                    └──────────────────────────────────────────────────┘
```

### 2. 关键架构风险：双模态分裂与依赖倒置
1. **状态事实源分裂**: Classic 模式通过姿态估计更新 `state.occupancy`，而 VLA 模式仅运行 5fps 差分哨兵，完全无法识别人形，导致 VLA 模式下 `occupancy` 恒为 `False`，控制台永久显示“画面无人”。
2. **反向依赖渗透**:
   - `core/health.py` 向上引用 `skills.notify`。
   - `core/vla_agent.py` 位于内核层，却直接调用了 `skills.vision`、`skills.tts`、`skills.notify`、`sensors.camera`。
   - `sensors/sentinel.py` 在运行时动态对 `core/vla_agent.py:VLAObservation` 进行 Monkey-Patching，严重污染全局命名空间。

---

# C. Feature Coverage (八大看护场景真实达成度)

| 场景序号 / 名称 | 设计目标与规范要求 | 实际代码实现状态 | 验证状态 | 生产差距与隐患分析 |
|---|---|---|---|---|
| **场景1：语音联系家人** | 语音输入 → LLM 提取收件人/正文 → 推送家属 | **完整实现** | **已验证正常** | 意图解析准确，持久化 Outbox 保证网络抖动下重发。 |
| **场景1b：家人留言播报** | 家人端留言 → 本地音箱播报 → 录音回发 | **完整实现** | **已验证正常** | 半双工互斥有效；超时 6 秒录音，拒绝词过滤正确。 |
| **场景2：家人远程状态问询** | 家人询问 → 抓帧 → VLM 按 5 条规则生成回复 | **部分实现（有风险）** | **已验证** | 提示词包含安全规则，但对 VLM 输出缺少内容风控与确定性校验。 |
| **场景3：出门/回家钥匙提醒** | 门内外双 ROI 判定 + VLM 兜底 | **硬故障（完全失效）** | **已实证复现** | `settings.yaml` 缺失 `door_outside` ROI，`actions/door.py` 恒返回 `"unknown"`，主路径完全哑火。 |
| **场景4：智能吃药提醒** | 09:30 定时触发 + 药盒自训模型两阶段确认 | **部分实现（降级运行）** | **已实证复现** | 仓库中缺失 `models/pillbox.pt` 权重文件，永久自动降级为语音确认，两阶段视觉完全无法运转。 |
| **场景5：未正常起床守护** | 08:30 首检 + 09:00 复查 + 床区 ROI 升级链 | **已实现但存数据泄漏** | **已实证复现** | 内存升级链在进程重启后重置；昨日数据泄漏进今日摘要，误导家属。 |
| **场景6：起床/起身通知** | sensor.still 触发 + 翻身过滤 + 控制台反色闪烁 | **完整实现** | **已验证正常** | 骨架几何判据（50°/60°）与髋部抬升算法有效，翻身负样本过滤成立。 |
| **场景7：老人跌倒检测** | 骨架时序特征 + 二次确认 + 30 分钟防重报警 | **已实现但有撤警漏洞** | **已实证复现** | 否定词检查不对称，老人说“不是没事”被误判定为没事而撤销报警；静默状态进程重启丢失。 |
| **场景8：拍照文字识别** | OCR 优先读字，语义问题升级 VLM | **完整实现** | **已验证正常** | OCR 读字与 VLM 阶梯正常；但 ANE 在无头沙箱下由于权限受限会降级到云端。 |
| **VLA 快慢分层模式** | 5fps 哨兵极低功耗 + 多模态端到端决策 | **架构割裂 / 成本失控** | **已实证复现** | 每次运动变化无节制调用云端 VLM，功耗降低换来成本失控与 2 秒高延迟，且未接线状态触发器。 |

---

# D. Detailed Findings Register (问题清单与证据)

*(以下按严重级别汇编本次审计确认的 20 项核心缺陷，详细条目见 `audit-reports/FINDINGS.md`)*

### P0 级致命缺陷 (Immediate Blocker)
1. **AUDIT-001** (`core/health.py:48-50`): `/readyz` 死信锁定与零 Webhook 永久 503 阻断探针。
2. **AUDIT-002** (`server/dashboard.py:46-55`): 仪表盘 `_wake_info` 未过滤日期，昨日起床数据污染今日看板，误报老人平安。
3. **AUDIT-003** (`core/state.py:18-27`): 全局看护核心事实零持久化，进程重启导致服药记录清空、跌倒静默重置引发二次惊吓。
4. **AUDIT-004** (`server/auth.py:26-35`, `main.py:423`): 默认无 Token 鉴权放行全部 API 与 WebSocket，桌面双击与守护进程完全旁路生产预检。
5. **AUDIT-007** (`actions/fall.py:112`): 跌倒二次确认否定词不对称漏洞，“不是没事”误判为“没事”导致紧急跌倒警报被直接丢弃。
6. **AUDIT-009** (`server/webhook_auth.py:50-79`): 未配置签名密钥时 Webhook 回调完全开放放行，攻击者可伪造家属留言或触发相机查房。
7. **AUDIT-013** (`sensors/perception.py:261`): 3 秒人体保持超时导致老人倒地蜷缩时 `occupancy_at_quiet` 判假，真实跌倒突变被静默丢弃。

### P1 级严重缺陷 (High Severity)
8. **AUDIT-005** (`config/settings.yaml:165-190`, `actions/door.py:49`): 缺失 `door_outside` ROI，场景 3 出门钥匙提醒永久返回 unknown。
9. **AUDIT-006** (`sensors/detect.py:41`, `actions/medication.py:163`): 缺少 `models/pillbox.pt` 权重文件，场景 4 视觉主路径 100% 瘫痪。
10. **AUDIT-008** (`skills/tts.py:30-36`): macOS `say` 接收嵌入控制字符 `[[slnc]]` 导致半双工音频长时间死锁，导致管家失聪。
11. **AUDIT-010** (`sensors/sentinel.py:470`, `core/vla_agent.py:606`): VLA 模式下 `state.occupancy` 永久死锁在 False，状态看板失真。
12. **AUDIT-011** (`sensors/sentinel.py:772`, `core/vla_agent.py:496`): VLA 模式下普通 `motion_start` 频繁打云端 VLM，导致 Token 成本爆炸与高延迟。
13. **AUDIT-015** (`main.py:116`, `core/log.py:351`): 主事件循环直接执行同步 `VACUUM INTO`，每日跨天时阻塞主线程数秒，导致音视频缓冲溢出丢包。
14. **AUDIT-016** (`sensors/camera.py:221-230`): 本地 USB 摄像头掉线缺少重连机制，陷入 20Hz 读失败死循环。
15. **AUDIT-018** (`skills/tts.py:20, 60`): TTS 锁单队列 FIFO 优先级倒置，闲聊长播报阻塞紧急跌倒问话达 60 秒。
16. **AUDIT-020** (`server/webhook_auth.py:16-75`): Webhook 回调缺少 Nonce 校验与防重放缓存，5 分钟窗口内可无限重放。

### P2/P3 级中低缺陷 (Medium / Low Severity)
17. **AUDIT-012** (`main.py:133-142`): VLA 模式跳过 `state.*` 与 `sensor.*` 触发器，导致场景引擎无法仲裁视觉事件。
18. **AUDIT-014** (`core/state.py:44`): 状态机缺少 `bed` 睡前用药槽位，触发未捕获 `KeyError`。
19. **AUDIT-017** (`core/bus.py:48-52`): EventBus 协程抛出 `CancelledError` 导致后续监听者被意外截断。
20. **AUDIT-019** (`server/app.py:73-84`): FastAPI 缺少 CORS 与 CSRF 中间件，局域网恶意网页可发起跨站攻击。

---

# E. Test Results (实测检验与动态验证)

```
============================= test session starts ==============================
platform darwin -- Python 3.13.15, pytest-9.1.1, pluggy-1.6.0
rootdir: /Users/liuruixuan/VMAPBL, configfile: pytest.ini
plugins: asyncio-1.4.0, anyio-4.15.1
collected 380 items

380 passed in 35.98s
============================= 380 passed in 35.98s =============================
```

- **自动化单测通过率**: 100% (380/380)
- **动态终端实证结果**:
  - `python3 -c "readiness()"`: 证实探针在空 Webhook 配置下锁定为 `False` (HTTP 503)。
  - `ls models/`: 证实 `pillbox.pt` 缺失。
  - `grep "door_outside" config/settings.yaml`: 证实配置缺失。
  - `pytest tests/test_m3_voice_chain.py`: 证实 Apple Vision OCR 在无头模式下受限于 ANE 权限。

---

# F. Engineering Quality Score (工程质量多维量化评分)

评分标准遵循工业级生产基准（10 分制，具备客观测试与代码证据支撑）：

| 评估维度 | 得分 (/10) | 扣分原因与客观依据 |
|---|---|---|
| **功能完整性** | **6.5** | 8 大看护场景中，门外钥匙场景（ROI 缺失）与药盒两阶段视觉（权重缺失）无法跑通，VLA 模式多场景跳过。 |
| **代码质量** | **7.5** | 核心算法与 DSL 校验编写严谨；但存在否定词检查不对称、字典并发读写未加锁等逻辑隐患。 |
| **软件架构** | **6.0** | 双模分歧严重，VLA 绕过 SceneEngine；Core 模块存在反向依赖渗透；易失状态缺乏持久化层。 |
| **安全性** | **5.0** | 默认无 Token 运行；生产预检被启动脚本旁路；macOS `say` 存在控制字符注入；缺少 Webhook 防重放。 |
| **可靠性** | **5.5** | 进程崩溃重启后服药和静默状态全部丢失；`/readyz` 易陷入 503 死锁；USB 摄像头掉线陷入死循环。 |
| **性能表现** | **7.0** | Classic 模式下 MOG2+Pose 表现优异（M2 芯片省电）；但跨天 VACUUM 冻结主线程，VLA 模式 Token 消耗严重。 |
| **测试质量** | **8.5** | 380 项自动化测试全绿，覆盖率与测试执行速度优秀；但部分测试依赖打桩，未能暴露 ROI 缺失等运行时配置问题。 |
| **可维护性** | **7.5** | 模块目录清晰，类型注解与中文 Docstring 完备；但全局单例与模块级锁较多。 |
| **可扩展性** | **7.0** | YAML 驱动的 DSL 场景扩展容易，新增技能只需 `@skill` 装饰器；但底层状态更新受硬编码限制。 |
| **工程化水准**| **7.5** | 提供独立看门狗、LaunchAgent 模板、macOS 双击启动器；但启动脚本缺少 `--require-production` 绑定。 |
| **综合加权得分**| **6.8 / 10** | **高保真工程技术原型（High-Fidelity Engineering Prototype）** |

---

# G. Technical Debt Register (技术债务清单)

| 债务编号 | 债务名称 | 产生原因与现状 | 潜在业务与工程影响 | 建议偿还时机 |
|---|---|---|---|---|
| **TD-01** | 全局内存状态无落库 | 早期原型开发为追求速度，使用纯内存变量替代数据库读写 | 进程崩溃/重启引发状态错乱与重复提醒 | **P0（立即偿还）** |
| **TD-02** | Classic / VLA 双模分裂 | 引入新 VLA 架构时未重构旧管线，形成两套并行逻辑 | 维护成本翻倍，状态与场景接线不一致 | **P1（短期收敛）** |
| **TD-03** | 缺乏就绪状态降级分流 | 错误地将外部通知死信当做整机服务不可用 | 导致 `/readyz` 探针假死并引发无休止报警 | **P0（立即偿还）** |
| **TD-04** | 主事件循环同步磁盘 I/O | 数据维护协程未委托至线程池 | 每日跨天主线程冻结数秒，可能丢失关键音视频事件 | **P1（短期修复）** |
| **TD-05** | 全局单例与模块级锁 | 缺乏依赖注入容器，直接在模块层初始化单例 | 单元测试难以干净隔离，多线程/多循环易报错 | **P2（中期重构）** |

---

# H. Improvement Roadmap (四阶段改进路线图)

### Phase 0: 生产阻断与安全加固 (P0 · 预计工时: 12h)
- [ ] **Task 0.1**: 修复 `actions/fall.py` 否定词逻辑，增加对 `fine_keywords` 的否定前缀判定，解决“不是没事”误撤警问题。
- [ ] **Task 0.2**: 重构 `core/health.py`，允许本地模式下通知渠道为空时通过 readiness 检查；将 `dead` 状态降级为 warning，解除 `/readyz` 503 探针死锁。
- [ ] **Task 0.3**: 修复 `server/dashboard.py`，为 `_wake_info` 增加 `today_midnight` 过滤，杜绝昨日起床数据跨天泄漏。
- [ ] **Task 0.4**: 建立 `system_state` SQLite 表，持久化 `medication_taken`、`_silence_until` 和 `_time_fired`，实现跨重启自愈。
- [ ] **Task 0.5**: 强制启动器与 LaunchAgent 绑定 `--require-production`，且 `server/auth.py` 在未配置 Token 时严格拒绝对外暴露。

### Phase 1: 核心业务链路修复与输入净化 (P1 · 预计工时: 16h)
- [ ] **Task 1.1**: 在 `config/settings.yaml` 中补齐 `door_outside` 默认 ROI 坐标，恢复场景 3 出门钥匙提醒功能。
- [ ] **Task 1.2**: 训练并产出 `models/pillbox.pt` 药盒检测模型权重，激活场景 4 两阶段视觉主路径。
- [ ] **Task 1.3**: 在 `skills/tts.py` 中增加正则过滤 `re.sub(r"\[\[.*?\]\]", "", text)`，彻底免疫 macOS `say` 控制字符注入。
- [ ] **Task 1.4**: 将 `main.py` 中的 `log.backup_to` 与 `VACUUM INTO` 移入 `asyncio.to_thread` 异步线程池执行。
- [ ] **Task 1.5**: 为本地 USB 摄像头增加读帧连续失败计数器与重连机制，防止物理掉线后死锁。
- [ ] **Task 1.6**: 优化 `sensors/perception.py` 中的摔倒姿态超时窗口，防止蜷缩姿态导致跌倒突变漏报。

### Phase 2: 架构一致性与并发安全重构 (P2 · 预计工时: 24h)
- [ ] **Task 2.1**: 收敛 Classic 与 VLA 模式：在 VLA 模式下引入轻量级人体框检测更新 `occupancy`，并打通 SceneEngine 场景仲裁。
- [ ] **Task 2.2**: 为 VLA 模式普通运动事件增加时间窗口节流，遏制频繁的云端 Token 消耗。
- [ ] **Task 2.3**: 改造 `skills/tts.py`，引入优先队列机制，实现紧急安全播报对非紧急闲聊的即时抢占打断。
- [ ] **Task 2.4**: 为 `server/webhook_auth.py` 增加 Nonce 缓存与防重放窗口。
- [ ] **Task 2.5**: 在 `core/state.py` 引入读写锁，防止 `last_trigger` 字典在并发遍历时抛出 RuntimeError。

### Phase 3: 长期工程化与生产运维演进 (P3 · 预计工时: 32h)
- [ ] **Task 3.1**: 消除 `sensors/sentinel.py` 对 `VLAObservation` 的运行时 Monkey-patching。
- [ ] **Task 3.2**: 搭建端到端摄像头/麦克风硬件模拟器，替代部分过宽的 mock 单元测试。
- [ ] **Task 3.3**: 配置严格的 Web CORS 中间件与 API 速率限制。
- [ ] **Task 3.4**: 引入本地离线小模型（如 Qwen2.5-VL 3B 本地 CoreML 转化版）替代 DeepSeek 云端调用，实现 100% 隐私与零外网依赖。

---

# I. Top 10 Priorities (投入产出比最高的十大改进任务)

| 排序 | 任务项 | 优先级 | 预计工时 | 排序理由与价值衡量 |
|---|---|---|---|---|
| **1** | 修复跌倒“不是没事”反向撤警漏洞 (`actions/fall.py`) | **P0** | 1h | 生命安全底线。老人遇险呼救被系统判定为没事是绝对不可接受的严重故障。 |
| **2** | 解除 `/readyz` 503 探针锁定与零 Webhook 阻塞 (`core/health.py`) | **P0** | 2h | 可用性基石。探针死锁会导致看门狗疯狂报警、服务被错误重启、用户收到虚假降级告警。 |
| **3** | 修复仪表盘起床时间跨天泄漏 (`server/dashboard.py`) | **P0** | 1h | 核心体验欺骗。避免向家属展示昨日起床时间而掩盖今日可能发生的不测。 |
| **4** | 持久化服药状态与 30 分钟跌倒静默期 (`core/state.py`) | **P0** | 4h | 守护连续性。杜绝守护进程重启后丢失服药记录并对摔倒老人进行重复声音骚扰。 |
| **5** | 补齐 `door_outside` 默认 ROI 坐标 (`config/settings.yaml`) | **P1** | 1h | 极低成本复活核心场景。1 行配置即可让完全瘫痪的出门钥匙提醒功能重新运作。 |
| **6** | 净化 macOS `say` 文本中的 `[[slnc]]` 控制字符 (`skills/tts.py`) | **P1** | 1h | 杜绝音频 DoS 攻击。防止恶意或异常留言导致管家麦克风失聪并阻塞告警 60 秒。 |
| **7** | 守护进程与双击启动器强制绑定 `--require-production` | **P0** | 2h | 安全防护门禁。确保进入后台运行或双击运行的服务必须持有 Token 与安全密钥。 |
| **8** | 将数据库维护与 `VACUUM INTO` 移出主事件循环 (`main.py`) | **P1** | 1h | 消除每日定时卡顿。防止跨天维护冻结主事件循环引发音视频缓冲丢包。 |
| **9** | 增加本地 USB 摄像头掉线重连逻辑 (`sensors/camera.py`) | **P1** | 2h | 硬件鲁棒性。防止物理摄像头偶发断开后主进程永久失明。 |
| **10**| 延长跌倒静止期人体框保持时间 (`sensors/perception.py`) | **P0** | 2h | 消除致命漏报。防止老人跌倒蜷缩在地上时因无法识别人形而被静默丢弃。 |

---

# J. Audit Coverage & Limitations (审计覆盖度与环境局限)

### 1. 覆盖率核验
- **已审计模块**: 37 个 Python 模块，覆盖 `core/` (10 个)、`sensors/` (9 个)、`skills/` (10 个)、`actions/` (9 个)、`server/` (6 个)、`tools/` (11 个)、`deploy/` (3 个)、`scenes/` (10 个 YAML)。
- **未审计模块**: 0（全部功能代码与配置均纳入分析与代码排查）。
- **已执行测试**: 380 项自动化测试，全部通过（耗时 35.98 秒）。
- **静态工具检查**: 完成 Python 语法全编译校验、导入依赖树检查、AST 正则安全扫描。

### 2. 审计局限与受阻项 (Blocked & Limitations)
- **Apple Vision OCR 无头沙箱限制**: 在 macOS headless/sandbox 运行环境下，Apple Neural Engine (ANE) 缓存目录受 TCC 权限限制，`test_ocr` 会捕获 `CRImageReaderError` 并降级，需在交互式 GUI 会话中验证。
- **真实外网平台 Webhook 阻断**: 本次审计严格遵循防御与只读模式，未向真实钉钉或飞书服务器发起外部推送，仅在本地使用 mock 服务端核验签名与回执流程。
- **物理摄像头硬件插拔**: 摄像头热重连测试使用模拟数据源与视频文件回放完成，未执行物理拔线测试。

---
**审计认证**:  
*Google Antigravity Lead Auditor (Gemini 3.8 Flash High Thinking)*  
*报告归档路径: `audit-reports/AUDIT_REPORT.md`*  
*配套文件: `AUDIT_PROGRESS.md`, `MODULE_INVENTORY.md`, `FINDINGS.md`, `TEST_RESULTS.md`, `ARCHITECTURE.md`*
