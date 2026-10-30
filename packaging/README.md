# 打包层（packaging）

微信待办（WeChat Task Hub）的 Windows 绿色版打包入口：PyInstaller **onedir**
+ 无控制台窗口（windowed），入口是项目根 `launcher.pyw`，产物
`dist\WeChatTaskHub\WeChatTaskHub.exe`。

> 目录分层契约见 `docs/目录分层.md`；运行时路径唯一事实源是 `app/paths.py`，
> 版本唯一事实源是 `app/version.py`。本目录只负责「把项目打成固定布局」。

---

## 1. 产物目录契约（exe 同级 = dist\WeChatTaskHub\）

| 路径 | 来源 | 升级时 | 说明 |
| --- | --- | --- | --- |
| `WeChatTaskHub.exe` | PyInstaller | **替换** | ASCII 名，GUI 子系统，无黑框 |
| `_internal\` | PyInstaller 运行时 | **替换** | Python 解释器、依赖、包内 fallback 的 web/assets |
| `web\` | 项目 `web/`（同时打进 `_internal` 兜底） | **替换/热更** | 前端静态站点，外置优先 |
| `assets\` | 项目 `assets/`（同时打进 `_internal` 兜底） | **替换** | 图标等只读资源 |
| `tools\` | 项目 `tools/`（**只外置，不进包**） | **替换** | `ngrok\ngrok.exe`、`cloudflared.exe`（约 88MB）；`tunnel.py` 已作为模块打进 exe |
| `data\` | 首次运行生成 | **保留** | SQLite 数据库、pid、日志、`tunnel.json` |
| `.env` | 首次双击生成（复制 `.env.example` + 随机 `APP_TOKEN`） | **保留** | 用户配置；`--serve/--stop/--version/--tunnel` 等管理命令不生成 |
| `.env.example` | 项目根 | 替换 | 配置模板 |
| `README.md`、`启动说明.txt` | 项目根 / 本目录 | 替换 | 用户文档 |

包内还有一份 `_internal\web`、`_internal\assets` 作兜底：外置目录被删或改坏时
`app/paths.py` 会回落到 `BUNDLE_DIR`，保证程序仍能启动。

## 2. 构建环境

- Windows 10/11 x64
- Python 3.11+ 64 位（推荐 python.org 安装包或 `.venv`；Windows Store 版可尝试，见 §7）
- 构建机需要能访问 PyPI（安装 PyInstaller 与运行期依赖）

```powershell
# 项目根执行
python -m pip install -r requirements-build.txt
```

`requirements-build.txt` = `-r requirements.txt` + `pyinstaller>=6.10`。
运行期依赖必须先装齐，否则 PyInstaller 静态分析会漏模块（`openai`、`brotli`、
`tzdata` 等）。

## 3. 构建

```powershell
# 项目根执行
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

参数：

