# WeRSS 分组摘要与飞书推送

这是一个独立的 Python 小程序，放在 `tools/ai_digest/`，不导入 WeRSS 应用、不改现有抓取任务、后端、前端或 Docker 配置。运行位置是**云服务器宿主机**，不是本地 Mac，也不是现有 backend 容器。

Python 负责查库、正文清洗、去重、分组、定时判断、缓存、发送和重试；模型只接收短摘要指令、文章标题和正文。没有新正文就没有模型调用。每篇独立请求，不携带历史文章。

## 工作方式

| 环节 | 默认时间 | 行为 |
| --- | --- | --- |
| WeRSS 抓取科技新闻 A/B/C | 每小时 :00 | 继续使用已有消息任务 |
| WeRSS 抓取 AI 新闻 G/H | 每小时 :03 | 继续使用已有消息任务 |
| WeRSS 抓取股市新闻 D/E/F | 每小时 :06 | 继续使用已有消息任务 |
| WeRSS 补正文 | :00、:20、:40 | 由原来的 `GATHER.CONTENT_AUTO_INTERVAL=20` 控制 |
| `prepare` | 每两分钟 | 检查新正文，按需总结并保存摘要 |
| `send` | 每分钟检查 | 只在该组的推送时间生成一批消息；同时重试已排队的失败消息 |
| 科技 / AI / 股市推送 | :30 / :33 / :36 | 配置示例，可分别调整 |

消息任务 ID 是分组的稳定标识，名称和公众号列表每轮从 `message_tasks` 读取。修改 WeRSS 的分组成员后无需改这里的名单；任务停用后，该组停止准备新摘要和发送消息。空成员列表、非法列表和不存在的任务会报告错误，**不会自动选择全部公众号**。

现有正文补抓默认每轮最多 5 篇，所以 :30 不保证所有正文就绪。比如 10:40 才就绪的科技文章会先缓存摘要，在 11:30 推送。未生成摘要的文章也会保留到下一个时间点，不阻塞同组其他文章。

推送按“任务 ID + 文章 ID”去重，摘要按“文章 ID”全局复用。同一公众号属于两个分组时，同一篇文章会在两个分组各出现一次，但只调用一次模型。当前不做跨分组推送去重，也不做跨文章的内容相似度去重。

每组把多篇摘要直接拼成一份消息，不额外调用模型生成二次综合摘要。内容超过预算时按文章边界拆成多条，每条分别记录发送结果。

## 文件与运行要求

- `digest.py`：命令行、准备摘要、分组发送。
- `common.py`：配置、HTML 清洗、小时级时间表、进程锁。
- `storage.py`：只读源数据库、独立摘要缓存和持久发送队列。
- `providers.py`：Claude Code / Anthropic Messages API、飞书自定义机器人。
- `config.example.json` / `.env.example`：无凭据的配置示例。
- `systemd/`：两个服务和两个用户级定时器。
- `tests/`：只用临时数据库和模拟模型/HTTP 的测试。

需要 Linux、Python **3.9+** 和系统时区数据。没有额外 Python 包，不需要安装 WeRSS 的 `requirements.txt`。本地 Mac 可跑单元测试；定时部署说明针对 Ubuntu/systemd。

## 云端部署

以下命令都在云服务器上，以运行现有 Claude Code 的普通用户执行。示例项目路径是 `/home/haock/projects/we-mp-rss`。先把新增目录同步到服务器；如果使用 Git，需要先提交并推送本地代码，然后在服务器拉取相应分支。

### 1. 建立本地配置，确认任务 ID

```bash
cd /home/haock/projects/we-mp-rss/tools/ai_digest
cp -n config.example.json config.json
cp -n .env.example .env
chmod 600 config.json .env
python3 digest.py list-tasks
```

`list-tasks` 只输出 ID、名称、状态和公众号列表，不输出原消息任务中的 webhook、headers 或 cookies。把三个分组的 `task_id` 占位符替换为输出中的真实 ID。只配置需要摘要的任务即可，不必涵盖全部消息任务。

