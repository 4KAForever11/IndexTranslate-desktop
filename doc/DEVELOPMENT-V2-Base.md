往「大而全」版本演进，**第一步最不该做的是直接往界面塞划词弹窗或 PDF 翻译，而是先重构 `app/core/`，建立「统一翻译引擎抽象 + 管道调度中枢（Engine & Pipeline Core）」**。

原因很现实：你后面要做的划词浮窗、框选截屏、批量图片、PDF/SRT 队列、写作润色、多引擎对比以及升级版沉浸式翻译代理，本质上都是**不同的前端入口去调用同一个翻译内核**。如果现在不把「引擎调用、术语注入、限流排队、缓存、失败降级」从 `api_client.py` 和 `workers/` 里抽出来统一收口，后面每加一个大功能都会把这些逻辑复制粘贴一遍，项目会迅速失控。

做完这第一步后，不仅架构能平滑承接所有新功能，而且**当天就能收获 4 个用户立即可感知的升级**：本地代理不再轻易触发 429 限流、相同网页刷新毫秒级命中缓存、35B 挂掉自动降级 9B/2B 或备用接口、沉浸式翻译直接复用客户端术语表。



## 一、目标目录演进（最小侵入改造）

在尽量不动 `app/ui/` 和不破坏现有单元测试的前提下，对 `app/core/` 做如下增量拆分：

app/core/
├─ models.py              # [新增] 统一请求/响应/能力/异常数据结构
├─ engines/               # [新增] 多引擎插件层
│  ├─ __init__.py
│  ├─ base.py             # BaseEngine 抽象基类
│  ├─ index_translate.py  # 迁移原 api_client.py 核心请求与 SSE 逻辑
│  ├─ openai_compat.py    # 通用 OpenAI 兼容引擎（接 Ollama / DeepSeek 等）
│  └─ registry.py         # 引擎注册表与工厂
├─ rate_limiter.py        # [新增] 并发信号量 + 最小间隔限流器
├─ cache_db.py            # [新增] SQLite 译文缓存（沿用只读盘降级内存库策略）
├─ pipeline.py            # [新增] 统一调度管道（缓存 → 术语匹配 → 限流 → 降级执行）
├─ glossary_db.py         # [改造] 增加分组字段 + match_terms() 原文精准命中
├─ prompt_builder.py      # [保留] 严格维持与 call_api.py 逐字节对齐
├─ proxy_server.py        # [改造] 收口到 pipeline.py，支持限流/缓存/可选术语注入
└─ api_client.py          # [兼容层] 保留旧函数签名委托给新引擎，防止旧测试和旧调用崩坏



## 二、分步开发指南与参考代码

### 步骤 1：定义统一的数据协议（`app/core/models.py`）

把所有翻译场景（主窗口、代理、未来的划词/文档/润色）统一抽象为 `TranslateRequest` 和流式块 `TranslateChunk`。明确区分「可重试/可降级错误」和「不可恢复错误」（比如用户手动点击停止、图片损坏）。

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

SceneType = Literal["translate", "proxy", "popup", "document", "polish"]


class EngineError(Exception):
    """引擎基础异常"""


