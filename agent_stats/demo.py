"""Deterministic synthetic fixtures; never read user data."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def generate(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    count = 0
    for s in range(12):
        base = datetime(2026, 10, 22, 2, tzinfo=timezone.utc) + timedelta(days=s)
        events = []

        def emit(minutes, kind, payload):
            events.append({"timestamp": (base + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"), "type": kind, "payload": payload})

        emit(0, "session_meta", {"id": "demo-session-%02d" % s, "cwd": "/demo/" + ("atlas" if s % 3 else "orbit"), "cli_version": "synthetic-1", "source": "cli", "model_provider": "openai"})
        totals = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0, "total_tokens": 0}
        for t in range(1 + s % 4):
            minute = 2 + t * 8
            turn = "demo-%02d-turn-%d" % (s, t)
            emit(minute, "event_msg", {"type": "task_started", "turn_id": turn})
            emit(minute, "turn_context", {"turn_id": turn, "model": "demo-model-a" if s % 3 else "demo-model-b"})
            emit(minute + 1, "response_item", {"type": "function_call", "name": "exec_command", "call_id": turn + "-tool", "arguments": "SYNTHETIC_SECRET_DO_NOT_PERSIST"})
            emit(minute + 2, "response_item", {"type": "function_call_output", "call_id": turn + "-tool", "output": "Synthetic output " + "x" * (9000 if s == 7 else 40)})
            if not (s == 10 and t == 0):
                inp = (s + 1) * 1250 + t * 2400
                out = 350 + s * 30
                usage = {"input_tokens": inp, "cached_input_tokens": inp // 2, "output_tokens": out, "reasoning_output_tokens": out // 3, "total_tokens": inp + out}
                for k in totals:
                    totals[k] += usage[k]
                emit(minute + 3, "event_msg", {"type": "token_count", "info": {"total_token_usage": dict(totals), "last_token_usage": usage}})
                if s == 2:
                    emit(minute + 3, "event_msg", {"type": "token_count", "info": {"total_token_usage": dict(totals), "last_token_usage": usage}})
            if s != 11 or t != s % 4:
                emit(minute + 4, "event_msg", {"type": "task_complete", "turn_id": turn})
        file = directory / ("rollout-demo-%02d.jsonl" % s)
        file.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        count += len(events)
    return {"directory": str(directory), "files": 12, "events": count, "synthetic": True}
