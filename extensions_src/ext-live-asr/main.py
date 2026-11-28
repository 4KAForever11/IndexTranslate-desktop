"""ext-live-asr —— Windows 系统音频回环 + 实时转写（Sidecar）。

流程：WASAPI 回环抓「扬声器正在放什么」→ 能量 VAD 断句 → sherpa-onnx 离线识别
（SenseVoice / Whisper ONNX）→ 把句子按行吐给宿主 → 宿主的 TranslationPipeline 翻译 →
`live_subtitle_bar.py` 渲染置顶字幕。

方法：
    ping                                 -> {"version":..., "deps": {...}}
    start {"model": "<dir>", "language": "auto", "threshold": 0.012}
          -> {"started": true}           此后不断推送事件行：
             {"id": n, "event": "partial", "data": {"text": "..."}}
             {"id": n, "event": "final",   "data": {"text": "...", "start": 1.23, "end": 4.56}}
    stop                                 -> {"sentences": n}
    exit

依赖：PyAudioWPatch（WASAPI 回环）+ sherpa-onnx（纯 ONNX，CPU 可跑）。
不引 PyTorch / openai-whisper —— 那会把包撑到 2 GB。
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import traceback
import wave
from typing import Any

VERSION = "1.0.0"
TARGET_RATE = 16000
log = sys.stderr

_state = {
    "thread": None,
    "stop": threading.Event(),
    "req_id": None,
    "sentences": 0,
    "error": "",
}


def _deps() -> dict[str, Any]:
    status: dict[str, Any] = {"pyaudiowpatch": False, "sherpa_onnx": False, "numpy": False}
    try:
        import pyaudiowpatch  # noqa: F401

        status["pyaudiowpatch"] = True
    except Exception:
        pass
    try:
        import sherpa_onnx  # noqa: F401

        status["sherpa_onnx"] = True
    except Exception:
        pass
    try:
        import numpy  # noqa: F401

        status["numpy"] = True
    except Exception:
        pass
    return status


def _emit(event: str, data: dict[str, Any]) -> None:
    req_id = _state["req_id"]
    if req_id is None:
        return
    sys.stdout.write(
        json.dumps({"id": req_id, "event": event, "data": data}, ensure_ascii=False) + "\n"
    )
    sys.stdout.flush()


def _loopback_device(pa) -> dict[str, Any]:
    wasapi = pa.get_host_api_info_by_type(__import__("pyaudiowpatch").paWASAPI)
    default_speakers = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
    if not default_speakers.get("isLoopbackDevice"):
        for loopback in pa.get_loopback_device_info_generator():
            if default_speakers["name"] in loopback["name"]:
                return loopback
        raise RuntimeError("没有找到可用的 WASAPI 回环设备。请确认系统有正在播放的扬声器。")
    return default_speakers


def _resample_16k(samples, rate: int):
    """把任意采样率的单声道 float32 降到 16 kHz（简单抽点 + 均值，够 VAD/识别用）。"""
    import numpy as np

    if rate == TARGET_RATE:
        return samples.astype("float32")
    if rate < TARGET_RATE:
        return samples.astype("float32")
    step = rate / TARGET_RATE
    count = int(len(samples) / step)
    if count <= 0:
        return np.zeros(0, dtype="float32")
    idx = (np.arange(count) * step).astype("int64")
    return samples[idx].astype("float32")


def _capture(params: dict[str, Any]) -> None:
    import numpy as np
    import pyaudiowpatch as pyaudio

    model_dir = str(params.get("model") or "")
    language = str(params.get("language") or "auto")
    threshold = float(params.get("threshold") or 0.012)
    silence_sec = float(params.get("silence_sec") or 0.55)
    min_speech_sec = float(params.get("min_speech_sec") or 0.35)

    recognizer = None
    if os.path.isdir(model_dir):
        try:
            import sherpa_onnx

            model = os.path.join(model_dir, "model.onnx")
            tokens = os.path.join(model_dir, "tokens.txt")
            if not os.path.isfile(model) or not os.path.isfile(tokens):
                raise RuntimeError("模型目录里需要 model.onnx 与 tokens.txt")
            recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=model, tokens=tokens, num_threads=2, use_itn=True,
                language=language if language != "auto" else "",
            )
        except Exception as exc:
            _state["error"] = f"识别模型加载失败：{exc}"
            _emit("error", {"message": _state["error"]})
            return
    else:
        _state["error"] = (
            "未配置识别模型目录（设置 → 扩展中心 → 实时字幕 → 选择模型目录）。"
        )
        _emit("error", {"message": _state["error"]})
        return

    pa = pyaudio.PyAudio()
    stream = None
    try:
        device = _loopback_device(pa)
        rate = int(device["defaultSampleRate"])
        channels = int(device["maxInputChannels"])
        stream = pa.open(
            format=pyaudio.paFloat32,
            channels=channels,
            rate=rate,
            frames_per_buffer=1024,
            input=True,
            input_device_index=int(device["index"]),
        )
        log.write(f"loopback: {device['name']} {rate}Hz x{channels}\n")
        log.flush()

        buffer: list[Any] = []
        speaking = False
        silence_frames = 0
        speech_frames = 0
        started_at = time.monotonic()
        segment_start = 0.0
        last_partial = 0.0
        frame_sec = 1024 / rate

        while not _state["stop"].is_set():
            raw = stream.read(1024, exception_on_overflow=False)
            chunk = np.frombuffer(raw, dtype=np.float32)
            if channels > 1:
                chunk = chunk.reshape(-1, channels).mean(axis=1)
            chunk = _resample_16k(chunk, rate)
            rms = float(np.sqrt(np.mean(chunk ** 2)) + 1e-9)

            if rms >= threshold:
                if not speaking:
                    speaking = True
                    segment_start = time.monotonic() - started_at
                speech_frames += 1
                silence_frames = 0
                buffer.append(chunk)
            elif speaking:
                silence_frames += 1
                buffer.append(chunk)
                if silence_frames * frame_sec >= silence_sec:
                    _finish(buffer, recognizer, segment_start, time.monotonic() - started_at)
                    buffer.clear()
                    speaking = False
                    speech_frames = 0
                    silence_frames = 0
                    continue

            now = time.monotonic()
            if speaking and speech_frames * frame_sec >= min_speech_sec and now - last_partial > 0.9:
                last_partial = now
                text = _decode(buffer, recognizer)
                if text:
                    _emit("partial", {"text": text})

        if buffer and speaking:
            _finish(buffer, recognizer, segment_start, time.monotonic() - started_at)
    finally:
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception:
            pass
        pa.terminate()


def _decode(frames: list[Any], recognizer) -> str:
    import numpy as np

    if not frames:
        return ""
    audio = np.concatenate(frames).astype("float32")
    if audio.size < TARGET_RATE // 4:
        return ""
    try:
        result = recognizer.create_stream()
        result.accept_waveform(TARGET_RATE, audio)
        recognizer.decode_stream(result)
        return str(result.result.text or "").strip()
    except Exception as exc:
        log.write(f"识别失败：{exc}\n")
        log.flush()
        return ""


def _finish(frames: list[Any], recognizer, start: float, end: float) -> None:
    text = _decode(frames, recognizer)
    if not text:
        return
    _state["sentences"] = int(_state["sentences"]) + 1
    _emit("final", {"text": text, "start": round(start, 2), "end": round(end, 2)})


def start(params: dict[str, Any], req_id: Any) -> dict[str, Any]:
    if not _deps()["pyaudiowpatch"] or not _deps()["sherpa_onnx"]:
        raise RuntimeError(
            "实时字幕不可用：缺少 PyAudioWPatch 或 sherpa-onnx。"
            "请在扩展目录内执行 pip install pyaudiowpatch sherpa-onnx 后重启本扩展。"
        )
    if _state["thread"] and _state["thread"].is_alive():
        return {"started": True, "already": True}
    _state["stop"].clear()
    _state["req_id"] = req_id
    _state["sentences"] = 0
    _state["error"] = ""
    thread = threading.Thread(target=_guard_capture, args=(params,), daemon=True)
    _state["thread"] = thread
    thread.start()
    return {"started": True}


def _guard_capture(params: dict[str, Any]) -> None:
    try:
        _capture(params)
    except Exception as exc:
        _state["error"] = f"实时字幕中断：{exc}"
        log.write(f"{exc}\n{traceback.format_exc()}\n")
        log.flush()
        _emit("error", {"message": _state["error"]})


def stop() -> dict[str, Any]:
    _state["stop"].set()
    thread = _state["thread"]
    if thread and thread.is_alive():
        thread.join(timeout=4.0)
    _state["thread"] = None
    _state["req_id"] = None
    return {"sentences": int(_state["sentences"]), "error": _state["error"]}


def dispatch(method: str, params: dict[str, Any], req_id: Any) -> dict[str, Any]:
    if method == "ping":
        return {"version": VERSION, "deps": _deps(), "platform": sys.platform}
    if method == "start":
        return start(params, req_id)
    if method == "stop":
        return stop()
    raise ValueError(f"未知方法：{method}")


def main() -> int:
    log.write(f"ext-live-asr {VERSION} ready (deps={_deps()})\n")
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
            stop()
            _reply(req_id, True, {"bye": True})
            return 0
        try:
            _reply(req_id, True, dispatch(method, params, req_id))
        except Exception as exc:
            log.write(f"处理 {method} 失败：{exc}\n{traceback.format_exc()}\n")
            log.flush()
            _reply(req_id, False, str(exc))
    stop()
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
