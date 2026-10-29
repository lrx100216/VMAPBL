# VMAPBL · 本地 AI 管家

运行在 MacBook Air M2 上的多模态养老守护系统：本地廉价感知（OpenCV/YOLO）
+ 云端按需理解（DeepSeek 视觉模型），单进程 asyncio，macOS 原生音视频。

## 目录结构

```
VMAPBL/
├── main.py                     # 入口：装配事件总线、传感器、场景引擎、家人端服务
├── requirements.txt            # 全部 Python 依赖（按里程碑分组注释）
├── .env.example                # 环境变量模板（密钥/模型名/视频源），复制为 .env 后填写
├── config/
│   └── settings.yaml           # 全部阈值/冷却时间/ROI 多边形（唯一事实源，标定工具写入）
├── deploy/                     # 第三阶段：launchd 自启/防休眠与看门狗配置模板
├── scenes/                     # 场景定义（YAML，trigger/condition/action 三段式）
│   ├── door_key.yaml           # 场景3 出门/回家提醒钥匙
│   ├── medication.yaml         # 场景4 智能吃药提醒
│   ├── wake_up.yaml            # 场景5 未正常起床（三分类+升级链）
│   ├── get_up.yaml             # 场景6 起身通知（翻身过滤）
│   ├── fall.yaml               # 场景7 跌倒检测（骨架时序+二次确认）
│   ├── photo_ocr.yaml          # 场景8 拍照文字识别（OCR 优先）
│   ├── msg_to_family.yaml      # 场景1 老人语音联系家人
│   ├── msg_to_elder.yaml       # 场景1b 家人→老人播报留言与回复
│   └── family_query.yaml       # 场景2 家人远程询问老人状态（唯一必用 VLM 的场景）
├── core/
│   ├── bus.py                  # 事件总线：publish/subscribe，模块间唯一通信方式
│   ├── state.py                # StateStore：occupancy/静止/吃药/冷却/半双工标志
│   ├── scene_engine.py         # YAML 解释器 + 优先级仲裁（跌倒>未起床>起身>其他）
│   ├── config.py               # 读 .env + settings.yaml 的唯一入口（模型名只在这里和 .env 出现）
│   ├── log.py                  # SQLite 决策日志：每次触发/云端调用/花费落库
│   ├── health.py               # 第三阶段：就绪快照（/readyz 与心跳共用）
│   ├── heartbeat.py            # 第三阶段：原子写/读心跳文件
│   └── logging_setup.py        # 第三阶段：日志文件 + 轮转
├── sensors/
│   ├── camera.py               # 摄像头/视频文件采集：30fps + 60 秒环形缓冲（回放同一代码路径）
│   ├── motion.py               # MOG2 运动闸门（滞回 0.15/0.05）+ 静止检测
│   ├── perception.py           # 感知主管线：视频源→MOG2→节流姿态→occupancy/still 事件（main.py --perceive 调 run_perception）
│   ├── geometry.py             # 骨架几何判据：躯干倾角/髋部高度等共享计算
│   ├── pose.py                 # YOLO26n-pose 封装（优先 CoreML）
│   ├── detect.py               # YOLO26n 通用/药盒检测封装
│   ├── microphone.py           # 48kHz 采集→16kHz 重采样；播报期间丢帧（半双工）
│   └── vad.py                  # Silero VAD：连续音频切语句段
├── skills/                     # 「唯一出口」技能层
│   ├── vision.py               # json_call：唯一视觉出口（cloud=DeepSeek），JSON schema+1 次重试+uncertain 兜底
│   ├── llm.py                  # chat_json：纯文本 LLM 出口（意图分类、收件人抽取，同重试/落库语义）
│   ├── intent.py               # 语音意图三分类：message / photo / chat
│   ├── stt.py                  # listen：VAD 切句 + faster-whisper small/int8/zh，模型常驻内存
│   ├── tts.py                  # speak：唯一发声出口，say -v Tingting + 互斥锁 + 400ms 尾静默
│   ├── notify.py               # send：家人端消息（落 messages 表 + 总线广播，urgent 分级）
│   ├── voice_loop.py           # 第二阶段：常驻语音回路（背景监听，安全确认可抢占）
│   ├── family_router.py        # 第二阶段：家人消息分流（状态询问 / 留言）
│   └── ocr.py                  # Apple Vision OCR（L2.5 档，场景8 优先走这里）
├── actions/                    # 场景动作实现（YAML 里 type: skill 的 name 对应这里的函数）
│   ├── door.py                 # 场景3：ROI 落点判定 + VLM 兜底
│   ├── medication.py           # 场景4：药盒在场确认 + 5 帧滑窗投票（宁可漏报不可误报）
│   ├── wakeup.py               # 场景5：在床上/已离床/无法判断 + 升级链
│   ├── getup.py                # 场景6：髋部归一化高度+躯干倾角 + 翻身过滤
│   ├── fall.py                 # 场景7：骨架时序特征 + 持续静止 + 二次确认 + stop 防重复
│   ├── messaging.py            # 场景1/1b：消息收发（LLM 抽取收件人与正文，拒绝词识别）
│   ├── family_query.py         # 场景2：抓帧→VLM 按 5 条隐私规则生成给子女的话
│   └── photo_ocr.py            # 场景8：OCR 优先，需语义理解才升 VLM
├── server/
│   ├── app.py                  # FastAPI + WebSocket 端点（状态/消息/缩略图/手动触发）
│   ├── push.py                 # 总线事件 → WebSocket 转发（跨 loop 安全）
│   └── static/index.html       # 家人端单页控制台（自包含，无构建步骤）
├── tools/
│   ├── m1_minimal_loop.py      # M1 最小闭环：抓帧→VLM→say
│   ├── permission_check.py     # M0：摄像头/麦克风/DeepSeek 三项自检（区分权限 vs 代码问题）
│   ├── calibrate_roi.py        # ROI 标定：鼠标画多边形写入 settings.yaml
│   ├── replay.py               # 离线回放：逐场景跑 20 次出准确率统计表
│   ├── cost_report.py          # M8：从 butler.db 出成本与调用报表
│   ├── soak_test.py            # M8：循环感知稳压测试（RSS/异常监控）
│   ├── export_pose_coreml.py   # 一次性工具：导出 yolo26n-pose 为 CoreML
│   ├── collect_pillbox_frames.py  # M6：药盒训练素材采集（按间隔抓帧存盘待标注）
│   ├── train_pillbox.py        # M6：yolo26n 药盒检测器训练（device=mps）→ models/pillbox.pt
│   ├── install_launchd.py      # 第三阶段：生成 launchd 自启/防休眠配置
│   └── healthcheck.py          # 第三阶段：独立看门狗（只读心跳，独立告警通道）
├── tests/
│   ├── media/                  # 测试素材（文件名带真实标签；gitignore 不入库）
│   ├── conftest.py             # 全局拦截 tts：测试期间绝不真开腔（say 换成记录器）
│   ├── test_scene_engine.py    # 引擎单测（M2 起）
│   ├── test_bus_state.py       # bus/state/log 骨架单测
│   ├── test_m3_voice_chain.py  # M3：语音链互斥/OCR/重试语义/三个场景技能状态机
│   └── test_m8_regression.py   # M8：replay 扩展/稳压/成本报表单测
└── data/                       # 运行时生成：butler.db（SQLite）、自检样帧等（gitignore）
```

