# Home Assistant 接入详细计划（VMAPBL + TP-Link）

- 日期：2026-11-01
- 适用现状：MacBook 运行 VMAPBL；TP-Link `TL-IPC45CL-V2 2.0` 位于 `192.168.0.112`；用户已有 Home Assistant。
- 目标：让 HA 负责设备连接、看板与家人入口，VMAPBL 负责 AI 理解、语音与安全场景，两边通过明确契约互通，不重复做同一件事。

## 0. 已确认的基线

| 项目 | 当前状态 |
|---|---|
| Mac 局域网地址 | `192.168.0.105` |
| 摄像头 | `192.168.0.112`，RTSP 子码流 `640×360` 已验证可出图 |
| 主码流 | `2880×1620`，已验证可出图 |
| VMAPBL 服务 | `127.0.0.1:8321`，目前只监听本机 |
| 访问 token | 未配置 |
| 家人通知渠道 | 未配置 |
| ROI | 已有 `bed`、`door_inside`；`door_outside` 为空 |
| 测试基线 | 320 passed |
| HA 位置/安装方式 | **HAOS 专用设备**（地址待 Phase 0 确认） |
| 家人通知首选 | **钉钉**（HA App 作为可选备用） |
| 已有语音终端 | **有**（型号与 HA 兼容性待确认） |
| 门磁/Zigbee 协调器 | **暂无** |

> 关键前提：HA 在另一台设备上，**当前 VMAPBL 只监听 127.0.0.1，HA 无法调用**。Phase 2 必须调整监听地址并设置 token，或通过反向代理暴露。

## 1. 架构选择

### 推荐：HA 做设备层，VMAPBL 做 AI 决策层

```text
TP-Link / 门磁 / 语音终端 / 传感器
              │
              ▼
      Home Assistant
      ├─ 设备接入、状态、看板
      ├─ 家人手机 App 通知
      └─ 简单自动化（开关、提醒、场景按钮）
              │  REST / Webhook
              ▼
         VMAPBL（Mac）
         ├─ VLA 视觉理解
         ├─ 语音收/发与安全确认
         ├─ 跌倒/起床/吃药等场景
         └─ 钉钉/飞书/HA 通知
```

**边界原则：每个场景只能有一个“决策所有者”。** HA 负责设备事实与家人界面；VMAPBL 负责需要视觉/语音/多步推理的判断。两边都写一遍自动化会造成重复播报、重复告警和互相覆盖。

### 不推荐的两个极端

- **全搬到 HA**：当前 VMAPBL 的 VLA、姿态、语音抢占、送达门控无法低成本复制到 HA；会丢掉已完成的安全能力。
- **HA 只当摄像头页面**：浪费 HA 的设备管理、手机通知、远程访问和自动化能力。

## 2. 分阶段计划

### Phase 0：发现与基线（0.5 天）

1. 找到 HA：
   - 浏览器试 `http://homeassistant.local:8123`；
   - 路由器 DHCP 列表查找 HA 主机；
   - HA 手机 App 里查看服务器地址。
2. 记录：
   - HA 地址、版本（设置 → 关于）；
   - 安装方式（HAOS / Supervised / Container / Core）；
   - 主机硬件与可用磁盘；
   - 已安装集成、Add-ons、HACS；
   - 是否已有 Zigbee/Z-Wave 协调器；
   - 是否已有语音终端（Voice PE、音箱、麦克风）。
3. 网络基线：
   - HA、Mac、摄像头是否同一网段/VLAN；
   - HA 与 Mac 设为静态 IP 或 DHCP 保留；
   - 记录 Mac `192.168.0.105` 和摄像头 `192.168.0.112`。
4. 在 HA 做一次完整备份，并记录恢复步骤。
5. 在 HA 创建长期访问令牌：个人资料 → 安全 → 长期访问令牌，命名 `vmapbl`。
   - 令牌只写入 HA `secrets.yaml`，不进入 Git、不贴聊天。

**产出**：一页网络与 HA 能力清单；备份文件；HA 令牌。

### Phase 1：摄像头接入 HA（0.5–1 天）

目标：HA 能看到稳定画面，但不把 HA 变成第二套 AI 判断。

1. 优先尝试 **ONVIF 集成**：
   - 设置 → 设备与服务 → 添加集成 → ONVIF；
   - 主机 `192.168.0.112`，填写摄像头账号密码；
   - 若自动发现失败，再尝试手动配置端口（TP-Link 常见为 80 或 2020，以设备页面为准）。