`source_db` 的相对路径以 **config.json 所在目录** 为基准，默认 `../../data/we_mp_rss.db`，对应宿主机的 `/home/haock/projects/we-mp-rss/data/we_mp_rss.db`。

状态目录默认是 `/home/haock/.local/state/werss-ai-digest`，与 WeRSS 的 `data/` 完全分开，不要放到源数据库目录中。不同源数据库必须使用不同状态目录。

### 2. 设置推送时间和飞书凭据

每组的 `push_minutes` 是每小时的分钟列表，例如 `[30]`、`[33]`、`[36]`；`[0, 30]` 表示每小时两次。当前版本支持小时级时间表，**不是通用 cron 表达式**。时间按 `timezone`，默认 `Asia/Shanghai`，不依赖云主机是否设置成 UTC。

在同目录 `.env` 填入机器人地址：

```dotenv
FEISHU_WEBHOOK=你的飞书机器人地址
```

也可以分别配置 `FEISHU_TECH_WEBHOOK` 等变量，然后把对应组的 `webhook_env` 改为变量名。不同分组可以使用同一个机器人或不同机器人。脚本不会复用 WeRSS 消息任务的 webhook，以免与现有原文通知混淆。

机器人启用签名校验时，填写 `.env` 的 `FEISHU_SIGN_SECRET`，并将该组 `sign_secret_env` 改成 `FEISHU_SIGN_SECRET`。使用关键词校验时，确保消息任务名称或消息正文包含你设置的关键词。

`.env` 由 Python 读取，不需要 `source`；支持 `KEY=value`、整行注释和包裹整个值的单/双引号，不执行 shell、不展开 `${变量}`、不支持值后的行内注释。已有进程环境变量优先。不要把真实地址和密钥写进示例或提交 Git；本目录已忽略 `config.json`、`.env` 和运行数据库。

### 3. 选择模型调用方式

**默认：MiniMax 国内订阅 API。** `config.example.json` 已配置：

```json
"provider": "anthropic",
"name": "MiniMax-M3.1-Flash",
"effort": "low",
"api_url": "https://api.minimax.cn/anthropic/v1/messages",
"api_key_env": "MINIMAX_API_KEY"
```

这里 `anthropic` 表示请求协议；实际请求发往 MiniMax，使用你的 MiniMax 国内订阅 Key，不使用 Anthropic 账户。把 Key 填到云端本目录的 `.env`：

```dotenv
MINIMAX_API_KEY=你的国内订阅Key
```

