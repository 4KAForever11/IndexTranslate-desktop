第三步「批量多图与多格式文档工作台（统一任务队列 + 可插拔解析器）

在底座（多引擎/限流/缓存/降级）和系统交互层（托盘/划词/截屏）都跑通之后，选这一步作为第三阶段有三个直接收益：

1. **彻底清零 README 的「已知限制」**：直接补齐「暂不支持批量图片翻译」的短板，一次拖入整套漫画图、PPT 截图或外文资料页即可排队出结果。
2. **零重型依赖，先拿高价值格式**：只用 Python 标准库加轻量解析，就能先吃下 **多图、SRT/VTT 字幕、Markdown/TXT、i18n JSON** 四大高频场景，打包体积几乎不涨；同时把解析器抽象定型，为后面外挂 PDF 双语或本地语音字幕留好插槽。
3. **真正压测第一步的管道中枢**：批量任务动辄几十上百个片段，正好把你刚写的 `RateLimiter`、SQLite 缓存和 429 自动降级发挥到极致。



## 一、核心设计原则：把「文件翻译」拆成四段标准流水线

不要为每种文件各写一套翻译循环，而是强制所有格式都走同一条数据流：

1. **解析（Parse）**：不同格式的解析器把原始文件拆成一组扁平的 `TranslationUnit`（翻译单元），每个单元只关心「原始文本或图片路径 + 定位元数据」。
2. **保护（Protect）**：在发给模型前，用占位符守卫把 Markdown 代码块、HTML 标签、变量占位符（如 `{count}`、`%s`）替换成安全哨兵记号，翻完后再精准还原并校验。
3. **持久化执行（Persist & Run）**：任务和每个单元的状态落盘到 `jobs.db`。哪怕翻译到第 80/120 条时触发限流、断网或关掉软件，重启后也能**断点续翻**，并允许对单条失败或译得不满意的条目**单独重试或手工微调**。
4. **重组导出（Reassemble）**：由原解析器把译文写回原结构，支持**单语译文**与**双语对照**两种导出形态。

二、目录结构增量设计

app/
├─ core/
│  ├─ parsers/                  # [新增] 可插拔格式解析器（未来 PDF/EPUB/DOCX 也放这里）
│  │  ├─ __init__.py
│  │  ├─ base.py                # TranslationUnit 与 BaseDocumentParser 抽象基类
│  │  ├─ placeholder_guard.py   # 代码块 / HTML 标签 / 变量占位符保护与还原
│  │  ├─ image_batch_parser.py  # 多图批量翻译解析器（复用 image_utils.py）
│  │  ├─ subtitle_parser.py     # .srt / .vtt 时间轴保护与双语字幕导出
│  │  ├─ markdown_parser.py     # .md / .txt 分段解析（自动跳过围栏代码块与 Frontmatter）
│  │  └─ json_i18n_parser.py    # .json 递归遍历叶子字符串，严格保留键结构
│  └─ batch/                    # [新增] 持久化任务队列与调度器
│     ├─ __init__.py
│     ├─ job_db.py              # SQLite 任务与单元状态落盘（支持断点续翻）
│     └─ job_runner.py          # QThread 生产者-消费者调度器，对接 TranslationPipeline
└─ ui/
   └─ batch_page.py             # [新增] 侧边栏第 5 页：批量工作台界面



## 三、关键模块开发思路与核心骨架

### 模块 1：统一翻译单元与解析器协议（`app/core/parsers/base.py`）

把图片批量任务和文档段落统一到同一个 `TranslationUnit` 数据结构里，后续队列调度、重试、手工校对和进度统计全部复用一套逻辑：

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ExportMode = Literal["monolingual", "bilingual"]


@dataclass
class TranslationUnit:
    unit_index: int
    source_text: str = ""
    image_path: str | None = None      # 多图任务填图片路径，纯文本任务为 None
    context_hint: str = ""             # 可选：给字幕/长文带入前 1-2 句上下文
    meta: dict = field(default_factory=dict)  # 存时间戳、JSON 路径、行号、缩略图路径等
    translated_text: str = ""
    status: str = "pending"            # pending / running / done / failed / skipped
    error_msg: str = ""