（各 Python 包目录还含 `__init__.py`，不再单独列出。）

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 环境与权限自检（tools/permission_check.py） | ✅ 用户验收通过 |
| M1 | 最小闭环（tools/m1_minimal_loop.py）：抓帧→VLM→say 念出来 | ✅ 用户验收通过 |
| M2 | 骨架：bus/state/scene_engine/log | ✅ 用户验收通过 |
| M3 | 语音链 + 场景 8/1/2 | ✅ 用户验收通过 |
| M4 | 感知层：camera/pose/MOG2/静止/ROI 标定 | ✅ 76 项测试通过，已验收 |
| M5 | 场景 3/5/6（几何与骨架判据） | ✅ 109 项测试通过，已验收 |
| M6 | 场景 4/7（药盒检测器、跌倒降级方案） | ✅ 145 项测试通过，已验收 |
| M7 | 家人端控制台（FastAPI + WebSocket + 单页 HTML） | ✅ 158 项测试通过，已验收 |
| M8 | 回归测试与成本核算：replay 扩展 fall/药盒回放、soak_test 稳压工具、cost_report 成本报表 | ✅ 166 项测试通过，待用户实测验收 |

## 快速开始（M0）

`cp -n` 不会覆盖已有文件；执行后编辑 .env 填入 DEEPSEEK_API_KEY。
venv 与 M0 依赖已预装好。

