"""ext-live-asr —— Windows 系统音频回环 + 实时转写（Sidecar）。

流程：WASAPI 回环抓「扬声器正在放什么」→ 能量 VAD 断句 → sherpa-onnx 离线识别
（SenseVoice / Whisper ONNX）→ 把句子按行吐给宿主 → 宿主的 TranslationPipeline 翻译 →
`live_subtitle_bar.py` 渲染置顶字幕。

方法：
    ping                                 -> {"version":..., "deps": {...}}
    devices                              -> {"devices": [{index, name, default, rate, channels}]}
    test {"device": 3, "seconds": 3}     -> {"frames": n, "peak": x, "rms": y}
    start {"model": "<dir>", "device": 3, "source": "en", "threshold": 0.012}
          -> {"device": {...}, "model": "model.int8.onnx"}   此后不断推送事件行：
             {"id": n, "event": "level",   "data": {"rms": 0.01}}
             {"id": n, "event": "warning", "data": {"message": "..."}}
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
import re
import sys
import threading
import time
import traceback
from typing import Any

VERSION = "1.2.2"
TARGET_RATE = 16000
FRAME = 1024
# 一句话最长 6 秒：超过就强制切句，连续讲话也能持续出字幕
MAX_SEGMENT_SEC = 6.0
MODEL_FILES = ("model.int8.onnx", "model.onnx")
log = sys.stderr

# 宿主按 UTF-8 读我们的 stdout：这里先把标准流锁成 UTF-8，
# 否则中文设备名会以 GBK 字节写出，宿主解出来就是一串「◆」。
for _stream in (sys.stdout, sys.stdin):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 - 老解释器/被重定向过就算了
        pass

_state = {
    "thread": None,
    "stop": threading.Event(),
    "req_id": None,
    "sentences": 0,
    "error": "",
    "device": "",
}


def _deps() -> dict[str, Any]:
    status: dict[str, Any] = {"pyaudiowpatch": False, "sherpa_onnx": False, "numpy": False}
    for name, module in (
        ("pyaudiowpatch", "pyaudiowpatch"),
        ("sherpa_onnx", "sherpa_onnx"),
        ("numpy", "numpy"),
    ):
        try:
            __import__(module)
            status[name] = True
        except Exception:  # noqa: BLE001 - 缺依赖是正常状态
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


# ---------------------------------------------------------------- 设备
def _registry_friendly_names() -> list[str]:
    """从注册表读所有「播放设备」的友好名（带中文，例如「耳机 (HyperX ...)」）。

    PortAudio 给的名字在部分驱动上会被 C 层按 UTF-8 硬解成 U+FFFD，
    中文就彻底丢了；注册表里存的是原始 UTF-16 名字，拿它来对照补回。
    """
    try:
        import winreg
    except Exception:  # noqa: BLE001 - 非 Windows 直接跳过
        return []
    root = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render"
    prop = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
    names: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root) as key:
            index = 0
            while True:
                try:
                    guid = winreg.EnumKey(key, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{root}\\{guid}\\Properties") as props:
                        value, _ = winreg.QueryValueEx(props, prop)
                except OSError:
                    continue
                text = str(value or "").strip()
                if text and text not in names:
                    names.append(text)
    except OSError:
        return []
    return names


def _repair_from_registry(name: str, pool: list[str]) -> str:
    """用注册表里的名字补回被解坏的部分（按括号里的型号名匹配）。"""
    if not pool or "\ufffd" not in name:
        return name
    match = re.search(r"\(([^()]*)\)", name)
    if not match:
        return name
    needle = match.group(1).strip().lower()
    if not needle:
        return name
    for candidate in pool:
        if needle in candidate.lower():
            return candidate + (" [Loopback]" if "loopback" in name.lower() else "")
    return name


def _clean_name(raw: Any) -> str:
    """把 PortAudio 给的设备名还原成干净文本。

    各家声卡驱动给出的字节编码并不统一（UTF-8 / GBK / 系统 ANSI 都有），
    pyaudiowpatch 的 C 层有时直接按 UTF-8 replace 解，中文就成了一串 U+FFFD，
    在界面上显示为「◆◆◆◆◆」。能还原就还原，还原不了至少把坏字符去掉，
    留下还能认出来的英文部分（例如 "(HyperX ...) [Loopback]"）。
    """
    text = ""
    if isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
        for encoding in ("utf-8", "gbk", "cp936", "mbcs"):
            try:
                text = data.decode(encoding)
                break
            except (UnicodeDecodeError, LookupError):
                text = ""
    else:
        text = str(raw or "")
    text = text.replace("\x00", "")
    if "\ufffd" in text:
        text = re.sub(r"\ufffd+", "", text)
    return " ".join(text.split()).strip()


def _loopback_devices(pa) -> list[dict[str, Any]]:
    """列出所有 WASAPI 回环设备（就是「系统正在播放」的那条输出）。"""
    import pyaudiowpatch as pyaudio

    wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    try:
        default_out = _clean_name(pa.get_device_info_by_index(int(wasapi["defaultOutputDevice"]))["name"])
    except Exception:  # noqa: BLE001
        default_out = ""
    devices: list[dict[str, Any]] = []
    pool = _registry_friendly_names()
    for info in pa.get_loopback_device_info_generator():
        index = int(info["index"])
        raw_name = info["name"]
        text = raw_name if isinstance(raw_name, str) else _clean_name(raw_name)
        text = _repair_from_registry(text, pool)
        name = _clean_name(text) or f"回环设备 #{index}"
        devices.append(
            {
                "index": index,
                "name": name,
                "default": bool(default_out) and default_out in name,
                "rate": int(info["defaultSampleRate"]),
                "channels": int(info["maxInputChannels"]),
            }
        )
    return devices


def _pick_device(pa, wanted: Any) -> dict[str, Any]:
    """按 index / 名字片段挑回环设备，挑不到就用默认输出的那条。"""
    devices = _loopback_devices(pa)
    if not devices:
        raise RuntimeError(
            "没有找到 WASAPI 回环设备。请确认系统有正在工作的扬声器（不要把输出设备设为『无』）。"
        )
    if wanted not in (None, "", -1, "auto", "default"):
        if isinstance(wanted, int) or (isinstance(wanted, str) and wanted.isdigit()):
            index = int(wanted)
            for info in devices:
                if info["index"] == index:
                    return info
        else:
            needle = str(wanted).lower()
            for info in devices:
                if needle in info["name"].lower():
                    return info
    for info in devices:
        if info["default"]:
            return info
    return devices[0]


# ---------------------------------------------------------------- 模型
def _model_files(model_dir: str) -> tuple[str, str]:
    model = ""
    for name in MODEL_FILES:
        candidate = os.path.join(model_dir, name)
        if os.path.isfile(candidate):
            model = candidate
            break
    tokens = os.path.join(model_dir, "tokens.txt")
    if not model or not os.path.isfile(tokens):
        raise RuntimeError(
            "模型目录里需要 model.int8.onnx（或 model.onnx）与 tokens.txt："
            f"{model_dir}"
        )
    return model, tokens


def _load_recognizer(model_dir: str, source: str):
    import sherpa_onnx

    model, tokens = _model_files(model_dir)
    language = "" if source in ("", "auto") else source.split("_")[0]
    log.write(f"model={os.path.basename(model)} language={language or 'auto'}\n")
    log.flush()
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=model,
        tokens=tokens,
        num_threads=2,
        use_itn=True,
        language=language,
    )


def _resample_16k(samples, rate: int):
    """把任意采样率的单声道 float32 降到 16 kHz（抽点，够 VAD/识别用）。"""
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


# ---------------------------------------------------------------- 抓音
def _open_stream(pa, device: dict[str, Any], on_audio):
    """回调式打开回环流：空闲设备不会卡死 read()。"""
    import pyaudiowpatch as pyaudio

    wanted_channels = int(device.get("channels") or 2)

    def callback(in_data, frame_count, time_info, status):  # noqa: ANN001, ARG001
        on_audio(in_data)
        return (None, pyaudio.paContinue)

    stream = pa.open(
        format=pyaudio.paFloat32,
        channels=wanted_channels,
        rate=int(device["rate"]),
        frames_per_buffer=FRAME,
        input=True,
        input_device_index=int(device["index"]),
        stream_callback=callback,
    )
    stream.start_stream()
    return stream


def _drain_peak(frames: "queue.Queue[bytes]", seconds: float) -> float:
    """收一小段并丢掉，返回这段的峰值（只用于判断端点是不是在送数字静音）。"""
    import numpy as np

    end = time.monotonic() + max(0.0, seconds)
    peak = 0.0
    while time.monotonic() < end:
        try:
            raw = frames.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
            data = np.frombuffer(raw, dtype=np.float32)
        except Exception:  # noqa: BLE001
            continue
        if data.size:
            peak = max(peak, float(np.abs(data).max()))
    return peak


def _capture(params: dict[str, Any]) -> None:
    import numpy as np
    import pyaudiowpatch as pyaudio

    model_dir = str(params.get("model") or "")
    source = str(params.get("source") or params.get("language") or "auto")
    threshold = float(params.get("threshold") or 0.012)
    silence_sec = float(params.get("silence_sec") or 0.55)
    min_speech_sec = float(params.get("min_speech_sec") or 0.35)
    # 一直讲话 / 背景有音乐时 VAD 等不到静音，永远不吐 final——那样宿主就只能干等。
    # 到了上限就强制切句，长段照样能出译文。
    max_segment_sec = float(params.get("max_segment_sec") or MAX_SEGMENT_SEC)

    if not os.path.isdir(model_dir):
        raise RuntimeError(
            "还没有下载语音识别模型：请到「扩展中心 → 系统音频实时字幕 → 识别模型」下载（约 228 MB）。"
        )
    recognizer = _load_recognizer(model_dir, source)

    pa = pyaudio.PyAudio()
    stream = None
    try:
        device = _pick_device(pa, params.get("device"))
        rate = int(device["rate"])
        channels = int(device.get("channels") or 2)
        _state["device"] = str(device["name"])
        log.write(f"loopback: {device['name']} {rate}Hz x{channels}\n")
        log.flush()

        frames: queue.Queue[bytes] = queue.Queue(maxsize=96)

        def on_audio(raw: bytes) -> None:
            try:
                frames.put_nowait(raw)
            except queue.Full:
                try:
                    frames.get_nowait()
                except queue.Empty:
                    pass
                try:
                    frames.put_nowait(raw)
                except queue.Full:
                    pass

        stream = _open_stream(pa, device, on_audio)
        # WASAPI 回环有个坑：端点当时没声音的话，流会一直「送数字静音」，
        # 等用户真的开始播放也不会自己活过来（参考 KorenKrita/simul-subtitle 的 sysaudio：
        # 它是原生实现所以能在静音期写零样本保持实时）。这里改成：
        # 只要连续一段时间全是数字静音，就把流重开一次，直到真的拿到声音为止。
        _emit("ready", {"device": str(device["name"]), "rate": rate, "channels": channels})

        buffer: list[Any] = []
        speaking = False
        silence_frames = 0
        speech_frames = 0
        started_at = time.monotonic()
        segment_start = 0.0
        last_partial = 0.0
        last_level = 0.0
        last_audio = time.monotonic()
        warned_idle = False
        stuck_frames = 0
        reopened_idle = 0
        frame_sec = FRAME / rate
        emitted = {"device": True}

        while not _state["stop"].is_set():
            try:
                raw = frames.get(timeout=0.25)
            except queue.Empty:
                idle = time.monotonic() - last_audio
                # 端点没激活时 PortAudio 的回调干脆一次都不来（连静音都不给），
                # 所以「一帧都没收到」也要重开流，不能只等全 0 的帧。
                if idle > 2.0 and reopened_idle < 20 and not speaking:
                    reopened_idle += 1
                    last_audio = time.monotonic()
                    log.write(f"no frames for {idle:.1f}s, reopen #{reopened_idle}\n")
                    log.flush()
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:  # noqa: BLE001
                        pass
                    # 关键：在空闲端点上打开的回环流会「假活」——句柄没问题，
                    # 但回调一次都不来，而且同一个 PyAudio 实例再开流也还是死的。
                    # 所以每次重连都把 PortAudio 整个重来一遍（相当于 sysaudio 重新 Initialize）。
                    try:
                        pa.terminate()
                    except Exception:  # noqa: BLE001
                        pass
                    pa = pyaudio.PyAudio()
                    stream = _open_stream(pa, device, on_audio)
                    if reopened_idle == 1:
                        _emit(
                            "notice",
                            {
                                "message": (
                                    "音频端点还没激活，正在重新连接；"
                                    "开始播放后几秒内就会出字幕。"
                                )
                            },
                        )
                    continue
                if idle > 2.5 and not warned_idle:
                    warned_idle = True
                    _emit(
                        "warning",
                        {
                            "message": (
                                f"「{device['name']}」上收不到音频。"
                                "请确认视频的声音是从这条输出设备播放的，"
                                "或在扩展中心里换一条回环设备。"
                            )
                        },
                    )
                continue
            last_audio = time.monotonic()
            warned_idle = False

            chunk = np.frombuffer(raw, dtype=np.float32)
            if chunk.size % channels == 0 and channels > 1:
                chunk = chunk.reshape(-1, channels).mean(axis=1)
            chunk = _resample_16k(chunk, rate)
            if chunk.size == 0:
                continue
            rms = float(np.sqrt(np.mean(chunk**2)) + 1e-9)
            peak = float(np.abs(chunk).max())

            # 端点「送数据但全是数字静音」= 回环流卡在未激活状态（最常见：用户先开字幕、
            # 后播放视频）。连续 3.5 秒全 0 就重开一次流，最多重开 20 次（≈70 秒），
            # 一旦拿到真实声音就再也不重开。
            if peak <= 0.0:
                stuck_frames += 1
                if (
                    stuck_frames * frame_sec >= 3.5
                    and reopened_idle < 20
                    and not speaking
                ):
                    reopened_idle += 1
                    stuck_frames = 0
                    log.write(f"stream silent, reopen #{reopened_idle}\n")
                    log.flush()
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:  # noqa: BLE001
                        pass
                    while True:
                        try:
                            frames.get_nowait()
                        except queue.Empty:
                            break
                    try:
                        pa.terminate()
                    except Exception:  # noqa: BLE001
                        pass
                    pa = pyaudio.PyAudio()
                    stream = _open_stream(pa, device, on_audio)
                    if reopened_idle == 1:
                        _emit(
                            "notice",
                            {
                                "message": (
                                    "音频端点还没激活，正在重新连接；"
                                    "开始播放后几秒内就会出字幕。"
                                )
                            },
                        )
                    if reopened_idle == 20:
                        _emit(
                            "warning",
                            {
                                "message": (
                                    f"「{device['name']}」一直没有声音。"
                                    "请确认视频是从这条输出设备播放的，或在扩展中心换一条回环设备。"
                                )
                            },
                        )
            else:
                stuck_frames = 0

            if rms >= threshold:
                if not speaking:
                    speaking = True
                    segment_start = time.monotonic() - started_at
                    if not emitted.get("speech"):
                        emitted["speech"] = True
                        _emit("warning", {"message": f"检测到声音（{device['name']}），开始识别。"})
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
            if now - last_level >= 0.5:
                last_level = now
                _emit("level", {"rms": round(rms, 5), "device": device["name"]})
            if speaking and speech_frames * frame_sec >= min_speech_sec and now - last_partial > 0.9:
                last_partial = now
                text = _decode(buffer, recognizer)
                if text:
                    _emit("partial", {"text": text})

            if speaking and (now - started_at - segment_start) >= max_segment_sec:
                # 到了单句上限：先交这一句，再接着听下一句
                _finish(buffer, recognizer, segment_start, now - started_at)
                buffer.clear()
                speaking = False
                speech_frames = 0
                silence_frames = 0
                continue

        if buffer and speaking:
            _finish(buffer, recognizer, segment_start, time.monotonic() - started_at)
    finally:
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception:  # noqa: BLE001 - 退出路径不打扰
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
        stream = recognizer.create_stream()
        stream.accept_waveform(TARGET_RATE, audio)
        recognizer.decode_stream(stream)
        return str(stream.result.text or "").strip()
    except Exception as exc:  # noqa: BLE001
        log.write(f"识别失败：{exc}\n")
        log.flush()
        return ""


def _finish(frames: list[Any], recognizer, start: float, end: float) -> None:
    text = _decode(frames, recognizer)
    if not text:
        return
    _state["sentences"] = int(_state["sentences"]) + 1
    _emit("final", {"text": text, "start": round(start, 2), "end": round(end, 2)})


# ---------------------------------------------------------------- 方法
def devices() -> dict[str, Any]:
    import pyaudiowpatch as pyaudio

    pa = pyaudio.PyAudio()
    try:
        return {"devices": _loopback_devices(pa)}
    finally:
        pa.terminate()


def test(params: dict[str, Any]) -> dict[str, Any]:
    """录一小段，报告有没有声音 —— 排障时最有用的一条。"""
    import numpy as np
    import pyaudiowpatch as pyaudio

    seconds = float(params.get("seconds") or 3.0)
    pa = pyaudio.PyAudio()
    stream = None
    try:
        device = _pick_device(pa, params.get("device"))
        chunks: list[Any] = []

        def on_audio(raw: bytes) -> None:
            chunks.append(np.frombuffer(raw, dtype=np.float32))

        stream = _open_stream(pa, device, on_audio)
        time.sleep(seconds)
    finally:
        try:
            if stream is not None:
                stream.stop_stream()
                stream.close()
        except Exception:  # noqa: BLE001
            pass
        pa.terminate()

    total_frames = sum(int(c.size) for c in chunks)
    if chunks:
        audio = np.concatenate(chunks)
        peak = float(np.abs(audio).max())
        rms = float(np.sqrt(np.mean(audio.astype("float64") ** 2)))
        samples = int(audio.size)
    else:
        peak = rms = 0.0
        samples = 0
    return {
        "device": device["name"],
        "seconds": seconds,
        "buffers": len(chunks),
        "samples": samples,
        "peak": round(peak, 4),
        "rms": round(rms, 5),
        "silent": peak < 0.002,
    }


def start(params: dict[str, Any], req_id: Any) -> dict[str, Any]:
    deps = _deps()
    if not deps["pyaudiowpatch"] or not deps["sherpa_onnx"]:
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
    _state["device"] = ""
    thread = threading.Thread(target=_guard_capture, args=(params,), daemon=True)
    _state["thread"] = thread
    thread.start()
    return {"started": True}


def _guard_capture(params: dict[str, Any]) -> None:
    try:
        _capture(params)
    except Exception as exc:  # noqa: BLE001
        _state["error"] = str(exc) or exc.__class__.__name__
        log.write(f"{exc}\n{traceback.format_exc()}\n")
        log.flush()
        _emit("error", {"message": f"实时字幕中断：{_state['error']}"})


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
    if method == "devices":
        return devices()
    if method == "test":
        return test(params)
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
        except Exception as exc:  # noqa: BLE001
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
