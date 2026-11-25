# Index-Translate 桌面翻译软件 · 开发文档

> 本文档基于 `call_api.py` 源码 1:1 对齐，**已包含图片翻译（多模态）功能**。

---

## 1. 项目概述

### 1.1 项目背景

Index-Translate 是 Bilibili 开源的翻译模型系列，并提供**免费的 35B 公网 API**（`https://index-translate.bilibili.com/v1`），完全兼容 OpenAI Chat Completions 规范，**无需 API Key、无需注册**。`call_api.py` 脚本已暴露完整能力：

- 文本翻译（含源/目标语言、术语表、指令约束、JSON 保持）
- 流式输出（SSE）
- **图片翻译（多模态，2B/9B/35B 三档模型全支持）**
- 本地 OpenAI 兼容代理（可对接沉浸式翻译等扩展）

本项目将这些能力封装为一个**界面简洁、视觉统一、开箱即用**的桌面翻译应用。

### 1.2 目标用户

- 需要快速翻译文本、文档、截图、图片内容的普通用户；
- 需要术语一致性、格式保持（JSON / 代码）的译者与开发者；
- 希望不打开浏览器、不上传隐私到第三方商业平台的用户。

### 1.3 设计原则

- **零配置可用**：打开即用，无 API Key 负担；
- **视觉克制**：Fluent Design 留白，只暴露必要控件；
- **本地优先**：历史、术语表、草稿全部本地存储；
- **流式可感知**：译文逐字出现，减少等待焦虑；
- **图片即拖即译**：支持拖拽、粘贴、选文件三种方式。

---

## 2. API 规范（与 call_api.py 严格对齐）

### 2.1 端点与认证

| 项目             | 值                                         |
| ---------------- | ------------------------------------------ |
| Base URL         | `https://index-translate.bilibili.com/v1`  |
| Chat Completions | `POST /chat/completions`                   |
| 认证             | **无**（无 Authorization 头）              |
| User-Agent       | `Index-Translate-Client/1.0`（建议保留）   |
| 超时             | 120 秒（源码 `urlopen(req, timeout=120)`） |

### 2.2 模型

| 模型 ID                   | 定位          | 图片翻译 |
| ------------------------- | ------------- | -------- |
| `Index-Translate-35B-A3B` | 旗舰，默认    | ✅ 支持   |
| `Index-Translate-9B`      | 工作站 / 平衡 | ✅ 支持   |
| `Index-Translate-2B`      | 端侧 / 快速   | ✅ 支持   |

> 源码注释明确：**图片翻译 2B / 9B / 35B 全部支持**，只是复杂排版建议用 35B。

### 2.3 请求体

```json
{
  "model": "Index-Translate-35B-A3B",
  "messages": [{"role": "user", "content": "<prompt 或 content 数组>"}],
  "temperature": 0.0,
  "max_tokens": 1024,
  "stream": false,
  "chat_template_kwargs": {"enable_thinking": false}
}
```

**关键约束（易踩坑）**：

- `enable_thinking` **必须为 `false`**。源码注释：开启 thinking 时 CoT 文本会替代译文出现在响应中，模型被训练为直接输出译文。
- `temperature` 默认 `0.0`（贪心解码），保证输出稳定。
- `content` 在图片模式下是 **数组**：`[{"type":"text",...}, {"type":"image_url",...}]`；纯文本模式是字符串。

### 2.4 响应解析

**非流式**：

```python
content = res["choices"][0]["message"]["content"]
return strip_think(content)
```

**流式（SSE）**：

- 逐行读取，`line.startswith("data: ")` 过滤；
- `body == "[DONE]"` 终止；
- `json.loads(body)` 失败时 `continue`；
- **必须做防御性判断**（源码明确要求）：`chunk` 是 dict、`choices` 非空、`delta.content` 非空，三者缺一都要跳过，否则 IndexError / AttributeError。

**`strip_think` 正确实现**（注意是 `</think>` 而非 `...`）：

```python
def strip_think(text: str) -> str:
    """Strip CoT thought blocks if present."""
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip().removeprefix("<think>").strip()
```

### 2.5 Prompt 构建（文本，逐字对齐源码）

#### 2.5.1 无约束模式

```python
if src_name:
    return f"请将以下{src_name}文本翻译为{tgt_name}，直接输出翻译结果，不要进行任何解释。\n\n{text.strip()}"
return f"请将以下文本翻译为{tgt_name}，直接输出翻译结果，不要进行任何解释。\n\n{text.strip()}"
```

#### 2.5.2 约束模式（有术语表或指令时）

