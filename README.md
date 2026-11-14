# Agent Stats · Codex / pi

面向 Codex 与 pi 的本地用量分析工具：**本地日志 / agent-trace 元数据 → SQLite → 总览看板 → 会话 / 轮次及工具证据下钻**。

pi 通过 agent-trace 的 pi adapter 接入：会话、轮次、工具和 Token 来自 pi 的 session JSONL，费用优先使用 pi 在请求时记录的 `usage.cost.total`（provider 实报或模型费率），不再按当前价格表重算；没有记录成本的来源（如 Codex）继续按 API 标准价估算，看板会区分两种口径。

所有数据都留在本机：服务只监听 `127.0.0.1`，只保存统计元数据，不保存提示词、命令、思考过程或工具输出正文。缺失的信息保持「未知」，不做近似估算。

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

## 数据与隐私

- 默认只读取 `CODEX_HOME`（默认 `~/.codex`），只保存统计元数据。
- 路径与事件身份仍可能属于敏感信息，数据库文件请自行妥善保管。
- `follow` 的 token 只从环境变量读取，不写入配置或日志。
- 服务仅绑定回环地址，不提供公网访问能力。

## 许可

暂未附带开源许可证（默认保留所有权利）。如需复用请先联系作者。
