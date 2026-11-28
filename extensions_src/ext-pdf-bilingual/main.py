"""ext-pdf-bilingual —— PDF 版面提取与双语 PDF 合成（Sidecar）。

方法：
    ping                                  -> {"version":..., "deps": {...}}
    extract {"path": "<pdf>"}             -> {"pages":[{"index","width","height",
                                                       "blocks":[{"id","bbox","text"}]}]}
    render  {"path","output","translations": {"0:3": "译文"}, "mode": "bilingual"|"mono"}
                                          -> {"output": "<path>", "pages": n}
    exit

依赖：PyMuPDF（fitz，约 35–50 MB）。
翻译不在本扩展内做：宿主把 blocks 交给 TranslationPipeline / jobs.db 队列，
再把译文按 "<page>:<block>" 传回来渲染——这样术语表、缓存、限流、降级全部复用。
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

VERSION = "1.0.0"
log = sys.stderr

MIN_BLOCK_CHARS = 1
SKIP_KEYWORDS = ("http://", "https://", "www.")


def _deps() -> dict[str, Any]:
    status: dict[str, Any] = {"fitz": False}
    try:
        import fitz  # noqa: F401

        status["fitz"] = True
    except Exception:
        pass
    return status


def _require_fitz():
    if not _deps()["fitz"]:
        raise RuntimeError(
            "PDF 双语扩展不可用：缺少 PyMuPDF。"
            "请在扩展目录内执行 pip install pymupdf 后重启本扩展。"
        )
    import fitz

    return fitz


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


def _wanted(text: str) -> bool:
    text = text.strip()
    if len(text) < MIN_BLOCK_CHARS:
        return False
    low = text.lower()
    if any(low.startswith(word) for word in SKIP_KEYWORDS):
        return False
    # 纯数字 / 页码 / 公式符号行没有翻译价值
    if not any(ch.isalpha() for ch in text):
        return False
    return True


def extract(params: dict[str, Any]) -> dict[str, Any]:
    fitz = _require_fitz()
    path = str(params.get("path") or "")
    if not os.path.isfile(path):
        raise RuntimeError(f"找不到 PDF：{path}")
    max_pages = int(params.get("max_pages") or 0)
    doc = fitz.open(path)
    pages = []
    for page_index in range(doc.page_count):
        if max_pages and page_index >= max_pages:
            break
        page = doc[page_index]
        blocks = []
        for block in page.get_text("blocks"):
            x0, y0, x1, y1, text = block[0], block[1], block[2], block[3], block[4]
            text = _clean(text)
            if not _wanted(text):
                continue
            blocks.append(
                {
                    "id": f"{page_index}:{len(blocks)}",
                    "bbox": [round(float(x0), 1), round(float(y0), 1),
                             round(float(x1), 1), round(float(y1), 1)],
                    "text": text,
                }
            )
        pages.append(
            {
                "index": page_index,
                "width": round(float(page.rect.width), 1),
                "height": round(float(page.rect.height), 1),
                "blocks": blocks,
            }
        )
    doc.close()
    return {"path": path, "pages": pages}


def render(params: dict[str, Any]) -> dict[str, Any]:
    fitz = _require_fitz()
    source = str(params.get("path") or "")
    output = str(params.get("output") or "")
    if not os.path.isfile(source):
        raise RuntimeError(f"找不到 PDF：{source}")
    if not output:
        raise RuntimeError("缺少输出路径。")
    translations: dict[str, str] = {
        str(k): str(v) for k, v in (params.get("translations") or {}).items()
    }
    mode = str(params.get("mode") or "bilingual")
    font_path = str(params.get("font") or "")
    widths = params.get("widths") or {}

    doc = fitz.open(source)
    page_count = doc.page_count

    if mode == "mono":
        # 原位重排单语版：盖白底，按框重新排中文，字号自动缩到放得下
        for page_index in range(page_count):
            page = doc[page_index]
            page_width = page.rect.width
            for key, text in translations.items():
                if not key.startswith(f"{page_index}:"):
                    continue
                rect = _rect_for(widths.get(key))
                if rect is None:
                    continue
                shape = page.new_shape()
                shape.draw_rect(rect)
                shape.finish(color=None, fill=(1, 1, 1))
                shape.commit()
                size = _fit_size(fitz, text, rect.width, rect.height, start=11.0, font=font_path)
                page.insert_textbox(
                    rect,
                    text,
                    fontsize=size,
                    fontname=_font_name(page, font_path),
                    color=(0, 0, 0),
                    align=0,
                )
        doc.save(output, garbage=3, deflate=True)
        doc.close()
        return {"output": output, "mode": mode}

    # 双语对照：新文档里并排两页（左原文、右译文），复杂表格不会错位
    target = fitz.open()
    for page_index in range(page_count):
        source_page = doc[page_index]
        width, height = source_page.rect.width, source_page.rect.height
        left = target.new_page(width=width, height=height)
        left.show_pdf_page(left.rect, doc, page_index)
        right = target.new_page(width=width, height=height)
        right.show_pdf_page(right.rect, doc, page_index)
        # 右页用半透明白盖住原文，再把译文按同一坐标贴上去
        overlay = right.new_shape()
        overlay.draw_rect(right.rect)
        overlay.finish(color=None, fill=(1, 1, 1))
        overlay.commit()
        for key, text in translations.items():
            if not key.startswith(f"{page_index}:"):
                continue
            rect = _rect_for(widths.get(key))
            if rect is None:
                continue
            size = _fit_size(fitz, text, rect.width, rect.height, start=10.0, font=font_path)
            right.insert_textbox(
                rect,
                text,
                fontsize=size,
                fontname=_font_name(right, font_path),
                color=(0, 0, 0),
                align=0,
            )
    target.save(output, garbage=3, deflate=True)
    target.close()
    doc.close()
    return {"output": output, "mode": "bilingual", "pages": page_count * 2}


def _rect_for(raw: Any):
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    import fitz

    return fitz.Rect(float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3]))


def _font_name(page: Any, font_path: str) -> str:
    if not font_path or not os.path.isfile(font_path):
        # 没有中文字体时退回内置字体（中文会变方框，诚实告知由宿主提示）
        return "helv"
    try:
        page.insert_font(fontname="zhfont", fontfile=font_path)
        return "zhfont"
    except Exception:
        return "helv"


def _fit_size(fitz: Any, text: str, width: float, height: float, *, start: float, font: str) -> float:
    """从 start 往下试，直到文字放得进 bbox。"""
    font_name = "zhfont" if font and os.path.isfile(font) else "helv"
    size = max(4.0, start)
    while size > 4.0:
        measure = fitz.get_text_length(text, fontname=font_name, fontsize=size)
        lines = max(1, int(measure / max(width, 1.0)) + 1)
        if lines * size * 1.35 <= height:
            return round(size, 1)
        size -= 0.5
    return 4.0


def dispatch(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "ping":
        return {"version": VERSION, "deps": _deps(), "platform": sys.platform}
    if method in {"extract", "pdf_extract"}:
        return extract(params)
    if method in {"render", "pdf_render"}:
        return render(params)
    raise ValueError(f"未知方法：{method}")


def main() -> int:
    log.write(f"ext-pdf-bilingual {VERSION} ready (deps={_deps()})\n")
    log.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        req_id = request.get("id")
        method = str(request.get("method") or "")
        params = request.get("params") or {}
        if method == "exit":
            _reply(req_id, True, {"bye": True})
            return 0
        try:
            _reply(req_id, True, dispatch(method, params))
        except Exception as exc:
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
