<!-- 说明 JMeter 巡检套件的安装、配置、运行方式、输出结构和安全边界。 -->

# JMeter 巡检套件运行器

这是一个面向 Windows 的 JMeter 串行巡检工具。它读取 TOML 配置，按 JMX 文件名顺序以非 GUI 模式运行多个脚本，保存 XML JTL 和 JMeter 日志，并生成不依赖网络资源的中文离线 HTML 报告。

当前部署基线：

- Windows
- Python 3.14
- JMeter 不限定具体版本，启动时通过 `jmeter -v` 检查可用性并识别实际版本
- 与所选 JMeter 版本兼容的 Java；当前开发环境使用 Java 11
- APScheduler、Jinja2、tzdata

项目不调用 JMeter Dashboard，报告不加载 CDN 或其他外部资源。常驻调度服务可通过
钉钉 Stream 长连接接收群命令；定时巡检、群内巡检和 `run_once.py` 一次性巡检完成后
均可推送结论和报告 ZIP。`generate_report.py` 手动重建历史报告时不会发送钉钉消息。

> [!WARNING]
> **敏感信息与磁盘增长风险：原始 JTL 会保存 JMeter 实际记录的请求头、请求参数/请求体、响应头、响应正文和断言；HTML 报告只展示完整请求参数、响应参数和断言，不展示请求头或响应头。内容不脱敏、不截断，也不会自动清理。所有运行目录永久保留，可能泄露口令、令牌、个人信息或业务数据，并会持续占用磁盘。请只在访问受控、容量受监控的目录中运行，并由运维人员制定备份、访问控制和人工清理策略。**

## 安装

先安装任意可正常启动的 Apache JMeter，以及与该版本兼容的 Java，并确认 `jmeter.bat` 可执行。推荐在 PowerShell 中创建独立虚拟环境：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .\config\app.example.toml .\config\app.toml
```

`-e ".[dev]"` 会以 editable 模式安装项目及 pytest 开发依赖。运行时依赖包括：

- APScheduler：五段 Cron 调度；
- Jinja2：生成离线 HTML；
- tzdata：提供时区数据；
- dingtalk-stream：以 Stream 模式接收群机器人消息；
- requests：调用钉钉令牌、群消息和媒体上传接口。

## 配置

项目的配置与脚本目录如下。`config/app.example.toml` 是可复制的模板，
`config/app.toml` 是已加入 `.gitignore` 的本机配置；相对路径均以配置文件
所在的 `config/` 目录为基准解析。

```text
Inspection Items/
├─ run_once.py                  # 校验后立即执行全部 JMX
├─ scheduler_service.py         # 前台等待 Cron 触发
├─ check_dingtalk.py            # 向已绑定群测试消息与文件发送
├─ 启动定时巡检.bat             # 双击启动前台调度服务
├─ generate_report.py           # 使用已有 JTL 手动重建 HTML 报告
├─ config/
│  ├─ app.example.toml          # 可移植模板
│  └─ app.toml                  # 本机配置，不提交
├─ jmx/
│  └─ Inspection_Test.jmx       # 当前业务巡检脚本
├─ runs/                        # 运行后生成，不提交且不会自动清理
└─ manual_reports/              # 手动重建的 HTML 报告，不提交
```

编辑 `config/app.toml`：

```toml
[jmeter]
executable = "D:/tools/apache-jmeter/bin/jmeter.bat"
properties_file = "D:/tools/apache-jmeter/bin/jmeter.properties"

[scripts]
directory = "../jmx"
timeout_seconds = 3600

[schedule]
timezone = "Asia/Shanghai"
cron = "0 2 * * *"

[dingtalk]
enabled = false
# client_id = "dingxxxxxxxx"
# client_secret = "xxxxxxxx"

[output]
directory = "../runs"