根据当前 [MiniMax 官方接口文档](https://platform.minimax.cn/docs/api-reference/text-anthropic-api)，国内域名为 `api.minimax.cn`；默认模型为 `MiniMax-M3.1-Flash`，通过 `output_config.effort=low` 降低思考强度，输出上限为 4096 tokens。Flash 必须开启思考，不能设置为 disabled。具体订阅是否开放该模型、剩余额度及适用调用范围，以你的订阅控制台为准；脚本不会改用普通按量 Key 或自动切换 CLI。部署时先做单篇验收，示例配置不包含真实 Key。

若已复制过旧示例，需要同步修改现有 `config.json` 的上述字段，单独更新示例不会覆盖你的配置。切换备用 CLI 时将 `provider` 改为 `claude_cli`，将 `name` 清空或改为该 CLI 可用的模型；不要直接沿用 MiniMax 模型名调用默认 Claude 服务。

MiniMax 返回的 thinking 块不会被推送，只提取 text 摘要。若改用强制思考模型，思考也可能消耗输出预算；遇到 `max_tokens` 会记录失败并暂停该篇自动重试，不把截断结果当成成功。

**备用方式：沿用 Claude Code（手动切换，不自动降级）。**

```json
"provider": "claude_cli",
"name": "",
"claude_command": "/home/haock/.local/bin/claude",
"claude_safe_mode": true
```

上面是 `model` 中的字段片段，保留其他已有字段。用 `command -v claude` 确认真实路径，尤其是通过 npm/nvm 安装时；systemd 的 PATH 通常不同于交互 SSH 会话。Node 版本由 nvm 管理时，还需在 service 的 PATH 中加入该 Node 的 bin 目录。

程序调用一次性的 `claude -p`，正文走标准输入，替换系统提示词为摘要要求，禁用工具、MCP、技能和会话持久化，不使用 `--continue`。默认开启 `--safe-mode`，保留登录能力并避免加载项目/个人定制。每次在自动清理的临时目录中运行；飞书凭据不传给子进程。仍可能存在 CLI 自身或托管策略的上下文，不能保证其 token 与直接 API 完全相同。

用同一个普通用户完成 Claude Code 登录。`check` 会检查所需 CLI 选项是否存在，但不会调用模型，也不验证登录是否有效。旧版本不支持 `--safe-mode` 时建议更新；明确接受个人全局上下文可能被加载时，可设置 `claude_safe_mode=false`，其余工具禁用仍保留。

程序不加载用户/项目 settings 文件中的自定义配置。如果原先依赖它们设置第三方 API 地址或密钥，请改用运行环境中的 `ANTHROPIC_*` 变量，或选择下方直接 API 模式。不要加 `--bare` 来替代安全模式：官方说明 bare 模式不使用订阅登录。

**可选：其他 Anthropic Messages API 服务。**

```json
"provider": "anthropic",
"name": "填写你的API账户可用的模型ID",
"api_url": "https://api.anthropic.com/v1/messages",
"api_key_env": "ANTHROPIC_API_KEY",
"api_version": "2023-06-01"
```

在 `.env` 填写 `ANTHROPIC_API_KEY`。程序用 HTTPS POST 发送 `system + 一条 user 消息`，不附带工具、历史聊天或操作说明，读取返回摘要和 token 用量。每篇单独请求；示例最大输出 4096 tokens（思考模型的思考也占用此预算）。使用其他服务时，应确认其支持 `output_config.effort`，否则从配置中移除此字段；省略时脚本不会发送这个参数。API 使用账户独立计费，不假定包含在 Claude Code 订阅里。

只实现了 **Anthropic Messages 协议**，并非任意模型 API 协议。兼容服务需要支持相同请求/响应格式和 `end_turn` 结束原因，修改 `api_url` 后应先小批量验证。地址必须是 HTTPS，不跟随重定向。

切换 provider 不会自动重新总结已有文章，已有摘要缓存继续使用。

### 4. 检查、初始化和预览

```bash
python3 digest.py check
python3 digest.py init
python3 digest.py prepare --dry-run --limit 3
python3 digest.py status
```

`check` 不调用模型或飞书；它检查源库结构、有效分组、凭据是否已填写，以及 CLI 能否提供所需选项。网络连通、实际登录、API 模型权限和机器人配置要通过下面的单篇验证确认。

`init` 默认只处理**初始化时刻之后发布**的文章。如果希望从指定时刻开始，可在第一次初始化时使用：

```bash
python3 digest.py init --since '2026-11-13T00:00:00+08:00'
```

日期需要替换为你期望的起点。这是固定的起点，之后不会随着上次推送时间前移，因此较晚补齐正文的文章不会漏掉。再次运行 init 不会覆盖已有起点或去重记录。选很早的起点会产生相应的历史总结量；先检查 dry-run。

迁移时不会自动导入旧 Claude loop 的状态文件。默认从初始化时刻开始可避免回推之前的文章；如果选择回溯当天，旧 loop 当天已经发过的文章仍可能被处理。应根据切换时刻选择起点，并在启用新发送器前停用旧 loop。已有 WeRSS 原文 webhook 如仍在使用，也可能继续发送另一份原文通知。

下面第一条**实际调用模型**，第三条**实际发送飞书**，适合云端配置完成后的单篇验收。第二条只预览：

```bash
python3 digest.py prepare --limit 1
python3 digest.py send --group '真实任务ID' --now --dry-run
python3 digest.py send --group '真实任务ID' --now
```

`--now` 只跳过分组的时间判断，不跳过文章去重；没有待发送摘要则不会发送。预览不会写状态。超过 `max_input_chars` 的文章会报错留待处理，不会悄悄截取前半篇，也不会调用模型。

### 5. 启用云端定时器

确认两份 `.service` 的项目路径和 Python/Claude 路径符合你的服务器；下面假设 `~/projects/we-mp-rss`。两项服务独立运行，AI 总结较慢不会占用发送器的进程锁。

```bash
mkdir -p ~/.config/systemd/user
cp systemd/werss-digest-* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now werss-digest-prepare.timer werss-digest-send.timer
sudo loginctl enable-linger "$USER"
systemctl --user list-timers 'werss-digest-*'
```

`enable-linger` 让该用户的服务在 SSH 退出后继续运行并可随主机启动。若报无法连接用户总线，应以 haock 的正常 SSH 登录会话执行，而不是 root 的 `sudo -u` 临时环境。

两个 timer 都是系统负责唤醒 Python，没有 AI loop。服务文件不存密钥，程序读取自己的 `.env`。定时器第一次启用的补触发可能立即开始处理，启用前应完成配置、初始化与预览。

## 日常查看、重试和停用

以下摘要命令均在云服务器执行，先进入脚本目录：

```bash
cd ~/projects/we-mp-rss/tools/ai_digest
python3 digest.py status
```

`status` 只读查看状态，不调用模型、不发消息。`baseline.since` 是文章发布时间起点；失败记录会单独列出。

每组的统计字段：

| 字段 | 含义 |
| --- | --- |
| `articles` | 当前组内、发布时间起点之后且未删除的文章总数，包含人工跳过的文章 |
| `waiting_content` | 未人工跳过、但原始正文尚未就绪的篇数 |
| `pending_summary` | **尚无成功摘要且未人工跳过**的篇数，包括从未总结、等待正文、总结失败、重试中、达到次数上限或已暂停的文章 |
| `ready_to_send` | 正文已就绪且已有摘要、但尚未排队/发送的篇数，排除人工跳过的文章 |
| `skipped` | 该组已人工确认跳过的篇数 |

`pending_summary` 不是本轮立即可调用模型的数量，它包括暂时不能处理的文章。不要与 `waiting_content` 相加，两者存在重叠。纯图片未人工确认前仍计入 pending_summary，标记跳过后才扣除。

例如一组有 4 篇：1 篇等待正文、1 篇总结失败、1 篇已有摘要待推送、1 篇人工跳过，则显示 `articles: 4, waiting_content: 1, pending_summary: 2, ready_to_send: 1, skipped: 1`。

`totals.pending_summary` 是当前所有启用分组和起点范围内按文章 ID 去重的待总结总数；同一篇属于多组时，每组分别计数，总计只算一次。`totals.summaries` 是状态库已保存摘要总数，`sent_messages` 是成功发送的消息数（不是文章数），`totals.skipped` 是状态库中当前仍有效的人工跳过记录数。

### 空正文人工确认、跳过与撤销

正文清洗后为空时，程序不会自动认定为纯图片，也不会调用模型。`summary_failed` 日志和 `status` 的 `summary_error` 会给出 `article_id`、`title`、`mp_name`（公众号）、`url`（原文链接），并提示人工确认。它可能是纯图片，也可能是正文抓取不完整。

只读查看一篇文章及其处理状态：

```bash
python3 digest.py inspect \
  --article '3541026903-3541026903_jIQ-kSGiuQV3ZLefHwuTpA'
```

上面的 ID 是本次部署已确认的纯图片文章示例，处理其他文章时请替换。输出包含标题、公众号、链接、清洗后字数、是否已有摘要、是否跳过和上次错误，不打印全文。如果原文有文字，请先补抓正文；不应将抓取失败直接当成纯图片。

打开链接，**人工确认原文是纯图片后**，显式执行：

```bash
python3 digest.py skip \
  --article '3541026903-3541026903_jIQ-kSGiuQV3ZLefHwuTpA' \
  --reason image_only
```

执行该命令即表示已经人工确认，无额外弹窗。命令要求文章存在、原始正文已就绪、清洗后无文字且没有成功摘要；不满足时拒绝跳过。只在独立状态库保存标题、公众号、链接、原因及操作时间，不修改 WeRSS 原文，不伪造摘要或成功推送记录。

跳过后，该篇不再调用模型，不再出现在 `summary_error`，也不再计入 pending_summary。跳过标记按文章 ID 全局生效，包括该篇所属的所有分组；重启、正文变化、`retry summary` 均不会自动撤销。

查看当前全部人工跳过记录：

```bash
python3 digest.py skipped
```

误判后撤销：

```bash
python3 digest.py unskip \
  --article '3541026903-3541026903_jIQ-kSGiuQV3ZLefHwuTpA'
```

`unskip` 撤销跳过并清除该篇未成功摘要的失败次数和暂停状态，保留跳过记录及撤销时间，不立即调用模型。如果正文仍为空，下次 prepare 会重新产生待人工确认的错误。重复撤销会返回 `records: 0`。`inspect` 可在源文章被删除后使用仍有效的跳过快照展示信息，`source_missing: true` 表示源记录已不存在。

`skip` / `unskip` 与 prepare 共用运行锁；若返回 `already_running`，此次操作未执行，请等待 prepare 结束，或按下文暂停 timer 后再操作，不要删除锁文件。

**旧状态库升级：**不用重新 init 或清库。只读 status/inspect/skipped 可以直接读取旧版状态；首次运行新版写入命令会自动、原子地增加跳过表，将状态版本从 1 升到 2，保留起点、摘要和推送记录。部署前先暂停两个 timer，等待正在运行的 service 结束，再更新代码并恢复 timer，避免新旧版本混用。升级后旧版脚本不支持版本 2，应保留新版代码；不要通过删除状态库来降级。

### 预览新摘要与手动推送

预览所有分组待推送的文章，不调用模型、不发送飞书、不写入状态：

```bash
python3 digest.py send --now --dry-run
```

只预览“AI更新”分组（下面 ID 是本次部署的示例；其他部署先用 `list-tasks` 查询并替换）：

```bash
python3 digest.py send \
  --group '4e98c81f-3b5c-4c65-b868-a1d9e4bc3eb2' \
  --now --dry-run
```

这是**待发送摘要预览**，不是历史文章浏览器：只显示已有摘要且未排队/未发送的文章。已发送文章不会再次显示；之前已排队的消息会以 `pending_message` 显示记录，不会重新生成预览正文。

确认后去掉 `--dry-run` 即实际发送。下面两条是不同选择，不必依次执行：

```bash
# 实际发送所有启用分组的待推送摘要
python3 digest.py send --now

# 或仅实际发送“AI更新”组
python3 digest.py send \
  --group '4e98c81f-3b5c-4c65-b868-a1d9e4bc3eb2' \
  --now
```

`send` 不调用模型；`--now` 跳过分组时间判断，但保留文章去重、每批数量上限，以及失败消息的退避和重试次数限制。已成功推送的文章不会再次发送。如果一篇文章属于多个组，各组可分别发送。

若还没有摘要，可先预览候选文章，再按需运行一次真实总结：

```bash
# 只预览最多 3 篇候选，不调用 AI
python3 digest.py prepare --dry-run --limit 3

# 实际最多调用一次 AI，会消耗模型额度，只保存摘要，不发送飞书
python3 digest.py prepare --limit 1
```

`--limit 1` 按发布时间从早到晚处理符合条件的文章，不是指定重试某一篇。没有候选时不调用模型。

### 查看 AI 总结和推送日志

最近 30 分钟的 AI 总结日志（包含所有分组）：

```bash
journalctl --user -u werss-digest-prepare.service \
  --since "30 minutes ago" -n 100 --no-pager
```

最近 30 分钟的飞书推送日志：

```bash
journalctl --user -u werss-digest-send.service \
  --since "30 minutes ago" -n 100 --no-pager
```

实时跟踪两者的日志：

```bash
journalctl --user \
  -u werss-digest-prepare.service \
  -u werss-digest-send.service \
  -n 30 -f
```

按 Ctrl+C 只退出日志查看，不停止服务。需要更早的日志时扩大 `--since` 并去掉 `-n 100`；只能读取系统尚未清理的日志。

日志为 JSON 行：`prepare_finished` 包含等待正文数、模型调用次数、缓存命中数和失败数；`summary_saved` 包含模型返回的 token 用量；`slot_created` 记录某轮推送的篇数；`message_sent` 表示飞书返回 HTTP 200 且业务 `code=0`，`send_failed` 表示待重试。CLI 的 token 统计可能包括额外上下文和缓存量，不等于 API 账单。

### 重试、运行锁和定时器状态

默认每轮最多调用模型 5 篇、每组每个推送时间最多 20 篇，超出部分留到后续轮次。正文尚未就绪时不会调用模型。消息默认失败 5 次后暂停自动尝试；退避从 120 秒开始翻倍，上限 1 小时。

摘要失败按类型处理：

| 类别 | 自动处理 |
| --- | --- |
| `output_limit` / `stop_reason=max_tokens` | 暂停该篇，调整 effort/预算后手动重试 |
| `transport`：网络异常、HTTP 408/429/5xx | 退避重试，默认最多 3 次 |
| `response_format`：响应或摘要格式不合要求 | 有限重试，默认最多 3 次 |
| `configuration` / `api_configuration`：本地配置、HTTP 401/403/400/404 等非暂时错误 | 暂停该篇并停止本轮后续模型调用 |
| `empty_content` / `input_limit` / 其他异常结束原因 | 暂停该篇，人工检查 |

停止本轮不等于关闭定时器；下一轮仍可能尝试其他尚未处理的文章。若为全局密钥/模型配置问题，修复前可先停止 prepare.timer，避免后续轮次继续失败。摘要程序不自动提高预算、不自动切换模型或 CLI。

成功和失败的模型请求日志都包含 `model`、`effort`、`max_output_tokens`、`stop_reason`、`usage`、`elapsed_ms`、`has_text`；没有收到响应时，结束原因是 null、usage 为空，不代表实际消耗一定为零。未知结束原因显示 unknown，原始错误响应、正文、思考内容和 Key 均不写入日志。`status` 的 `summary_error.diagnostics` 可查看持久化的失败诊断及 `blocked` 标记。成功摘要继续复用原缓存。

预算耗尽后的暂停记录不会因为下一轮到来或提高重试次数上限而自动恢复。同一篇正文变化后会作为新的输入尝试；配置调整后则需要显式重置：

```bash
python3 digest.py retry summary
python3 digest.py retry send
```

这两条不会立即调用外部接口，也不会删除成功摘要或成功推送记录；后续定时任务继续处理。`retry summary` 会重置所有尚无摘要且未人工跳过的失败记录，可能再次产生模型费用；它不会撤销或改写人工跳过记录。

如果输出 `already_running`，本次操作没有执行；通常是定时 prepare 正在运行并持有锁。可暂停后续触发，等待当前一轮结束再重试：

```bash
systemctl --user stop werss-digest-prepare.timer
systemctl --user status werss-digest-prepare.service --no-pager -l
```

等服务不再处于 `activating`，确认本轮已经结束，再执行：

```bash
python3 digest.py retry summary
python3 digest.py prepare --limit 1
systemctl --user start werss-digest-prepare.timer
```

停止 timer 不会中断正在执行的总结；不要删除锁文件来强行并发运行。`retry_reset` 的 `records` 表示此次重置的记录数。

查看两个定时器及摘要服务状态：

```bash
systemctl --user list-timers 'werss-digest-*' --all --no-pager
systemctl --user status werss-digest-prepare.timer werss-digest-prepare.service --no-pager -l
```

prepare 每两分钟检查一次，send 每分钟检查分组时间和失败重试；send 并不每分钟都发消息。若 prepare 的 NEXT 是 `-`，先看 service 是否仍在执行，以及日志是否持续有进展，不能仅凭这一列判断卡死。

需要彻底停用时，下面命令先禁用定时器，再终止可能仍在运行的服务：

```bash
systemctl --user disable --now werss-digest-prepare.timer werss-digest-send.timer
systemctl --user stop werss-digest-prepare.service werss-digest-send.service
```

以上停用独立摘要服务，不停止 WeRSS 四个容器。保留状态目录，下次启动可以继续去重。

## 调整时间配置

修改 `tools/ai_digest/config.json` 的分组 `push_minutes` 或模型参数后，新启动的脚本会重新读取配置，不需要重启 systemd 或 WeRSS。已经运行中的那一批仍使用启动时的配置。新推送时间如果已过去超过默认 5 分钟宽限期，会等待下一个时间点。

只有修改 `.service` / `.timer` 文件时才需要重新复制到 `~/.config/systemd/user/` 并执行 `systemctl --user daemon-reload`；改变 timer 触发频率后还需要重启对应 timer。

**WeRSS 网页补正文频率是另一项配置**，不在摘要程序的 config.json 中。例如改为每 10 分钟一次，编辑项目根目录 `.env`：

```bash
nano ~/projects/we-mp-rss/.env
```

设置：

```dotenv
GATHER.CONTENT_AUTO_CHECK=True
GATHER.CONTENT_MODE=web
GATHER.CONTENT_AUTO_INTERVAL=10
```

保存后在空闲时重建后端容器，使环境变量生效（会重启后端，可能中断当时的抓取）：

```bash
cd ~/projects/we-mp-rss
sudo docker compose -f compose/docker-compose.dev.yaml \
  up -d --no-deps --force-recreate backend

sudo docker compose -f compose/docker-compose.dev.yaml exec backend \
  printenv GATHER.CONTENT_AUTO_INTERVAL GATHER.CONTENT_MODE
```

预期输出 `10` 和 `web`。补正文会在 :00、:10、:20、:30、:40、:50 触发，启动后还会立即触发一次；每轮默认最多 5 篇。该修改不改变 AI 总结和飞书推送频率。

## 查看 WeRSS 抓取任务的 Docker 日志

这是网页“消息任务”和补正文的后端日志，与上面的 AI/systemd 日志分开。所有命令在云端项目根目录执行：

```bash
cd ~/projects/we-mp-rss

# 最近 30 分钟，最后 200 行，显示 Docker 记录的时间
sudo docker compose -f compose/docker-compose.dev.yaml logs \
  --timestamps --no-color --since 30m --tail 200 backend

# 实时跟踪
sudo docker compose -f compose/docker-compose.dev.yaml logs \
  --timestamps --no-color -f --since 5m --tail 100 backend
```

`[SQL]` 和 `[参数]` 是项目打印的数据库查询及参数，不是报错；打开文章列表、翻页和查看详情也会产生大量 SQL。抓取任务应重点看“加入队列”“采集模式”“发现新文章”“补抓文章内容”，以及完成信息、异常和失败原因。`暂无需要获取内容的文章` 表示没有符合补抓条件的候选，不等于所有历史文章都已有正文。

如果要看更多、更早的记录，去掉 `--tail` 限制并使用 `less`：

```bash
sudo docker compose -f compose/docker-compose.dev.yaml logs \
  --timestamps --no-color --since 3h backend | less
```

按具体北京时间查询（日期和时间按需要替换）：

```bash
sudo docker compose -f compose/docker-compose.dev.yaml logs \
  --timestamps --no-color \
  --since '2026-11-13T14:00:00+08:00' \
  --until '2026-11-13T16:00:00+08:00' \
  backend | less
```

`less` 中方向键/PageUp/PageDown 滚动，`g` 跳到开头，`G` 跳到末尾，输入 `/执行任务` 回车搜索，`n` 找下一处，`q` 退出。日志时间末尾的 `Z` 表示 UTC，例如 `08:09Z` 对应北京时间 `16:09`。Docker 只能显示仍保留的日志，已删除容器的日志或轮转清理的历史记录不在当前查询范围内。

## 可靠性与边界

- 源数据库始终以 `mode=ro` + `query_only` 读取，设置 10 秒 busy timeout；取完数据立即关闭连接，模型调用和 HTTP 期间不持有源库读事务。不复制正在写入的 `.db/-wal/-shm`。若打不开，检查宿主机路径与数据库/WAL/SHM/父目录权限，不使用 `immutable=1` 读取活跃数据库，也不要直接 `chmod -R 777`。
- 独立状态库使用 SQLite 事务和 WAL。缓存摘要、排队整批消息、登记去重记录均持久化；一条消息失败时只重发该条，不重新调用 AI，已发送成功的拆分消息不会整批重发。
- 同一状态目录通过 `flock` 防止两个 prepare 或两个 send 重叠；prepare 和 send 使用不同锁，SQLite 写事务很短。该设计面向同一台云服务器，不支持多台主机用网络共享目录同时运行。
- 时间点即生成消息快照。默认允许晚到 5 分钟；同一时间点已经检查过，即使当时没有摘要，后续到达的摘要也留到下一时间点。主机停机超过宽限后不会补发所有旧时段，未发送文章在下一个正常时段处理；已排队失败消息每分钟按退避规则重试。
- 任务停用会暂停该组及其待发送消息。成员或机器人地址变更后，未发消息若含已移除的文章或目标变化，会取消并在下一时间点重新组装；成功推送记录继续保留。
- 这不是严格的 exactly-once 投递：飞书已收到但响应丢失，或成功返回后进程在记录状态前被杀，仍可能重发。自定义机器人接口缺少与本地事务共同提交的机制。程序选择优先不丢消息，无法承诺这些极端情况下绝不重复。类似地，模型返回后尚未保存缓存就被终止，重试可能再次调用模型。
- 摘要生成后默认固定复用，不因更换模型、提示词或正文修订自动重算，以避免意外费用。暂不提供网页配置界面、日报综合总结、基于图表图片的阅读、任意 cron 表达式、自动修改 WeRSS 的正文补抓参数。
- `.env`、状态库和消息正文均保留在云端；模型只收到需要总结的文章数据。不要删除状态库来“重启”，否则会丢失摘要和去重记录。

## 验证

在项目根目录运行：

```bash
python3 -m unittest discover -s tools/ai_digest/tests -v
```

测试使用临时数据，不调用真实模型，不发送飞书，不连接真实 WeRSS 数据库。覆盖分组时间、晚到正文、批量文章、去重、重启、正文清洗、只读源库、摘要缓存、推送失败、分段重试和两种模型接口。

接口参考：[MiniMax 国内 API](https://platform.minimax.cn/docs/api-reference/text-anthropic-api)、[Claude Code CLI](https://code.claude.com/docs/en/cli-reference)、[非交互调用](https://code.claude.com/docs/en/headless)、[Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create)、[飞书自定义机器人](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。

## 从旧版升级低思考强度与诊断功能

只需更新本目录代码，不用重新 init 或清理状态库。失败诊断复用既有 `usage_json` 字段，成功摘要和推送记录保留。若同时升级到包含人工跳过功能的版本，请先按上文说明暂停两个 timer 并等待 service 结束；状态库会自动升级，无需手动迁移。

1. 暂停新的摘要触发：`systemctl --user stop werss-digest-prepare.timer`。
2. 等当前 `prepare.service` 结束再更新代码；暂停 timer 不会中断当前模型请求。
3. 修改云端实际 `config.json` 的 model：`name` 为 `MiniMax-M3.1-Flash`，新增 `effort: "low"`，先用 `max_output_tokens: 4096`。JSON 字段需用双引号，其他字段保持原值。更新 example 不会自动修改 config.json。
4. `python3 digest.py check`，然后 `python3 digest.py retry summary` 解除之前失败文章的暂停/次数限制（仅重置没有成功摘要的记录）。
5. `python3 digest.py prepare --limit 1`：这是实际调用，会消耗订阅额度。查看日志中的 effort、结束原因、耗时、token；可用 `send --now --dry-run` 预览摘要并人工核对关键数字。先验证少量文章，不立即批量提高输出预算。
6. 验证后 `systemctl --user start werss-digest-prepare.timer`。service/timer 文件未改变，不需要 daemon-reload。

模拟测试只能验证调用参数和重试逻辑，实际摘要质量和 token 节省幅度需要用你的云端 Key、小批量文章验证。失败批次仍返回非零退出码以便监控；systemd 显示 FAILURE 时，要结合 `prepare_finished` 的成功/失败篇数判断，已成功摘要不会丢失。
