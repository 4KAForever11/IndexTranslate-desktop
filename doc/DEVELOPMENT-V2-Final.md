第五阶段是「大而全」版本的**终极收官层**：用「轻核心 + Sidecar 独立进程扩展包」**拿下重型多模态能力（PDF 保留排版双语对照、本地 OCR 原位贴片覆盖、WASAPI 系统音频实时悬浮字幕），并用**多端同步与自动更新补齐跨设备工程底座。

这一阶段最关键的工程决策，是**绝对不要把 PyMuPDF、ONNX Runtime、OCR 和语音识别直接打进主程序 exe**：

- 直接打包会让主程序从几十 MB 膨胀到 500 MB 至 1.5 GB，启动变慢且极易触发 DLL / OpenMP 冲突；
- 重型 C++ 推理库一旦在某个畸形 PDF 或异常音频帧上发生段错误（Segfault），会连带把你的系统托盘、本地沉浸式翻译代理和主窗口一起拉崩。

正确做法是**主程序永远保持纯 API 轻核心（体积控制在 60 MB 以内）**，将三大重型能力拆成按需下载的独立 Sidecar 子进程扩展包，存放在数据目录的 `extensions/` 下（天然兼容你已有的 `INDEX_TRANSLATE_HOME` 便携模式）。



IndexTranslate-desktop/
├─ app/
│  ├─ core/
│  │  ├─ extensions/              # [新增] 轻核心侧扩展管理器（零重型依赖）
│  │  │  ├─ __init__.py
│  │  │  ├─ manifest.py           # 扩展清单协议（版本号、SHA-256、兼容宿主版本）
│  │  │  ├─ manager.py            # 扩展在线下载、哈希校验、原子解压与卸载
│  │  │  └─ sidecar_client.py     # 子进程拉起、JSONL 管道通信、空闲自动释放内存
│  │  ├─ sync/                    # [新增] 多端配置与资产同步
│  │  │  ├─ __init__.py
│  │  │  ├─ snapshot.py           # SQLite 资产（术语/TM/生词本）序列化与记录级合并
│  │  │  └─ webdav_sync.py        # WebDAV（坚果云/NAS/Alist）与 GitHub Gist 同步
│  │  └─ updater.py               # [新增] GitHub Releases 版本检测与自更新脚本
│  └─ ui/
│     ├─ extensions_page.py       # [新增] 扩展中心页（安装/更新/启停/内存状态）
│     ├─ overlay_renderer.py      # [新增] 截屏原位译文贴片覆盖层
│     └─ live_subtitle_bar.py     # [新增] 置顶半透明实时悬浮字幕条
└─ extensions_src/                # [新增] 独立构建的三大重型扩展源码（单独打包发布）
   ├─ ext-pdf-bilingual/          # PyMuPDF 版面提取与双语 PDF 合成
   ├─ ext-ocr-overlay/            # RapidOCR ONNX 本地文字与坐标识别
   └─ ext-live-asr/               # PyAudioWPatch + sherpa-onnx 实时回环转写



## 二、四大核心模块开发思路与避坑要点

### 模块 1：Sidecar 进程通信与生命周期守护（`app/core/extensions/`）

不要用 `importlib` 在主进程内动态加载重型依赖，也不要盲目给每个扩展开本地 HTTP 端口（容易弹出 Windows Defender 防火墙拦截弹窗）。最适合 Windows 桌面端的通信方案是 **标准输入输出 JSON Lines（`stdin`/`stdout`）+ Win32 Job Object**：

1. **进程通信协议**：主程序每写一行 JSON 请求到子进程 `stdin`（带 `req_id`、`method`、`params`），子进程每处理完一帧或一条任务就往 `stdout` 写一行 JSON 响应（支持流式推送进度或实时字幕），日志统一走 `stderr`。
2. **杜绝僵尸进程（Win32 Job Object）**：直接用仓库已有的 `pywin32` 创建一个带 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 标志的 Job Object，把所有拉起的 `ext-*.exe` 绑定进去。哪怕主程序崩溃或被任务管理器强杀，Windows 内核也会在毫秒内自动清理全部子进程。
3. **空闲自动回收内存（Idle Timeout）**：OCR 和语音模型常驻会吃掉 300 MB 至 1 GB 内存。`SidecarClient` 记录最后一次调用时间，超过阈值（如 5 分钟无调用）自动优雅关闭子进程释放内存，下次触发再按需冷启动。



from __future__ import annotations
import json
import subprocess
import threading
import time
import win32job

CREATE_NO_WINDOW = 0x08000000