[report]
excluded_url_keywords = [
    "/login/login/singlelogin",
    "/health",
]
additional_date_parameter_names = ["account_period"]
```

主要规则：

- `jmeter.executable` 必须显式指向本机 JMeter `bin` 目录中的
  `jmeter.bat`。`jmeter.properties_file` 必须显式指向同一本机
  JMeter `bin` 目录下已存在的 properties 配置文件；文件不存在、
  不是普通文件或不可读时，启动校验会失败。程序只通过
  `-q <properties_file>` 读取它，不复制、不生成、也不修改该文件。
  两个路径不支持 `*` 等通配符，但可以指向任意 JMeter 版本；程序不会
  设置最低版本、最高版本或版本白名单。
- `scripts.directory` 的值精确界定本次发现范围：程序只读取该目录本层，
  不递归扫描，也不会自动扩大到相邻目录。该目录本身不得是符号链接，
  必须已存在且至少包含一个要执行的普通 `.jmx` 文件；不得放入其他文件、
  子目录或符号链接。任一边界不满足都会使启动校验失败。
- 脚本按文件名排序后串行执行（先按忽略大小写的名称，再按原始名称稳定
  排序）。需要固定顺序时使用 `01-`、`02-` 等文件名前缀。
- `scripts.timeout_seconds` 是目录内全部脚本的统一超时，必须是正整数。
- 运行命令不会追加任何 `-Jkey=value` 参数；JMX 必须包含运行所需的目标
  与参数。
- `schedule.cron` 必须是标准五段 Cron：`分 时 日 月 周`；示例 `0 2 * * *` 表示每天 02:00。
- `schedule.timezone` 决定 Cron 和运行目录时间前缀；示例使用 `Asia/Shanghai`。
- `[dingtalk]` 是可选配置块，省略或 `enabled = false` 时完全保持原有行为。
  启用时 `client_id` 和 `client_secret` 必须是非空字符串。它们只用于 Stream
  连接和钉钉开放接口，不会写入调度日志、报告或 manifest。真实凭据只填写到已被
  Git 忽略的 `config/app.toml`，不要填写到示例配置或提交到版本库。
- `output.directory` 可以尚未创建，但其最近的现有父目录必须可写。
- `[report]` 是可选配置块；`report.excluded_url_keywords` 必须是字符串数组，
  默认空数组表示不过滤。生成报告时，程序先移除接口 URL 的 Query 和 fragment，
  再按关键字做不区分大小写的字面包含匹配。命中的接口会从失败概览、请求明细、
  请求/失败/断言统计和 manifest 计数中完全排除，也不影响脚本或套件结论；原始
  JTL 和 JMeter 日志保持不变。如果某份 JTL 中的全部 HTTP 接口均被过滤，该脚本
  标记为 `error`，错误信息为“URL 关键字过滤后没有可报告的 HTTP 接口”。
- “查询数据范围”仍根据实际请求参数识别。程序保留内置日期字段规则；
  `report.additional_date_parameter_names` 可额外配置未被内置规则覆盖的参数名，
  必须是非空字符串数组，按参数名精确匹配且不区分大小写。例如配置
  `account_period` 后，实际参数 `account_period=2026-08` 会显示为 `2026-08`。
- 所有会进入 Windows CMD/batch 边界的路径都拒绝 `& % ^ ! | < > ( )`
  以及 CR/LF 换行字符。路径中的空格和中文受支持。

`jmeter.properties_file` 的保存字段必须满足巡检报告需要；若改成不含请求、
响应或断言明细的格式，离线报告也无法恢复这些数据。修改 JMeter 配置后，
应先使用受控测试目标检查运行产物。

## 启动方式

项目 v1 从源码目录启动下面四个 Python 文件，并额外提供一个双击启动调度器的
BAT。四个 Python 脚本和 BAT 都不接受任何启动参数；它们不会根据当前工作目录
寻找配置或运行产物。
`run_once.py`、`scheduler_service.py` 和 `check_dingtalk.py` 固定读取脚本所在项目
目录下的 `config/app.toml`；`generate_report.py` 只读取其中的 `output.directory` 和可选
`report.excluded_url_keywords` 和 `report.additional_date_parameter_names`，不会读取
或校验 JMeter、Java、JMX 配置。

推荐在 IDE 中选择项目 `.venv` 的解释器，然后直接运行目标文件。在源码
目录中也可使用 PowerShell：

```powershell
.\.venv\Scripts\python.exe .\run_once.py
.\.venv\Scripts\python.exe .\scheduler_service.py
.\.venv\Scripts\python.exe .\check_dingtalk.py
.\.venv\Scripts\python.exe .\generate_report.py
```

需要常驻调度时，也可以直接双击项目根目录的 `启动定时巡检.bat`。BAT 会切换到
自身所在目录，检查 `.venv\Scripts\python.exe` 和 `scheduler_service.py`，然后
以前台方式启动调度器。调度器退出后窗口会显示退出码并暂停，避免错误信息一闪而过。

只有当 `.py` 文件已明确关联到本项目 `.venv\Scripts\python.exe` 时才可双击启动；
不得使用 `pythonw.exe`，因为必须保留可见的控制台、退出状态和人工中断能力。
安装 wheel 后从任意目录启动不在 v1 支持范围内。

### `run_once.py`

启动后会先完整校验 TOML、必需文件与目录、五段 Cron、时区、文件名顺序、
统一超时，以及 JMeter 能否正常启动并识别版本。校验通过后，它会立即按文件名顺序串行执行
`scripts.directory` 中的全部 JMX，向 JMX 中配置的目标发出真实请求，并生成
JTL、manifest 和离线 HTML 报告。它不是无副作用的“仅校验”工具。

断言失败与 JMeter 进程失败是两个层次：JMeter 进程退出 0 且生成非空 JTL 时，
原始执行可正常完成；报告仍会根据 JTL 中的失败样本或失败断言把脚本/套件
标记为 `failed`，脚本返回 1。

启用 `[dingtalk]` 且已经通过群命令绑定巡检群时，`run_once.py` 生成报告后会向该群
发送相同的巡检结论和 ZIP 附件，但不会启动 Stream 监听。尚未绑定群或发送失败时会
在控制台显示原因；通知结果不会改变原巡检的退出码。

### `scheduler_service.py`

启动后完成与 `run_once.py` 相同的完整配置和 JMeter 可用性预检，然后作为
前台 Python 进程占用当前控制台窗口等待 Cron。它启动时不会立即执行 JMX；
任务只由配置的五段 Cron 触发，允许 30 秒 misfire。每次有效触发都会向 JMX 目标
发出真实请求并生成报告。

按 `Ctrl+C` 会安全停止：设置取消事件，必要时终止当前 JMeter 进程树，并等待
调度器和钉钉 Stream 监听收尾。该脚本不会注册或后台分离为 Windows 系统服务。
配置只在应用启动时读取一次；修改 `config/app.toml` 后必须停止并重新启动该前台
进程才会生效。

启用 `[dingtalk]` 后，只有 `scheduler_service.py` 会启动 Stream 监听并接收群命令。
启动时会校验 SDK 和应用凭据：确定的凭据错误会阻止服务启动；网络、限流或钉钉
服务端临时故障会记录到 `scheduler.log`，调度器仍会启动并由 Stream 自动重连。
程序会在创建钉钉客户端前，将 `.dingtalk.com` 合并到进程的 `NO_PROXY` 和
`no_proxy` 环境变量中，使令牌、文件上传、群消息和 Stream 连接绕过本机代理；已有
`NO_PROXY` 规则会完整保留，不需要关闭 HTTPS 证书校验。
`run_once.py` 只在报告生成后进行一次主动推送，不启动 Stream；`generate_report.py`
始终不会连接或发送钉钉消息。任何钉钉推送失败都不会改变巡检的通过/失败状态。

钉钉侧需创建并发布企业内部应用机器人，启用机器人能力和 Stream 模式，并授予
“企业内机器人发送消息”相关权限，再把机器人添加到目标群。SDK 使用方式可参考
[钉钉官方 Python Stream SDK](https://github.com/open-dingtalk/dingtalk-stream-sdk-python)，
群消息与文件类型参考[机器人群消息接口](https://open.dingtalk.com/document/orgapp/the-robot-sends-a-group-message)
和[机器人消息类型](https://open.dingtalk.com/document/orgapp/types-of-messages-sent-by-robots)。
不需要公网回调地址，也不需要自定义 Webhook。

机器人进群后，所有命令都必须在群聊中明确 `@机器人`：

- `@机器人 绑定巡检群`：群内任何成员均可执行；同一时间只能绑定一个群。
- `@机器人 执行巡检`：绑定群内任意成员均可执行，不要求管理员身份。
- `@机器人 解绑巡检群`：当前绑定群内任何成员均可执行，其他群不能解绑。

绑定状态保存在 `runs/.runtime/dingtalk-binding.json`，无需修改 TOML。私聊、未 @、
重复消息和非绑定群不会启动巡检；已有定时、群内或外部 `run_once.py` 巡检占用套件
锁时，机器人立即回复“已有巡检正在执行，本次未启动”，不会排队。巡检结束后会向
当前绑定群发送运行 ID、北京时间、状态、耗时及请求/断言统计，并上传
`自动化巡检报告_<运行ID>.zip`。ZIP 只包含最终 `report.html`，不包含 JTL、JMeter
日志或 manifest；超过 20 MiB 时只发送结论、超限说明和本地报告路径。

投入定时运行前，应先将 JMX 指向受控目标，人工运行一次 `run_once.py` 并检查
真实请求、XML JTL、报告内容和磁盘权限；确认敏感数据访问控制和磁盘容量后，
再启动 `scheduler_service.py`。

### `check_dingtalk.py`

该脚本用于单独验证当前钉钉配置和已绑定群，不启动 JMeter，也不启动 Stream。
运行前必须在 `config/app.toml` 中启用 `[dingtalk]`、填写 Client ID/Client Secret，
并至少通过 `@机器人 绑定巡检群` 成功绑定过一次目标群。

运行后会在控制台依次验证：

1. Client ID/Client Secret 能否正常换取访问令牌；
2. Markdown 文本消息能否发送到已绑定群；
3. 临时 ZIP 测试文件能否上传；
4. `sampleFile` 文件消息能否发送到已绑定群。

该操作会在目标群中真实产生一条“钉钉连通性测试”消息和一个
`钉钉连通性测试.zip` 文件。ZIP 只存在于系统临时目录，测试结束后自动删除。
任一步失败都会在控制台标明失败阶段，并保留钉钉返回的 HTTP 状态、错误码或错误
说明；Client Secret 会被替换为 `***`。脚本不会修改群绑定、巡检报告或 manifest。

### `generate_report.py`

该脚本不会启动 JMeter，也不会校验 Java、JMeter 或 JMX。它读取
`config/app.toml` 的 `output.directory` 和可选 URL 过滤配置，按时间倒序列出其中已生成 manifest v1
且仍保留全部 JTL 的完成记录。输入序号选择来源，直接回车默认选择最新一次；
非法序号会继续提示，不会误选其他目录。

手动报告固定写入项目根目录下的
`manual_reports/<报告生成时间>_Inspection_Report/report.html`。报告生成时间使用
北京时间并精确到秒；同秒重名时追加 `-02`、`-03`，不会覆盖已有报告。生成过程
先在临时目录完成，成功后才发布最终文件，因此解析或渲染失败不会破坏旧报告。
每个手动报告目录只保留一个已内嵌样式和脚本的 `report.html`，不会复制原始 JTL、
JMeter 日志，也不会生成新的 manifest。
原运行即使包含失败请求，只要 HTML 重建成功，该脚本仍返回 0，并在控制台打印
原巡检状态和最终报告路径。

## 退出码

| 退出码 | 含义 |
|---:|---|
| `0` | `run_once.py` 的手动套件状态为 `PASSED`，`scheduler_service.py` 正常退出，`check_dingtalk.py` 的消息和文件均发送成功，或 `generate_report.py` 成功生成 HTML。 |
| `1` | 巡检/报告、调度运行、钉钉凭据或发送链路、手动报告重建中的运行失败。 |
| `2` | 任意启动参数，配置/JMeter预检、钉钉启用状态或群绑定校验失败，套件/调度器锁冲突，或手动报告没有可选择记录。 |
| `130` | 配置读取、验证、JMX、报告、调度等待或钉钉测试期间发生人工中断。 |

## JMeter 执行形态

每个发现的脚本都会启动一个独立 JMeter 子进程，命令形态为：

```text
<jmeter.bat> -n -t <script.jmx> -l <artifact>/result.jtl -j <artifact>/jmeter.log -q <jmeter.properties>
```

实际调用使用参数列表、`shell=False`，工作目录为 `scripts.directory`。运行器不会追加 `-J` 参数。Windows 子进程使用新的进程组。超时或取消时会通过 `taskkill /PID <pid> /T /F` 终止进程树；该命令有固定的 10 秒超时，且只有 `taskkill` 返回 0、直接 JMeter 进程也已退出时才确认进程树清理成功。`taskkill` 超时、启动失败、返回非零或直接进程仍存活都会记为清理未确认。后备 `process.kill()` 只强制终止直接进程，不能把进程树清理状态提升为已确认。

只有同时满足以下条件才记为原始执行 `completed`：

- JMeter 退出码为 0；
- `result.jtl` 存在；
- `result.jtl` 非空。

非零退出、启动失败、超时、缺失/空 JTL 或其他脚本异常都会记录在该脚本结果中。普通非零退出、JTL 错误和已确认清理成功的超时不会阻止后续脚本；人工取消会停止套件。只要超时或取消后的进程树清理未确认，错误中会明确记录清理失败，套件会立即停止，不再启动下一个 JMX，避免残留进程与后续巡检并行。

## 输出目录与单页报告

每轮会在 `output.directory` 下按北京时间创建分钟级目录，例如 `2026-08-03_10-30`。同一分钟再次运行时依次使用 `2026-08-03_10-30-02`、`2026-08-03_10-30-03`，原子创建且不会覆盖历史报告；同一分钟最多分配 100 个目录。

```text
runs/
├─ .runtime/
│  ├─ suite.lock
│  ├─ scheduler.lock
│  ├─ scheduler.log[.1 ... .5]
│  └─ dingtalk-binding.json     # 启用并绑定群后生成
└─ 2026-08-04_07-00/
   ├─ 2026-08-04_07-00-01_Inspection_Report/
   │  └─ report.html             # 单文件套件报告入口
   ├─ manifest.json              # 汇总、状态、时间和相对路径，不含正文
   ├─ artifacts/
   │  ├─ 01-脚本安全名称/
   │  │  ├─ result.jtl
   │  │  └─ jmeter.log
   │  └─ 02-脚本安全名称/
   │     ├─ result.jtl
   │     └─ jmeter.log