2. ONVIF 不兼容时，使用 **Generic Camera**：

```yaml
# configuration.yaml（示例，地址放 secrets）
camera:
  - platform: generic
    name: "老人房"
    stream_source: !secret tplink_rtsp_sub
    still_image_url: !secret tplink_snapshot_url
    verify_ssl: false
```

```yaml
# secrets.yaml
tplink_rtsp_sub: "rtsp://admin:密码@192.168.0.112:554/stream2"
tplink_snapshot_url: "http://admin:密码@192.168.0.112/tmp/snapshot.jpg"
```

> 快照 URL 以该型号实际页面为准；不确定时先只配 `stream_source`。

3. 验证：
   - HA 仪表盘添加 Picture Entity / Camera 卡片；
   - 连续观察 24 小时，记录断流次数；
   - 确认 HA 主机 CPU/内存可承受该码流；卡顿时改用 `stream2`。
4. 事件检测：
   - 若 TP-Link 集成支持移动侦测/人形事件，接入为 `binary_sensor`；
   - 若不支持，**不要**在 HA 里重复做视觉判断，继续由 VMAPBL 负责。

**验收**：HA 卡片能稳定显示画面 ≥24h；无账号密码出现在 HA 日志；HA 重启后摄像头自动恢复。

### Phase 2：HA ↔ VMAPBL 双向桥（1 天）

#### 2.1 先修 VMAPBL 的网络前置

当前 `server.host=127.0.0.1`，HA 无法访问。改为局域网地址，并设置 token：

```yaml
# config/settings.yaml
server:
  host: 192.168.0.105   # 或 0.0.0.0；后者必须配合 macOS 防火墙与 token
  port: 8321
```

```bash
# .env（不要提交）
BUTLER_TOKEN=一段足够长的随机口令
```

```bash
chmod 600 .env
```

启动后用另一台设备验证：

```bash
curl -s -H "Authorization: Bearer 你的口令" http://192.168.0.105:8321/api/state
```

**安全要求**：

- 只在局域网监听；不要把 8321 端口转发到公网；
- macOS 防火墙仅允许 HA 主机访问该端口；
- 远程家人访问优先用 HA 自带远程访问或 Tailscale，不直接暴露 Mac。

#### 2.2 HA → VMAPBL：留言与场景触发

HA `secrets.yaml`：

```yaml
butler_token: "与 .env 相同的口令"
```

HA `configuration.yaml`：

```yaml
rest_command:
  vmapbl_message:
    url: "http://192.168.0.105:8321/api/msg_to_elder"
    method: POST
    headers:
      Authorization: "Bearer !secret butler_token"
      Content-Type: application/json
    payload: '{"text": {{ message | tojson }}}'
    timeout: 10

  vmapbl_trigger:
    url: "http://192.168.0.105:8321/api/trigger/{{ scene_id }}"
    method: POST
    headers:
      Authorization: "Bearer !secret butler_token"
      Content-Type: application/json
    payload: "{}"
    timeout: 30
```

HA 脚本示例：

```yaml
script:
  vmapbl_say_to_elder:
    alias: "给老人留言"
    fields:
      message:
        selector:
          text:
    sequence:
      - action: rest_command.vmapbl_message
        data:
          message: "{{ message }}"
```

注意：

- `/api/trigger/family_query` 会等待抓帧 + VLM，最长可达数十秒；HA 自动化里应放在 script 中并设置超时，不要让 UI 按钮长时间卡住。
- 家人状态询问需要向 HA 返回结果时，当前 VMAPBL 只通过 WebSocket/通知推送；后续可增加“查询结果写回 HA”的接口。

#### 2.3 VMAPBL → 家人：钉钉为主，HA 看板为辅

你已选择钉钉作为首选告警渠道。保持 VMAPBL 作为**唯一告警发送者**，避免 HA 和 VMAPBL 同时推送：

```bash
# .env
DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=...
DINGTALK_SIGN_SECRET=...
```

验证：

```bash
.venv/bin/python -c "import asyncio; from skills import notify; print(asyncio.run(notify.send_with_receipt('链路测试', urgent=False)))"
```

HA 侧只做看板与可选备用：

- 若希望 HA 仪表盘也显示告警，可把 `NOTIFY_WEBHOOK_URL` 指向 HA webhook（可选）；
- **不要**把同一条告警同时推到钉钉和 HA 手机 App，否则家人会收到两遍；
- 若以后切换为 HA App 为主，再停用钉钉 Webhook，保持单一主渠道。