```text
请将以下{src_name}{genre}翻译成{tgt_name}，并且严格遵循所有约束要求。

【源文】
{text}

【约束要求】
1. {constraint_1}
2. {constraint_2}

{suffix}
```

- `genre` 默认 `"文本"`（源码 `build_prompt(..., genre="文本")`）；
- 约束标签使用 **【硬性要求】【注意】【软性要求】**（全角方括号，不是〖〗）；
- 术语表：`【硬性要求】专名/术语对照: A→B、C→D`；
- 指令：自动按行拆分，若行首没有约束标签，则加 `【注意】` 前缀；
- `suffix` 特殊规则：**输入是合法 JSON dict 且任一约束含 `JSON`/`json` 字样**时：

```python
suffix = "请以相同的 JSON 格式输出翻译结果，key 保持不变，value 为对应译文。只输出 JSON，不要有任何额外说明。"
# 否则：
suffix = "只输出译文，不要有任何额外说明。"
```

### 2.6 术语表解析（`parse_glossary_terms`）

支持三种输入，按顺序尝试：

1. **JSON 文件路径**：内容 `{"term": "target", ...}` → 渲染为 `term→target` 列表；
2. **内联 JSON 字符串**：以 `{` 开头、`}` 结尾，同上；
3. **分隔符对**：用 `,` 或 `，` 分隔每对，每对用 `:`、`：`、`->`、`→` 分隔键值。

```python
for item in glossary_input.replace("，", ",").split(","):
    item = item.strip()
    for sep in ("->", "→", ":", "："):
        if sep in item:
            k, v = item.split(sep, 1)
            pairs.append(f"{k.strip()}→{v.strip()}")
            break
    else:
        pairs.append(item)  # 无分隔符时原样保留
```

### 2.7 语言表 `LANG_NAMES`（32 种，完整移植）

```python
LANG_NAMES = {
    "en": "英语", "zh": "中文", "de": "德语", "fr": "法语", "es": "西班牙语",
    "ja": "日语", "ko": "韩语", "pt": "葡萄牙语", "ru": "俄语", "ar": "阿拉伯语",
    "it": "意大利语", "nl": "荷兰语", "pl": "波兰语", "ro": "罗马尼亚语",
    "sv": "瑞典语", "tr": "土耳其语", "hi": "印地语", "vi": "越南语",
    "th": "泰语", "id": "印尼语", "ms": "马来语", "fil": "菲律宾语",
    "ukr_cyrl": "乌克兰语", "fas_arab": "波斯语", "ces_latn": "捷克语",
    "ell_grek": "希腊语", "dan_latn": "丹麦语", "hun_latn": "匈牙利语",
    "fin_latn": "芬兰语", "nob_latn": "书面挪威语", "slk_latn": "斯洛伐克语",
    "bul_cyrl": "保加利亚语",
}
```

`source_lang` 默认 `"auto"`；当 `source_lang` 为 `"auto"` 或空时，prompt 中省略源语言描述。

### 2.8 图片翻译（多模态）规范

#### 2.8.1 图片编码（`encode_image_to_data_url`）

```python
def encode_image_to_data_url(image_path_or_url: str) -> str:
    s = image_path_or_url.strip()
    if s.startswith(("http://", "https://", "data:image/")):
        return s  # URL / data URL 直接透传

    if not os.path.isfile(s):
        raise FileNotFoundError(f"Image file not found: {s}")

    mime, _ = mimetypes.guess_type(s)
    if not mime:
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}.get(
                    os.path.splitext(s)[1].lower(), "image/png")
    with open(s, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"
```

支持格式：**png / jpg / jpeg / webp / gif / bmp**；支持 http/https/data URL 直传。

#### 2.8.2 图片 prompt 构建

```python
base_prompt = (
    f"请将图片中的{src_name}文本翻译为{tgt_name}，直接输出翻译结果，不要进行任何解释。"
    if src_name
    else f"请将图片中的文本翻译为{tgt_name}，直接输出翻译结果，不要进行任何解释。"
)

extras = []
if text:        extras.append(f"【参考文本/关注内容】\n{text}")
if instruction: extras.append(f"【指令要求】{instruction}")
if glossary:    extras.append(f"【术语对照】{'、'.join(terms)}")  # terms 来自 parse_glossary_terms

prompt_text = (
    f"{base_prompt}\n\n" + "\n\n".join(extras) + "\n\n只输出翻译结果，不要进行任何解释。"
    if extras else base_prompt
)
```

#### 2.8.3 Content 数组格式

