# WorkBuddy2API-Center — 多账号网关


<p align="center">
  <a href="https://github.com/flower-elf/workbuddy2api-center/releases"><img src="https://img.shields.io/badge/Release-v0.1.0-2496ED?style=flat-square" alt="Version 0.1.0"></a>
  <img src="https://img.shields.io/badge/Python-3.9+-blue.svg?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/API-OpenAI_Compatible-412991?style=flat-square" alt="OpenAI API">
  <img src="https://img.shields.io/badge/Dual_Realm-Intl_&_CN-0DBD8B?style=flat-square" alt="Dual Realm">
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/Vibe_Coding-100%25-ff69b4?style=flat-square" alt="Vibe Coding">
</p>

把腾讯 **[www.workbuddy.ai](https://www.workbuddy.ai)** 国际版与 **[codebuddy.cn](https://www.codebuddy.cn)** 国内版的原生服务封装成 OpenAI 兼容接口，一次提供 Chat Completions、Completions、Responses 与 Anthropic Messages 四条协议，并附多账号调度、密钥治理、定时任务与审计看板。程序只使用 Python 标准库，放在自己的电脑、局域网服务器或容器里都能得到一个统一入口：Codex、Claude Code 这类客户端指向同一个地址，账号池、出口与配额都在看板上管理。

**特性一览**

- **零第三方依赖**：只需要 Python 3.9 以上；发布包自带精简 Python，Windows、macOS 与 Linux 都提供启动脚本；
- **多账号池与双区域调度**：国际版与国内版分别配置与调度；看板右上角切换正在查看的版本，网关默认出口在仪表盘切换；出口还可以固定绑在 API Key 上，不同客户端各走各的出口；
- **出站身份与配额通道**：每个账号可以在 WB、VSC、CLI 三种官方身份之间切换，三者对应不同的出站指纹与配额线；打开「429 自动切换出站身份」后，账号撞到限流会自动换一条通道重试；
- **稳定的设备指纹隔离**：按账号 UID 派生机器码与会话标识，同一账号每次出站都来自同一台虚拟设备，账号之间互不关联；
- **模型清单跟随上游**：与官方桌面端使用同一份清单，自动去掉行内补全专用模型与多云专线变体，上游新增模型不发版即可出现在 `/v1/models`；看板可以逐个勾选启用，被关掉的模型对客户端隐藏；
- **OAuth 登录**：在看板点授权链接或扫码完成登录，账号自动加入账号池；
- **代理槽**：每个账号可以绑定一个代理出口；看板能从 mihomo 自动发现本地端口、逐个测试连通性并自动分配给账号；
- **国内版自动化**：每日签到、成长任务与积分任务自动接取并领取，猫猫日常自动出发与领奖，夜猫子任务按整点执行；
- **国际版每日活跃打卡**：自动发起桌面端轻量对话，可选再走一次网页通道并把这一轮会话执行到结束，领取官方每日活跃积分；
- **后台定时调度器**：按 UTC+8 在 09:00、21:00、22:00 与 01:00 执行签到、旅行、保活与夜猫任务，排程日志留在看板上；
- **账号调度规则**：优先级、会话粘性、保留积分与每日 Token 限额共同决定每次请求交给哪个账号；
- **密钥治理**：出口绑定、模型白名单、有效期、Token 配额、积分配额与 IP 白名单，以及每把 Key 的用量统计与重置；
- **用量与审计**：每条请求记下模型、Token、耗时、积分、密钥、来源 IP、状态码与接口类型，请求日志可以按多个维度筛选并查看全部字段；
- **本地网络工具**：代为执行客户端声明的 `web_search` 与 `web_fetch`，只放行公网地址，本机与私网一律拒绝；
- **Web 看板**：仪表盘、账号、任务、积分、密钥、模型、测试台、用量、请求日志、运行日志与设置共十一个页面，底部浮动菜单栏切换，支持浅色与深色主题、`Ctrl+K` 搜索页面与手机窄屏；
- **测试与持续集成**：52 个套件，一条命令全部执行；GitHub Actions 在 Ubuntu 的 Python 3.9 与 3.12、Windows 的 Python 3.12 上跑同一套测试。

## 目录

- [一、快速开始](#一快速开始)
- [二、看板](#二看板)
- [三、核心机制](#三核心机制)
- [四、客户端接入](#四客户端接入)
- [五、接口一览](#五接口一览)
- [六、命令行参数与环境变量](#六命令行参数与环境变量)
- [七、版本更新记录](#七版本更新记录)
- [八、致谢与引用声明](#八致谢与引用声明)
- [九、免责声明](#九免责声明)

> 本项目由 **吃白饭的蓝色大肥鱼(Deepseek-V4.1-Flash)** 配合开发：人类开发者提供架构与需求，大肥鱼完成界面编写与后端改进。

> 本仓库基于上游项目 **[ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)** 开发，看板界面参照 **[ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager)**；完整的致谢与引用见 [七、致谢与引用声明](#七致谢与引用声明)。

---

## 一、快速开始

### 运行环境

- **Python 3.9 或更新版本**：启动脚本会使用系统 PATH 里面的 python，请确保 python 被正确安装在系统上；
- **macOS 与 Linux 的查找顺序**：包内 `python/bin/python3`、`/usr/bin/python3`、Homebrew 的 python3；macOS 上未安装 Python 时，可以先执行 `xcode-select --install`，或者用 `brew install python`；
- **零第三方依赖**：不需要 `pip install` 任何东西；
- 只有运行看板的 JavaScript 测试才需要 **Node.js 22 或更新版本**，缺少时对应的套件会被跳过并提示。

### 本机运行

- **Windows**：双击 **`start-wb-proxy.bat`**，保持窗口运行；
- **macOS**：双击 **`start-wb-proxy.command`**，首次被 Gatekeeper 拦截时右键选择「打开」确认一次；
- **Linux 与 macOS 终端**：

```bash
./start-wb-proxy.sh          # 默认 8788 端口
./start-wb-proxy.sh 9000     # 自定义端口
```

启动后：

- **API 接口地址**：`http://127.0.0.1:8788/v1`
- **Web 监控看板**：`http://127.0.0.1:8788/`

首次启动账号池为空，打开看板点底部菜单栏的「添加账号」或「账号」页的 **「添加账号 (OAuth)」** 完成授权，账号会自动加入账号池。

> zip 解压后若提示权限不足，先执行一次：
> `chmod +x start-wb-proxy.sh start-wb-proxy.command start-wb-proxy-lan.sh start-wb-proxy-lan.command allow-firewall.command`

### 局域网共享

允许局域网内的其他设备（手机、平板、协同电脑）访问：

- **Windows**：双击 `start-wb-proxy-lan.bat`；
- **macOS**：双击 `start-wb-proxy-lan.command`，或：

```bash
./start-wb-proxy-lan.sh              # 端口 8788，自动生成并复用 API Key
./start-wb-proxy-lan.sh 8788 我的Key  # 自定义端口与 Key
```

- **Base URL**：`http://<本机局域网IP>:8788/v1`；带密钥直达面板：`http://<IP>:8788/?key=生成的Key`；
- **API Key**：不使用固定的默认密钥。首次启动生成高强度随机 Key，保存到 `accounts/settings.json` 并打印在终端，重启后继续使用；也可以在第二个参数里传入自己的 Key；
- **macOS 防火墙**：首次监听端口时系统会询问是否允许 Python 接受连接，选择「允许」；macOS 15 以上还需要在「系统设置 → 隐私与安全性 → 本地网络」中允许终端访问。可以用 `./allow-firewall.command` 查看状态并把 Python 加入允许列表。

### 面板访问密码

打开看板需要先输入**面板访问密码**（默认 `admin`）。它与 API Key 相互独立，只用于打开看板，可以在「设置 → 面板密码」里修改。也可以在启动时用 `--panel-password` 指定。密码以 PBKDF2-SHA256 摘要保存在 `accounts/settings.json`；把密码写进地址栏的 `?pwd=` 参数在读取后会从地址栏移除。登录状态保存在浏览器会话中，关闭浏览器或重启网关后需要重新输入。

> 首次登录后推荐立即修改默认密码。

### Docker 容器

自带完整的容器配置，不需要额外安装依赖：

```bash
docker compose up -d          # 后台启动（自动构建）
docker compose logs -f        # 查看网关日志
```

也可以直接使用 `docker run`：

```bash
docker run -d --name wb-proxy-center --restart unless-stopped -p 8788:8788 \
  -v $(pwd)/accounts:/app/accounts -v $(pwd)/usage:/app/usage \
  -e API_KEY=your_secret_key $(docker build -q .)
```

每次 GitHub Release 发布后，也可以从 GHCR 拉取预编译镜像运行（正式版同步更新 `latest`，预发布版只有版本标签）：

```bash
docker pull ghcr.io/flower-elf/workbuddy2api-center:latest
docker run -d --name wb-proxy-center --restart unless-stopped -p 8788:8788 \
  -v $(pwd)/accounts:/app/accounts -v $(pwd)/usage:/app/usage \
  -e API_KEY=your_secret_key ghcr.io/flower-elf/workbuddy2api-center:latest
```

GHCR 新包默认私有；如需免登录拉取，首次发布后在 Packages 设置中将其改为 Public。保持私有时需要先登录 `ghcr.io`。

- **持久化目录**：`./accounts`（账号凭证、面板设置与模型目录缓存）与 `./usage`（请求流水与积分变动记录）；
- **配置参数**：环境变量 `API_KEY`、`PORT` 与 `TZ`；
- **改 `PORT` 时要同步改端口映射**：`PORT` 决定容器内监听的端口，`-p HOST:CONTAINER` 的**右侧必须与它一致**，例如 `-e PORT=9000 -p 9000:9000`；只改 `PORT` 而映射仍是 `8788:8788`，请求会发到没有监听的端口。用 compose 时 `ports` 与 `PORT` 要同时改，默认的 `8788:8788` 与 `PORT=8788` 本来就一致。
- **鉴权**：容器以 `--lan` 启动（监听 `0.0.0.0`），生成的 API Key 写入 `./accounts/settings.json` 并打印在启动日志里：
  `docker compose logs wb-proxy-center | grep -i "api key"`。不带这个 Key 调用 `/v1` 会收到 401；想用自己的 Key 就传 `-e API_KEY=...`。

### 运行测试

全部测试集中在 `tests/`，一条命令运行：

```bash
python tests/run_all.py            # 全部套件
python tests/run_all.py realm      # 只运行名字里含 realm 的
```

- **52 个套件：39 个 Python 与 13 个 JavaScript**；JS 套件需要 PATH 上有 Node.js 22 或更新版本，看板模块与 JS 套件都是 ESM 文件，缺失时会跳过并提示；
- `tests/manual/_mobile_check.py` 是独立的 Playwright 手机与桌面布局检查器（需要自行安装 Playwright 与 Firefox），逐页检查横向溢出、表格容纳、菜单栏收起与展开、弹窗宽度与触控尺寸，按需手动运行，不在上面的套件里；
- CI（`.github/workflows/tests.yml`）运行同一条命令：Ubuntu 上 Python 3.9 与 3.12，3.9 是本项目声明的最低版本；Windows 上 Python 3.12。

---

## 二、看板

访问 `http://127.0.0.1:8788/` 即可使用集成看板。底部浮动菜单栏按「总览 / 运营 / 治理」分组，鼠标靠近时图标放大，桌面端可以长按空白处拖动位置，窄屏收起为右下角按钮；右上角切换正在查看的版本（国际版 / 国内版），`Ctrl+K`（macOS 为 `⌘K`）打开页面搜索；「面板选项」可以切换浅色、深色或跟随系统的主题，调整自动刷新间隔（默认 60 秒，填写 0 表示不自动刷新，只影响本浏览器），也可以退出登录。积分页在打开时距离上次读取超过这个间隔才会重新读取，停留期间不自动刷新；运行日志页保持每 2 秒实时跟随，由页头的「实时监听中 / 已暂停监听」控制。

### 页面一览

| 页面 | 内容 |
|---|---|
| 仪表盘 | 账号总数、可用账号、积分余额、今日请求与 Token 四张卡片；近 14 天调用趋势，可在两者、请求数与 Token 之间切换；网关默认出口切换与调度器状态；账号健康快照 |
| 账号 | 账号池的搜索、筛选、排序与分页；每行的积分、有效期、出站身份、优先级、代理槽、备注与各项操作 |
| 任务 | 调度器状态与排程日志、国内版签到与成长任务、国际版活跃打卡、积分变动记录 |
| 积分 | 按账号列出每一笔套餐的来源、说明、已用与剩余、周期与到期时间，同一个来源的多笔到账归并成一行、可以逐笔展开 |
| 密钥 | API Key 的出口绑定、模型限制、有效期、配额、IP 白名单、用量与接入片段 |
| 模型 | 模型目录与能力规格、目录来源与获取时间，每个模型一个「处理」勾选框，被关掉的模型从 `/v1/models` 里消失 |
| 测试台 | 用面板会话直接流式试调模型，显示每条回答的 Token 与积分，可以随时终止；请求默认交给账号池调度，也可以选定一个账号，只让它服务这次调试 |
| 用量 | 今日 / 本周 / 本月 / 全部 / 自定义区间的请求、Token、积分与缓存命中，趋势图可在请求数与 Token 之间切换或同屏显示，按账号、按密钥与按模型的统计 |
| 请求日志 | 表格里每条记录的版本以「国内版 / 国际版」标签给出，默认只显示当前查看的版本，可按时间、版本、密钥、状态、模型、账号与来源 IP 筛选，点一行查看全部字段 |
| 运行日志 | 网关控制台输出，可以复制、导出为文件或清空 |
| 设置 | 网关行为、代理槽、面板密码与运行信息四个标签；面板密码在弹窗里修改 |

「用量」页的统计范围中，本周从周一零点起算，本月从 1 号零点起算；自定义可以指定起止时间，终点留空表示到现在。

### 看板前端

看板前端位于 `app/web/`：`index.html` 是页面外壳，`css/` 放设计变量、基础排版与通用组件样式，`css/pages/` 每个页面一份样式；`js/core/` 是请求、路由、状态与格式化，`js/ui/` 是弹窗、提示、菜单栏、命令面板与图表等通用组件，`js/pages/<页面>.js` 是页面入口，`js/pages/<页面>/logic.js` 放可以在 Node 里测试的纯逻辑；`vendor/` 是随项目分发的第三方库（lit-html、lucide、Chart.js、qrcode-generator、eventsource-parser），不需要构建步骤。网关经 `/web/<路径>` 提供这些静态资源，按 ETag 协商缓存。

### 账号

在「账号」页操作。页面顶部可以按昵称、UID、文件名或备注搜索，按「可用 / 需处理 / 冷却中 / 已停用」筛选，按有效期、积分或优先级排序；每页 20 个账号，窄屏下表格换成卡片列表。

**添加账号（OAuth）**

1. 点击底部菜单栏的「添加账号」，或「账号」页的 **「添加账号 (OAuth)」**；
2. 选择要登录的区域（国际版 / 国内版），用手机扫描弹窗里的二维码，或打开授权链接在浏览器里完成登录；
3. 程序自动检测回调，账号会自动加入账号池，不需要手动复制凭证。关闭弹窗会取消这次登录。

**从本地桌面应用导入暂不可用**：桌面客户端自 2026-09-24 起把 `accessToken` 与 `refreshToken` 改为加密存储（`$wbEncrypted` 信封）。扫描仍能读到文件，但无法取得可用的 token，导入后每个请求都会返回 401，聊天、刷新凭证、查积分都会被拒绝。看板上的「扫描桌面客户端账号」入口已隐藏，请使用上面的 OAuth 方式添加账号。相关代码保留未删（前端 `app/web/js/pages/accounts/dialogs.js` 的 `openDesktopScanDialog()` 与后端 `/accounts/import/desktop`），等解密可用或改用其他凭据来源后再放出。

**账号行上的操作**

- **备注**：可以给账号写一句备注（最长 100 字），保存在账号文件里，导出与导入时一并带上，搜索框也能按备注查找；
- **优先级**：数字越小越优先被选中，默认 100，改完立即生效并保存；
- **出站身份**：在 WB、VSC、CLI 之间切换，切换时弹确认，切换后立即写入凭证文件；
- **代理槽**：给账号绑定一个代理出口，也可以保持直连；
- **测试**：向该账号自己的上游端点发一条真实对话请求（内容固定为 `hi`），确认账号当前能否正常出话，结果里会带回本次实际使用的模型；
- **启用与停用、删除、导入与导出**：删除与导入都有确认弹窗；导出的文档带 `workbuddy-accounts` 格式标记，可以再次导入，也可以手写账号列表导入。

**积分构成、积分到期与积分变动**

- **积分构成**：「积分」页按账号列出每个来源给了多少积分、还剩多少、什么时候到期。来源取自上游的套餐名称与说明（例如「拉新权益包」与「腾讯云代码助手 (IDE) - 赠送包」），同一个来源分多笔到账时归并成一行并写明笔数，点开可以看每一笔的周期、总额、已用与到期时刻；页头的「刷新全部」按当前查看的版本重新读取，账号卡片右上角的「刷新」只读取这一个账号；
- **积分到期**：读取余额时同时解析每个套餐的到期时间（上游给的是 UTC+8 时间），积分列旁显示最近一笔「额度 · N 天后到期」，一天内到期标红、七天内标黄，悬停可看全部套餐；仪表盘的积分卡片同时提示最近一笔的到期日期；
- **积分变动记录**：每次读取余额后与上一次比较，余额增加就在 `usage/credit_events.jsonl` 记一条（签到、打卡、旅行与套餐到账都会体现），首次读取只建立基线；「任务」页的「积分变动记录」显示这些记录。

**模型冷却**：若上游对某账号的单个模型返回 429，账号行会显示受限模型与预计恢复时间（浏览器本地时间），该账号仍可继续服务其他模型。恢复时间取自上游 `code 6004` 消息里写明的重置时刻，中英文两种写法都能识别，没写时区时按 UTC+8；消息里没有时间时读 HTTP `Retry-After` 头，两者都没有才按 60 秒冷却；返回给客户端的 `retry_after` 与 `Retry-After` 同样是这个时间。冷却状态只在当前服务进程中保留，重启后清空。

### 密钥

在看板「密钥」页可以管理多个 API Key，并为每个 Key 指定独立出口。不同客户端使用各自的 Key，国内与国外流量互不影响，不需要频繁切换全局出口：

- **添加与生成**：点「新建密钥」，填写名称后点「随机生成」或填写自定义密钥；创建成功的弹窗里给出完整密钥、Base URL 与 curl、OpenAI SDK、Claude Code 的接入片段，列表里也可以随时复制；
- **出口绑定**：可固定走国际版（`www.workbuddy.ai`）或国内版（`copilot.tencent.com`）；不绑定则跟随网关默认出口，默认出口在「仪表盘 → 网关出口」切换；
- **模型限制**：可为每个 Key 添加允许调用的模型（如 `deepseek*`、`gpt-6-astra`，支持 `*` 通配）；不添加表示不限制。不在列表内的模型请求由网关直接返回 400 并说明原因，不会发送到上游，也不消耗额度，用于拦截客户端背景请求偷偷调用的付费模型；
- **有效期、配额与 IP 白名单**：每个 Key 可以设置有效期（永不过期或 N 天后过期）、Token 配额、积分配额（按上游返回的真实扣费累计）与 IP 白名单（每行一个 IP 或 CIDR）。过期返回 401，来源 IP 不在白名单返回 403，任一配额用满返回 429，Messages 接口按 Anthropic 错误格式返回；面板会话发起的请求不受这些限制；
- **用量与重置**：列表显示每个 Key 的请求次数、Token、积分与最近使用时间，统计从上次「重置用量」起累计，重置后配额重新计算；
- **启停与删除**：可单独启用或停用，删除后立即失效；所有 Key 保存在 `accounts/settings.json`，重启后保留；
- **防冲突**：在面板保存过 Key 后，启动命令或脚本里的旧参数（如 `--api-key`）自动失效；
- **区域自检**：Key 绑定的出口与请求的模型不匹配时（如用国际版 Key 调用国内独占的 `deepseek-v4-pro`），直接返回 400 校验错误并说明原因，不会等到上游 WAF 拒绝时返回难以理解的错误；
- **完整密钥只在面板会话里读取**：列表平时只画掩码，复制完整密钥走一次显式请求（`GET /settings/reveal`），只认面板会话，API Key 不能代替。

### 模型

「模型」页把当前目录列成表格，每个模型一行，带能力规格（上下文窗口、单次最大输出、视觉支持、工具调用与推理档位）；右上角显示目录的来源与获取时间。每个模型有一个「处理」勾选框：取消勾选后该模型的请求被直接拒绝（错误里会说明是看板关掉的），同时从 `/v1/models` 里消失，客户端不会发现一个必定被拒绝的名字。上游列出但被筛选规则排除的模型默认不勾选、默认隐藏，点「显示未处理的模型」展开后勾选即可启用，重新勾选不会被下一次加载再次关闭；「强制重新获取」按钮从服务器重新拉取目录，失败时提示并继续显示上次获取的结果。

### 用量与日志

每条请求记录除模型、Token、耗时与积分外，还记下使用的 Key（`key_id`，面板测试台为 `panel`）、来源 IP（`ip`）、返回给客户端的状态码（`status`）与接口类型（`api`）。「请求日志」页点击一行可以查看全部字段，「运行日志」页显示网关控制台输出，可以复制、导出为文件或清空。

### 任务与调度

「任务」页集中四块内容：调度器的运行状态与排程日志，手工触发巡检与停止、启动调度器都在这里；国内版的签到与成长任务，可以按账号或全部账号执行；国际版的每日活跃打卡，桌面端对话与网页通道两种触发方式；以及积分变动记录。任何手动按钮都不会改动右上角正在查看的版本。

---

## 三、核心机制

### 出站身份与配额通道

官方每个账号有两套以上的身份、对应不同的出站端点与配额线：

- **WB**：WorkBuddy 独立桌面客户端；
- **VSC**：官方 VSCode 插件，国内版走 `www.workbuddy.cn`；
- **CLI**：官方 CodeBuddy CLI。

某个身份在某条配额线上被打满时，可以换到另一条继续使用。「账号」页每一行的身份下拉可以随时切换，切换立即写入凭证文件并保存；「设置 → 网关行为 → 429 自动切换出站身份」打开后，账号在某模型上收到上游 429 时会自动在三种身份之间轮换重试，同一账号与模型 60 秒内最多切换 4 次，切换后的身份会保存、重启后继续沿用。这个开关默认关闭：自动切换会吃掉重试预算，也会把账号留在操作者没有主动选过的身份上。

### 账号调度

每次请求交给哪个账号，由以下规则共同决定：

- **可用性**：被停用、冷却中、剩余积分触到保留积分、当日 Token 超过限额的账号一律跳过；
- **会话粘性**：一条对话绑定到某个账号后（用于复用上游的 prompt cache），只要该账号还能接单就继续用它；绑定按会话前缀识别，可以用 `WB_AFFINITY_BY_PREFIX=0` 关闭；
- **优先级**：数字小的账号先被选中，默认 100；数字相同的账号之间保持原来的轮询顺序，因此没有调整过的账号池行为与以前一致。会话粘性优先于优先级，绑定失效后才按优先级重新选择；
- **保留积分**：账号余额低于或等于设定值时暂停接单，设置项在「设置 → 网关行为 → 保留积分」，填写 0 表示关闭，避免余额被用到 0 之后收到上游提醒短信。判定依据是最近一次查询到的余额，账号仍留在账号池中并继续执行定时任务，充值后自动恢复接单；
- **每日 Token 限额**：账号当日消耗的 token 达到设定值后暂停接单，请求自动切到其他账号，本地时间 0 点后恢复，设置项在「设置 → 网关行为 → 每日 Token 限额」，填写 0 表示不限额。它与保留积分各自独立，任一命中都会暂停接单；
- **代理槽**：绑定了代理槽的账号经对应出口出网，槽位地址变化时绑定账号自动跟随。

### 设备指纹隔离

国际版与国内版使用同一套算法：以账号 UID 结合固定业务盐值做单向哈希，派生机器码与会话标识。同一账号每次出站都来自同一台虚拟设备，不会随机变化；不同账号之间互相独立，避免被关联风控。

### 模型目录与过滤

对官方本地配置清单做过清理：去掉行内代码补全专用模型（如 `codewise-*`、`completion-gf`、`hunyuan-3b/7b`）与底层多云专线变体（如 `*-volc`、`*-lkeap`），模型与官方桌面端保持一致，并按桌面端显示上下文窗口、单次最大输出、视觉支持、工具调用与推理档位。过滤规则还会去掉 5 个档位别名与 `auto`、去掉 `-sg` 与 `-x` 变体、同名时只保留 0.00 倍率那一档。

清单与上游 `GET /v3/config` 的 `agents[cli].models` 保持同步，每次成功获取都保存在 `accounts/cache/model_catalog.json`，24 小时内直接使用这份副本，重启或批量请求都不会反复访问上游；副本过期时重新获取，获取失败时过期副本仍然可用，再往后依次回退到桌面端本地文件与内置快照。接口返回值带 `source`（`server` / `cache` / `local` / `bundled`）与 `fetched_at`，看板按这两个字段显示目录来源与获取时间。上游新增的模型无需发版即可出现在 `/v1/models`。

> **同模型跨区域混合轮询**：目前同名模型（如 `deepseek-v4.1-flash`）虽然同时存在于国内版与国际版，但**暂未实现跨区域账号的自动混合轮询**，两个区域分别配置与调度，请求只走当前所选出口。原因是各区域网络环境隔离、出站指纹保持独立与账号防风控的要求；待实测确认长期稳定、没有封号风险后再补上。

### 国内版每日自动化

- **每日签到**：自动完成国内版打卡并领取积分；
- **成长任务与积分任务**：自动接取未接任务，上报规范行为事件点亮任务（画布创建、灵感案例、模板使用、模型体验、多轮对话等），然后自动领取奖励；
- **猫猫日常**：自动检查旅行状态，在家时自动派出，回来后自动领奖。按官方规则奖励必须在下次出发之前领取，网关的领奖先于派出执行，自动派出不会让待领奖励作废；旅行时长与单次奖励由上游随机决定（1～4 小时、5～10 积分），网关只读取并显示；
- **夜猫子任务**：每天 01:00 执行一次专属任务并上报夜猫事件。

### 国际版每日活跃打卡

官方规则要求从客户端发起一次有效对话才能领取每日活跃奖励。调度器在 09:00 与 21:00 巡检时为当天还没有活跃的国际版账号发一条轻量对话，使用官方 `WB` 出站标头与低消耗模型。看板切换到国际版时，顶部工具栏提供「每日活跃打卡」，账号行提供「网页通道打卡」按钮；后者会把这一轮会话按网页端的顺序执行到结束：创建会话 → 读取沙箱 `link` 与 `token` → ACP（JSON-RPC over HTTP，服务端事件走 SSE）`initialize` → `session/load` → `session/prompt` → 轮询到 `completed`，领取每日 30 积分（Pro 为 50 积分）。一轮最多等待 120 秒，`WB_WEB_TURN_TIMEOUT` 可调；网页通道会真实起一次任务并消耗该账号少量积分，所以按钮先弹确认。

- 每个账号每天只触发一次，记录在账号文件的 `lastDailyChat` 里，不浪费额度；
- 「设置 → 网关行为 → 国际版每日活跃打卡网页通道」可以关掉网页通道，只发桌面端对话。

### 后台定时调度器

常驻后台，按 UTC+8 每天在固定整点执行自动化任务（时区不跟随运行机器的设置）：

- **每天 09:00 与 21:00**：国内版账号自动签到与猫猫旅行；国际版账号执行每日活跃打卡对话；
- **每天 22:00**：扫描全部账号，Token 剩余有效期不足 2 小时时自动调用 Refresh Token 保活；
- **每天 01:00**：执行夜猫子任务；
- **手动触发**：「账号」页与「任务」页提供「每日活跃打卡」「网页通道打卡」「每日签到」，调度器本身也可以在「任务」页手工触发一次巡检或停止、启动。

### 代理槽

代理槽给账号一个固定的出网出口，适合本机跑着 mihomo 之类代理的场景：

- **绑定语义**：每个账号固定绑定一个槽位；槽位地址变化时绑定账号自动跟随；槽位停用后绑定账号回退直连，重新启用即恢复；槽位删除会解除绑定并回退直连。绑定在「账号」页的出口下拉里调整；
- **自动发现**：点「自动发现 mihomo 端口」扫描同一 Docker 网络里的 mihomo 出口，默认主机名 `cli-proxy-mihomo`、端口范围 `17901-17910`，可用 `WB_PROXY_DISCOVER_HOST` 与 `WB_PROXY_DISCOVER_PORTS` 改；扫描结果里会标出每个出口的公网 IP 与延迟，不可达的地址不可勾选；
- **自动分配**：把启用中的账号按顺序分配给各槽位，省去逐个手填；
- **测试**：槽位的一次测试会真实经过该代理访问一次外网。

### 本地网络工具

有的客户端（如 Codex App）会在 Responses 请求里声明 `web_search` 与 `web_fetch` 这类服务端工具，而上游没有对应的执行器：声明发送到上游后，模型看得到工具却无法执行，客户端只会收到 unsupported call。

打开看板「设置 → 网关行为 → 本地网络工具」后，网关把这份声明换成自己的同名 function，拦截模型发起的调用并在本地执行（搜索使用 DuckDuckGo HTML 版，抓取模型给出的 URL），再把结果返回给模型；最多连跑 3 轮（`WB_MAX_WEB_ROUNDS` 可调，上限 8）。搜索过程会以 `web_search_call` 卡片事件与 `url_citation` 引用返回客户端。

- **默认关闭**：工具声明原样转发，客户端自己声明的搜索工具不受影响，升级不受影响；
- **打开后网关会主动访问模型给出的 URL**：主机名先解析成 IP，任一地址属于本机、私网、链路本地或保留网段就拒绝，实际连接只连校验过的 IP，每次重定向都重新校验（最多 5 次）；单个响应最多读 4 MiB，一次抓取总时限 40 秒。每轮都会多调用一次上游、多消耗该账号的额度；国内网络下 DuckDuckGo 可能无法连接，此时模型收到的是错误文本；
- **只影响声明了这两个工具的客户端**，普通 `/v1/chat/completions` 客户端不经过这条路径。

### Messages 接口

网关同时提供 `POST /v1/messages`，按 Anthropic Messages 协议收发，便于 Claude Code 这类只认 Messages 协议的客户端直接接入：请求体、SSE 事件（`message_start` / `content_block_start` / `content_block_delta` / `message_delta` / `message_stop`）与错误信封都按该协议给出，鉴权与 `/v1/chat/completions` 相同（`x-api-key` 或 `Authorization: Bearer`）。

请求与回复都要在两种协议之间翻译，翻译走哪条路径由看板「设置 → 网关行为 → Messages 接口翻译」决定：

- **旧版 openai-completions**（默认）：Anthropic 请求直接翻译成对话补全请求，回复按同一条路径翻回 Anthropic 消息；
- **新版 openai-responses**：Anthropic 请求先翻译成 Responses 请求，再交给 Responses 那条转换链继续处理，回复同样按这条路径翻回。客户端带 Responses 风格自定义工具（Codex 这类）时用这一条。

两者的差别在于工具调用、自定义工具与思考内容的处理方式；已支持的内容包括 `system`、多轮对话、图片输入、`tools` / `tool_use` / `tool_result`、`tool_choice`、思考内容（作为 `thinking` 块）与流式输出。`max_tokens` 与 `messages` 是必填项，缺失时返回 400 的 Anthropic 错误信封。上游的 429、403、不可达等失败按同协议的错误类型给出（限流带 `Retry-After`）。

---

## 四、客户端接入

- **API 接口地址（Base URL）**：`http://127.0.0.1:8788/v1`，局域网为 `http://<局域网IP>:8788/v1`；
- **API Key**：
  - 本机单机模式：未配置 Key 且未开启 LAN 时可留空或填任意字符；
  - 已在看板配置 Key 或开启 LAN 模式：在「密钥」页添加或复制已绑定出口的 API Key（如固定走国际版的 Key 或国内版的 Key）；
- **模型名称**：填写 `/v1/models` 中列出的任意模型 ID（如 `deepseek-v4.1-flash`、`glm-5.3`、`kimi-k2.8-preview` 等），实际可用的清单以看板「模型」页为准。

### Codex CLI / Claude Code (Responses API)
网关内置 Responses 协议双向转换与 WAF 指纹脱敏：
```bash
export OPENAI_BASE_URL="http://127.0.0.1:8788/v1"
export OPENAI_API_KEY="你在看板「密钥」页添加的 API Key"
```

### Claude Code (Messages API)
只认 Messages 协议的客户端指向同一个地址即可，鉴权用同一个 Key：
```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8788"
export ANTHROPIC_AUTH_TOKEN="你在看板「密钥」页添加的 API Key"
export ANTHROPIC_MODEL="deepseek-v4.1-flash"
```

### curl

```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H "Authorization: Bearer 你的Key" \
  -H "Content-Type: application/json" \
  -d '{"model": "deepseek-v4.1-flash", "messages": [{"role": "user", "content": "你好"}]}'
```

`/v1/chat/completions`、`/v1/completions`、`/v1/responses`、`/v1/messages` 与 `/v1/models` 都接受去掉 `/v1` 前缀的别名（`/chat/completions`、`/completions`、`/responses`、`/messages`、`/models`），方便把 Base URL 填到根路径的客户端直接使用。

---

## 五、接口一览

**对外接口**（鉴权用 `Authorization: Bearer` 或 `x-api-key`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /v1/chat/completions | Chat Completions 协议，支持流式 |
| POST | /v1/completions | 旧版补全协议 |
| POST | /v1/responses | Responses 协议，支持流式与自定义工具 |
| POST | /v1/messages | Anthropic Messages 协议，翻译路径在设置页选择 |
| GET | /v1/models | 模型列表，含能力、规格、目录来源（`source`）与获取时间（`fetched_at`） |

**看板接口**（需要面板会话，浏览器登录后由页面携带）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | / | Web 看板，静态资源经 `/web/<路径>` 提供 |
| GET | /health | 存活检查 |
| GET / POST | /realm | 读取或切换网关默认出口 |
| GET | /panel/status | 运行信息：版本、账号统计、调度器状态 |
| GET | /panel/models | 模型目录的管理视图，含被关掉的模型 |
| POST | /panel/login | 面板登录，返回会话令牌 |
| POST | /panel/logout | 退出登录 |
| POST | /panel/password | 修改面板密码 |
| GET | /accounts | 账号列表与筛选所需字段 |
| GET | /accounts/export | 导出账号文档 |
| POST | /accounts/import | 导入账号文档，支持 `dryRun` 与 `overwrite` |
| POST | /accounts/import/desktop | 导入桌面端凭据（当前不可用，见「账号」一节） |
| POST | /accounts/refresh | 刷新一个或全部账号的令牌 |
| POST | /accounts/test | 测试账号出话，可临时指定模型 |
| POST | /accounts/set | 单字段更新：`enabled`、`priority`、`note`、`proxy`、`proxySlot` |
| POST | /accounts/set-all | 批量启用或停用全部账号 |
| POST | /accounts/delete | 删除账号 |
| POST | /accounts/product | 切换账号的出站身份（WB / VSC / CLI） |
| POST | /accounts/checkin | 国内版签到 |
| POST | /accounts/daily-chat | 国际版每日活跃：桌面端对话 |
| POST | /accounts/daily-chat-web | 国际版每日活跃：网页通道 |
| POST | /accounts/login/start | 发起 OAuth 登录，返回授权链接与二维码 |
| GET | /accounts/login/poll | 轮询登录状态 |
| POST | /accounts/login/cancel | 取消这次登录 |
| GET / POST | /accounts/credits | 刷新积分构成：GET 刷新全部账号，POST 可带 `uid` 或 `realm` 只刷新指定账号 |
| GET | /accounts/credit-events | 积分变动记录 |
| GET | /usage | 用量汇总，可选 `realm` 与时间范围 |
| GET | /usage/recent | 请求记录，可按 `realm`、`key`、`status`、`model`、`account`、`ip`、`range` / `since` / `until` 筛选并分页 |
| GET | /usage/trend | 调用趋势：`days=N` 按天，`range=today` 按小时 |
| GET | /usage/analytics | 按账号、模型与密钥的统计 |
| GET | /usage/by-account | 按账号的用量明细 |
| GET | /usage/perf | 性能采样：首字延迟与生成速度 |
| GET | /tasks | 国内版成长任务、连续打卡与猫猫日常状态 |
| POST | /tasks/run | 领取国内版成长任务奖励 |
| POST | /tasks/travel | 触发猫猫旅行（派出 / 领奖） |
| GET | /scheduler | 调度器状态与排程日志 |
| POST | /scheduler/trigger | 立即执行一次后台巡检 |
| POST | /scheduler/toggle | 停止或启动调度器 |
| GET | /settings | 读取设置与每把 Key 的用量、状态 |
| POST | /settings/save | 保存设置；`reset_key_usage` 重置某把 Key 的用量 |
| GET | /settings/reveal | 面板会话专用，读取一把 Key 的完整值 |
| GET / POST | /proxy/slots | 读取代理槽列表 |
| POST | /proxy/slots/save | 整体保存代理槽列表 |
| POST | /proxy/slots/test | 测试一个代理槽，真实经过该代理由外网返回 |
| POST | /proxy/discover | 扫描 mihomo 出口候选，返回公网 IP 与延迟 |
| GET | /logs | 运行日志 |
| GET | /logs/export | 下载日志文件 |
| POST | /logs/clear | 清空运行日志 |

---

## 六、命令行参数与环境变量

### 命令行参数

| 参数 | 说明 |
|---|---|
| `--host` | 监听地址，默认 `127.0.0.1`，也可以用环境变量 `HOST` 指定 |
| `--port` | 监听端口，默认 `8788`，也可以用环境变量 `PORT` 指定 |
| `--lan` | 监听全部网卡，供局域网设备访问；隐含 `--host 0.0.0.0`，并强制要求 API Key |
| `--api-key` | 要求 `/v1/*` 携带这个 Bearer Token；面板保存过 Key 后本参数自动失效，也可以用环境变量 `API_KEY` 或 `WB_PROXY_KEY` 指定 |
| `--panel-password` | 启动时设置面板密码，默认 `admin` |
| `--accounts-dir` | 账号凭证目录，默认 `./accounts`，也可以用环境变量 `ACCOUNTS_DIR` 指定 |
| `--usage-dir` | 用量流水目录，默认 `./usage`，也可以用环境变量 `WB_PROXY_USAGE_DIR` 指定 |
| `--import-desktop` | 导入桌面端凭据后退出 |
| `--system-prompt` | 请求没有 system 消息时注入的内容（上游必填） |
| `--user-agent` | 覆盖出站 User-Agent，默认模仿官方 WorkBuddy AI 客户端 |
| `--info` | 指定 WorkBuddy `*.info` 凭据文件路径 |

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8788` | 监听地址与端口 |
| `API_KEY` / `WB_PROXY_KEY` | 空 | 启动时固定 API Key |
| `ACCOUNTS_DIR` | `./accounts` | 账号凭证与设置目录 |
| `WB_PROXY_USAGE_DIR` | `./usage` | 用量流水目录 |
| `WB_PROXY_DEFAULT_REALM` | `intl` | 启动时的默认出口 |
| `WB_CLIENT_TIMEOUT` | `120` | 客户端连接读写超时，单位秒 |
| `WB_MAX_PAYLOAD_BYTES` | `52428800` | 请求体上限，默认 50 MiB，超出返回 413 |
| `WB_MAX_CONCURRENT_CHAT` | `32` | 同时处理的对话请求数 |
| `WB_CHAT_SLOT_WAIT` | `30` | 并发满员时排队等待的秒数 |
| `WB_MAX_WEB_ROUNDS` | `3` | 本地网络工具最多连跑的轮数，上限 8 |
| `WB_WEB_TURN_TIMEOUT` | `120` | 国际版网页通道一轮对话的等待上限，单位秒 |
| `WB_AFFINITY_BY_PREFIX` | `1` | 会话粘性开关，设为 `0` 关闭 |
| `WB_PROMPT_CACHE_KEY` | `0` | 实验开关：向上游附带 `prompt_cache_key` |
| `WB_PROXY_DISCOVER_HOST` | `cli-proxy-mihomo` | 代理槽自动发现的主机名 |
| `WB_PROXY_DISCOVER_PORTS` | `17901-17910` | 代理槽自动发现的端口范围 |
| `TZ` | `Asia/Shanghai` | 容器时区，定时调度一律按 UTC+8 计算 |

---


## 七、致谢与引用声明

### 上游项目与界面参照

- **[ardeyouxipianyi/workbuddy2api-hub](https://github.com/ardeyouxipianyi/workbuddy2api-hub)**：本仓库的上游项目。协议转换、账号调度、定时任务与请求侧的风控处理都建立在这个项目之上，本仓库在它的基础上继续开发；
- **[ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager)**：看板界面的参照。把单文件看板重写成 `app/web/` 多文件前端时，页面划分、卡片与表格的布局、底部浮动菜单栏、命令面板与删除确认弹窗都参考了这个项目；
- **[linux-do/cdk](https://github.com/linux-do/cdk)**：LINUX DO 社区开源的 CDK 发放平台。社区用它公开、可追溯地分发兑换码，本项目使用原生 H5 与 JavaScript 重新实现了其部分视觉风格，感谢社区与这个项目。

### 其它参考项目

协议兼容、风控规避与任务流程设计过程中，还参考了以下开源项目的经验与研究成果：

- **[Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)**：成长任务完整流程分析、设备指纹稳定派生（`derive_id`）、整点排程（`Scheduler`）、指纹脱敏与 `reasoning_content` 回填。

---

## 八、免责声明

1. 本项目为非官方自托管网关，仅供技术研究、协议学习与个人合法授权账号在私有环境测试使用。
2. 本项目不提供任何账号与额度。请遵守官方服务条款，禁止用于商业转售、恶意并发或违规滥用。