#### 2.4 契约与幂等

| 方向 | 接口 | 用途 | 失败处理 |
|---|---|---|---|
| HA → VMAPBL | `POST /api/msg_to_elder` | 家人留言播报 | HA 脚本重试 1 次；失败提示用户 |
| HA → VMAPBL | `POST /api/trigger/{scene}` | 手动触发场景（状态查询等） | 超时 30s；避免高频点击 |
| VMAPBL → HA | `POST /api/webhook/vmapbl_notify` | 告警/消息转发 | HA 侧记录日志；VMAPBL 保留 outbox |
| VMAPBL ↔ 家人 | 现有钉钉/飞书回调 | 消息回复 | 已有签名与重试 |

约定：

- 同一条消息不要同时走 HA 和钉钉推给同一个人；
- 触发类请求带唯一 `request_id`（后续在 VMAPBL 增加字段），避免 HA 重试造成重复播报；
- 所有日志脱敏，不记录 token 和 RTSP 密码。

**验收**：

1. HA 脚本发送“妈，吃饭了吗”，老人端音箱播报，VMAPBL 记录 `to_elder`；
2. VMAPBL 触发跌倒告警，HA 手机收到通知；
3. 去掉 token 后 HA 调用返回 401，且不产生播报；
4. HA 与 Mac 重启后链路可恢复。

### Phase 3：语音终端（1–2 天，已有终端，先做兼容性确认）

你已有语音终端，Phase 0 先确认型号与协议：

- **Home Assistant Voice PE**：可直接走 HA Assist；用方案 B/C。
- **小智/AKU 等 ESP32 音箱**：需确认是否支持 HA Assist 或 ESPHome；不支持时只能作为普通音箱/麦克风，仍由 VMAPBL 驱动。
- **普通蓝牙/USB 音箱麦克风**：接 Mac，方案 A。

三种方案：

#### 方案 A：VMAPBL 继续做语音主链路（兼容性最好）

- 保留当前常驻语音回路；
- 语音终端作为 Mac 的麦克风/音箱；
- HA 只做按钮、看板和通知；
- 优点：不改代码；缺点：语音入口固定在 Mac。

#### 方案 B：HA Voice PE / Assist 做前端

- HA 负责唤醒词、STT、TTS；
- 通过 HA script → `rest_command.vmapbl_message` 把指令转给 VMAPBL；
- 优点：远场体验与设备管理更好；缺点：自然语言理解仍在 VMAPBL，HA Assist 只能做固定指令；
- 需要确认 Voice PE 的 STT/TTS 语言包与中文效果。

#### 方案 C：双语音入口

- HA Voice 负责“开灯/提醒/留言”等固定指令；
- VMAPBL 负责“读字/找东西/状态问答/安全确认”；
- 需要严格划分唤醒词和场景，避免两个系统同时应答。

**推荐**：终端若是 Voice PE 且中文可用，用方案 B/C；否则先用方案 A，避免为了语音再引入一套调试链。**不要同时让两个系统监听同一空间**，否则会互相触发。

### Phase 4：设备补齐（按场景，不一次性全买）

| 设备 | 解决什么 | 是否现在需要 |
|---|---|---|
| 门磁（Zigbee/Thread） | 真实“开门”事件，替代当前门外 ROI 不可见的问题 | **推荐**；但需先购买 Zigbee/Thread 协调器 |
| Zigbee 协调器（如 SkyConnect / USB dongle） | 门磁、按钮、人体传感器接入 | 与门磁一起购买 |
| 远场语音终端 | 老人自然说话 | 体验需要；跌倒检测非必需 |
| 人体存在传感器（毫米波） | 静止/在床/离床的辅助判断 | 可选，先验证现有视觉 |
| 跌倒按钮/SOS 按钮 | 老人主动求助 | 低成本，建议加 |
| 智能插座 | 吃药提醒联动、设备重启 | 可选 |

**当前最值得补的是门磁**：你的机位看不到门外，`door_outside` 为空，出门/回家场景无法可靠判断。门磁可以直接提供“门开了/关了”，比继续猜 ROI 稳。

### Phase 5：场景归属表

