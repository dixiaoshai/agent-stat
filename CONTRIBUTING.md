# 贡献指南

本文说明本仓库的 GitHub 工作流：CI 流水线、`main` 分支保护、Dependabot 和敏感信息检查。

## 开发流程

```bash
git switch -c feat/short-description          # 从最新 main 拉分支
# ... 修改代码并在本地跑检查（见下文）...
git push -u origin feat/short-description
gh pr create --base main --fill
# CI 通过后 squash 合并，分支会自动删除
```

`main` 不允许直接 push，所有改动都必须经过 Pull Request。合并方式建议 **Squash**，保持线性历史。

## CI 流水线

配置文件：`.github/workflows/ci.yml`

**触发条件**

| 事件 | 说明 |
|---|---|
| `pull_request` → `main` | 打开 PR、往 PR 推新 commit |
| `push` → `main` | 合并后的回归检查 |
| `workflow_dispatch` | 在 Actions 页面手动触发 |

**Job 组成**

| Job | 内容 |
|---|---|
| `Python 3.9` / `Python 3.13` | `pip install -e .` + 全部 unittest（矩阵，runner 固定 `ubuntu-24.04`） |
| `Frontend` | `npm ci` → `tsc --noEmit` → `prettier --check` → `node --test` → `vite build` |
| `Sensitive data scan` | `python3 scripts/check_sensitive.py` |
| `CI success` | 汇总以上所有 Job，**只有这一个**被设为分支保护的必需检查 |

同一分支的新 push 会取消上一次未完成的运行（`concurrency`），各 Job 有 `timeout-minutes`。

**本地复现**

```bash
# 后端
python3 -m unittest discover -s tests -p 'test_*.py'

# 前端
cd frontend && npm ci && npm run check && npm run format:check && npm test && npm run build

# 敏感信息
python3 scripts/check_sensitive.py
```

> 前端构建输出到 `agent_stats/static`，该目录**随仓库提交**。修改前端后请一并提交构建产物。

## `main` 分支保护

当前使用 GitHub 经典分支保护（不是 Rulesets），对管理员同样生效：

| 规则 | 值 |
|---|---|
| 必须走 Pull Request | 是 |
| 必需状态检查 | `CI success` |
| 分支必须与 main 同步 | 是（strict） |
| 必需 review 数 | 0（单人开发；有协作者后可调高） |
| 新 commit 后撤销旧 approval | 是 |
| 合并前必须解决所有对话 | 是 |
| 管理员绕过 | 禁止 |
| Force push / 删除分支 | 禁止 |

查看当前规则：

```bash
gh api repos/dixiaoshai/agent-stat/branches/main/protection
```

> 修改 CI Job 名称时，必须同步更新必需状态检查名（当前为 `CI success`），否则 PR 会永远等不到检查结果。

## Dependabot

配置文件：`.github/dependabot.yml`（必须在默认分支上才生效）。

| 生态 | 目录 | 频率 | 分组 |
|---|---|---|---|
| `github-actions` | `/` | 每周一 | 所有 action 合成 1 个 PR |
| `npm` | `/frontend` | 每周一 | minor + patch 合成 1 个 PR，major 单独开 |

- `open-pull-requests-limit: 5` 控制同时打开的 PR 数。
- 安全漏洞修复由仓库设置 `Settings → Code security` 中的 Dependabot security updates 驱动，**不受上面的 schedule 和分组限制**，命中即开 PR。
- 手动触发检查：`Insights → Dependency graph → Dependabot → Check for updates`。

**处理原则**

1. patch / minor：CI 绿即可合。
2. major（如 vite、typescript、plugin-react）：先看 CI，再本地跑一次 `npm run dev` 确认运行时行为。
3. 锁文件冲突：在分支上合入最新 `main`，手工解决 `package.json` 后重新生成 lockfile，并重新构建 `agent_stats/static`。
4. 安全更新 PR 不要长期搁置，它们对应真实的漏洞告警。

## 敏感信息检查

三道防线：

1. **GitHub secret scanning push protection**：`git push` 时服务端直接拒绝已知格式的 token（`GH013`）。
2. **CI `Sensitive data scan`**：PR 阶段扫描所有 tracked 文件（token、私钥、JWT、URL 内嵌凭证、个人主目录路径、`.env` 类文件），命中则 `CI success` 失败。
3. **本地自检**：提交前运行 `python3 scripts/check_sensitive.py`。

**如果误提交了敏感信息**，不要只 `git revert` —— 内容仍留在历史里。正确顺序：

1. 立刻在服务端**轮换/吊销**该凭证；
2. 从工作树移除并提交；
3. 如已推到远端，用 `git filter-repo` 清洗历史后 force push（需要临时关闭分支保护）。

本仓库只保存统计元数据：不要提交 `~/.codex`、`~/.local/share/agent-stats`、`.agent-stats/`、真实 SQLite 数据库、会话原始日志或截图。

## 修改 workflow 文件

推送 `.github/workflows/*` 需要 token 具备 `workflow` scope。若 `git push` 报：

```
refusing to allow an OAuth App to create or update workflow ... without `workflow` scope
```

二选一：

```bash
gh auth refresh -h github.com -s workflow          # 给当前 token 加权限
git remote set-url origin git@github.com:dixiaoshai/agent-stat.git   # 或改用 SSH
```

## 发布

暂未配置自动发布流水线。当前前端构建产物随仓库和 Python 包一起分发；后续可增加 tag 触发的 GitHub Release / PyPI 发布。
