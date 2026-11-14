import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .core import Store
from .server import make_server
from .content import ContentReader, default_content_config


def default_sources():
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    return [str(home / "sessions"), str(home / "archived_sessions")]


def default_db():
    return os.environ.get("AGENT_STATS_DB", str(Path.home() / ".local/share/agent-stats/stats.sqlite3"))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="agent-stats", description="Agent Stats · 本机 agent 用量看板（Codex / pi / dsh）")
    parser.add_argument("--db", default=None, help="SQLite 路径（也可放在 follow 子命令之后）")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("import", "serve"):
        sub = commands.add_parser(command)
        sub.add_argument("--source", action="append", help="JSONL 文件或目录，可重复；默认 CODEX_HOME 下的 sessions 与 archived_sessions")
        sub.add_argument("--instance", default="local", help="来源实例命名空间；不同设备使用不同值")
        if command == "serve":
            sub.add_argument("--port", type=int, default=8765)
            sub.add_argument("--archive-only", "--no-import", dest="archive_only", action="store_true",
                             help="只消费已接收的归档数据：启动和页面刷新都不读取 Codex 源日志")
            content = sub.add_mutually_exclusive_group()
            content.add_argument('--content-config', help='任务详情只读的 Agent Trace content 配置；默认发现本机 content.json')
            content.add_argument('--no-content', action='store_true', help='关闭任务详情的正文读取')
    summary = commands.add_parser("summary")
    for key in ("start", "end", "project", "model", "source"):
        summary.add_argument("--" + key)
    archive = commands.add_parser("import-archive", help="消费共享归档的 metadata JSON 批次，不读取原始日志")
    archive.add_argument("--input", required=True, help="agent-trace export --view metadata 的输出文件")
    follow = commands.add_parser("follow", help="跟随 agent-trace 本机 API 自动拉取 metadata")
    follow.add_argument("--config", required=True, help="agent-stats.follow.json 路径")
    follow.add_argument("--db", dest="follow_db", default=None, help="显式覆盖配置中的目标数据库")
    demo = commands.add_parser("demo", help="向指定目录生成纯合成 Codex 事件")
    demo.add_argument("--output", default=".agent-stats/demo")
    args = parser.parse_args(argv)
    if args.command == "demo":
        from .demo import generate
        print(json.dumps(generate(Path(args.output)), ensure_ascii=False))
        return 0
    if args.command == "follow":
        from .follow import run_follow
        return run_follow(args.config, db_override=args.follow_db or args.db)
    try:
        db = Path(args.db or default_db()).expanduser()
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
            # Archive-only mode never opens Codex logs: no startup import and no
            # source refresh. The dashboard only reads already-received data.
            if not args.archive_only:
                print(json.dumps(store.import_paths(paths, instance=args.instance), ensure_ascii=False))
            content_path = None if args.no_content else args.content_config or default_content_config()
            server = make_server(store, paths, args.port, args.instance, archive_only=args.archive_only,
                                 content_reader=ContentReader(content_path))
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
