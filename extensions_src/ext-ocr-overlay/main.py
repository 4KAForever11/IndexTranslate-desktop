"""ext-ocr-overlay —— 本地 OCR 原位贴片扩展（Sidecar）。

协议：stdin/stdout 每行一个 JSON。参见 app/core/extensions/sidecar_client.py。

方法：
    ping                              -> {"ok": true, "result": {"version": ..., "deps": {...}}}
    ocr     {"image": "<path>"}       -> {"segments": [{"text": ..., "bbox": [x0,y0,x1,y1], "score": ...}]}
    ocr_b64 {"image_b64": "<base64>"} -> 同上
    exit                              -> {"ok": true} 然后退出

依赖：rapidocr-onnxruntime（纯 CPU ONNX，约 30–45 MB）。
没装依赖时 ping 照常返回，ocr 返回友好的错误信息，宿主据此降级为“本地识别不可用”。
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import traceback
from typing import Any

VERSION = "1.1.0"
log = sys.stderr


def _deps() -> dict[str, Any]:
    status: dict[str, Any] = {"rapidocr": False, "pillow": False, "numpy": False}
    try:
        import rapidocr_onnxruntime  # noqa: F401

        status["rapidocr"] = True
    except Exception:
        pass
    try:
        import PIL  # noqa: F401

        status["pillow"] = True
    except Exception:
        pass
    try:
        import numpy  # noqa: F401

        status["numpy"] = True
    except Exception:
        pass
    return status


_ENGINE = None


def _engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        _ENGINE = RapidOCR()
    return _ENGINE


def _to_bbox(points: Any) -> list[int]:
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    return [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]


def _run_ocr(image: Any) -> dict[str, Any]:
    if not _deps()["rapidocr"]:
        raise RuntimeError(
            "本地 OCR 不可用：缺少 rapidocr-onnxruntime。"
            "请在扩展目录内执行 pip install rapidocr-onnxruntime 后重启本扩展。"
        )
    result, _elapse = _engine()(image)
    segments = []
    for item in result or []:
        points, text, score = item[0], item[1], item[2]
        text = str(text or "").strip()
        if not text:
            continue
        segments.append(
            {"text": text, "bbox": _to_bbox(points), "score": round(float(score), 4)}
        )
    segments.sort(key=lambda s: (s["bbox"][1] // 12, s["bbox"][0]))
    return {"segments": segments, "width": _width, "height": _height}


_width = 0
_height = 0


def _load_image(params: dict[str, Any]):
    global _width, _height
    from PIL import Image

    if params.get("image_b64"):
        raw = base64.b64decode(str(params["image_b64"]))
        image = Image.open(io.BytesIO(raw))
    else:
        path = str(params.get("image") or "")
        if not path or not os.path.isfile(path):
            raise RuntimeError(f"找不到图片：{path}")
        image = Image.open(path)
    image = image.convert("RGB")
    _width, _height = image.size
    return image


def dispatch(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "ping":
        return {"version": VERSION, "deps": _deps(), "platform": sys.platform}
    if method == "ocr":
        return _run_ocr(_load_image(params))
    if method == "ocr_b64":
        data = dict(params)
        return _run_ocr(_load_image(data))
    raise ValueError(f"未知方法：{method}")


def main() -> int:
    log.write(f"ext-ocr-overlay {VERSION} ready (deps={_deps()})\n")
    log.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            log.write(f"忽略非 JSON 输入：{line[:120]}\n")
            log.flush()
            continue
        req_id = request.get("id")
        method = str(request.get("method") or "")
        params = request.get("params") or {}
        if method == "exit":
            _reply(req_id, True, {"bye": True})
            return 0
        try:
            result = dispatch(method, params)
            _reply(req_id, True, result)
        except Exception as exc:  # 任何异常都必须变成一条 JSON，而不是让宿主等超时
            log.write(f"处理 {method} 失败：{exc}\n{traceback.format_exc()}\n")
            log.flush()
            _reply(req_id, False, str(exc))
    return 0


def _reply(req_id: Any, ok: bool, payload: Any) -> None:
    message = {"id": req_id, "ok": ok}
    if ok:
        message["result"] = payload
    else:
        message["error"] = str(payload)
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
