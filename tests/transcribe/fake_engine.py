"""Подделка движка для тестов службы: тот же протокол строк JSON. Режим — первый аргумент:
`ok` (ответ `code 0`), `doubtful` (`code 2`), `crash` (выход без ответа), `hang` (молчит), `error` (`code 1`),
`garbage` (не JSON), `preload_error` (как `ok`, но загрузка заранее не удаётся). Ф79-1: `{"preload": true}` —
ответ без задания; подсказка `lang` видна в тексте (`… [ru]`). Второй аргумент — файл, куда дописывается pid
на каждое задание."""

import json
import os
import sys
import time


def main() -> int:
    mode = sys.argv[1]
    pids = sys.argv[2] if len(sys.argv) > 2 else None
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("preload") is True:
            # Ф79-1: загрузка модели заранее — не задание (в файл pid не пишется).
            preload = {"code": 0, "preload": True, "device": "cuda", "model": "large-v3-turbo"}
            if mode == "preload_error":
                preload = {"code": 1, "preload": True, "error": "RuntimeError: нет памяти"}
            sys.stdout.write(json.dumps(preload) + "\n")
            sys.stdout.flush()
            continue
        if pids:
            with open(pids, "a", encoding="utf-8") as f:
                f.write(f"{os.getpid()}\n")
        if mode == "crash":
            return 1
        if mode == "hang":
            time.sleep(600)
        if mode == "garbage":
            sys.stdout.write("не json\n")
        elif mode == "error":
            sys.stdout.write(json.dumps({"code": 1, "error": "RuntimeError: сломалось"}) + "\n")
        else:
            doubtful = mode == "doubtful"
            response = {
                "code": 2 if doubtful else 0,
                "text": ""
                if doubtful
                else "проверка "
                + os.path.basename(request["wav"])
                + (f" [{request['lang']}]" if request.get("lang") else ""),
                "lang": None if doubtful else "ru",
                "lang_prob": None if doubtful else 0.91,
                "problem": "пусто" if doubtful else None,
                "device": "cuda",
                # 14-6b: флаг ручного повтора из запроса службы — виден тесту через поле модели.
                "model": "large-v3-turbo" + (" manual" if request.get("manual") is True else ""),
                "elapsed": 1.25,
            }
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
