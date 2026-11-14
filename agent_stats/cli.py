import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .core import Store
from .server import make_server


def default_sources():
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    return [str(home / "sessions"), str(home / "archived_sessions")]


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-stats", description="Agent Stats · Codex 本机用量看板")
    parser.add_argument("--db", default=os.environ.get("AGENT_STATS_DB", str(Path.home() / ".local/share/agent-stats/stats.sqlite3")), help="SQLite 路径")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("import", "serve"):
        sub = commands.add_parser(command)
        sub.add_argument("--source", action="append", help="JSONL 文件或目录，可重复；默认 CODEX_HOME 下的 sessions 与 archived_sessions")
        sub.add_argument("--instance", default="local", help="来源实例命名空间；不同设备使用不同值")
        if command == "serve":
            sub.add_argument("--port", type=int, default=8765)
            sub.add_argument("--no-import", action="store_true", help="启动时只读已采集数据库")
    summary = commands.add_parser("summary")
    for key in ("start", "end", "project", "model", "source"):
        summary.add_argument("--" + key)
    archive = commands.add_parser("import-archive", help="消费共享归档的 metadata JSON 批次，不读取原始日志")
    archive.add_argument("--input", required=True, help="agent-trace export --view metadata 的输出文件")
    demo = commands.add_parser("demo", help="向指定目录生成纯合成 Codex 事件")
    demo.add_argument("--output", default=".agent-stats/demo")
    args = parser.parse_args(argv)
    if args.command == "demo":
        from .demo import generate
        print(json.dumps(generate(Path(args.output)), ensure_ascii=False))
        return 0
    try:
        db = Path(args.db).expanduser()
        db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        store = Store(db)
        if args.command == "summary":
            print(json.dumps(store.query({k: getattr(args, k) for k in ("start", "end", "project", "model", "source") if getattr(args, k)}), ensure_ascii=False, indent=2))
        elif args.command == "import-archive":
            from .archive import import_batch
            with open(args.input, encoding="utf-8") as stream:
                batch = json.load(stream)
            print(json.dumps(import_batch(store, batch), ensure_ascii=False))
        elif args.command == "import":
            print(json.dumps(store.import_paths(args.source or default_sources(), instance=args.instance), ensure_ascii=False, indent=2))
        else:
            paths = args.source or default_sources()
            if not args.no_import:
                print(json.dumps(store.import_paths(paths, instance=args.instance), ensure_ascii=False))
            server = make_server(store, paths, args.port, args.instance)
            print("Agent Stats: http://127.0.0.1:%s · Ctrl+C 停止" % server.server_port, flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print("Agent Stats: %s" % exc, file=sys.stderr)
        return 1
