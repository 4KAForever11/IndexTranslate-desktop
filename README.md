# IndexTranslate desktop

一款 Windows 桌面翻译客户端，调用 **B 站 Index-Translate** 公网翻译服务（`https://index-translate.bilibili.com/v1`），**无需 API Key、无需登录**。

支持**文本翻译**与**图片翻译**，流式输出、术语表、指令约束、历史记录、本地代理（可接入沉浸式翻译）、深浅色主题。

![IndexTranslate desktop 浅色主题](doc/screenshots/translate-light.png)

<p align="center">
  <img src="doc/screenshots/translate-dark.png" alt="深色主题" width="720">
</p>

---

## 目录

- [核心功能](#核心功能)
- [快速开始](#快速开始)
- [下载与安装](#下载与安装)
- [关于"Could not create temporary directory!"](#关于could-not-create-temporary-directory)
- [使用说明](#使用说明)
- [本地代理：接入沉浸式翻译](#本地代理接入沉浸式翻译)
- [从源码运行](#从源码运行)
- [自行打包](#自行打包)
- [项目结构](#项目结构)
- [数据存放位置](#数据存放位置)
- [自检与测试](#自检与测试)
- [已知限制](#已知限制)
- [致谢与许可](#致谢与许可)

---

## 核心功能

| 功能 | 说明 |
| --- | --- |
| 文本翻译 | 32 种语言，源语言可自动检测；SSE 流式输出，边翻边出 |
| 图片翻译 | 拖入 / `Ctrl+V` 粘贴截图 / 选择文件 / **http(s) 图片链接**，自动压缩超大图 |
| 术语表 | 自定义 `原词 → 译法` 对照，翻译时以硬性约束注入提示词；支持批量导入导出 |
| 指令约束 | 顶部「可选指令」输入框可临时追加要求（如"只输出三个词以内"），也可在设置里配默认值 |
| 翻译历史 | SQLite 本地存储，缩略图 + 原文 + 译文 + 耗时，支持搜索、一键回填、再次翻译 |
| 模型切换 | 35B（旗舰）/ 9B（均衡）/ 2B（极速）三档 |
| 本地代理 | 内置 OpenAI 兼容代理，可供沉浸式翻译等第三方工具调用 |
| 深浅色主题 | 跟随系统或手动指定 |
| 流式中断 | 随时点「停止」保留已生成的部分译文 |

---

## 快速开始

1. 下载 Release 里的压缩包并解压。
2. 双击 `IndexTranslate-desktop.exe`。
3. 在左侧粘贴原文，回车或按 `Ctrl+Enter` 开始翻译。

> 无需安装 Python，无需任何配置，服务端点默认已内置。

---

## 下载与安装

前往 **[Releases](https://github.com/4KAForever11/IndexTranslate-desktop/releases)** 下载：

| 产物 | 适用场景 | 说明 |
| --- | --- | --- |
| `IndexTranslate-desktop-<版本>-win64.zip` | **推荐** | 文件夹版，解压即用，启动最快，不会触碰系统临时目录 |
| `IndexTranslate-desktop-<版本>-win64-onefile.exe` | 单文件 | 只有一个 exe，体积较大，启动时会在自身目录解包 |

解压后目录结构：

```
IndexTranslate-desktop/
└─ IndexTranslate-desktop.exe   ← 双击运行
```

---

## 关于 "Could not create temporary directory!"

这是 PyInstaller **单文件版**的典型问题：单文件 exe 启动时要把自身解包到 `%TEMP%`，而很多国内 Windows 环境的用户目录被安全软件 / 权限策略锁死（`C:\Users\<中文用户名>\AppData\Local\Temp` 常见），于是弹出该报错。

**管理员运行就不报错**，正是这个原因——提权后的令牌有权写那个目录。

本项目做了两手处理：

1. **文件夹版（默认发布产物）根本不解包**，因此完全不受该问题影响；
2. **单文件版**在打包时指定了 `--runtime-tmpdir .IndexTranslateRuntime`，解包目录固定在 exe 所在目录，**完全绕开 `%TEMP%`**。

此外，应用在数据目录不可写时（受限配置 / 只读盘）会自动降级为内存数据库，**保证窗口仍能正常打开**。

---

## 使用说明

### 快捷键

| 快捷键 | 功能 |
| --- | --- |
| `Ctrl + Enter` | 开始翻译 |
| `Ctrl + Shift + C` | 复制译文 |
| `Ctrl + L` | 清空输入与结果 |
| `Ctrl + 1` ~ `Ctrl + 4` | 切换 翻译 / 历史 / 术语表 / 设置 |

### 图片翻译

- 把图片**拖进窗口**；
- 截图后 `Ctrl+V` 粘贴；
- 点工具栏的图片按钮选择文件；
- 点链接按钮输入 `http(s)` 图片地址。

图片超过 **2048 px** 或 **4 MB** 会自动等比压缩后上传；服务只接受 Base64，远程链接会先在本地下载再转 Base64。

### 术语表

在「术语表」页添加 `原词 → 译法`，翻译时以 `【硬性要求】专名/术语对照: A→B、C→D` 注入提示词。导入支持 `A=>B`、`A→B`、`A->B`、`A=B`、`A: B`、制表符分隔；`#` 开头为注释。

---

## 本地代理：接入沉浸式翻译

「设置 → 本地代理」可启动一个 OpenAI 兼容的本地服务（默认 `127.0.0.1:8080`）：

```
http://127.0.0.1:8080/v1
```

在沉浸式翻译的「自定义 API」里填入该地址，模型名填 `Index-Translate-35B-A3B` 即可。代理会转发请求并自动关闭模型的思维链输出。

---

## 从源码运行

需要 Python 3.10+。

```bash
git clone https://github.com/4KAForever11/IndexTranslate-desktop.git
cd IndexTranslate-desktop

pip install -r requirements.txt
python run.py
```

依赖：`PyQt6`、`PyQt6-Fluent-Widgets`、`httpx`、`Pillow`（Windows 另需 `pywin32`）。

---

## 自行打包

```powershell
# 文件夹版（默认，推荐）
powershell -ExecutionPolicy Bypass -File build.ps1

# 单文件版
powershell -ExecutionPolicy Bypass -File build.ps1 -OneFile

# 两者都出
powershell -ExecutionPolicy Bypass -File build.ps1 -Both
```

或双击 `build.cmd`。产物在 `dist/`。

---

## 项目结构

```
IndexTranslate-desktop/
├─ app/
│  ├─ core/           # 与界面无关的逻辑
│  │  ├─ api_client.py     # OpenAI 兼容客户端 + SSE 解析 + 重试
│  │  ├─ prompt_builder.py # 提示词构造（与 call_api.py 保持一致）
│  │  ├─ image_utils.py    # 图片压缩 / Base64 / 缩略图 / 远程下载
│  │  ├─ history_db.py     # SQLite 翻译历史
│  │  ├─ glossary_db.py    # SQLite 术语表
│  │  ├─ proxy_server.py   # 本地 OpenAI 兼容代理
│  │  ├─ languages.py      # 32 种语言定义
│  │  └─ config.py         # 配置与设置持久化
│  ├─ workers/        # 后台翻译线程
│  ├─ ui/             # 四个页面 + 主题 + 自定义控件
│  └─ main.py         # 程序入口
├─ assets/            # 图标
├─ doc/               # 设计文档与截图
├─ tests/             # 单元测试
├─ tools/make_icon.py # 图标生成
├─ build.ps1 / build.cmd
└─ run.py
```

---

## 数据存放位置

默认 `%LOCALAPPDATA%\IndexTranslate`：

| 文件 | 内容 |
| --- | --- |
| `settings.json` | 设置（主题、模型、接口地址、指令…） |
| `history.db` | 翻译历史 |
| `glossary.db` | 术语表 |

设置环境变量 `INDEX_TRANSLATE_HOME` 可改为自定义目录（便携使用、U 盘携带都适用）：

```powershell
$env:INDEX_TRANSLATE_HOME = "D:\Tools\IndexTranslate"
```

---

## 自检与测试

```powershell
# 程序自检：真实跑一遍文本 + 图片翻译，结果写入数据目录 selftest.json
.\dist\IndexTranslate-desktop\IndexTranslate-desktop.exe --selftest

# 单元测试
python -m unittest discover -s tests
```

测试覆盖：提示词与 `call_api.py` 逐字节对齐、术语表解析、图片压缩、远程图片下载、SQLite 读写、SSE 解析、代理转发。

---

## 已知限制

- 依赖公网免费服务，可能限流或临时不可用；长文本需要多等几秒。
- 提示词逻辑与 `call_api.py` 严格对齐，因此输出风格偏"直译"。
- 服务端只接受 Base64 图片，不接受远程地址（客户端会先下载再转换）。
- 暂不支持批量图片翻译与输入草稿自动保存。

---

## 致谢与许可

- 翻译服务由 **B 站 Index-Translate** 提供，遵循其公开使用条款。
- 提示词与调用方式参考仓库内的 [`call_api.py`](call_api.py)。
- 界面基于 [PyQt6-Fluent-Widgets](https://github.com/QtExcel/PyQt-Fluent-Widgets)。
- 本项目以 MIT 许可证发布。