class RetryableEngineError(EngineError):
    """可触发自动降级/重试的异常（429 限流、5xx、连接超时）"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class EngineCapabilities:
    supports_stream: bool = True
    supports_image: bool = False
    supports_instruction: bool = True
    supports_glossary: bool = True


@dataclass
class TranslateRequest:
    text: str = ""
    image_b64: str | None = None
    source_lang: str = "auto"
    target_lang: str = "zh"
    instruction: str = ""
    engine_id: str = "index_translate"
    model: str = "Index-Translate-35B-A3B"
    glossary_group: str | None = "default"  # None 表示不启用术语表
    use_cache: bool = True
    stream: bool = True
    scene: SceneType = "translate"
    extra: dict = field(default_factory=dict)  # 预留给未来多模态/润色参数

@dataclass
class TranslateChunk:
    delta: str = ""
    done: bool = False
    engine_used: str = ""
    model_used: str = ""
    from_cache: bool = False
    matched_terms: list[tuple[str, str]] = field(default_factory=list



### 步骤 2：抽象引擎基类与注册表（`app/core/engines/`）

关键点：你的软件已经有「随时点停止并保留已生成译文」的特性，所以引擎接口必须原生接受 `cancel_event: threading.Event`，在读取 `httpx` SSE 流时每一帧都检查中断信号。

**1. 抽象基类（`app/core/engines/base.py`）**

from __future__ import annotations
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from app.core.models import EngineCapabilities, TranslateChunk, TranslateRequest


class BaseEngine(ABC):
    engine_id: str = ""
    display_name: str = ""
    capabilities: EngineCapabilities = EngineCapabilities()

    @abstractmethod
    def translate_stream(
        self,
        req: TranslateRequest,
        matched_terms: list[tuple[str, str]],
        cancel_event: threading.Event,
    ) -> Iterator[TranslateChunk]:
        """流式产出译文片段；遇到 429/5xx/超时必须抛出 RetryableEngineError"""





**2. 迁移默认主力引擎（`app/core/engines/index_translate.py`）**

把原来 `api_client.py` 里的 `httpx` 流式调用搬进来，**原样复用你现有的 `prompt_builder.py`**，保证与 `call_api.py` 逐字节对齐的单元测试 100% 通过：

from __future__ import annotations
import threading
from collections.abc import Iterator
import httpx
from app.core.engines.base import BaseEngine
from app.core.models import (
    EngineCapabilities,
    EngineError,
    RetryableEngineError,
    TranslateChunk,
    TranslateRequest,
)

# 复用你现有的提示词构造器与 SSE 解析工具
from app.core import prompt_builder


class IndexTranslateEngine(BaseEngine):
    engine_id = "index_translate"
    display_name = "Bilibili Index-Translate"
    capabilities = EngineCapabilities(
        supports_stream=True,
        supports_image=True,
        supports_instruction=True,
        supports_glossary=True,
    )

    def __init__(self, base_url: str, timeout: float = 45.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
    
    def translate_stream(
        self,
        req: TranslateRequest,
        matched_terms: list[tuple[str, str]],
        cancel_event: threading.Event,
    ) -> Iterator[TranslateChunk]:
        # 1. 调用你现有的 prompt_builder，注意传入的是命中后的 matched_terms
        payload = prompt_builder.build_payload(
            text=req.text,
            image_b64=req.image_b64,
            source_lang=req.source_lang,
            target_lang=req.target_lang,
            instruction=req.instruction,
            glossary=matched_terms,
            model=req.model,
            stream=req.stream,
        )
    
        url = f"{self.base_url}/chat/completions"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream("POST", url, json=payload) as resp:
                    if resp.status_code in (429, 500, 502, 503, 504):
                        raise RetryableEngineError(
                            f"HTTP {resp.status_code}", status_code=resp.status_code
                        )
                    if resp.status_code >= 400:
                        raise EngineError(f"HTTP {resp.status_code}: {resp.read().decode(errors='ignore')}")
    
                    for line in resp.iter_lines():
                        if cancel_event.is_set():
                            return
                        delta = self._parse_sse_delta(line)  # 复用原 SSE 解析逻辑
                        if delta:
                            yield TranslateChunk(
                                delta=delta,
                                engine_used=self.engine_id,
                                model_used=req.model,
                                matched_terms=matched_terms,
                            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise RetryableEngineError(f"网络异常: {exc}") from exc
    
        yield TranslateChunk(
            done=True,
            engine_used=self.engine_id,
            model_used=req.model,
            matched_terms=matched_terms,
        )
    
    @staticmethod
    def _parse_sse_delta(line: str) -> str:
        # 直接迁入你原 api_client.py 里的 SSE 解析函数
        ...





3. 引擎注册表（`app/core/engines/registry.py`）

from __future__ import annotations
from app.core.engines.base import BaseEngine


class EngineRegistry:
    def __init__(self) -> None:
        self._engines: dict[str, BaseEngine] = {}

    def register(self, engine: BaseEngine) -> None:
        self._engines[engine.engine_id] = engine
    
    def get(self, engine_id: str) -> BaseEngine:
        if engine_id not in self._engines:
            raise KeyError(f"未注册的翻译引擎: {engine_id}")
        return self._engines[engine_id]
    
    def list_engines(self) -> list[BaseEngine]:
        return list(self._engines.values())



**开发建议**：同步顺手写一个 50 行左右的 `openai_compat.py`（带 `Authorization: Bearer <api_key>` 和自定义 `base_url`）注册进去。只有真正接入 **2 个以上引擎**，你才能立刻验证 `BaseEngine` 接口有没有设计死角。



### 步骤 3：升级术语表为「分组 + 原文命中精准注入」（`app/core/glossary_db.py`）

过去全量注入术语表，词条一多就会撑大 Prompt、拖慢首字延迟，还容易污染无关句子。只需两步无痛升级：

1. **安全表结构迁移**（兼容老用户的 `glossary.db`，不丢数据）：

def _migrate_schema(conn) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(glossary)").fetchall()}
    if "group_name" not in cols:
        conn.execute("ALTER TABLE glossary ADD COLUMN group_name TEXT NOT NULL DEFAULT 'default'")
    if "enabled" not in cols:
        conn.execute("ALTER TABLE glossary ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
    conn.commit()



**按原文命中筛选词条**：对纯英文/拉丁词做大小写不敏感 + 词边界匹配（避免 `cat` 误命中 `category`），对中日韩文字做子串匹配，并按原词长度倒序排列（防止短词抢占长词）：

import re

def match_terms(
    self,
    text: str,
    group_name: str | None = "default",
    max_terms: int = 30,
) -> list[tuple[str, str]]:
    if not text or group_name is None:
        return []

    # 按分组读取已启用词条（若 group_name == "*" 则查全部分组）
    entries = self.list_enabled(group_name=group_name)
    # 长词优先，减少短词干扰
    entries.sort(key=lambda item: len(item[0]), reverse=True)
    
    matched: list[tuple[str, str]] = []
    for src, dst in entries:
        if not src:
            continue
        if src.isascii():
            pattern = rf"(?<![A-Za-z0-9_]){re.escape(src)}(?![A-Za-z0-9_])"
            hit = re.search(pattern, text, flags=re.IGNORECASE) is not None
        else:
            hit = src in text
    
        if hit:
            matched.append((src, dst))
            if len(matched) >= max_terms:
                break
    return matched



### 步骤 4：实现限流器、本地缓存与管道中枢

#### 4.1 并发与频率控制（`app/core/rate_limiter.py`）

专门用来保护免费公网接口，防止沉浸式翻译或未来批量文档任务瞬间打爆 429：

from __future__ import annotations
import threading
import time
from contextlib import contextmanager


class RateLimiter:
    """控制最大并发数 + 最小请求间隔（线程安全）"""

```python
def __init__(self, max_concurrency: int = 3, min_interval_sec: float = 0.25):
    self._sem = threading.BoundedSemaphore(max(1, max_concurrency))
    self._min_interval = max(0.0, min_interval_sec)
    self._lock = threading.Lock()
    self._last_ts = 0.0

@contextmanager
def acquire(self, cancel_event: threading.Event | None = None):
    # 轮询等待信号量，确保用户在排队期间点「停止」能立刻退出
    while not self._sem.acquire(timeout=0.1):
        if cancel_event and cancel_event.is_set():
            yield False
            return
    try:
        with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_ts = time.monotonic()
        yield True
    finally:
        self._sem.release()
```



#### 译文缓存库（`app/core/cache_db.py`）

用 `SHA-256` 对 `(text, source_lang, target_lang, engine_id, model, instruction, matched_terms)` 生成缓存键，**继承你项目里「目录不可写时自动降级 `:memory:` 数据库」的优良实践**：

from __future__ import annotations
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path


class TranslationCacheDB:
    def __init__(self, db_path: Path | None, ttl_sec: int = 7 * 86400, max_rows: int = 10000):
        self._lock = threading.Lock()
        self._ttl_sec = ttl_sec
        self._max_rows = max_rows
        self._conn = self._open_db(db_path)
        self._init_table()

    def _open_db(self, db_path: Path | None) -> sqlite3.Connection:
        if db_path is not None:
            try:
                db_path.parent.mkdir(parents=True, exist_ok=True)
                return sqlite3.connect(str(db_path), check_same_thread=False)
            except OSError:
                pass
        return sqlite3.connect(":memory:", check_same_thread=False)
    
    def _init_table(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS translation_cache (
                    cache_key TEXT PRIMARY KEY,
                    result_text TEXT NOT NULL,
                    engine_used TEXT NOT NULL,
                    model_used TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            self._conn.commit()
    
    @staticmethod
    def make_key(
        text: str,
        source_lang: str,
        target_lang: str,
        model: str,
        instruction: str,
        matched_terms: list[tuple[str, str]],
    ) -> str:
        raw = json.dumps(
            [text, source_lang, target_lang, model, instruction, matched_terms],
            ensure_ascii=False,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    
    def get(self, key: str) -> tuple[str, str, str] | None:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT result_text, engine_used, model_used, updated_at "
                "FROM translation_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
            if not row:
                return None
            if now - row[3] > self._ttl_sec:
                self._conn.execute("DELETE FROM translation_cache WHERE cache_key = ?", (key,))
                self._conn.commit()
                return None
            return row[0], row[1], row[2]
    
    def set(self, key: str, result_text: str, engine_used: str, model_used: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO translation_cache
                (cache_key, result_text, engine_used, model_used, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (key, result_text, engine_used, model_used, now),
            )
            # 轻量淘汰：防止缓存无限膨胀
            self._conn.execute(
                """
                DELETE FROM translation_cache WHERE cache_key IN (
                    SELECT cache_key FROM translation_cache
                    ORDER BY updated_at DESC LIMIT -1 OFFSET ?
                )
                """,
                (self._max_rows,),
            )
            self._conn.commit()



#### 统一管道中枢（`app/core/pipeline.py`）

这是整个重构最核心的文件。重点注意一个工程细节：**只有在「尚未吐出任何字符」之前遇到 429/5xx/超时，才自动切换到降级候选模型**；如果已经吐了一半文字才断线，绝不默默换模型拼接半截译文，而是如实抛错并保留已生成文本。

from __future__ import annotations
import copy
import threading
from collections.abc import Iterator
from app.core.cache_db import TranslationCacheDB
from app.core.engines.registry import EngineRegistry
from app.core.models import RetryableEngineError, TranslateChunk, TranslateRequest
from app.core.rate_limiter import RateLimiter


class TranslationPipeline:
    def __init__(
        self,
        registry: EngineRegistry,
        glossary_db,
        cache_db: TranslationCacheDB,
        rate_limiter: RateLimiter,
        fallback_chain: list[tuple[str, str]] | None = None,
    ):
        self.registry = registry
        self.glossary_db = glossary_db
        self.cache_db = cache_db
        self.rate_limiter = rate_limiter
        # 例如: [("index_translate", "Index-Translate-9B"), ("index_translate", "Index-Translate-2B")]
        self.fallback_chain = fallback_chain or []

    def execute_stream(
        self,
        req: TranslateRequest,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[TranslateChunk]:
        cancel = cancel_event or threading.Event()
    
        # 1. 按原文精准匹配术语（纯图片输入时暂无原文，可跳过）
        matched_terms: list[tuple[str, str]] = []
        if req.text and req.glossary_group is not None:
            matched_terms = self.glossary_db.match_terms(req.text, group_name=req.glossary_group)
    
        # 2. 纯文本请求先查缓存
        cache_key: str | None = None
        if req.use_cache and req.text and not req.image_b64:
            cache_key = self.cache_db.make_key(
                req.text, req.source_lang, req.target_lang,
                req.model, req.instruction, matched_terms,
            )
            cached = self.cache_db.get(cache_key)
            if cached:
                text, eng, mdl = cached
                yield TranslateChunk(
                    delta=text,
                    done=True,
                    engine_used=eng,
                    model_used=mdl,
                    from_cache=True,
                    matched_terms=matched_terms,
                )
                return
    
        # 3. 构造执行候选列表：首选引擎/模型 + 去重后的降级链
        candidates = [(req.engine_id, req.model)]
        for pair in self.fallback_chain:
            if pair not in candidates:
                candidates.append(pair)
    
        last_err: Exception | None = None
        for idx, (eng_id, mdl_name) in enumerate(candidates):
            if cancel.is_set():
                return
    
            engine = self.registry.get(eng_id)
            # 图片请求跳过不支持视觉的降级引擎
            if req.image_b64 and not engine.capabilities.supports_image:
                continue
    
            sub_req = copy.copy(req)
            sub_req.engine_id = eng_id
            sub_req.model = mdl_name
    
            emitted_parts: list[str] = []
            try:
                with self.rate_limiter.acquire(cancel) as acquired:
                    if not acquired:
                        return
                    for chunk in engine.translate_stream(sub_req, matched_terms, cancel):
                        if chunk.delta:
                            emitted_parts.append(chunk.delta)
                        yield chunk
                        if cancel.is_set():
                            return
    
                # 完整结束后写入缓存
                if cache_key and emitted_parts and not cancel.is_set():
                    self.cache_db.set(cache_key, "".join(emitted_parts), eng_id, mdl_name)
                return
    
            except RetryableEngineError as exc:
                last_err = exc
                # 只要尚未吐出内容且还有下一个备用模型，就静默降级
                if not emitted_parts and idx < len(candidates) - 1:
                    continue
                raise
    
        if last_err:
            raise last_err





### 步骤 5：收口双入口（`workers/` 与 `proxy_server.py`）

完成中枢后，把现有的两个入口统一接到 `TranslationPipeline` 上：

1. **改造 `app/workers/`（给主窗口供流）**：
   - Worker 不再直接 import `api_client`，而是持有全局单例 `pipeline` 和一个 `self._cancel_event = threading.Event()`。
   - 点击界面「停止」按钮时只调 `self._cancel_event.set()`，无需强杀线程。
   - 在收到 `chunk` 时发射 Qt 信号给 UI；如果 `chunk.from_cache` 为真，或 `chunk.model_used != req.model`（发生了自动降级），顺手在状态栏显示一个小标签（如 `缓存命中` / `已自动降级至 9B`），用户体验会非常好。
2. **改造 `app/core/proxy_server.py`（给沉浸式翻译供流）**：
   - 本地代理收到 `/v1/chat/completions` 后，区分两种路由策略：
     - **透传模式（默认安全模式）**：对原始 `messages` 走 `rate_limiter` 排队与关闭思维链参数，避免破坏沉浸式翻译自带的 Prompt 格式；若设置了「代理注入术语表」开关，则用 `glossary_db.match_terms(user_text)` 命中词条，仅在命中时往 `system` 消息追加 `【硬性要求】专名/术语对照: ...`。
     - **缓存键**：直接对标准化后的 `(model, messages)` 做 SHA-256 缓存，沉浸式翻译重刷同一网页直接返回 SSE/JSON 缓存。
3. **改造 `app/core/config.py`（新增最少配置项）**：
   - `fallback_enabled: bool = True`
   - `fallback_models: list[str] = ["Index-Translate-9B", "Index-Translate-2B"]`
   - `proxy_max_concurrency: int = 3`
   - `proxy_use_glossary: bool = False`（默认关闭，用户按需开启）
   - `cache_enabled: bool = True`
4. **保留 `app/core/api_client.py` 薄兼容层**：
   - 保留原函数签名，内部转调 `IndexTranslateEngine`，等全量测试通过、重构稳定后再决定是否删掉，避免一次改动太大导致排错困难。



### 步骤 6：验收与回归测试清单（`tests/` & `--selftest`）

你的仓库已经有很规范的测试习惯，这一步务必同步补齐 5 个高价值单元测试，确保地基稳固：

| **测试项**               | **验证内容**                                                 |
| ------------------------ | ------------------------------------------------------------ |
| **提示词逐字节对齐**     | 确认迁入 `IndexTranslateEngine` 后，原 `call_api.py` 逐字节对齐测试依然全绿 |
| **429 自动降级**         | Mock 首选 35B 在首字前抛 `RetryableEngineError(429)`，断言自动切换 9B 成功输出且 `chunk.model_used == "Index-Translate-9B"` |
| **流式半途报错不误降级** | Mock 输出 2 个 chunk 后抛 `RetryableEngineError`，断言直接向上抛错而不拼接第二个模型输出 |
| **术语按需命中**         | 术语表含 `cat→猫`、`API→接口`、`量子→Quantum`，输入 `"category uses API"` 只命中 `API`，不误命中 `cat` |
| **并发排队与取消**       | 启动 5 个并发请求（限流并发度为 2），验证同时执行数不超过 2，且排队中触发 `cancel_event.set()` 能在 0.2s 内退出 |

最后按你原来的命令跑一遍全量验证与打包自检：

python -m unittest discover -s tests
powershell -ExecutionPolicy Bypass -File build.ps1
.\dist\IndexTranslate-desktop\IndexTranslate-desktop.exe --selftest



这套底座跑通后，你的软件就真正具备了「大而全工作站」的骨架——接下来第二步无论是做 **托盘常驻 + 全局划词浮窗 + 框选截屏**，还是做 **多引擎同屏对比** 或 **批量图片/SRT/PDF 队列**，都只需要写对应的入口壳子，组装一个 `TranslateRequest` 丢进 `pipeline.execute_stream()` 就能直接复用缓存、术语、限流和降级全套能力。