"""Loopback-only HTTP delivery. No hooks, network clients, or external assets."""
import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .content import ContentError, ContentReader


CAPABILITIES = {
    "source": "codex+pi", "adapter_version": "1", "mode": "local rollout JSONL / agent-trace metadata / manual refresh",
    "items": [
        {"name": "会话与轮次", "status": "已验证 / 版本相关", "detail": "session_meta.id；turn_context.turn_id；task_started/task_complete 生命周期。无稳定轮次身份时保留推断或未归属说明。"},
        {"name": "子代理父轮次", "status": "明确身份关联 / 部分覆盖", "detail": "每个子代理轮次记录 root_turn_id，仅在其直接父会话中找到唯一匹配轮次时挂载到该轮。复用的子会话逐轮关联；归属未知或父轮次不在筛选范围时单列标注，不按时间猜测。"},
        {"name": "会话名称", "status": "本地索引 / 版本相关", "detail": "读取来源目录所属 Codex 状态库 threads.name，回退 session_index.jsonl 的 thread_name；刷新采集同步重命名。无名称时标为未命名或子代理会话，单列原生 ID；会话名不从提示词提取。"},
        {"name": "轮次名称", "status": "Prompt 摘要 / 部分覆盖", "detail": "从该轮可读用户 prompt 或子代理 NEW_TASK 提取最多 80 字符的单行摘要；排除运行环境等注入上下文。无可读内容或加密正文时显示轮次序号；会话 ID 与轮次 ID 分别标注。刷新采集后更新历史轮次。"},
        {"name": "工作区名称", "status": "Codex 项目元数据", "detail": "读取来源目录所属状态库 projects.name，优先会话项目关联，否则按项目根目录匹配，并支持 Git worktree。无可核实项目名时回退目录名；原路径保留用于筛选和悬浮查看。刷新采集同步项目重命名。"},
        {"name": "Token", "status": "字段已验证 / 部分覆盖", "detail": "token_count.info.total_token_usage 累计快照；按差分归属。样本总计为输入+输出；缓存与推理分类单独展示、不重复相加；不据此推算计费。"},
        {"name": "缓存 Token", "status": "输入子集 / 部分覆盖", "detail": "缓存读取 = cached_input_tokens；普通输入 = input_tokens − cached_input_tokens；缓存占输入 = 同批有效样本的缓存读取之和 / 输入之和（Token 比例，非请求命中率）。缺失或无效分类不补零，展示轮次覆盖；零输入时比例未知。缓存写入单独展示来源字段与覆盖，不混入读取比例。"},
        {"name": "工具", "status": "已验证 / 部分覆盖", "detail": "response_item function_call/custom_tool_call 与 output 通过 call_id 关联。仅存名称、字节数和事件定位，不保存正文。"},
        {"name": "费用估算", "status": "API 标准价等价估算", "detail": "按已核实模型的当前 Standard 短上下文美元单价，分别计算普通输入、缓存输入、输出；展示估算覆盖与部分小计。未还原逐请求长上下文和服务等级；不含工具附加费。未知模型或缺失分类不补零。实际账单未知。"},
        {"name": "模型请求数", "status": "不支持", "detail": "没有可靠逐请求身份，不从 token_count 快照或工具次数反推请求数。"},
        {"name": "子 Agent 与分叉", "status": "本机样本验证 / 版本相关", "detail": "保留明确的父子关系；基于本机样本，按各会话新增用量合计。分叉聊天扣除可确认的继承历史；基线缺失或无效时保留未知并提示。旧记录缺少计量标记时，父子同现需刷新采集。"},
        {"name": "pi 适配", "status": "请求级用量 / 记录成本", "detail": "消费 agent-trace 的 pi metadata：按用户 entry 合成轮次，assistant 请求的 input+cacheRead 计入输入，工具调用按 call id 关联；直接使用 pi 记录的 cost.total（provider 实报或模型费率），不以当前 OpenAI 单价重算。fork/clone 按 header 时间跳过复制历史。"},
        {"name": "隐私", "status": "统计元数据与短摘要", "detail": "保存会话显示名称、最多 80 字符的 Prompt 轮次摘要与统计元数据，不保存完整 prompt、思考、命令、工具参数或输出正文。摘要、名称、项目路径、身份及文件定位仍可能包含敏感信息，仅保存在本机，无上传。"},
    ],
    "documentation": "docs/CODEX-CAPABILITIES.md",
}