```bash
cd VMAPBL
cp -n .env.example .env
source .venv/bin/activate
python tools/permission_check.py
```

**首次必须在 Terminal.app 或 iTerm 的本地窗口里运行**（不能 SSH / IDE 集成终端），
否则 macOS 不会弹摄像头/麦克风授权框。

**M2 骨架验证**（不需要摄像头/麦克风权限）。
预期：`--list` 列出 demo_hello；连触两次时第一次「已执行 ✅」、第二次「被拦截 ❌」
（5 秒冷却；冷却是进程内存态，必须同一次调用内连触两次才能看到拦截；
被拦截时命令退出码为 1，是预期表现不是崩溃）；sqlite3 应看到
trigger/condition/action/action_result/done/skipped 六种事件；pytest 37 passed。

```bash
python main.py --list
python main.py --trigger demo_hello --trigger demo_hello
sqlite3 data/butler.db "SELECT kind, COUNT(*) FROM events GROUP BY kind;"
./.venv/bin/python -m pytest -q
```

> 注意：本 README 的命令块一律不带 `#` 行内注释——若你的 zsh 未开
> `setopt interactivecomments`，`#` 会被当成普通参数传给命令导致报错。

**M3 语音链与场景 8/1/2 验证**。

预期：`--voice` 进入「等待说话 → 识别 → 意图 → 场景 → 播报」回路，直接对麦克风说话即可，
Ctrl+C 退出；播报期间和播完 400ms 内麦克风丢弃音频（不会听到自己的播报自问自答）。
说「帮我念一下上面写的什么」（镜头对着文字）走本地 OCR 不花钱；说「这药一天吃几次」
才升云端 VLM；说「告诉儿子晚上过去吃饭」抽取收件人发消息并语音回执。
`--trigger` 三条分别模拟场景 1b（家人留言→播报→听 6 秒回复）、场景 2（抓帧→VLM→
隐私合规回复推给家人端）、场景 8。pytest 63 passed。

首次使用麦克风/摄像头必须在 Terminal.app 或 iTerm 的本地窗口里运行（TCC 授权）。

```bash
python main.py --voice
python main.py --trigger msg_to_elder --text "妈，吃饭了吗"
python main.py --trigger family_query
python main.py --trigger photo_ocr --text "帮我念一下上面写的什么"
sqlite3 data/butler.db "SELECT direction, sender, recipient, urgent, text FROM messages ORDER BY id DESC LIMIT 5;"
./.venv/bin/python -m pytest -q
```

**M4 感知层验证**。

预期：`--perceive` 进入感知回路并实时打印状态变化，Ctrl+C 退出。走进画面
→ 打印 `occupancy: off → on`；离开画面约 3 秒（settings.yaml occupancy.hold_s）
→ 打印 `occupancy: on → off`；原地翻身/持续小动作 → 不断刷新运动，不会打印
`sensor.still`；静止不动约 10 秒（motion.still_seconds）→ 打印静止事件。
结束时打印帧率、姿态耗时中位数、环形缓冲占用。首次会下载 yolo26n-pose.pt
（7.5MB）；已导出 CoreML 版（tools/export_pose_coreml.py）则自动优先加载。
用 VIDEO_SOURCE 指向视频文件即离线回放（同一管线、按文件帧率定速）。
roi 标定：运行后左键加点、回车闭合、s 保存进 settings.yaml。

```bash
python main.py --perceive
VIDEO_SOURCE=./tests/media/your_clip.mp4 python main.py --perceive
python tools/calibrate_roi.py door_inside
python tools/export_pose_coreml.py --bench-only
./.venv/bin/python -m pytest -q
```

**M5 场景 3/5/6 验证**。

前置：先用标定工具画三个 ROI（场景3 要 door_inside + door_outside 两个多边形，
场景5 要 bed）。没标定的场景只会落库不会播报（缺 ROI 不猜）。

```bash
python tools/calibrate_roi.py door_inside
python tools/calibrate_roi.py door_outside
python tools/calibrate_roi.py bed
```

预期：`--perceive` 下场景触发器全部在线（state / event / time 三类已接线）。
走到门口 ROI → 场景3 播报「出门别忘记拿钥匙」；运动后静止 → 场景6 判定，
起身播报「请等一下再下床」并通知家人（控制台反色闪烁代替物理反馈），
翻身只落库不播报；场景5 每天 08:30 首检 / 09:00 复查（升级链状态机），
调试时用手动触发模拟。规范验收：用预录视频回放，每场景跑 20 次统计准确率
（素材文件名带真实标签，翻身/只喝水等负样本是误报率的唯一验证手段）：

