"""ПОДДЕЛЬНЫЙ `cursor-agent` для тестов хоста (Ф71-1). Настоящий Cursor не запускается.

Один запуск — один ход, как у настоящего `cursor-agent -p`: текст реплики — из stdin, события — NDJSON в
stdout по образцу проб 19.10 (`system/init`, `thinking`, дельты `assistant` с `timestamp_ms`, полный сегмент с
`model_call_id`, `tool_call` started/completed, `result` с `usage`). Хуки — из `--plugin-dir`
(`hooks/hooks.json`): нужный вызов исполняет команду хука через `/bin/sh -c` с JSON на stdin, как Cursor.

Каталог подделки — каталог этого файла:
- `scenario.json` — `{"turns": [[шаг, …], …]}`, ход N — N-й список (кончились — последний);
- `calls.jsonl` — на каждый запуск: argv, stdin, pid, выбранные переменные окружения;
- `hooks.jsonl` — на каждый хук: событие, вход, код выхода, ответ.

Шаги: `{"text": "…"}`; `{"thinking": "…"}`; `{"tool": "shell|read|edit|delete|grep|mcp", "args": {…},
"result": {…}, "hook": {"event": …, "input": {…}}}` — с хуком: запрет — `rejected`, разрешение — `result`;
`{"hang": секунды}`; `{"spawn": "команда"}` — запустить в своей группе (pid — `spawned.jsonl`);
`{"exit": код, "stderr": "…"}` — выйти без `result`; `{"final": {…}}` — поля `result`
хода поверх обычных (ошибка, текст, `usage`); `"no_end": true` у вызова — конца вызова поток не скажет.
SIGINT — «Aborting operation…» в stderr и код 130."""

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_KEYS = (
    "HUB_SESSION",
    "HUB_SESSION_KEY",
    "HUB_SESSION_SUB_PROFILE",
    "HUB_CURSOR_SOCK",
    "HUB_CURSOR_PID",
    "HUB_WORK",
    "ANTHROPIC_API_KEY",
    "CLAUDECODE",
    "DISPLAY",
)


def _append(name: str, record: dict) -> None:
    with open(HERE / name, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _out(event: dict) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _flag(argv: list[str], name: str) -> str | None:
    return argv[argv.index(name) + 1] if name in argv else None


def _on_sigint(_signum, _frame) -> None:
    sys.stderr.write("Aborting operation...\n")
    sys.stderr.flush()
    os._exit(130)


def _hook(plugin: str, event: str, data: dict) -> dict:
    hooks = json.loads((Path(plugin) / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    [entry] = hooks["hooks"][event]
    proc = subprocess.run(
        ["/bin/sh", "-c", entry["command"]],
        input=json.dumps(data, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=60,
    )
    try:
        answer = json.loads(proc.stdout.strip().splitlines()[-1])
    except ValueError, IndexError:
        answer = {"permission": "deny", "agent_message": "битый ответ хука"}
    _append("hooks.jsonl", {"event": event, "input": data, "rc": proc.returncode, "answer": answer})
    if proc.returncode != 0 and entry.get("failClosed"):
        return {"permission": "deny", "agent_message": answer.get("agent_message") or "хук упал"}
    return answer


KINDS = {
    "shell": "shellToolCall",
    "read": "readToolCall",
    "edit": "editToolCall",
    "delete": "deleteToolCall",
    "grep": "grepToolCall",
    "glob": "globToolCall",
    "mcp": "mcpToolCall",
    "task": "taskToolCall",
}


def main() -> None:
    signal.signal(signal.SIGINT, _on_sigint)
    argv = sys.argv[1:]
    stdin = sys.stdin.read()
    calls = HERE / "calls.jsonl"
    n = len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0
    _append(
        "calls.jsonl",
        {
            "argv": argv,
            "stdin": stdin,
            "pid": os.getpid(),
            "cwd": os.getcwd(),
            "env": {k: os.environ[k] for k in ENV_KEYS if k in os.environ},
        },
    )
    scenario = (
        json.loads((HERE / "scenario.json").read_text(encoding="utf-8"))
        if (HERE / "scenario.json").exists()
        else {}
    )
    turns = scenario.get("turns") or [[{"text": "ок"}]]
    steps = turns[min(n, len(turns) - 1)]
    chat = _flag(argv, "--resume") or f"chat-{uuid.uuid4().hex[:12]}"
    plugin = _flag(argv, "--plugin-dir") or ""
    _out(
        {
            "type": "system",
            "subtype": "init",
            "session_id": chat,
            "permissionMode": "default",
            "model": "Grok 4.7 256K High",
            "cwd": os.getcwd(),
        }
    )
    _out(
        {
            "type": "user",
            "session_id": chat,
            "message": {"role": "user", "content": [{"type": "text", "text": stdin}]},
        }
    )
    texts: list[str] = []
    final: dict = {}
    call = 0
    for step in steps:
        if "thinking" in step:
            _out({"type": "thinking", "subtype": "delta", "text": step["thinking"], "session_id": chat})
            _out({"type": "thinking", "subtype": "completed", "session_id": chat})
        elif "text" in step:
            text = step["text"]
            half = max(1, len(text) // 2)
            for piece in (text[:half], text[half:]):
                if piece:
                    _out(
                        {
                            "type": "assistant",
                            "session_id": chat,
                            "timestamp_ms": int(time.time() * 1000),
                            "message": {"role": "assistant", "content": [{"type": "text", "text": piece}]},
                        }
                    )
            _out(
                {
                    "type": "assistant",
                    "session_id": chat,
                    "model_call_id": f"mc-{len(texts)}",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
                }
            )
            texts.append(text)
        elif "tool" in step:
            call += 1
            call_id = f"call_{call}\nfc_{call}"
            kind = KINDS[step["tool"]]
            args = dict(step.get("args") or {})
            _out(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": call_id,
                    "session_id": chat,
                    "tool_call": {kind: {"args": args}},
                }
            )
            if step.get("no_end"):
                continue  # вызов начат, конца поток не скажет
            result = step.get("result") or {"success": {}}
            if step.get("hook"):
                data = {
                    "conversation_id": chat,
                    "tool_use_id": f"tool_{call}",
                    "workspace_roots": [os.getcwd()],
                    **step["hook"]["input"],
                }
                answer = _hook(plugin, step["hook"]["event"], data)
                if answer.get("permission") != "allow":
                    result = {"rejected": {"reason": answer.get("agent_message") or "denied"}}
            _out(
                {
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": call_id,
                    "session_id": chat,
                    "tool_call": {kind: {"args": args, "result": result}},
                }
            )
        elif "final" in step:
            final.update(step["final"])
        elif "hang" in step:
            time.sleep(float(step["hang"]))
        elif "spawn" in step:
            # Команда Shell Cursor — в своей группе процессов (Ф74-8); pid — в `spawned.jsonl`.
            child = subprocess.Popen(  # noqa: S603
                ["/bin/sh", "-c", step["spawn"]],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            with (HERE / "spawned.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"pid": child.pid}) + "\n")
        elif "exit" in step:
            sys.stderr.write(step.get("stderr") or "")
            sys.stderr.flush()
            sys.exit(int(step["exit"]))
    _out(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "\n".join(texts),
            "session_id": chat,
            "duration_ms": 1234,
            "usage": {"inputTokens": 100, "outputTokens": 20, "cacheReadTokens": 3000, "cacheWriteTokens": 7},
            **final,
        }
    )


if __name__ == "__main__":
    main()