| 场景 | 决策所有者 | 触发 | 动作 | 现状 |
|---|---|---|---|---|
| 1 老人联系家人 | VMAPBL | 语音 | 播报回执 + 通知 | 已实现 |
| 2 家人问状态 | VMAPBL | HA 按钮/钉钉 | 抓帧 + VLM + 通知 | 已实现，HA 入口待接 |
| 3 出门带钥匙 | **HA 门磁 + VMAPBL 播报** | 门磁开门 | 播报 | 缺门磁；门外 ROI 为空 |
| 4 智能吃药 | VMAPBL | 时间/手动 | 提醒 + 确认 + 升级 | 已实现；药盒模型未训练 |
| 5 未正常起床 | VMAPBL | 08:30/09:00 | 床区判定 + 语音 + 通知 | 已实现；床区 ROI 已标 |
| 6 起身通知 | VMAPBL | 运动后静止 | 骨架判定 + 通知 | 已实现 |
| 7 跌倒 | VMAPBL / VLA | 突变后静止 | 语音确认 + 紧急通知 | 已实现 |
| 8 读字 | VMAPBL | 语音 | OCR 优先 + VLM | 已实现 |

### Phase 6：验收、回滚与运行

1. **功能验收**：Phase 2 的四条验收 + 摄像头 24h + 一次真实家人留言往返。
2. **故障演练**：
   - HA 停止 → VMAPBL 继续本地安全场景；
   - Mac 停止 → HA 显示设备离线，不重复告警；
   - 摄像头断网 → VMAPBL `perception_degraded`，HA 卡片显示不可用；
   - token 错误 → 401，无副作用。
3. **安全**：
   - HA 与 Mac 均不暴露公网 8123/8321；
   - HA 令牌、VMAPBL token、RTSP 密码只存 secrets/.env；
   - 日志脱敏抽查；
   - 每月 HA/VMAPBL 备份。
4. **回滚**：
   - HA：恢复 Phase 0 备份；
   - VMAPBL：恢复 `server.host=127.0.0.1`，移除 HA 相关 webhook；
   - 保留钉钉/飞书原链路作为备用。

## 3. 时间表

| 天 | 工作 | 产出 |
|---|---|---|
| Day 1 上午 | Phase 0 发现、备份、HA token | 网络清单、备份 |
| Day 1 下午 | Phase 1 摄像头接入 HA | HA 看板可看画面 |
| Day 2 | Phase 2 VMAPBL 监听/token/双向桥 | 留言往返 + HA 通知 |
| Day 3 | Phase 3 语音终端兼容性确认与接入 | 一个可用语音入口 |
| Day 4 | 联调、故障演练、文档 | 验收记录 |
| Week 2 | 48h/7 天试运行 | go/no-go 记录；门磁/协调器按需采购 |

## 4. 主要风险

| 风险 | 影响 | 应对 |
|---|---|---|
| HA 与 Mac 不在同一网段/VLAN | rest_command 与 webhook 不通 | 先确认路由与防火墙；必要时同 VLAN 或放行端口 |
| TP-Link ONVIF 不兼容 | HA 加不上摄像头 | 用 Generic Camera + RTSP；保留 VMAPBL 直连 |
| HA 主机性能不足 | 转码卡顿/CPU 高 | 用子码流；关闭 HA 转码；只做看板 |
| 两套自动化重复 | 重复播报/告警 | 每个场景单一所有者；先关掉 HA 里重复的自动化 |
| Mac 休眠/重启 | 桥接中断 | 已完成 launchd + caffeinate；仍需实机验证 |
| token/密码泄露 | 家人端被操控 | token 只存 secrets/.env；不公网暴露；日志脱敏 |
| 通知多渠道重复 | 家人被轰炸 | 先只保留 HA 或钉钉一个主渠道 |

## 5. 本次计划明确不做

- 不把 VMAPBL 的 VLA/姿态/语音迁移到 HA；
- 不在 HA 里重建跌倒/起床/吃药视觉判定；
- 不购买雷达、手环等新传感器，先验证现有链路；
- 不实现视频通话与方言录音（当前为 mock，缺完整链路）；
- 不开放公网端口，远程访问走 HA 官方远程或 Tailscale。

## 6. Phase 0 现场确认清单

你已确认：**HAOS 专用设备、钉钉主通知、已有语音终端、暂无门磁/协调器**。开始前还差：

1. HA 的局域网地址（路由器 DHCP 列表或 HA App 查看）。
2. 语音终端型号与协议（Voice PE / ESPHome / 小智 / 普通音箱）。
3. 钉钉机器人 Webhook 与签名密钥。
4. 摄像头主/子码流密码是否就是 `admin` 账号当前密码（无需贴出，确认可用即可）。

拿到第 1 项即可开始 Phase 0/1；第 2 项决定 Phase 3 走 A 还是 B/C。