```python
prompt = [
    {"type": "text", "text": prompt_text},
    {"type": "image_url", "image_url": {"url": data_url}},
]
```

#### 2.8.4 图片翻译要点

- 图片模式下**源文本 `text` 可选**，作为"参考文本/关注内容"辅助；
- 指令和术语表可与图片同时生效；
- 图片翻译的 prompt **不使用**文本模式的【源文】/【约束要求】结构，而是上面 `extras` 拼接式；
- 流式输出同样可用；
- 大图 base64 体积大，**客户端需先压缩**（建议 ≤ 4MB，长边 ≤ 2048px）。

---

## 3. 技术选型

| 层面     | 选型                         | 理由                                                  |
| -------- | ---------------------------- | ----------------------------------------------------- |
| 语言     | **Python 3.10+**             | 与 `call_api.py` 同语言，逻辑可直接移植               |
| GUI      | **PyQt6**                    | 跨平台、性能稳定、信号槽天然适配流式 UI               |
| 组件库   | **PyQt6-Fluent-Widgets**     | Fluent Design 现代控件，快速实现"简洁好看"            |
| HTTP     | **httpx**                    | 原生同步/流式、SSE 读取、连接复用；比 `urllib` 更简洁 |
| 图片处理 | **Pillow**                   | 压缩、尺寸限制、格式校验（比 QImage 更灵活）          |
| 存储     | **SQLite（stdlib sqlite3）** | 历史 / 术语表，零额外依赖                             |
| 打包     | **PyInstaller**              | PyQt6 支持成熟，可单文件分发                          |

**不选 Electron / Tauri 的理由**：会引入 Node/Rust 工具链，与"零依赖 Python 脚本"精神背离，且体积显著更大。PyQt6 + Fluent Widgets 在视觉上足以达到现代桌面水准。

---

## 4. 功能设计

### 4.1 功能清单

| 优先级 | 功能             | 说明                                  |
| ------ | ---------------- | ------------------------------------- |
| P0     | 文本翻译         | 源语言自动检测，目标语言下拉（32 种） |
| P0     | 流式输出         | 默认开 SSE，译文逐字呈现              |
| P0     | 图片翻译（文件） | 选择本地图片 → 翻译图中文字           |
| P0     | 图片翻译（拖拽） | 拖拽图片到窗口即译                    |
| P0     | 图片翻译（粘贴） | Ctrl+V 粘贴剪贴板截图                 |
| P0     | 复制译文         | 一键复制                              |
| P0     | 错误提示         | 网络 / HTTP / 超时 / 空响应，可读提示 |
| P0     | 停止生成         | 流式中可中断                          |
| P1     | 术语表           | 本地维护，参与文本与图片 prompt       |
| P1     | 指令约束         | 自由文本，如"严格保留 JSON 格式"      |
| P1     | 图片 + 参考文本  | 图片 + 可选源文本（作为关注内容）     |
| P1     | 翻译历史         | SQLite 存储，支持搜索                 |
| P1     | 模型切换         | 2B / 9B / 35B                         |
| P2     | 大图自动压缩     | 长边 > 2048 或 > 4MB 自动缩放         |
| P2     | 深色/浅色主题    | Fluent Widgets 内置                   |
| P2     | 本地代理模式     | 移植 `--serve`，供沉浸式翻译等使用    |
| P3     | 批量图片         | 多图逐张翻译                          |

### 4.2 非功能需求

- 启动时间 ≤ 2 秒；
- 流式首 token 在正常网络下 ≤ 3 秒；
- 历史默认保留最近 500 条，可配置；
- 输入草稿自动保存，崩溃不丢失；
- 图片处理（编码/压缩）不得阻塞 UI 线程。

---

## 5. UI 设计

### 5.1 主窗口布局（统一文本 + 图片）

采用 **左右双栏 + 顶部模式切换 + 底部操作栏**，图片作为输入区的"附件"，不单独开页面，保持界面简洁。

```text
┌───────────────────────────────────────────────────────────┐
│  [Logo] Index Translate              [设置] [历史] [主题]  │
├──────────────────────────┬────────────────────────────────┤
│  源文本（可选）           │  译文输出                       │
│  QPlainTextEdit          │  QTextBrowser（只读、可滚动）    │
│                          │                                │
│  ┌──── 图片附件区 ────┐   │                                │
│  │ [🖼 预览缩略图]     │   │                                │
│  │ 或“拖入 / 粘贴 /   │   │                                │
│  │  点击选择图片”      │   │                                │
│  └───────────────────┘   │                                │
│                          │  [复制] [重新翻译] [停止]       │
├──────────────────────────┴────────────────────────────────┤
│ 源语言 [自动▾]  目标语言 [中文▾]  模型 [35B▾]  □ 流式      │
│ ⚙ 高级约束（术语表 / 指令）         [ 🚀 翻译 ]             │
├───────────────────────────────────────────────────────────┤
│ ● 就绪 / 翻译中… / 图片加载中… / 错误                     │
└───────────────────────────────────────────────────────────┘
```