| 参数 | 说明 |
| --- | --- |
| `-Python <path>` | 指定构建解释器，默认 `python`；可传 `.venv\Scripts\python.exe` |
| `-SkipInstall` | 跳过 PyInstaller 安装检查（CI 已预装时用） |
| `-NoZip` | 只产出 `dist\WeChatTaskHub\`，不打 `releases\*.zip` |

脚本流程：

1. 从 `app/version.py` 读 `__version__`（唯一事实源），生成
   `packaging\build\version_info.txt`（公司名/产品名/文件版本/产品版本）；
2. 检查 PyInstaller，缺失且未 `-SkipInstall` 时执行
   `python -m pip install -r requirements-build.txt`；
3. `pyinstaller --clean --noconfirm --distpath dist --workpath build/pyinstaller packaging\WeChatTaskHub.spec`
   （spec 通过环境变量 `WECHAT_TASK_VERSION_FILE` 读取版本资源）；
4. 把 `web\`、`assets\`、`tools\`、`.env.example`、`README.md`、`启动说明.txt`
   复制到 `dist\WeChatTaskHub\`；
5. 打 `releases\WeChatTaskHub-<版本>-win64.zip`，排除 `data\`、`.env`、
   `__pycache__`、`*.log`；
6. 打印最终目录树与下一步提示。

构建日志：`output\packaging\build.log`（脚本内 Start-Transcript，每次覆盖）。

> **脚本编码**：`build.ps1`、`WeChatTaskHub.spec` 等含中文的文件统一保存为
> **UTF-8 with BOM**。Windows PowerShell 5.1 会把无 BOM 的 UTF-8 按 ANSI
> 解码，中文尾字节可能吞掉引号，直接报 `ParserError`。`pwsh`（PowerShell 7）
> 与 Windows PowerShell 5.1 均可运行；唯一例外是脚本生成的
> `packaging\build\version_info.txt`——它必须是无 BOM UTF-8（PyInstaller 的要求）。

## 4. spec 说明（`WeChatTaskHub.spec`）

- `Analysis([launcher.pyw], pathex=[项目根], datas=[web→web, assets→assets])`
- `hiddenimports`：
  - `app.*`：扫描 `app/*.py` 全量纳入（入口是间接导入，避免静态分析漏模块）
  - `openai`、`brotli`、`tzdata`、`tools.tunnel`
  - `uvicorn` 全量子模块（`collect_submodules("uvicorn")`，uvicorn 大量动态导入）
- `collect_data_files("tzdata")`：Windows 无系统时区库，`zoneinfo` 依赖它
- `EXE(console=False, icon=assets/wechat-task.ico, name="WeChatTaskHub")`
  + `COLLECT(upx=False, name="WeChatTaskHub")`
- 不启用 UPX：降低杀软误报概率

## 5. 分发

把 `releases\WeChatTaskHub-<版本>-win64.zip` 发给用户，解压到任意**可写**目录
（不要放 `C:\Program Files`），双击 `WeChatTaskHub.exe`。首次运行自动生成
`data\`、`.env` 并打开浏览器。

## 6. 升级

**只替换** `WeChatTaskHub.exe`、`_internal\`、`web\`、`assets\`、`tools\`、
`README.md`、`启动说明.txt`；**保留** `data\` 与 `.env`。

前端小改动可以只替换 `web\`（外置优先于包内兜底），不必重新打包。

## 7. 已知注意事项

- **Windows Store Python**：本机是 Store 版 3.13.14，已实测用
  `pyinstaller 6.22.3` 完整构建成功（日志见 `output\packaging\build.log`）。
  若换机器构建或产物启动失败，改用 python.org 官方安装包 + venv：

  ```powershell
  # 安装 python.org 的 Python 3.12/3.13 x64 后：
  py -3.13 -m venv .venv
  .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
  powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Python .\.venv\Scripts\python.exe
  ```

- **杀软误报**：未签名 exe 偶发误报，添加信任即可；不要开 UPX。
- **中文路径**：构建与运行均支持；但分发目录避免超长路径。
- **`--version` 无输出**：windowed 子系统的 exe 没有 stdout，`print` 会被忽略，
  以退出码判断（0 = 正常）。
- **`.env` 首启生成（已实现）**：双击 `WeChatTaskHub.exe`（无参启动）时
  `app/config.py` 的 `ensure_env_file()` 会复制 `.env.example` 并把
  `APP_TOKEN` 换成随机串（`secrets.token_urlsafe(16)`），幂等不覆盖已有配置；
  启动器随即用这份 token 打开网页。`--serve` / `--tunnel` / `--stop` /
  `--version` 等管理命令**不会**生成 `.env`（无写文件副作用）。
- **tools 不进包**：`tools\tunnel.py` 作为 Python 模块 hiddenimport 进 exe
  （`WeChatTaskHub.exe --tunnel` 调用），`ngrok.exe`/`cloudflared.exe` 只外置。

## 8. 冒烟验证（构建后）

```powershell
$exe = "dist\WeChatTaskHub\WeChatTaskHub.exe"
& $exe --version                       # 退出码 0
& $exe --serve --demo --port 8799      # 前台起服务（另开窗口）
# 浏览器/curl 访问 http://127.0.0.1:8799/api/health 应为 200
& $exe --stop                          # 停止服务
```

注意：`--stop` 会弹 MessageBox（无服务时），自动化时设置
`$env:WECHAT_TASK_NO_DIALOG=1`。