```

每轮只生成一个 `report.html`，放在以报告生成北京时间命名的秒级子目录中；同秒重名时追加 `-02`、`-03`。报告目录内只有这个 HTML，CSS 和 JavaScript 直接内嵌，并通过 SHA-256 CSP 哈希放行，因此整个报告目录可以直接发送和离线打开。顶部是中文结论、北京时间以及请求和断言统计，不展示脚本总数或正常脚本结果。执行概览会忽略 URL 的 Query 和 fragment，跨脚本按接口聚合失败请求，依次展示接口名称、规范化 URL、失败次数、失败数据范围和第一条失败请求的请求时间；同一范围重复失败显示“范围 ×N”，点击后可直达该范围第一条失败请求。缺少 URL 的请求不会互相合并。没有失败接口时显示通过结论。

概览和请求明细各有一个同步的“查询数据范围”下拉框，选项根据本次报告的实际识别结果自动去重生成，并按显示值精确筛选。识别为“未携带时间条件”的接口不受该筛选影响，无论选择哪个数据范围都始终显示；它仍可被请求状态筛选或名称/URL 搜索过滤。明细搜索只匹配请求名称和 URL，不匹配响应码、请求时间或查询数据范围。搜索或筛选期间自动展开有匹配请求的接口组并隐藏空组，清空全部条件后恢复筛选前的接口组状态。

下方在每个脚本内按规范化 URL 生成接口组，组头展示接口名称、URL、请求总数、通过/失败数和各数据范围状态；包含失败的组默认展开，纯通过组默认折叠。组内失败请求优先并默认展开，通过请求默认收起，相同状态保持实际执行顺序。“展开/收起全部接口组”和“展开/收起全部正文”互不改变对方状态。每条请求都展示 JMeter `ts` 对应的北京时间“请求时间”和根据实际请求参数识别的“查询数据范围”。当前本地 JMeter 配置 `sampleresult.timestamp.start=true`，因此 `ts` 表示请求发起时间。查询数据范围支持单月、跨月、全年、表单、URL Query、JSON 和 `dateRange`；没有日期字段时显示“未携带时间条件”，无法解析时显示“无法识别”，日期无效或开始晚于结束时标记“日期参数异常”。

报告只将 JTL 中的 `<httpSample>` 视为 HTTP 接口；事务控制器等逻辑控制器产生的 `<sample>` 汇总记录不会展示，也不参与通过/失败、断言和请求数量统计。展开后的请求明细展示响应码、耗时、URL、请求参数、响应参数和断言，不展示请求头、响应头或线程、层级等元数据。GET 请求在 JTL 的 `queryString` 为空时会从 URL 查询串补取参数；必要时使用 JMeter 保存的请求正文作为请求参数回退。失败请求正文直接写入 HTML；通过请求的完整请求/响应正文逐条使用确定性 gzip+Base64 保存，首次展开或复制时由新版 Edge/Chrome 离线恢复并缓存。正文不截断，压缩不代表加密或脱敏。

配置了 `report.excluded_url_keywords` 时，同一套过滤规则同时作用于即时执行、
定时执行和手动重建报告。过滤只改变报告视图、统计和结论，不会删除或修改源 JTL。
`report.additional_date_parameter_names` 同样作用于这三种报告生成路径，仅补充日期
参数名识别，不替换程序已有规则，也不会写入或改变 manifest。

`manifest.json` 只保存状态、计数、时间、错误摘要和产物相对路径，不保存请求/响应正文；其中套件和脚本的开始、结束时间均使用带 `+08:00` 时区标识的北京时间 ISO 8601 格式。敏感原文仍完整存在于 JTL 和单页 HTML 中。

## 锁、重叠运行与调度日志

- `.runtime/suite.lock` 是 `run_once.py`、Cron 任务和钉钉群内手动任务共用的非阻塞跨进程锁。已有套件占锁时，新触发不会并行运行或排队。
- APScheduler 作业同时限制 `max_instances=1`；同一调度器中的重叠触发会跳过并写日志。
- 调度器使用内存 JobStore，仅由 Cron 触发；`misfire_grace_time=30`、`coalesce=True`，同一作业积压的触发会合并处理。
- `.runtime/scheduler.lock` 防止启动第二个常驻调度器进程。
- 调度任务发现外部 `run_once.py` 正在占用套件锁时，会记录“已有巡检任务正在执行”并跳过本次触发。
- 单次调度任务的异常会记录到日志，不会终止常驻调度器；下一次 Cron 仍可继续。
- `.runtime/scheduler.log` 使用 UTF-8 滚动日志：单文件 10 MiB，保留 5 个备份。

锁文件可以继续存在于磁盘；互斥依赖操作系统文件锁是否被当前进程持有，而不是依赖删除文件。

## 保留策略

程序不删除历史运行目录，不压缩 JTL，不截断大响应，也不提供自动保留期。每次 `run_once.py` 和每次有效调度触发都会永久新增一套产物，其中 JTL 和 HTML 继续保留完整原文，不脱敏、不截断，并持续占用磁盘。请严格限制 `output.directory` 访问权限、监控容量，并通过受审计的外部运维流程人工归档或清理；清理前确认合规和问题追溯要求。

## 测试

普通单元测试不启动真实 JMeter：

```powershell
.\.venv\Scripts\python.exe -m pytest -m "not integration"
```

Pytest 的临时文件和缓存统一保存在 `.test-output/`：用例临时目录为 `.test-output/pytest/`，缓存为 `.test-output/pytest-cache/`。每次运行会复用这一固定位置，不再在项目根目录创建不同名称的 `.pytest-tmp-*` 文件夹。

真实集成测试优先从 `JMETER_HOME` 查找 JMeter，未设置时读取 `config/app.toml` 中手动指定的 `jmeter.executable`；找不到时 skip。两种方式都不限制 JMeter 版本。测试使用本机 `ThreadingHTTPServer` 和动态端口，不访问外网：

```powershell
.\.venv\Scripts\python.exe -m pytest -m integration
```

真实测试覆盖仅通过套件、断言失败套件、脚本串行、失败后继续、完整中文请求/响应进入样本 HTML，以及 manifest 不包含正文。