### 5.2 图片附件区交互

- **点击**：`QFileDialog.getOpenFileName`，过滤 `*.png *.jpg *.jpeg *.webp *.gif *.bmp`；
- **拖拽**：重写 `dragEnterEvent` / `dropEvent`，接受 `text/uri-list`（文件）与 `image/*`；
- **粘贴**：监听 `QShortcut(Ctrl+V)`，若剪贴板 `hasImage()`，取 `QImage` 转 PNG bytes；
- **预览**：`QLabel` + `QPixmap` 按比例缩放到 120px 高，右上角 × 清除；
- **多图**（P3）：横向缩略图列表，逐张翻译，结果按图片分隔；
- **URL 输入**（P2）：附件区支持粘贴 http(s) 图片链接，直接透传给 API。

### 5.3 视觉规范（Fluent Widgets）

- 主色 `#0078D4`，`setThemeColor("#0078D4")`；
- 圆角 8px；双栏间距 24px；外边距 20px；
- 译文区字号比源文区大 1–2px；
- 底部状态点 + 文本，流式时显示 `IndeterminateProgressBar`；
- 错误用 `InfoBar.error(...)` 弹出，同时写入状态栏。

### 5.4 交互细节

- `Ctrl+Enter` 触发翻译；
- 目标语言下拉支持输入搜索（`ComboBox.setEditable(True)` + `QCompleter`）；
- 流式输出时译文区自动滚动到底，用户手动上滚后暂停自动滚动；
- 翻译中"翻译"按钮变"停止"，点击后终止 QThread 与 httpx 流；
- 术语表 + 指令收在底部 `Expander` / 弹出对话框，不占主界面。

---

## 6. 模块划分

```text
index_translate/
├── main.py                      # 入口，QApplication 初始化
├── core/
│   ├── api_client.py            # httpx 封装：文本 / 图片 / 流式 / 非流式
│   ├── prompt_builder.py        # build_prompt / build_image_prompt / parse_glossary_terms / strip_think
│   ├── languages.py             # LANG_NAMES（32 种，逐条对齐源码）
│   ├── image_utils.py           # 文件/URL/剪贴板 → data URL；Pillow 压缩
│   └── models.py                # TranslationRequest / TranslationResult / ImageInput
├── workers/
│   └── translation_worker.py    # QThread，支持取消、流式信号
├── storage/
│   ├── history_db.py            # SQLite：history CRUD
│   └── glossary_db.py           # SQLite：术语表 CRUD
├── ui/
│   ├── main_window.py           # FluentWindow 主窗口
│   ├── text_panel.py            # 双栏 + 图片附件
│   ├── image_drop_area.py       # 拖拽/粘贴/预览组件
│   ├── settings_dialog.py       # 模型 / 超时 / 主题 / 历史条数
│   └── styles.py                # QSS 微调
├── proxy/
│   └── local_proxy.py           # （P2）移植 --serve 的 OpenAI 兼容代理
└── requirements.txt
```

---

## 7. 核心代码骨架

### 7.1 `core/prompt_builder.py`