class BaseDocumentParser(ABC):
    parser_id: str = ""
    supported_exts: tuple[str, ...] = ()

    @abstractmethod
    def parse(self, file_paths: list[Path]) -> list[TranslationUnit]:
        """将单个文件或多张图片拆解为标准翻译单元"""
    
    @abstractmethod
    def export(
        self,
        original_paths: list[Path],
        units: list[TranslationUnit],
        output_path: Path,
        mode: ExportMode = "bilingual",
    ) -> Path:
        """按单语或双语对照模式重组并写出目标文件"""



### 模块 2：格式与占位符守卫（`app/core/parsers/placeholder_guard.py`）

文档翻译最容易翻车的地方，是模型顺手把 `{user_name}` 翻成 `{用户名}`、把行内代码 ``npm run build`` 改坏、或者吞掉 HTML 标签。不要只靠提示词约束，必须在程序侧做**硬保护 + 还原校验**：

from __future__ import annotations
import re

# 按优先级保护：行内代码 -> HTML 标签 -> 常见 printf / i18n 占位符
PROTECT_PATTERN = re.compile(
    r"(`[^`]+`|<\/?[a-zA-Z][^>]*>|\{\{[^}]+\}\}|\{[a-zA-Z0-9_.-]+\}|%(?:\([a-zA-Z0-9_]+\))?[sdif])"
)


class PlaceholderGuard:
    def protect(self, text: str) -> tuple[str, dict[str, str]]:
        mapping: dict[str, str] = {}

        def _repl(match: re.Match) -> str:
            token = f"⟦P{len(mapping)}⟧"
            mapping[token] = match.group(0)
            return token
    
        protected = PROTECT_PATTERN.sub(_repl, text)
        return protected, mapping
    
    def restore(self, translated: str, mapping: dict[str, str]) -> tuple[str, list[str]]:
        """还原占位符，并返回模型漏掉或篡改的哨兵列表供 UI 标黄提醒"""
        missing: list[str] = []
        result = translated
        for token, raw in mapping.items():
            if token not in result:
                missing.append(raw)
            else:
                result = result.replace(token, raw)
        return result, missing



调用 `TranslationPipeline` 时，只要 `mapping` 非空，就在 `instruction` 里自动追加一句：`保留原文中所有 ⟦P0⟧、⟦P1⟧ 形式的占位符原样输出，不要翻译或增删`。还原时如果 `missing` 不为空，就把该单元标记为「需人工复核」高亮出来。



### 模块 3：持久化任务队列与断点续翻（`job_db.py` + `job_runner.py`）

- **双表落盘（`jobs.db`）**：
  - `batch_jobs` 表：记录任务 ID、文件名、解析器类型、源/目标语言、术语分组、导出模式、总条数、已完成数、创建时间。
  - `batch_units` 表：外键关联 `job_id`，记录 `unit_index`、`source_text`、`image_path`、`meta_json`、`translated_text`、`status`、`error_msg`。
- **防卡死与内存控制**：
  - 批量图片任务**绝不在解析阶段把 50 张图一次性转 Base64 塞进内存**。`parse()` 阶段只存文件路径；`job_runner` 消费到当前单元时，才调用 `image_utils.py` 现场压缩转 Base64，翻完立刻释放对象。
  - `BatchJobRunner` 继承 `QThread`，通过 `threading.Event` 支持随时暂停/继续；每消费完一个单元立刻写回 `jobs.db` 并向 UI 发射 `unit_updated(job_id, unit_index, status, text)` 信号。
  - **无缝复用第一步底座**：对每个 `pending` 单元直接组装 `TranslateRequest(stream=False, scene="document", ...)` 交给 `TranslationPipeline.execute_stream()`，自动享受限流排队、术语按需命中、缓存命中和 429 自动降级。



### 模块 4：首批 4 个高价值轻量解析器的细节要点

| **解析器**                          | **核心处理逻辑**                                             | **特色细节**                                                 |
| ----------------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------ |
| **多图批量 (`image_batch_parser`)** | 接收多选图片或文件夹，按文件名自然排序生成单元               | 导出为带原图缩略图/文件名的 Markdown 汇总文档或纯文本，完美补齐 README 已知限制 |
| **字幕 (`subtitle_parser`)**        | 解析 `.srt` / `.vtt` 编号、时间轴和台词块                    | 注入轻量指令控制译文紧凑防超行；导出双语字幕时支持选择「译文在上 / 原文在上」 |
| **文档 (`markdown_parser`)**        | 识别 YAML Frontmatter 与三反引号代码块，直接标为 `skipped` 原样透传 | 其余段落走 `PlaceholderGuard` 保护行内代码与链接；双语模式输出「原文段落 + 空行 + 译文段落」 |
| **本地化 (`json_i18n_parser`)**     | 递归展开嵌套 JSON，用 JSON Path（如 `menu.file.open`）存入 `meta` | 跳过纯数字、URL 和布尔值，只翻字符串叶子节点，导出保持原缩进与键顺序 |