```bash
python main.py --perceive
python main.py --trigger wake_up
python main.py --trigger door_key
python tools/replay.py door_key --expect out tests/media/door_out_*.mp4 --repeat 5
python tools/replay.py get_up --expect turn_over tests/media/turn_over_*.mp4 --repeat 20
python tools/replay.py wake_up --expect in_bed tests/media/wakeup_inbed_*.mp4
sqlite3 data/butler.db "SELECT scene_id, kind, detail FROM events WHERE kind IN ('trigger_fire','action_result') ORDER BY id DESC LIMIT 10;"
./.venv/bin/python -m pytest -q
```

回放按文件帧率定速（保真），墙钟≈素材总时长×次数；明细 JSON 存 data/replay_*.json。

**M6 场景 4/7 验证**。

场景7 无需标定。预期：`--perceive` 下用力挥一下手或走动几步，然后坐下静止
不动约 10 秒（motion.still_seconds）→ 打印「运动突变后静止 → sensor.sudden_still」
→ 语音问「您还好吗」→ 对着麦克风说「没事」→ 播报安抚、不通知家人；
若不回答或说「救命/疼/起不来」→ 家人端收到紧急通知，且之后 30 分钟
（fall.re_alert_s）内不再重复打扰。场景4 每天 09:30–10:30 自动触发；
窗外手动触发会被 time_window 条件拦截（落库可见，属预期）。药盒权重
models/pillbox.pt 缺失时场景4 自动降级为纯语音确认（问「药吃了吗」），
训练完权重即自动升级为视觉两阶段判定。

```bash
python main.py --list
python main.py --perceive
python main.py --trigger fall
sqlite3 data/butler.db "SELECT scene_id, kind, detail FROM events WHERE scene_id IN ('fall','medication') ORDER BY id DESC LIMIT 10;"
./.venv/bin/python -m pytest -q
```

药盒检测器自训流程（规范：50–100 张标注，M2 上 device=mps 十几分钟）。
采集时把药盒换位置/角度/光照/距离拍，并拍一批无药盒空镜当负样本；
标注成 YOLO 格式 txt（class id 0），按 tools/train_pillbox.py 头部注释的
目录布局放好；images/val 留空会自动从 train 划出 1/5。

```bash
python tools/collect_pillbox_frames.py
python tools/train_pillbox.py
python main.py --trigger medication
```

**09:30–10:30 时间窗内联调场景4 的技巧**：直接改 scenes/medication.yaml 的
time_window 与 time.at 到当前时刻，或临时删掉 time_window 条件后
`--trigger medication`（测试里就是这么绕过墙钟的，改完记得改回）。

**M7 家人端控制台验证**。

预期：`--serve` 启动完整系统（感知回路 + 常驻语音 + 场景触发器 + 家人端服务常驻），
浏览器打开 http://127.0.0.1:8321 看到控制台：`/` 为家人端仪表盘（演示 mock 数据、
无画面只有结论、事件时间线与智能提醒配置），`/debug` 保留实时画面/ROI/场景调试。
- 右上「老人状态」面板实时更新（画面有人/最后活动/持续静止/播报中/今日吃药）；
- 「当前画面」缩略图每 3 秒刷新（感知回路在跑时取它的最新帧，不二次开摄像头）；
- 「与老人的对话」：家人留言输入框发送 → 音箱播报「家人给您留言：…」→
  老人语音回复会回发到消息流（WebSocket 实时推送）；
- 「手动触发场景（调试）」按钮：点任意场景即触发（最有用的是 door_key / fall /
  family_query），结果在右下角 toast 提示。
Ctrl+C 退出后服务干净关停（无挂起的后台任务）。

```bash
python main.py --serve
curl -s http://127.0.0.1:8321/api/state
curl -s http://127.0.0.1:8321/api/scenes
curl -s -X POST http://127.0.0.1:8321/api/trigger/demo_hello
sqlite3 data/butler.db "SELECT id, direction, text, urgent FROM messages ORDER BY id DESC LIMIT 5;"
./.venv/bin/python -m pytest -q
```

离线回放也能起控制台：VIDEO_SOURCE 指向视频文件时感知按文件帧率跑，
回放结束服务自动退出（局域网联调/演示用同一命令）。