```python
import json, os, sys

LANG_NAMES = { ... }  # 完整复制 §2.7


def parse_glossary_terms(glossary_input: str) -> list[str]:
    if not glossary_input:
        return []
    s = glossary_input.strip()

    if os.path.isfile(s):
        try:
            with open(s, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return [f"{k.strip()}→{str(v).strip()}" for k, v in data.items()]
        except Exception as e:
            sys.stderr.write(f"Warning: failed to read glossary file {s}: {e}\n")

    if s.startswith("{") and s.endswith("}"):
        try:
            data = json.loads(s)
            if isinstance(data, dict):
                return [f"{k.strip()}→{str(v).strip()}" for k, v in data.items()]
        except Exception:
            pass

    pairs = []
    for item in s.replace("，", ",").split(","):
        item = item.strip()
        if not item:
            continue
        for sep in ("->", "→", ":", "："):
            if sep in item:
                k, v = item.split(sep, 1)
                pairs.append(f"{k.strip()}→{v.strip()}")
                break
        else:
            pairs.append(item)
    return pairs


def _lang(code: str) -> str | None:
    return LANG_NAMES.get(code.lower()) if code and code.lower() != "auto" else None


def build_prompt(text, target_lang, source_lang="auto",
                 instruction="", glossary="", genre="文本") -> str:
    tgt_name = LANG_NAMES.get(target_lang.lower())
    if tgt_name is None:
        raise ValueError(f"unknown target language code: {target_lang}")
    src_name = _lang(source_lang)

    constraints = []
    if glossary:
        terms = parse_glossary_terms(glossary)
        if terms:
            constraints.append(f"【硬性要求】专名/术语对照: {'、'.join(terms)}")

    if instruction:
        for line in instruction.strip().splitlines():
            line = line.strip().lstrip("0123456789. ")
            if not line:
                continue
            if line.startswith(("【硬性要求】", "【注意】", "【软性要求】")):
                constraints.append(line)
            else:
                constraints.append(f"【注意】{line}")

    if constraints:
        header_src = f"{src_name}{genre}" if src_name else genre
        header = f"请将以下{header_src}翻译成{tgt_name}，并且严格遵循所有约束要求。"
        req = "\n".join(f"{i+1}. {c}" for i, c in enumerate(constraints))
        stripped = text.strip()

        is_json = False
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                is_json = isinstance(json.loads(stripped), dict)
            except Exception:
                is_json = False

        if is_json and any(("JSON" in c or "json" in c) for c in constraints):
            suffix = "请以相同的 JSON 格式输出翻译结果，key 保持不变，value 为对应译文。只输出 JSON，不要有任何额外说明。"
        else:
            suffix = "只输出译文，不要有任何额外说明。"

        return f"{header}\n\n【源文】\n{stripped}\n\n【约束要求】\n{req}\n\n{suffix}"

    if src_name:
        return f"请将以下{src_name}文本翻译为{tgt_name}，直接输出翻译结果，不要进行任何解释。\n\n{text.strip()}"
    return f"请将以下文本翻译为{tgt_name}，直接输出翻译结果，不要进行任何解释。\n\n{text.strip()}"


def build_image_prompt(data_url: str, target_lang: str, source_lang="auto",
                       ref_text="", instruction="", glossary="") -> list[dict]:
    tgt_name = LANG_NAMES.get(target_lang.lower())
    if tgt_name is None:
        raise ValueError(f"unknown target language code: {target_lang}")
    src_name = _lang(source_lang)

    base = (
        f"请将图片中的{src_name}文本翻译为{tgt_name}，直接输出翻译结果，不要进行任何解释。"
        if src_name else
        f"请将图片中的文本翻译为{tgt_name}，直接输出翻译结果，不要进行任何解释。"
    )

    extras = []
    if ref_text:
        extras.append(f"【参考文本/关注内容】\n{ref_text}")
    if instruction:
        extras.append(f"【指令要求】{instruction}")
    if glossary:
        terms = parse_glossary_terms(glossary)
        if terms:
            extras.append(f"【术语对照】{'、'.join(terms)}")

    prompt_text = (
        f"{base}\n\n" + "\n\n".join(extras) + "\n\n只输出翻译结果，不要进行任何解释。"
        if extras else base
    )

    return [
        {"type": "text", "text": prompt_text},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]


def strip_think(text: str) -> str:
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip().removeprefix("<think>").strip()
```

### 7.2 `core/image_utils.py`

```python
import base64, mimetypes, os
from io import BytesIO

MAX_BYTES = 4 * 1024 * 1024     # 4MB
MAX_SIDE = 2048                  # 长边上限


def encode_image_to_data_url(path_or_url: str) -> str:
    s = path_or_url.strip()
    if s.startswith(("http://", "https://", "data:image/")):
        return s
    if not os.path.isfile(s):
        raise FileNotFoundError(f"Image file not found: {s}")
    mime, _ = mimetypes.guess_type(s)
    if not mime:
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}.get(
                    os.path.splitext(s)[1].lower(), "image/png")
    with open(s, "rb") as f:
        raw = f.read()
    raw, mime = maybe_compress(raw, mime)
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def maybe_compress(raw: bytes, mime: str) -> tuple[bytes, str]:
    """超过大小或长边时压缩（Pillow）。GIF 动图保持原样。"""
    from PIL import Image
    if mime == "image/gif" and len(raw) <= MAX_BYTES:
        return raw, mime
    img = Image.open(BytesIO(raw))
    if img.width > MAX_SIDE or img.height > MAX_SIDE or len(raw) > MAX_BYTES:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = BytesIO()
        fmt = "PNG" if mime == "image/png" else "JPEG"
        if fmt == "JPEG" and img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(buf, format=fmt, quality=85, optimize=True)
        return buf.getvalue(), f"image/{fmt.lower()}"
    return raw, mime


def qimage_to_data_url(qimage) -> str:
    """剪贴板 QImage → PNG data URL。"""
    from PyQt6.QtCore import QBuffer, QIODevice
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    qimage.save(buf, "PNG")
    b64 = base64.b64encode(bytes(buf.data())).decode("ascii")
    return f"data:image/png;base64,{b64}"
```