def make_server(store, paths, port=8765, instance="local", archive_only=False, content_reader=None):
    lock = threading.RLock()
    static = Path(__file__).parent / "static"
    capabilities = dict(CAPABILITIES)
    content_reader = content_reader or ContentReader(None)
    capabilities['items'] = list(CAPABILITIES['items']) + [
        {'name': '执行正文', 'status': '按需读取 Trace content 归档',
         'detail': '任务详情展示已脱敏的请求、回复、推理摘要和工具输入输出；正文不写入统计库，不依赖 Knowledge。未覆盖的任务明确提示。'}]
    if archive_only:
        capabilities["mode"] = "agent-trace metadata archive / no Codex or pi source access"
    # Archive-only refresh reports the already-received projection without
    # touching Codex logs; it never pretends to have imported anything.
    archive_only_report = {
        "scanned": 0, "imported": 0, "skipped": 0, "errors": [],
        "warnings": ["归档只读模式：本次刷新未读取 Codex 源日志，仅重新读取已接收的数据。"],
        "mode": "archive_only",
    }

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, fmt, *args):
            pass  # Never print source paths, query strings, or payloads.

        def respond(self, status, data, content_type="application/json; charset=utf-8"):
            body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode() if isinstance(data, (dict, list)) else data
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            host = self.headers.get("Host", "")
            expected = "127.0.0.1:%s" % self.server.server_port
            if host not in (expected, "localhost:%s" % self.server.server_port):
                self.respond(403, {"error": "仅允许本机访问"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in ("http://" + host,):
                self.respond(403, {"error": "拒绝跨来源访问"})
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            url = urlsplit(self.path)
            params = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                with lock:
                    if url.path == "/api/overview":
                        result = store.query(params)
                    elif url.path == "/api/session":
                        result = store.detail(params.get("id", ""), params)
                        if result is None:
                            return self.respond(404, {"error": "会话不存在"})
                    elif url.path in ('/api/session-content', '/api/session-content/blob'):
                        identity = params.get('id', '')
                        detail = store.detail(identity)
                        if detail is None:
                            return self.respond(404, {'error': '会话不存在'})
                        turn_id = params.get('turn_id')
                        if turn_id and not any(turn.get('native_id') == turn_id for turn in detail['turns']):
                            return self.respond(404, {'error': '轮次不存在'})
                        if url.path.endswith('/blob'):
                            result = content_reader.blob(identity, params.get('event_id', ''), params.get('pointer', ''))
                        else:
                            result = content_reader.page(identity, turn_id, params.get('after', 0), params.get('limit', 50),
                                                         params.get('archive_id'), params.get('revision'))
                    elif url.path == "/api/capabilities":
                        result = capabilities
                    elif url.path in ("/", "/index.html") or re.fullmatch(r"/assets/[A-Za-z0-9_-]+\.(?:js|css)", url.path):
                        filename = "index.html" if url.path == "/" else url.path[1:]
                        if not (static / filename).is_file():
                            return self.respond(404, {"error": "静态资源不存在，请重新构建前端"})
                        mime = {"html": "text/html", "js": "text/javascript", "css": "text/css"}[filename.rsplit(".", 1)[-1]]
                        return self.respond(200, (static / filename).read_bytes(), mime + "; charset=utf-8")
                    else:
                        return self.respond(404, {"error": "接口不存在"})
                self.respond(200, result)
            except ContentError as exc:
                self.respond(exc.status, {'error': str(exc)})
            except (ValueError, TypeError) as exc:
                self.respond(400, {"error": str(exc)})
            except Exception:
                self.respond(500, {"error": "本机查询失败，请检查数据库和运行权限"})

        def do_POST(self):
            if not self.allowed():
                return
            if self.path != "/api/refresh":
                return self.respond(404, {"error": "接口不存在"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size < 0 or size > 4096:
                    return self.respond(413, {"error": "请求过大"})
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    return self.respond(415, {"error": "需要 application/json"})
                json.loads(self.rfile.read(size) or b"{}")
                if archive_only:
                    report = dict(archive_only_report)
                    report["received_at"] = datetime.now(timezone.utc).isoformat()
                    return self.respond(200, report)
                with lock:
                    result = store.import_paths(paths, instance=instance)
                self.respond(200, result)
            except (ValueError, TypeError):
                self.respond(400, {"error": "无效请求"})
            except Exception:
                self.respond(500, {"error": "采集失败，Agent 运行不受影响；请检查源文件与数据库权限"})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
