# Agent Stats · Codex / pi / dsh

面向 Codex、pi 与 DeepSeek Harness（`dsh`）的本地用量分析工具：**本地日志 / agent-trace 元数据 → SQLite → 总览看板 → 会话 / 轮次及工具证据下钻**。

pi 通过 agent-trace 的 pi adapter 接入：会话、轮次、工具和 Token 来自 pi 的 session JSONL，费用优先使用 pi 在请求时记录的 `usage.cost.total`（provider 实报或模型费率），不再按当前价格表重算；没有记录成本的来源（如 Codex 与 dsh）继续按 API 标准价估算，看板会区分两种口径。dsh 通过 agent-trace 的 dsh adapter 接入：Trace 解码 `session.vN.jsonl.zstd` 后提供相同的 metadata 事件，dsh 日志只记录请求级 TokenUsage，不记录请求成本。dsh 子代理会话按 header 的 `origin`/`parentSession` 归属父会话，在「各轮明细」的会话视图中嵌套在父会话下；dsh 日志没有父轮次身份，子代理轮次保持「所属父轮次未知」而不按时间猜测。

所有数据都留在本机：服务只监听 `127.0.0.1`，统计库只保存元数据。任务详情可按需只读 Agent Trace 已脱敏的正文归档，正文不写入统计库。缺失的信息保持「未知」，不做近似估算。

## 特性

- **本地优先**：只读 `CODEX_HOME` 下的 `sessions` / `archived_sessions`，不联网、不上传。
- **覆盖度诚实**：Token、费用、模型请求数等字段按来源实际可得性展示，缺失不记为零。
- **零后端依赖**：后端仅用 Python 标准库（3.9+），自带构建好的前端产物，装完即可运行。
- **前端可开发**：React + TypeScript + Vite，按页面 / 组件 / 数据访问拆分。

## 快速开始

```bash
python3 -m agent_stats serve
```

访问 <http://127.0.0.1:8765/>。

本机当前正式部署使用 `follow` + `serve --archive-only`：看板只读已接收数据，新会话由 `follow` 从本机 agent-trace API 拉取，不再直接读取 Codex 日志。安装/重启见 [运行说明](docs/RUNNING.md) 的“macOS 登录自动启动”。

### 在任务详情查看执行正文

打开会话或轮次详情，点击“查看执行正文”，按时间线查看用户请求、Agent 回复、最终回答、推理摘要和工具输入输出。工具记录和摘要默认折叠；通过“加载更多”续读。“来源证据”保留事件 ID、源文件及行号；大型外置正文需点击读取，服务端校验文件范围、大小及 SHA-256。

正文直接来自 Agent Trace 的独立 content 归档，不依赖 Agent Knowledge，也不读取原始 Codex/pi 日志。默认发现 `~/.local/share/agent-trace/content.json`；可通过 `AGENT_STATS_CONTENT_CONFIG` 或 `serve --content-config PATH` 指定 Trace content 配置，使用 `serve --no-content` 关闭。没有配置、会话未采集或归档不可用时显示明确提示，已有统计仍可使用。

```sh
python3 -m agent_stats serve --archive-only --content-config /absolute/path/to/agent-trace/content.json
```

会话详情展示整个会话的正文，不随统计日期/模型筛选裁剪；轮次详情只展示对应轮次。推理摘要是源日志中的模型摘要，不能当作完整内部思考；工具返回也不等于执行成功。正文按普通文本展示，不执行其中的 HTML、脚本或命令。

默认数据库为 `~/.local/share/agent-stats/stats.sqlite3`；受限环境可加全局参数 `--db .agent-stats/stats.sqlite3`。

先看纯合成演示（不读真实日志）：

```bash
python3 -m agent_stats demo --output .agent-stats/demo
python3 -m agent_stats --db .agent-stats/demo.sqlite3 serve --source .agent-stats/demo
```

也可以安装为命令：

```bash
python3 -m pip install -e .
agent-stats serve
```

## 可选：跟随元数据归档

`follow` 可消费兼容的元数据事件 API（回环地址），按已提交游标增量拉取，统计投影与游标在同一事务内提交：

```json
{
  "schema_version": 1,
  "trace_url": "http://127.0.0.1:8766",
  "token_env": "AGENT_TRACE_STATS_TOKEN",
  "database": "./.agent-stats/stats.sqlite3",
  "page_size": 500,
  "idle_seconds": 15,
  "request_timeout_seconds": 20,
  "max_backoff_seconds": 300
}
```

```sh
export AGENT_TRACE_STATS_TOKEN=...      # 明文 token 只从环境变量读取，不写入配置或日志
python3 -m agent_stats follow --config agent-stats.follow.example.json
python3 -m agent_stats --db .agent-stats/stats.sqlite3 serve --archive-only
```

stdout 逐行输出 `follow.started` / `follow.page` / `follow.idle` / `follow.error` / `follow.state` / `follow.stopped`（NDJSON，不含 token、正文或原始事件）。`Ctrl+C` / SIGTERM 优雅停止并释放租约，退出码 `0`；配置 / 启动 / 租约失败 `1`；启动后遇到永久错误 `2`。

也可以离线导入归档批次：

```sh
python3 -m agent_stats --db .agent-stats/shared.sqlite3 import-archive --input /path/to/metadata-page.json
python3 -m agent_stats --db .agent-stats/shared.sqlite3 summary
```

## 项目结构

```
agent_stats/          后端（采集、SQLite 投影、HTTP API、CLI）
agent_stats/static/   前端构建产物（随包发布）
frontend/             React + TypeScript + Vite 源码与前端测试
tests/                Python 单元 / 集成测试
scripts/              辅助脚本（如 macOS LaunchAgent 安装）
```

## 开发与测试

后端测试（无需第三方依赖）：

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

前端（Node 22+）：

```bash
cd frontend
npm ci
npm test          # node --test + 类型剥离
npm run check     # tsc --noEmit
npm run build     # 输出到 agent_stats/static
```

CI 会在每个 Pull Request 上运行 Python 3.9 / 3.13、前端全套检查，以及敏感信息扫描，结果汇总为 `CI success`；`main` 分支要求该检查通过后才能合并。

提交前可本地先跑一遍敏感信息扫描（token、私钥、URL 内嵌凭证、个人主目录路径、`.env` 之类文件），命中即失败：

```bash
python3 scripts/check_sensitive.py
```

## 参与贡献

CI 流水线、`main` 分支保护、Dependabot 和敏感信息检查的详细说明见 [CONTRIBUTING.md](CONTRIBUTING.md)。所有改动请走 Pull Request，`CI success` 通过后才能合并。

## 数据与隐私

- 默认只读取 `CODEX_HOME`（默认 `~/.codex`），只保存统计元数据。
- 路径与事件身份仍可能属于敏感信息，数据库文件请自行妥善保管。
- `follow` 的 token 只从环境变量读取，不写入配置或日志。
- 服务仅绑定回环地址，不提供公网访问能力。

## 许可

暂未附带开源许可证（默认保留所有权利）。如需复用请先联系作者。
