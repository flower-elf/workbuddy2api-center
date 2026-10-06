# WorkBuddy2API-Center — 多账号网关

<p align="center">
  <a href="https://github.com/flower-elf/workbuddy2api-center/releases"><img src="https://img.shields.io/badge/Release-v0.1.0-2496ED?style=flat-square" alt="Version 0.1.0"></a>
  <img src="https://img.shields.io/badge/Python-3.9+-blue.svg?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/API-OpenAI_Compatible-412991?style=flat-square" alt="OpenAI API">
  <img src="https://img.shields.io/badge/Dual_Realm-Intl_&_CN-0DBD8B?style=flat-square" alt="Dual Realm">
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/Vibe_Coding-100%25-ff69b4?style=flat-square" alt="Vibe Coding">
</p>

把腾讯 **[www.workbuddy.ai](https://www.workbuddy.ai)** 国际版与 **[codebuddy.cn](https://www.codebuddy.cn)** 国内版的服务封装成 OpenAI 兼容接口。Codex、Claude Code 等客户端指向同一个地址即可使用；账号池、出口、密钥与配额都在 Web 看板上管理。程序只使用 Python 标准库。

> 本项目由 **吃白饭的蓝色大肥鱼(Deepseek-V4.1-Flash)** 配合开发：人类开发者提供架构与需求，大肥鱼完成界面编写与后端改进。

> 本仓库基于上游项目 **[ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)** 开发，看板界面参照 **[ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager)**；完整的致谢与引用见 [七、致谢与引用声明](#七致谢与引用声明)。

## 特性

- **四种协议**：Chat Completions、Completions、Responses 与 Anthropic Messages；
- **流式纠错**：上游中断、报错或缺少收尾事件时按各自协议返回错误收尾，半段回答不会被当成完成；上游心跳原样转发；
- **智能调度**：国际版与国内版分别调度；尽可能节省 token；
- **出站身份**：账号可在 WB、VSC、CLI 三种官方身份之间切换。
- **密钥分配**：每把 API Key 可以绑定出口，设置模型白名单、有效期、Token 配额、积分配额与 IP 白名单；
- **代理槽**：账号可以绑定固定的代理出口，支持自动发现 mihomo 端口；
- **用量审计**：每条请求记下模型、Token、积分、密钥、来源 IP、状态码与本次实际使用的思考档位；
- **本地网络工具**：代为执行客户端声明的 `web_search` 与 `web_fetch`；
- **零依赖**：只需要 Python 3.9 以上，提供 Windows、macOS、Linux 启动脚本与 Docker 镜像。

## 目录

- [一、快速开始](#一快速开始)
- [二、客户端接入](#二客户端接入)
- [三、看板](#三看板)
- [四、核心机制](#四核心机制)
- [五、接口一览](#五接口一览)
- [六、命令行参数与环境变量](#六命令行参数与环境变量)
- [七、致谢与引用声明](#七致谢与引用声明)
- [八、免责声明](#八免责声明)

---

## 一、快速开始

### 本机运行

需要 Python 3.9 或更新版本，不需要 `pip install` 任何东西。

| 系统 | 启动方式 |
|---|---|
| Windows | 双击 `start-wb-proxy.bat`，保持窗口运行 |
| macOS | 双击 `start-wb-proxy.command`；首次被 Gatekeeper 拦截时右键选择「打开」 |
| Linux 与 macOS 终端 | `./start-wb-proxy.sh`，可以在后面加端口号，例如 `./start-wb-proxy.sh 9000` |

macOS 与 Linux 的脚本按顺序查找包内的 `python/bin/python3`、`/usr/bin/python3` 与 Homebrew 的 python3。macOS 上没有 Python 时，执行 `xcode-select --install` 或 `brew install python`。zip 解压后提示权限不足时，先执行：

```bash
chmod +x start-wb-proxy.sh start-wb-proxy.command start-wb-proxy-lan.sh start-wb-proxy-lan.command allow-firewall.command
```

启动后：

- **API 地址**：`http://127.0.0.1:8788/v1`
- **看板**：`http://127.0.0.1:8788/`，默认密码 `admin`

首次启动账号池为空。登录看板后，点击「账号」页的 **「添加账号 (OAuth)」**，扫码或打开授权链接完成登录，即可开始使用。

### 面板密码

面板密码只用于打开看板，与 API Key 相互独立。默认是 `admin`，登录后请立即在「设置 → 面板密码」里修改，也可以在启动时用 `--panel-password` 指定。密码以 PBKDF2-SHA256 摘要保存在 `accounts/settings.json`。登录状态保存在浏览器会话里，关闭浏览器或重启网关后需要重新登录。

### 局域网共享

让手机、平板或其他电脑访问网关：

- **Windows**：双击 `start-wb-proxy-lan.bat`；
- **macOS**：双击 `start-wb-proxy-lan.command`；
- **终端**：

```bash
./start-wb-proxy-lan.sh               # 端口 8788，自动生成并复用 API Key
./start-wb-proxy-lan.sh 8788 我的Key   # 自定义端口与 Key
```

局域网模式强制要求 API Key。首次启动会生成随机 Key，保存到 `accounts/settings.json` 并打印在终端，重启后继续使用。客户端的 Base URL 填写 `http://<本机局域网IP>:8788/v1`。

macOS 首次监听端口时会询问是否允许 Python 接受连接，选择「允许」；macOS 15 以上还要在「系统设置 → 隐私与安全性 → 本地网络」里允许终端访问。`./allow-firewall.command` 可以查看状态并把 Python 加入允许列表。

### Docker

```bash
docker compose up -d          # 构建并在后台启动
docker compose logs -f        # 查看日志
```

也可以使用 GHCR 上的预编译镜像。正式版同步更新 `latest` 标签，预发布版只有版本标签：

```bash
docker run -d --name wb-proxy-center --restart unless-stopped -p 8788:8788 \
  -v $(pwd)/accounts:/app/accounts -v $(pwd)/usage:/app/usage \
  -e API_KEY=your_secret_key ghcr.io/flower-elf/workbuddy2api-center:latest
```

- **数据目录**：`./accounts` 保存账号凭证、设置与模型目录缓存，`./usage` 保存请求记录与积分变动记录，两者都要挂载；
- **鉴权**：容器以局域网模式启动。不传 `API_KEY` 时会生成一个，执行 `docker compose logs wb-proxy-center | grep -i "api key"` 查看；
- **修改端口**：`PORT` 决定容器内监听的端口，端口映射的右侧必须与它相同，例如 `-e PORT=9000 -p 9000:9000`；

### 运行测试

```bash
python tests/run_all.py            # 全部套件
python tests/run_all.py realm      # 只运行名字里含 realm 的套件
```

共 55 个套件：42 个 Python 与 13 个 JavaScript。JavaScript 套件需要 Node.js 22 或更新版本，没有安装时跳过并提示。GitHub Actions 在 Ubuntu 的 Python 3.9 与 3.12、Windows 的 Python 3.12 上运行同一条命令。`tests/manual/_mobile_check.py` 是手动运行的 Playwright 布局检查器，不在套件里。

---

## 二、客户端接入

- **Base URL**：`http://127.0.0.1:8788/v1`，局域网为 `http://<局域网IP>:8788/v1`；
- **API Key**：本机运行且没有配置任何 Key 时可以留空或任意填写；配置过 Key 或开启局域网模式后，使用「密钥」页里的 Key；
- **模型名称**：`/v1/models` 里列出的任意模型 ID，例如 `deepseek-v4.1-flash`、`glm-5.3`。

**Codex CLI 等 Responses 客户端**

```bash
export OPENAI_BASE_URL="http://127.0.0.1:8788/v1"
export OPENAI_API_KEY="你在「密钥」页添加的 API Key"
```

**Claude Code 等 Messages 客户端**

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8788"
export ANTHROPIC_AUTH_TOKEN="你在「密钥」页添加的 API Key"
export ANTHROPIC_MODEL="deepseek-v4.1-flash"
```

**curl**

```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H "Authorization: Bearer 你的Key" \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "你好"}]}'
```

所有 `/v1/...` 接口都可以去掉 `/v1` 前缀访问，例如 `/chat/completions`，方便把 Base URL 填成根地址的客户端。

---

## 三、看板

打开 `http://127.0.0.1:8788/`。底部菜单栏切换页面，右上角切换正在查看的版本（国际版 / 国内版），`Ctrl+K`（macOS 为 `⌘K`）搜索页面。「面板选项」里可以切换主题、调整自动刷新间隔，或退出登录。

| 页面 | 内容 |
|---|---|
| 仪表盘 | 账号数、可用账号、积分余额、今日请求与 Token；近 14 天调用趋势；切换网关默认出口；调度器状态 |
| 账号 | 搜索、筛选与排序账号；每个账号的积分、有效期、出站身份、优先级、代理槽、备注与操作按钮 |
| 任务 | 调度器状态与日志、国内版签到与成长任务、国际版每日活跃打卡、积分变动记录 |
| 积分 | 每个账号每一笔套餐的来源、已用、剩余与到期时间 |
| 密钥 | API Key 的出口、模型限制、有效期、配额、IP 白名单、用量与接入示例 |
| 模型 | 模型目录与能力规格；取消勾选的模型会被拒绝，也不再出现在 `/v1/models` |
| 测试台 | 用面板会话直接调试模型，可以指定账号 |
| 用量 | 按时间范围统计请求、Token、积分与缓存命中，按账号、密钥与模型细分 |
| 请求日志 | 每条请求的全部字段，可以按时间、版本、密钥、状态、模型、账号与来源 IP 筛选 |
| 运行日志 | 网关控制台输出，可以复制、导出或清空 |
| 设置 | 网关行为、代理槽、面板密码与运行信息 |

### 账号

- **添加账号**：「账号」页的「添加账号 (OAuth)」→ 选择国际版或国内版 → 扫码或打开授权链接登录，网关自动检测回调；
- **优先级**：数字越小越先被选中，默认 100；
- **并发上限**：限制同一账号同一模型同时在途的请求数，填 0 表示不限，提示里会显示当前在途数；
- **出站身份**：在 WB、VSC、CLI 之间切换，切换后立即保存；
- **测试**：向这个账号发送一条内容为 `hi` 的真实对话，确认它能否正常回复；
- **备注**：最长 100 字，保存在账号文件里，可以用来搜索；
- **导入与导出**：导出的文档可以再次导入，也可以手写账号列表导入。

从桌面客户端导入账号暂不可用：桌面客户端自 2026-09-24 起加密存储令牌，网关读不出可用的令牌，因此看板隐藏了这个入口。相关代码保留在 `app/web/js/pages/accounts/dialogs.js` 的 `openDesktopScanDialog()` 与接口 `/accounts/import/desktop`。

### 积分

- **积分构成**：「积分」页按来源列出每个账号的套餐，同一来源的多笔到账合并成一行，可以展开查看每一笔；
- **积分到期**：账号的积分旁显示最近一笔「额度 · N 天后到期」，一天内到期标红，七天内标黄；
- **积分变动**：每次读取余额时与上一次比较，余额增加就在 `usage/credit_events.jsonl` 记一条，只保留最近 30 天；「任务」页分页显示，每页 20 条。

### 密钥

不同客户端使用不同的 Key，每把 Key 可以单独设置：

- **出口**：固定走国际版或国内版；不绑定时跟随网关默认出口；
- **模型限制**：允许调用的模型，支持 `*` 通配，例如 `deepseek*`。不在列表里的模型请求直接返回 400，不发往上游；
- **有效期与配额**：过期返回 401，Token 或积分配额用完返回 429；
- **IP 白名单**：每行一个 IP 或 CIDR，来源不在名单里返回 403；
- **用量**：统计从上次「重置用量」起累计；
- **区域校验**：Key 绑定的出口不提供所请求的模型时，直接返回 400 并说明原因。

Key 保存在 `accounts/settings.json`。在看板保存过 Key 之后，启动参数 `--api-key` 不再生效。完整的 Key 只能通过面板会话读取。

### 模型冷却

上游对某个账号的某个模型返回 429 时，只有这个模型进入冷却，账号仍然服务其他模型。恢复时间依次取自上游消息里写明的重置时刻、HTTP `Retry-After` 头，两者都没有时冷却 60 秒。冷却状态只保存在内存里，重启后清空。

---

## 四、核心机制

### 账号调度

每次请求按以下规则选择账号：

1. **可用性**：跳过停用、冷却中、余额不高于保留积分、今日 Token 已达限额的账号；
2. **会话粘性**：同一段对话固定使用同一个账号，账号不能接单时才换号。客户端没有给出会话标识时，按前两条消息识别对话。默认打开，开关在「设置 → 网关行为 → 会话粘性」，关闭后每一轮请求都重新选择账号；
3. **分配规则**：「智能分配」打开时进行智能分配，关闭时按优先级与轮询顺序分配。开关在「设置 → 网关行为 → 智能分配」，默认打开。

**其他规则**：

- **保留积分**：余额不高于设定值时暂停接单，避免余额用到 0 后收到上游提醒短信。填写 0 表示关闭，根据最近一次查询到的余额判定；
- **每日 Token 限额**：今日用量达到设定值时暂停接单，本地时间 0 点后恢复。填写 0 表示不限额；
- **单账号单模型并发上限**：每个账号可以在账号页单独设一个数，限制「同一账号 + 同一模型」同时在途的请求数。上限按模型分开计数，同一账号的 `glm-5.3` 和 `deepseek` 各自算一份，互不占用名额；填 0 表示不限。名额满的账号在选号时直接跳过，请求交给其他账号；对话绑定的账号名额暂满时换号接手，绑定跟着接手的账号走：接手账号重算了完整前缀，缓存从此在它那里。本地网络工具的后续轮次沿用这次请求已经占着的名额，换号时名额随之转到新账号。整个出口的可用账号都满时返回 429，测试台指定的账号名额满时同样返回 429。在途数量只存在内存里，重启后归零，账号页的「并发上限」列会提示当前正在服务多少个请求。

### 出站身份

每个账号有三种官方身份，对应不同的出站端点与配额：

- **WB**：WorkBuddy 桌面客户端；
- **VSC**：VSCode 插件，国内版走 `www.workbuddy.cn`；
- **CLI**：CodeBuddy CLI。

### 设备指纹

用账号 UID 加固定盐值做单向哈希，派生机器码与会话标识。同一账号每次出站都是同一台虚拟设备，不同账号之间互不关联。

### 模型目录

- **来源**：上游 `GET /v3/config` 的 `agents[cli].models`，与官方桌面端一致；上游新增的模型无需发版即可出现在 `/v1/models`；
- **过滤**：去掉代码补全专用模型（如 `codewise-*`）、多云专线变体（如 `*-volc`）、档位别名、`auto`、`-sg` 与 `-x` 变体；同名模型只保留 0.00 倍率的一档；
- **缓存**：成功获取后保存到 `accounts/cache/model_catalog.json`，24 小时内直接使用；获取失败时依次使用过期缓存、桌面端本地文件与内置快照。

国际版与国内版的同名模型分别调度，请求只走当前出口，暂不跨区域混合轮询。

### 定时任务

调度器按 UTC+8 运行，与机器的时区设置无关：

| 时间 | 任务 |
|---|---|
| 每天 09:00 与 21:00 | 国内版签到与猫猫旅行；国际版每日活跃打卡 |
| 每天 22:00 | 令牌剩余有效期不足 2 小时的账号自动刷新 |
| 每天 01:00 | 夜猫子任务 |

「任务」页可以手动触发巡检、停止或启动调度器，也可以按账号手动执行签到与打卡。

- **国内版成长任务**：自动接取任务、上报行为事件并领取奖励；
- **猫猫日常**：在家时自动派出，回来后先领奖再派出，避免奖励作废；
- **国际版每日活跃打卡**：为当天还没活跃的账号发送一条轻量对话，每个账号每天只执行一次，记录在账号文件的 `lastDailyChat`；
- **网页通道打卡**：按网页端的流程完整执行一轮会话，领取每日 30 积分，Pro 账号为 50 积分。它会消耗少量积分，所以点击按钮后先弹出确认；一轮最多等待 120 秒，可以用 `WB_WEB_TURN_TIMEOUT` 调整；「设置 → 网关行为」里可以关闭。

### 代理槽

在「设置 → 代理槽」里配置，账号在「账号」页选择绑定哪个槽位：

- **跟随**：槽位地址变化时，绑定的账号自动使用新地址；槽位停用或删除时，账号改为直连；
- **自动发现**：扫描同一 Docker 网络里的 mihomo 出口，默认主机 `cli-proxy-mihomo`、端口 `17901-17910`，显示每个出口的公网 IP 与延迟；
- **自动分配**：把启用中的账号依次分配到各个槽位；
- **测试**：经过这个代理真实访问一次外网。

### 本地网络工具

有些客户端（如 Codex App）会声明 `web_search` 与 `web_fetch` 工具，上游无法执行它们。打开「设置 → 网关行为 → 本地网络工具」后，网关在本地执行这两个工具，再把结果交给模型：

- **搜索**使用 DuckDuckGo HTML 版，国内网络下可能无法连接；
- **抓取**只访问公网地址：域名解析出本机、私网、链路本地或保留地址时拒绝，每次重定向都重新检查，最多 5 次；单个响应最多 4 MiB，一次抓取最多 40 秒；
- **轮数**默认最多 3 轮，可以用 `WB_MAX_WEB_ROUNDS` 调整，上限 8；每一轮都会多调用一次上游。

默认关闭，关闭时工具声明原样转发。

### Messages 接口

`POST /v1/messages` 按 Anthropic Messages 协议收发，支持 `system`、多轮对话、图片、工具调用、思考内容与流式输出，错误也按该协议的格式返回。翻译方式在「设置 → 网关行为 → Messages 接口翻译」里选择：

- **openai-completions**（默认）：翻译成对话补全请求；
- **openai-responses**：翻译成 Responses 请求，客户端使用 Responses 风格的自定义工具时选这一项。

### 流式转发

Chat Completions 的收尾事件是 `data: [DONE]`，Responses 是 `response.completed`，Messages 是 `message_stop`。

上游把流截断时（连接中断、读取异常、上游自己发错误帧、没有收尾事件就结束），网关按各自协议换成错误收尾，客户端据此把这条回答当作失败：

- Chat Completions 发一个 `{"error": {...}}` 事件，不再补 `[DONE]`；
- Responses 发 `response.failed`，响应体 `status` 为 `failed`，`error` 里带上原因与上游的错误码；
- Messages 发 `error` 事件，不再发 `message_stop`；
- 非流式请求返回 `502`。

已经流出去的正文照原样保留，网关不会重新请求上游重放一遍：重新请求会再次生成、再次计费。上游在思考期间发的 SSE 注释心跳（`: ping`）原样转发，客户端自己的空闲超时不会在长静默时把连接先断开。

---

## 五、接口一览

**对外接口**：鉴权用 `Authorization: Bearer` 或 `x-api-key`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /v1/chat/completions | Chat Completions 协议，支持流式 |
| POST | /v1/completions | 旧版补全协议 |
| POST | /v1/responses | Responses 协议，支持流式与自定义工具 |
| POST | /v1/messages | Anthropic Messages 协议 |
| GET | /v1/models | 模型列表，含能力、规格、目录来源 `source` 与获取时间 `fetched_at` |

**健康检查**：`GET /health` 不需要凭据，用于外部探活：能接单时返回 `200` 与 `{"ok": true}`；没有账号可以处理请求或启动没有完成时返回 `503` 与 `{"ok": false, "error": "..."}`

**看板接口**：需要面板会话，由浏览器登录后自动携带。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | / | Web 看板，静态资源经 `/web/<路径>` 提供 |
| GET / POST | /realm | 读取或切换网关默认出口 |
| GET | /panel/status | 运行信息：版本、账号统计；没有面板会话返回 401 |
| GET | /panel/models | 模型目录的管理视图，含被关掉的模型 |
| POST | /panel/login | 面板登录，返回会话令牌 |
| POST | /panel/logout | 退出登录 |
| POST | /panel/password | 修改面板密码 |
| GET | /accounts | 账号列表 |
| GET | /accounts/export | 导出账号文档 |
| POST | /accounts/import | 导入账号文档，支持 `dryRun` 与 `overwrite` |
| POST | /accounts/import/desktop | 导入桌面端凭据，当前不可用 |
| POST | /accounts/refresh | 刷新一个或全部账号的令牌 |
| POST | /accounts/test | 测试账号，可以临时指定模型 |
| POST | /accounts/set | 修改单个字段：`enabled`、`priority`、`note`、`concurrencyLimit`、`proxy`、`proxySlot` |
| POST | /accounts/set-all | 启用或停用全部账号 |
| POST | /accounts/delete | 删除账号 |
| POST | /accounts/product | 切换出站身份：WB、VSC、CLI |
| POST | /accounts/checkin | 国内版签到 |
| POST | /accounts/daily-chat | 国际版每日活跃：桌面端对话 |
| POST | /accounts/daily-chat-web | 国际版每日活跃：网页通道 |
| POST | /accounts/login/start | 发起 OAuth 登录，返回授权链接与二维码 |
| GET | /accounts/login/poll | 查询登录状态 |
| POST | /accounts/login/cancel | 取消登录 |
| GET / POST | /accounts/credits | 刷新积分：GET 刷新全部账号，POST 可以用 `uid` 或 `realm` 指定范围 |
| GET | /accounts/credit-events | 最近 30 天的积分变动记录，按 `realm` 筛选，`limit` 与 `offset` 分页 |
| GET | /usage | 用量汇总，可选 `realm` 与时间范围 |
| GET | /usage/recent | 请求记录，可以按 `realm`、`key`、`status`、`model`、`account`、`ip`、`range` / `since` / `until` 筛选并分页 |
| GET | /usage/trend | 调用趋势：`days=N` 按天，`range=today` 按小时 |
| GET | /usage/analytics | 按账号、模型与密钥的统计 |
| GET | /usage/by-account | 按账号的用量明细 |
| GET | /usage/perf | 首字延迟与生成速度 |
| GET | /tasks | 国内版成长任务、连续打卡与猫猫日常状态 |
| POST | /tasks/run | 领取国内版成长任务奖励 |
| POST | /tasks/travel | 猫猫旅行：派出或领奖 |
| GET | /scheduler | 调度器状态与日志 |
| POST | /scheduler/trigger | 立即执行一次巡检 |
| POST | /scheduler/toggle | 停止或启动调度器 |
| GET | /settings | 读取设置与每把 Key 的用量、状态 |
| POST | /settings/save | 保存设置；`reset_key_usage` 重置某把 Key 的用量 |
| GET | /settings/reveal | 读取一把 Key 的完整值 |
| GET / POST | /proxy/slots | 读取代理槽列表 |
| POST | /proxy/slots/save | 保存代理槽列表 |
| POST | /proxy/slots/test | 测试一个代理槽 |
| POST | /proxy/discover | 扫描 mihomo 出口，返回公网 IP 与延迟 |
| GET | /logs | 运行日志 |
| GET | /logs/export | 下载日志文件 |
| POST | /logs/clear | 清空运行日志 |

---

## 六、命令行参数与环境变量

### 命令行参数

| 参数 | 说明 |
|---|---|
| `--host` | 监听地址，默认 `127.0.0.1`；环境变量 `HOST` |
| `--port` | 监听端口，默认 `8788`；环境变量 `PORT` |
| `--lan` | 监听全部网卡并强制要求 API Key，相当于 `--host 0.0.0.0` |
| `--api-key` | 要求 `/v1/*` 携带这个 Key；在看板保存过 Key 后此选项会被忽略；也可使用环境变量 `API_KEY` 或 `WB_PROXY_KEY` |
| `--panel-password` | 启动时设置面板密码 |
| `--accounts-dir` | 账号凭证与设置目录，默认 `./accounts`；也可使用环境变量 `ACCOUNTS_DIR` |
| `--usage-dir` | 用量记录目录，默认 `./usage`；也可使用环境变量 `WB_PROXY_USAGE_DIR` |
| `--import-desktop` | 导入桌面端凭据后退出 |
| `--system-prompt` | 请求没有 system 消息时注入的内容 |
| `--user-agent` | 覆盖出站 User-Agent，默认与官方 WorkBuddy 客户端相同 |
| `--info` | 指定 WorkBuddy `*.info` 凭据文件路径 |

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8788` | 监听地址与端口 |
| `API_KEY` / `WB_PROXY_KEY` | 空 | 启动时固定 API Key |
| `ACCOUNTS_DIR` | `./accounts` | 账号凭证与设置目录 |
| `WB_PROXY_USAGE_DIR` | `./usage` | 用量记录目录 |
| `WB_PROXY_DEFAULT_REALM` | `intl` | 启动时的默认出口 |
| `WB_CLIENT_TIMEOUT` | `120` | 客户端连接读写超时，单位秒 |
| `WB_MAX_PAYLOAD_BYTES` | `52428800` | 请求体上限，超出返回 413 |
| `WB_MAX_CONCURRENT_CHAT` | `32` | 同时处理的对话请求数 |
| `WB_CHAT_SLOT_WAIT` | `30` | 并发已满时排队等待的秒数 |
| `WB_MAX_WEB_ROUNDS` | `3` | 本地网络工具最多运行的轮数，上限 8 |
| `WB_WEB_TURN_TIMEOUT` | `120` | 网页通道打卡一轮的等待上限，单位秒 |
| `WB_PROMPT_CACHE_KEY` | `0` | 实验开关：向上游附带 `prompt_cache_key` |
| `WB_PROXY_DISCOVER_HOST` | `cli-proxy-mihomo` | 代理槽自动发现的主机名 |
| `WB_PROXY_DISCOVER_PORTS` | `17901-17910` | 代理槽自动发现的端口范围 |
| `TZ` | `Asia/Shanghai` | 容器时区；定时任务始终按 UTC+8 计算 |

---

## 七、致谢与引用声明

- **[ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)**：本仓库的上游项目，协议转换、账号调度、定时任务与请求侧的风控处理都建立在它的基础上；
- **[ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager)**：看板界面的参考，包括页面划分、卡片与表格布局、底部菜单栏、命令面板与删除确认弹窗；
- **[linux-do/cdk](https://github.com/linux-do/cdk)**：LINUX DO 社区的 CDK 发放平台，本项目用原生 H5 与 JavaScript 重新实现了它的部分视觉风格；
- **[Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)**：成长任务流程分析、设备指纹派生 `derive_id`、整点调度 `Scheduler`、指纹脱敏与 `reasoning_content` 回填。

---

## 八、免责声明

1. 本项目为非官方自托管网关，仅供技术研究学习，以及在私有环境中使用个人合法授权的账号进行测试。
2. 本项目不提供任何账号与额度。请遵守官方服务条款，不得用于除个人家庭使用外的用途，禁止恶意多并发或违规滥用。