class SidecarClient:
    """基于 stdin/stdout JSONL 与 Win32 Job Object 的隔离扩展客户端"""

    def __init__(self, exe_path: str, idle_timeout_sec: int = 300):
        self.exe_path = exe_path
        self.idle_timeout_sec = idle_timeout_sec
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._last_used = 0.0
        self._job = self._create_kill_on_close_job()
    
    @staticmethod
    def _create_kill_on_close_job():
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        return job
    
    def ensure_started(self) -> None:
        if self._proc and self._proc.poll() is None:
            return
        self._proc = subprocess.Popen(
            [self.exe_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=CREATE_NO_WINDOW,
        )
        win32job.AssignProcessToJobObject(self._job, int(self._proc._handle))
        self._last_used = time.monotonic()
    
    def call(self, method: str, params: dict) -> dict:
        with self._lock:
            self.ensure_started()
            assert self._proc and self._proc.stdin and self._proc.stdout
            payload = json.dumps({"method": method, "params": params}, ensure_ascii=False)
            self._proc.stdin.write(payload + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
            self._last_used = time.monotonic()
            if not line:
                raise RuntimeError("Sidecar 扩展进程意外退出")
            return json.loads(line)
    
    def reclaim_if_idle(self) -> None:
        with self._lock:
            if self._proc and (time.monotonic() - self._last_used) > self.idle_timeout_sec:
                self.stop()
    
    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None





### 模块 2：三大重型扩展包的技术选型与协同流

这三套扩展都只负责「识别 / 解析 / 排版」，**所有翻译调用全部回传给主程序的 `TranslationPipeline` 或第三步的 `jobs.db` 队列**，从而直接复用你已做好的术语表、翻译记忆库、缓存、限流与降级。

| 扩展包            | 推荐轻量技术栈                                               | 预估扩展包体积 | 核心实现闭环                                                 |
| ----------------- | ------------------------------------------------------------ | -------------- | ------------------------------------------------------------ |
| PDF 双语对照      | `PyMuPDF (fitz)` + 开源中文字体子集（如思源黑体精简版）      | 约 35–50 MB    | 提取页面文本块与 `bbox`，过滤公式/页眉页脚 → 走第三步 `BaseDocumentParser` 接入批量队列翻译 → 支持「原位重排单语版」**（`insert_htmlbox` 自动缩放字号）和**「左右/交错双页对照版」（复杂论文表格零错位） |
| 本地 OCR 原位贴片 | `RapidOCR-ONNXRuntime`（PP-OCRv4 Mobile 模型，纯 CPU 极速）  | 约 30–45 MB    | 承接第二步的 `capture_overlay.py` 截图 → 扩展返回多边形坐标框与合并段落 → 主程序译完后由 `overlay_renderer.py` 采样原图边界背景色，在原文位置绘制自适应字号的半透明/不透明贴片，支持按键切换「原文 / 译文透视」 |
| 系统音频实时字幕  | `PyAudioWPatch`（原生 WASAPI 回环，免虚拟声卡） + `Silero-VAD` + `sherpa-onnx`（SenseVoice / Whisper ONNX） | 约 160–260 MB  | 抓取系统扬声器输出 → VAD 静音过滤与断句（防幻觉复读） → 扩展流式吐出原文片段 → 主程序附带前 2 句上下文送入 `2B`/`9B` 极速翻译 → 渲染到置顶可穿透鼠标的 `live_subtitle_bar.py` |

**关键选型避坑**：做语音识别千万不要直接引 `PyTorch` + `openai-whisper`，打包会直接飙破 2 GB；改用纯 C++ ONNX 后端的 `sherpa-onnx`（支持 SenseVoice 多语种高精度识别），CPU 上也能做到低延迟，体积砍掉近九成。



### 模块 3：多端配置与资产同步（`app/core/sync/`）

**最大避坑点：千万不要把 SQLite 的 `.db` 二进制文件直接传上 WebDAV 覆盖同步。** 两台电脑同时打开软件或一端没及时拉取时，直接覆盖二进制库必然导致数据丢失甚至数据库损坏。

正确同步架构分为三层：

1. **明确同步白名单**：
   - **同步**：`glossary.db`（术语表）、`tm_db`（已审校翻译记忆）、`vocab_db`（生词本）、自定义指令预设、通用偏好配置。
   - **不同步**：`cache_db`（临时缓存）、`jobs.db`（含本机绝对路径）、`extensions/` 二进制包、TTS 音频缓存、窗口坐标。
2. **逻辑记录级同步（软删除 + 时间戳）**：
   - 给需要同步的表统一加 `uuid`、`updated_at` 和 `deleted`（软删除标记）三列。
   - 同步时导出为压缩 JSON 快照上传到 WebDAV（优先兼容国内最好用的**坚果云**、Alist、Nextcloud）或私密 GitHub Gist。
   - 拉取合并时按 `uuid` 逐条对比 `updated_at` 执行 **LWW（Last-Write-Wins）** 合并，合并前自动在本地备份一份 `.bak`，确保万无一失。
3. **密钥安全处理**：WebDAV 密码或自定义引擎 API Key 用 Windows 原生 `CryptProtectData`（DPAPI，`pywin32` 自带）或 `keyring` 加密存储，同步配置时默认剔除明文密钥（或允许用户设置独立端到端同步口令加密）。



### 模块 4：工程化打磨与分发收官

- **自动更新器（`updater.py`）**：启动后异步请求 GitHub Releases API 对比语义化版本；提供官方直连与镜像加速切换。用户确认更新后，下载发布包并校验 `SHA-256`，生成一个极简临时更新批处理脚本等待主进程退出、覆盖文件并自动重启（单文件版与文件夹版分别处理）。
- **视觉与双形态体验**：利用 `PyQt6-Fluent-Widgets` 开启 Win11 原生 Mica / Acrylic 背景（Win10 自动优雅回退纯色），并增加「**迷你悬浮条模式**」（仅保留单行输入框与快捷切窗按钮），兼顾极简与重度工作台两类用户。
- **界面多语言（i18n）与构建流水线**：用轻量 JSON 词表或 Qt `.ts`/`.qm` 抽离界面文案（默认支持简中/繁中/英文）；扩展 `build.ps1`，支持 `-Target host` 与 `-Target extensions` 分离构建，并在 `--selftest` 中新增扩展协议握手与 WebDAV 连通性自检。

下面的系统架构图展示了轻核心宿主、Sidecar 隔离进程管理层、三大重型扩展包以及跨设备同步层之间的通信协议与资源边界。点击顶部按钮可以切换观察不同工作负载下的运行状态与内存回收机制：

![image-20261127225230836](C:\Users\用户\AppData\Roaming\Typora\typora-user-images\image-20261127225230836.png)



![image-20261127225343526](C:\Users\用户\AppData\Roaming\Typora\typora-user-images\image-20261127225343526.png)



## 三、收官开发节奏与终极验收清单

建议按 4 个独立里程碑收尾，每完成一项就发一个小版本收集真实反馈：

1. **闭环 A（扩展基础设施）**：先落 `manifest.py`、`manager.py`、`sidecar_client.py`、`extensions_page.py`，配合最轻量的 `ext-ocr-overlay` + `overlay_renderer.py` 跑通第一条 Sidecar 端到端闭环（截图 → 本地 OCR → 原位译文贴片）。
2. **闭环 B（文档终极形态）**：开发 `ext-pdf-bilingual`，直接挂进第三步的 `BaseDocumentParser` 和 `batch_page.py`，支持双页对照与原位重排导出。
3. **闭环 C（音视频实时化）**：开发 `ext-live-asr` 与 `live_subtitle_bar.py`，打通 Windows 系统音频回环抓取、VAD 断句与置顶双语悬浮字幕。
4. **闭环 D（工程化收官）**：补齐 WebDAV 逻辑快照同步、GitHub Releases 自动更新、Win11 Mica 外观、多语言界面以及 `build.ps1` 分体打包脚本。



| **终极验收场景**            | **预期结果**                                                 |
| --------------------------- | ------------------------------------------------------------ |
| **主安装包体积红线**        | 完整包含 1–5 阶段宿主代码打包后，主程序 ZIP 体积依然稳定在 60 MB 以内 |
| **子进程隔离与防泄漏**      | 手动在任务管理器强杀 `ext-ocr-overlay.exe`，主程序弹出友好降级提示不崩溃；强杀主程序后，系统内无任何残留 `ext-*.exe` 僵尸进程 |
| **空闲内存释放**            | 关闭实时悬浮字幕或停止使用 OCR 超过设定空闲时间后，任务管理器中对应扩展进程自动消失，内存完全归还系统 |
| **多端并发冲突安全**        | 两台电脑分别新增不同术语与生词后先后点击 WebDAV 同步，两端均完整保留双方新增记录，无数据库损坏与覆盖丢失 |
| **复杂 PDF 与实时字幕压力** | 30 页含图表与双栏的英文论文 PDF 双语导出成功且文字不重叠；连续播放 30 分钟外文视频，实时字幕无明显内存增长与静音幻觉乱码 |



做到这里，你的 `IndexTranslate-desktop` 就走完了从「轻量免费客户端」→「统一多引擎调度中枢」→「系统级划词/截屏助手」→「全格式文档批量工作台」→「查词/润色/记忆/生词闭环」→「隔离式重型多模态扩展生态」的完整演进路线，既具备顶级全能翻译软件的所有能力，又避开了桌面端最致命的臃肿与崩溃陷阱。