### 7.3 `core/api_client.py`

```python
import json
from typing import Generator, Union
import httpx
from .prompt_builder import build_prompt, build_image_prompt, strip_think

API_BASE = "https://index-translate.bilibili.com/v1"
DEFAULT_MODEL = "Index-Translate-35B-A3B"


def _payload(model, content, stream, max_tokens, temperature):
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": stream,
        "chat_template_kwargs": {"enable_thinking": False},  # 关键！
    }


def translate_text(text: str, target_lang: str, source_lang="auto",
                   instruction="", glossary="", model=DEFAULT_MODEL,
                   max_tokens=1024, temperature=0.0, stream=True,
                   api_base=API_BASE) -> Union[Generator[str, None, None], str]:
    prompt = build_prompt(text, target_lang, source_lang, instruction, glossary)
    body = _payload(model, prompt, stream, max_tokens, temperature)
    return _stream(api_base, body) if stream else _once(api_base, body)


def translate_image(data_url: str, target_lang: str, source_lang="auto",
                    ref_text="", instruction="", glossary="",
                    model=DEFAULT_MODEL, max_tokens=2048, temperature=0.0,
                    stream=True, api_base=API_BASE) -> Union[Generator[str, None, None], str]:
    content = build_image_prompt(data_url, target_lang, source_lang,
                                 ref_text, instruction, glossary)
    body = _payload(model, content, stream, max_tokens, temperature)
    return _stream(api_base, body) if stream else _once(api_base, body)


def _once(api_base: str, body: dict) -> str:
    with httpx.Client(timeout=120.0) as c:
        r = c.post(f"{api_base.rstrip('/')}/chat/completions", json=body,
                   headers={"Content-Type": "application/json",
                            "User-Agent": "Index-Translate-Client/1.0"})
        r.raise_for_status()
        return strip_think(r.json()["choices"][0]["message"]["content"])


def _stream(api_base: str, body: dict) -> Generator[str, None, None]:
    with httpx.stream("POST", f"{api_base.rstrip('/')}/chat/completions",
                      json=body,
                      headers={"Content-Type": "application/json",
                               "User-Agent": "Index-Translate-Client/1.0"},
                      timeout=httpx.Timeout(120.0)) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            line = line.strip()
            if not line or not line.startswith("data: "):
                continue
            raw = line[6:]
            if raw == "[DONE]":
                break
            try:
                chunk = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                yield piece
```

### 7.4 `workers/translation_worker.py`

```python
from PyQt6.QtCore import QThread, pyqtSignal
from core.api_client import translate_text, translate_image


class TranslationWorker(QThread):
    chunk     = pyqtSignal(str)
    finished_ = pyqtSignal()
    error     = pyqtSignal(str)

    def __init__(self, *, text="", image_data_url="", target_lang="en",
                 source_lang="auto", instruction="", glossary="",
                 model="Index-Translate-35B-A3B", stream=True, parent=None):
        super().__init__(parent)
        self._cancelled = False
        self._kwargs = dict(
            target_lang=target_lang, source_lang=source_lang,
            instruction=instruction, glossary=glossary,
            model=model, stream=stream,
        )
        self._text = text
        self._image = image_data_url

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            if self._image:
                gen = translate_image(self._image, ref_text=self._text, **self._kwargs)
            else:
                gen = translate_text(self._text, **self._kwargs)

            if isinstance(gen, str):
                self.chunk.emit(gen)
            else:
                for piece in gen:
                    if self._cancelled:
                        return
                    self.chunk.emit(piece)
            if not self._cancelled:
                self.finished_.emit()
        except Exception as e:
            if not self._cancelled:
                self.error.emit(str(e))
```

### 7.5 `ui/image_drop_area.py`（拖拽 + 粘贴 + 预览）