## 四、界面交互布局（`app/ui/batch_page.py`）

沿用你现有的 `PyQt6-Fluent-Widgets` 视觉风格，左侧导航新增「批量」页（`Ctrl+5`），页面采用经典的**上控制 + 左任务队列 + 右对照审校**三区布局：

- **顶部操作栏**：拖放文件/文件夹区域、源/目标语言、模型选择、术语表分组下拉框、输出模式（单语 / 双语对照）、「开始 / 暂停 / 导出」按钮。
- **左侧任务列表**：展示历史与进行中的批量任务卡片（文件名、进度条、成功/失败计数），支持重启软件后继续未完成任务。
- **右侧单元审校表**：三列表格（序号/位置 | 原文或图片缩略图 | 可双击编辑的译文 + 状态标签）。
  - 支持顶部过滤芯片：「全部 / 失败 / 占位符告警」，方便用户只检查有问题那几条。
  - 每行末尾提供「单条重试」按钮，避免整份文件重翻。

下面的状态流转图展示了批量文档与多图从 UI 拖入、解析分块、占位符保护、SQLite 断点持久化到后台 `QThread` 调度并回流界面审校的完整异步生命周期，可以点击「上一步 / 下一步」逐步核对线程边界与异常处理时机：

![image-20261127210014819](C:\Users\用户\AppData\Roaming\Typora\typora-user-images\image-20261127210014819.png)



![image-20261127210124456](C:\Users\用户\AppData\Roaming\Typora\typora-user-images\image-20261127210124456.png)



![image-20261127210136392](C:\Users\用户\AppData\Roaming\Typora\typora-user-images\image-20261127210136392.png)

![image-20261127210146988](C:\Users\用户\AppData\Roaming\Typora\typora-user-images\image-20261127210146988.png)

![image-20261127210158090](C:\Users\用户\AppData\Roaming\Typora\typora-user-images\image-20261127210158090.png)

![image-20261127210207404](C:\Users\用户\AppData\Roaming\Typora\typora-user-images\image-20261127210207404.png)

![image-20261127210217638](C:\Users\用户\AppData\Roaming\Typora\typora-user-images\image-20261127210217638.png)

## 五、分阶段开发节奏与验收清单

建议拆成 4 个可独立提交的小闭环推进：

1. **闭环 A（核心骨架）**：完成 `base.py`、`placeholder_guard.py`、`job_db.py`、`job_runner.py`，先写单元测试跑通模拟队列的暂停、恢复和单条重试。
2. **闭环 B（先解已知限制）**：接入 `image_batch_parser.py` 和 `batch_page.py` 基础界面，实现「拖入多张图片 → 排队翻译 → 导出汇总 Markdown」。
3. **闭环 C（字幕与文档）**：补齐 `subtitle_parser.py`、`markdown_parser.py`、`json_i18n_parser.py`，打通双语对照导出与占位符告警高亮。
4. **闭环 D（打磨体验）**：补齐表格内双击手工微调译文、筛选「仅看失败/告警」以及一键重试全部失败项。

| **验收测试场景**         | **预期结果**                                                 |
| ------------------------ | ------------------------------------------------------------ |
| **断点续翻验证**         | 20 条任务的批量文件翻到第 8 条时直接关闭进程，重启后点击「继续」，从第 9 条接着翻，前 8 条不重复消耗接口 |
| **代码块与占位符完整性** | 含 Python 围栏代码块、行内 ``cmd`` 与 `{count}` 变量的 `.md`/`.json` 翻完后，代码原样未动、JSON 仍可被 `json.loads` 合法解析 |
| **字幕时间轴零破坏**     | 100 条 `.srt` 翻完后导出双语版，编号与 `00:01:02,300 --> 00:01:05,100` 时间轴与原文件逐行一致，播放器加载正常 |
| **50 张大图连续翻译**    | 任务运行期间内存占用平稳（无随图片数量线性暴涨），单张损坏图仅标红该行而不中断剩余 49 张 |
| **UI 流畅度与取消响应**  | 批量任务高速运行时切换语言、滚动表格或点击「暂停」，界面零卡顿且 0.5 秒内停止拉取新单元 |