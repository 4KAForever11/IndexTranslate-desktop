"""测试用的假扩展：实现宿主 JSONL 协议，不依赖任何重型库。

支持方法：
    ping                     -> {"ok": true, "result": {"version": "0.0.1", ...}}
    echo    {"text": "..."}   -> 原样返回
    ocr     {"image": "..."}  -> 固定两段文字（带 bbox）
    boom                      -> 返回 ok:false，用于验证错误透传
    stream  {"count": 3}      -> 推 3 条 chunk 事件 + result
    start   {}               -> 立刻返回 started，并推 2 条 final 事件
    stop                      -> {"sentences": 2}
    slow    {"sec": 5}        -> 睡 sec 秒再返回（验证超时）
    exit                      -> 退出
"""
from __future__ import annotations

import json
import sys
import time

VERSION = "0.0.1"


def reply(req_id, ok: bool, payload) -> None:
    message = {"id": req_id, "ok": ok}
    if ok:
        message["result"] = payload
    else:
        message["error"] = str(payload)
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def event(req_id, name: str, data: dict) -> None:
    sys.stdout.write(
        json.dumps({"id": req_id, "event": name, "data": data}, ensure_ascii=False) + "\n"
    )
    sys.stdout.flush()


def main() -> int:
    sys.stderr.write(f"fake-sidecar {VERSION} started\n")
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        req_id = request.get("id")
        method = str(request.get("method") or "")
        params = request.get("params") or {}

        if method == "ping":
            reply(req_id, True, {"version": VERSION, "pid": 0, "echo": "pong"})
        elif method == "echo":
            reply(req_id, True, {"text": params.get("text", "")})
        elif method == "ocr":
            reply(
                req_id,
                True,
                {
                    "segments": [
                        {"text": "Hello", "bbox": [10, 20, 90, 40], "score": 0.99},
                        {"text": "World", "bbox": [10, 50, 95, 70], "score": 0.98},
                    ],
                    "width": 200,
                    "height": 100,
                },
            )
        elif method == "boom":
            reply(req_id, False, "假扩展故意失败")
        elif method == "stream":
            count = int(params.get("count") or 3)
            for index in range(count):
                event(req_id, "chunk", {"index": index, "delta": f"part{index}"})
            reply(req_id, True, {"count": count})
        elif method == "start":
            reply(req_id, True, {"started": True})
            event(req_id, "final", {"text": "Hello there", "start": 0.0, "end": 1.2})
            event(req_id, "final", {"text": "General Kenobi", "start": 1.3, "end": 2.4})
        elif method == "stop":
            reply(req_id, True, {"sentences": 2})
        elif method == "slow":
            time.sleep(float(params.get("sec") or 5))
            reply(req_id, True, {"slept": True})
        elif method == "exit":
            reply(req_id, True, {"bye": True})
            return 0
        else:
            reply(req_id, False, f"未知方法：{method}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