```python
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QFileDialog, QApplication
from core.image_utils import encode_image_to_data_url, qimage_to_data_url


class ImageDropArea(QLabel):
    imageChanged = pyqtSignal(str)   # 发出 data URL，空字符串表示清除

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setText("拖入图片 / Ctrl+V 粘贴 / 点击选择")
        self.setMinimumHeight(120)
        self.setStyleSheet("border:1px dashed #888; border-radius:8px; color:#888;")
        self._data_url = ""

    # --- 点击选文件 ---
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            path, _ = QFileDialog.getOpenFileName(
                self, "选择图片", "",
                "图片 (*.png *.jpg *.jpeg *.webp *.gif *.bmp)")
            if path:
                self._set_path(path)

    # --- 拖拽 ---
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            path = e.mimeData().urls()[0].toLocalFile()
            if path:
                self._set_path(path)
        elif e.mimeData().hasImage():
            img = e.mimeData().imageData()
            self._set_data_url(qimage_to_data_url(img))

    # --- 外部粘贴入口（由主窗口快捷键调用）---
    def paste_from_clipboard(self):
        md = QApplication.clipboard().mimeData()
        if md.hasImage():
            self._set_data_url(qimage_to_data_url(md.imageData()))

    def clear_image(self):
        self._data_url = ""
        self.setPixmap(QPixmap())
        self.setText("拖入图片 / Ctrl+V 粘贴 / 点击选择")
        self.imageChanged.emit("")

    def _set_path(self, path: str):
        try:
            url = encode_image_to_data_url(path)
        except Exception as e:
            self.setText(f"图片加载失败: {e}")
            return
        self._set_data_url(url)

    def _set_data_url(self, url: str):
        self._data_url = url
        # 预览
        import base64
        header, b64 = url.split(",", 1)
        pix = QPixmap()
        pix.loadFromData(base64.b64decode(b64))
        self.setPixmap(pix.scaledToHeight(110, Qt.TransformationMode.SmoothTransformation))
        self.imageChanged.emit(url)

    @property
    def data_url(self) -> str:
        return self._data_url
```

### 7.6 `ui/main_window.py`（骨架）

```python
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout
from PyQt6.QtGui import QShortcut, QKeySequence
from qfluentwidgets import (
    FluentWindow, PrimaryPushButton, PushButton, ComboBox,
    PlainTextEdit, TextBrowser, InfoBar, setThemeColor,
)
from core.languages import LANG_NAMES
from workers.translation_worker import TranslationWorker
from ui.image_drop_area import ImageDropArea


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        setThemeColor("#0078D4")
        self.setWindowTitle("Index Translate")
        self.resize(1000, 660)
        self._worker: TranslationWorker | None = None
        self._build_ui()

    def _build_ui(self):
        root = QWidget(); self.setCentralWidget(root)
        outer = QVBoxLayout(root)

        # 双栏
        cols = QHBoxLayout()
        self.source_edit = PlainTextEdit()
        self.source_edit.setPlaceholderText("输入或粘贴待翻译文本（图片模式下可作为参考文本，可留空）…")
        self.drop = ImageDropArea()
        self.target_browser = TextBrowser()
        self.target_browser.setPlaceholderText("译文将在此显示")

        left = QVBoxLayout()
        left.addWidget(self.source_edit, 3)
        left.addWidget(self.drop, 1)
        cols.addLayout(left, 1)
        cols.addWidget(self.target_browser, 1)
        outer.addLayout(cols, 1)

        # 底部操作栏
        bar = QHBoxLayout()
        self.src_lang = ComboBox(); self.src_lang.addItems(["自动检测"] + list(LANG_NAMES.values()))
        self.tgt_lang = ComboBox(); self.tgt_lang.addItems(list(LANG_NAMES.values()))
        self.tgt_lang.setCurrentText("中文")
        self.model_box = ComboBox()
        self.model_box.addItems(["Index-Translate-35B-A3B",
                                 "Index-Translate-9B",
                                 "Index-Translate-2B"])
        self.go_btn = PrimaryPushButton("翻译")
        self.go_btn.clicked.connect(self._on_translate)
        bar.addWidget(self.src_lang); bar.addWidget(self.tgt_lang)
        bar.addWidget(self.model_box); bar.addStretch(); bar.addWidget(self.go_btn)
        outer.addLayout(bar)

        QShortcut(QKeySequence("Ctrl+Return"), self, self._on_translate)
        QShortcut(QKeySequence("Ctrl+V"), self, self._on_paste)

    def _on_paste(self):
        # 若剪贴板有图片则进图片区，否则默认粘贴到文本
        if QApplication.clipboard().mimeData().hasImage():
            self.drop.paste_from_clipboard()
        else:
            self.source_edit.paste()

    def _code_from_display(self, display: str) -> str:
        for code, name in LANG_NAMES.items():
            if name == display:
                return code
        return "en"

    def _on_translate(self):
        if self._worker and self._worker.isRunning():
            self._worker.cancel(); return   # 二次点击 = 停止

        text = self.source_edit.toPlainText().strip()
        image = self.drop.data_url
        if not text and not image:
            InfoBar.warning("请输入文本或添加图片", "", parent=self, duration=2000)
            return

        self.target_browser.clear()
        self._worker = TranslationWorker(
            text=text,
            image_data_url=image,
            target_lang=self._code_from_display(self.tgt_lang.currentText()),
            model=self.model_box.currentText(),
        )
        self._worker.chunk.connect(self.target_browser.insertPlainText)
        self._worker.finished_.connect(self._on_done)
        self._worker.error.connect(lambda m: (
            InfoBar.error("翻译失败", m, parent=self, duration=4000),
            self._on_done(),
        ))
        self.go_btn.setText("停止")
        self._worker.start()

    def _on_done(self):
        self.go_btn.setText("翻译")


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    w = MainWindow(); w.show()
    sys.exit(app.exec())
```