**M8 回归与成本核算验证**。

预期：M8 起 fall 进入回放白名单，跌倒素材可离线回放出准确率统计；
soak_test 循环跑感知管线直到时间预算耗尽，全程零异常、RSS 漂移 <20MB
即为稳压通过（先短时冒烟，再上 120 分钟正式稳压）；cost_report 从
butler.db 汇总云端调用次数/token/成本与各场景决策事件统计，`--since`
可按起始日期过滤。

```bash
python tools/cost_report.py
python tools/cost_report.py --since 2026-09-01
python tools/soak_test.py --video tests/media/smoke_none_01.mp4 --minutes 5
python tools/replay.py fall --expect fall tests/media/fall_*.mp4
./.venv/bin/python -m pytest -q
```

**第二阶段：常驻语音与家人消息路由验证**。

`--serve` 现在同时启动感知与常驻语音回路：老人直接说「给儿子发消息说我晚上过去吃饭」
走场景1，说「帮我念一下上面写的什么」走场景8。跌倒/起床/吃药的安全语音确认
会抢占背景监听（约一个音频块内让出麦克风），不会出现两条链路抢录音或确认被拖到超时。

钉钉/飞书回调仍走 `POST /api/webhook/family_reply`，现在按内容分流：

- 「我妈在干嘛」「查状态」「老人状态怎么样」→ 场景2（抓帧 + VLM → 推回家人端/机器人）；
- 其余文字 → 家人留言播报 + 老人语音回复（场景1b）；
- 以「留言：」开头强制当留言，避免关键词误判；超过 24 字的文本一律当留言。

```bash
python main.py --serve
curl -s -X POST http://127.0.0.1:8321/api/webhook/family_reply -H 'Content-Type: application/json' -d '{"text":"我妈在干嘛","sender":"儿子"}'
curl -s -X POST http://127.0.0.1:8321/api/webhook/family_reply -H 'Content-Type: application/json' -d '{"text":"妈，吃饭了吗","sender":"女儿"}'
./.venv/bin/python -m pytest -q
```

语音开关与超时在 `config/settings.yaml` 的 `voice:` 分区；路由关键词与长度上限在
`family_router:` 分区。麦克风不可用时语音回路有限重试后自动退出，感知与告警继续运行。

**安全加固：token 与回调签名**。

- 设置 `BUTLER_TOKEN` 后：`/api/*`（回调除外）必须带 `Authorization: Bearer <token>` 或 `?token=`；
  WebSocket 必须 `?token=`，且浏览器 `Origin` 只能是本机或 `server.allowed_origins` 白名单；
  `/healthz`、`/readyz` 保持公开供 launchd/看门狗探测。未设置时保持本机调试体验。
- 设置 `DINGTALK_SIGN_SECRET` / `FEISHU_SIGN_SECRET` 后：
  `/api/webhook/family_reply` 按平台算法验签（钉钉 HMAC-SHA256、飞书 sha256），
  时间戳窗口 5 分钟；签名缺失/错误/过期一律 401，**不落库、不播报**。
  未配置密钥时保持向后兼容，生产环境必须配置。

```bash
BUTLER_TOKEN=你的口令 python main.py --serve
curl -s -H "Authorization: Bearer 你的口令" http://127.0.0.1:8321/api/state
```

家人端页面可用 `http://127.0.0.1:8321/?token=你的口令` 打开；没带 token 时，
接口返回 401 会弹窗要求输入口令。

**第一批安全切片（2026-10-29）**。

- VLA `UPDATE_STATE` 仅允许写 `is_still: bool`，其余字段拒绝；策略或参数异常
  统一进入本地安全兜底并落 `error` 审计。
- 服药语音确认必须明确提到药且无否定表达，classic 与 VLA 共用判定；
  「我吃了饭了」不记服药，VLA 按实际时间写 morning/noon/evening。
- TTS 最长 200 字，`say` 超时自动 kill；成功、异常、取消、尾静默中断均会
  复位 `speaking`，不再把麦克风永久锁在半双工状态。

**生产预检与依赖锁（2026-10-29）**。

直接依赖范围见 `requirements.txt`，当前验证过的完整环境固定在
`requirements-lock.txt`（OpenCV 4.x + `av<16`，对齐 FFmpeg 61，降低原生库 ABI 冲突）。
新机器建议按锁文件安装，密钥文件权限设为 `0600`：

