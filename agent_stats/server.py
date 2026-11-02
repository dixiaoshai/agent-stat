"""Loopback-only HTTP delivery. No hooks, network clients, or external assets."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


CAPABILITIES = {
    "source": "codex", "adapter_version": "1", "mode": "local rollout JSONL / manual refresh",
    "items": [
        {"name": "会话与轮次", "status": "已验证 / 版本相关", "detail": "session_meta.id；turn_context.turn_id；task_started/task_complete 生命周期。无稳定轮次身份时保留推断或未归属说明。"},
        {"name": "Token", "status": "字段已验证 / 部分覆盖", "detail": "token_count.info.total_token_usage 累计快照；按差分归属。样本总计为输入+输出；缓存与推理分类单独展示、不重复相加；不据此推算计费。"},
        {"name": "工具", "status": "已验证 / 部分覆盖", "detail": "response_item function_call/custom_tool_call 与 output 通过 call_id 关联。仅存名称、字节数和事件定位，不保存正文。"},
        {"name": "费用 / 模型请求数", "status": "不支持", "detail": "未核实可靠费用与逐请求身份；不套模型近似费率，不从 token_count 数量反推请求数。"},
        {"name": "子 Agent", "status": "包含关系未知", "detail": "有明确父身份时保留关联；父子用量分开展示，不给出确定联合总量。"},
        {"name": "隐私", "status": "元数据模式", "detail": "不保存 prompt、思考、命令、工具参数或输出正文。项目路径、身份及文件定位仍属于本机元数据。无上传。"},
    ],
    "documentation": "docs/CODEX-CAPABILITIES.md",
}


def make_server(store, paths, port=8765, instance="local"):
    lock = threading.RLock()
    static = Path(__file__).parent / "static"

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
                    elif url.path == "/api/capabilities":
                        result = CAPABILITIES
                    elif url.path in ("/", "/index.html") or re.fullmatch(r"/assets/[A-Za-z0-9_-]+\.(?:js|css)", url.path):
                        filename = "index.html" if url.path == "/" else url.path[1:]
                        if not (static / filename).is_file():
                            return self.respond(404, {"error": "静态资源不存在，请重新构建前端"})
                        mime = {"html": "text/html", "js": "text/javascript", "css": "text/css"}[filename.rsplit(".", 1)[-1]]
                        return self.respond(200, (static / filename).read_bytes(), mime + "; charset=utf-8")
                    else:
                        return self.respond(404, {"error": "接口不存在"})
                self.respond(200, result)
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
                with lock:
                    result = store.import_paths(paths, instance=instance)
                self.respond(200, result)
            except (ValueError, TypeError):
                self.respond(400, {"error": "无效请求"})
            except Exception:
                self.respond(500, {"error": "采集失败，Agent 运行不受影响；请检查源文件与数据库权限"})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