---

## 8. 打包与部署

### 8.1 安装依赖

```bash
pip install PyQt6 PyQt6-Fluent-Widgets httpx Pillow pyinstaller
```

> ⚠️ 只装 `PyQt6-Fluent-Widgets`，**不要**同时装 PySide 版本，否则包名冲突。

### 8.2 打包命令

```bash
pyinstaller --noconfirm --windowed --onefile ^
  --name IndexTranslate ^
  --icon=assets/icon.ico ^
  --add-data "assets;assets" ^
  main.py
```

macOS / Linux 把 `;` 换成 `:`。`--windowed` 在 Windows 隐藏控制台。

### 8.3 体积优化（可选）

PyQt6 单文件通常 80–120MB。可加 `--exclude-module` 排除未用的 Qt 模块（QtWebEngine 等），或改用 `--onedir` 加快启动。

---

## 9. 开发计划

| Sprint | 内容                                                         | 预计   |
| ------ | ------------------------------------------------------------ | ------ |
| **S1** | 项目骨架 + `prompt_builder` + `api_client` + 双栏 UI 静态布局 | 3–4 天 |
| **S2** | 文本流式打通 + 语言切换 + 错误处理 + 复制 + 停止             | 2–3 天 |
| **S3** | **图片翻译**：文件选择 + 拖拽 + 粘贴 + 预览 + 压缩 + 图片流式 | 3–4 天 |
| **S4** | 术语表 + 指令约束 + 历史（SQLite）+ 主题                     | 3–4 天 |
| **S5** | 设置页 + 打包测试 + 代理模式（P2）                           | 2–3 天 |

合计 **约 2.5–3 周** 达到 P2 功能完备、可分发。

---

## 10. 风险与注意事项

1. **`enable_thinking` 必须显式 `false`**。这是本 API 最容易踩的坑，遗漏会导致 CoT 文本覆盖译文。
2. **SSE 解析必须防御**。服务端可能发送无 `choices`、空 `choices`、非 dict 的 chunk，必须 `continue` 而非崩溃（源码已强调）。
3. **`strip_think` 分割标记是 `</think>`**，不是 `...`。移植时切勿写错。
4. **图片翻译 prompt 与文本不同**。图片走 `【参考文本/关注内容】/【指令要求】/【术语对照】` 拼接式，**不要**套用文本的【源文】/【约束要求】结构。
5. **大图 base64 体积**。超过 4MB 或长边 > 2048 建议先压缩（Pillow），否则请求体过大易超时。
6. **免费公网 API 无 SLA**。脚本本身无重试，应用中建议**至少重试 1 次 + 指数退避**，状态栏注明"免费服务，遇拥塞请稍后重试"。
7. **`LANG_NAMES` 必须完整移植**。缺失语言会导致目标语言名称为空、prompt 错误。
8. **图片编码不得阻塞 UI**。压缩/编码放 worker 线程，主线程只更新预览。
9. **剪贴板 QImage 路径**：`QClipboard.mimeData().imageData()` 返回 `QImage`，需经 `QBuffer` 转 PNG bytes，不能直接用 `toImage()` 的临时对象。
10. **代理功能（`--serve`）默认只绑 127.0.0.1**。上游无鉴权，暴露到 `0.0.0.0` 等于把免费 API 开放给整个局域网，UI 中需明确警告。