```bash
python -m pip install -r requirements-lock.txt
chmod 600 .env
python main.py --require-production --serve
```

`--require-production` 会在启动前检查：访问 token、至少一个家人通知渠道、
对应平台的回调签名、姿态模型文件、SQLite/日志路径可写、`.env` 权限。
任一项不满足会以退出码 2 停止并列出脱敏原因；也可先单独运行
`python tools/permission_check.py --require-production`，不会触碰硬件或云端。

**P1-1 健康降级**。

- 姿态模型支持 CoreML 加载/预热失败后回退 `.pt`，全部失败进入 `degraded`；
  `/readyz` 将模型状态纳入就绪判定，启动时写降级事件并尝试通知家人。
- 控制台绑定端口失败不再打印“启动成功”；classic/VLA 都先确认服务真正监听，
  再启动语音与心跳。
- 实时摄像头/网络流超过 `camera.stale_s` 无新帧时设置 `perception_degraded`、
  发布 `sensor.health.degraded` 并告警；恢复新帧后自动解除。`/api/frame` 对陈旧
  画面返回 503，不再用旧图伪装实时。
- `bus.publish_nowait` 保留任务强引用，新增 `await bus.drain()` 用于停服和测试排空。

**P1-2 通知可靠性**。

- 仅传输异常、429 和 5xx 重试；协议/URL/重定向等确定性错误立即失败。
- 429 尊重有上限的 `Retry-After`，整次回执受 `notify.receipt_deadline_s` 限制。
- 紧急通知未送达时写入 SQLite `notification_outbox`，返回 `pending`，不是 `ok`。
- `notification_retry_loop` 随 `--serve` 启动，重启后继续消费 pending；
  达到 `retry_total_attempts` 后转 `dead`，避免无限重试。
- `/readyz` 与心跳公开 pending/dead；存在未恢复通知时 `ready=false`，
  独立看门狗可给出 `not_ready:notify`。只有实际平台回执成功才算 delivered，
  原有的“未送达不武装跌倒静默”保持不变。

**第三阶段：真机部署与常驻运维验证**。

网络摄像头：`VIDEO_SOURCE` 支持 `rtsp://`、`rtmp://`、`http(s)://`，走 FFmpeg 后端，
断线按 `camera.reconnect_s` 自动重连；健康快照看 `camera.stale_s` 内是否有新帧。
日志、心跳、看门狗参数在 `settings.yaml` 的 `camera:` 与 `ops:` 分区。

```bash
VIDEO_SOURCE=rtsp://user:pass@192.168.1.10/stream python main.py --serve
curl -s http://127.0.0.1:8321/healthz
curl -s http://127.0.0.1:8321/readyz
cat data/heartbeat.json
tail -f logs/butler.log
```

`/readyz` 只在视频帧新鲜且 SQLite 可写时返回 200，否则 503 并列出逐项原因。
主进程每 10 秒原子写 `data/heartbeat.json`；独立看门狗每 60 秒读一次，
心跳超过 180 秒未更新或 `ready=false` 时，用 `WATCHDOG_WEBHOOK_URL`
（或 `ops.watchdog_webhook_url`）发送告警，重复告警默认 30 分钟内不刷屏。

安装开机自启（LaunchAgent，含 `caffeinate -dimsu` 防休眠）：

```bash
python tools/install_launchd.py --dry-run
python tools/install_launchd.py
```

按脚本输出的两条 `launchctl bootstrap` 命令加载。**不要改成 LaunchDaemon**：
麦克风/AVFoundation 在系统会话中不可用，进程会在导入阶段失败。日志落盘为
`logs/butler.log`（10MB × 5 轮转），stdout/stderr 另由 launchd 写入
`logs/butler.out.log` 与 `logs/butler.err.log`。

**VLA 模式对齐**。`python main.py --mode vla --serve` 现在与 classic 共用同一套
运维外壳：常驻语音回路、心跳/看门狗、定时吃药/起床、语音指令、家人留言与状态询问
全部接通。`sensor.*` 事件只由 VLA 消费，不再接场景，避免同一次跌倒双重确认/双重告警。
VLA 的 `NOTIFY_FAMILY` 改为等待平台送达回执，只有确认送达后才执行 `STOP_ALERT`
静默；未送达时保持后续再次告警的机会。VLA 仍需要摄像头、麦克风、通知渠道和外网
视觉模型，但不需要门口/床区 ROI 标定，也不需要自训药盒检测器。

