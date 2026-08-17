# AGENTS.md — portkit 项目约定

> 给 AI 助手看的项目上下文。新会话接手时先读这份，再读 `.impeccable.md`（设计上下文）。

## 这是什么

解决「端口被占用」的小工具。两个入口共用同一套核心逻辑：

- `portkit.py` — 核心逻辑 + 命令行入口（采集 / 过滤 / 终止都在这）
- `portkit_gui.py` — tkinter 图形界面，**只做展现，不重复实现任何逻辑**
- `theme.py` — 设计令牌（OKLCH 色板推导、字号阶梯、间距标尺）

仓库：https://github.com/gzytop/portkit　本地：`D:\桌面\git_project\portkit`

## 不可动摇的约定

违反下面任何一条都要先跟用户确认，不要自行决定。

### 1. 零第三方依赖

只用 Python 标准库 + 系统自带命令。**不要引入 requests / rich / Pillow / psutil 等任何包**。
CI 里有一步专门检查 `requirements.txt` / `pyproject.toml` 不存在，出现即失败。

需要新能力时的做法：先找标准库方案。已有先例：
- 没用 Pillow → `make_icon.py` 手写 ICO/BMP 字节
- 没有 CSS → `theme.py` 自己实现 OKLCH → sRGB 转换

PyInstaller 是唯一例外，只装在 `.venv/`，仅打包时用，不属于运行时依赖。

### 2. 杀进程的安全边界

这个工具会终止进程，误杀系统关键进程可能导致蓝屏。以下逻辑改动需格外谨慎：

- `PROTECTED_PROCESS_NAMES` / `PROTECTED_PIDS` 是保护名单，默认拒绝终止，只有显式 `--force` 才越过
- 终止流程必须「先温和后强硬」：先 SIGTERM/`taskkill`，2 秒不退再强杀
- 终止后必须复查端口是否真的释放，**不许直接返回成功**
- `check` 子命令的退出码约定（占用=1，空闲=0）**是对外契约**，README 教用户用 `check || kill` 写启动脚本，改了会静默影响所有人

### 3. 测试不用 mock 端口和进程

这个工具的全部价值在于跟真实操作系统打交道，mock 掉等于什么都没测。
`tests/smoke_test.py` 的做法：让系统分配一个空闲端口、起真实子进程、再真的终止它。

### 4. 跨平台不能只测 Windows

Windows 走 `netstat`/`tasklist`/`taskkill`，POSIX 走 `lsof`/`ss` + 信号。
**只在本机（Windows）测等于 POSIX 分支从未被验证。** 必须靠 CI 的四平台矩阵兜住。

CI 故意不在 Ubuntu 上装 `lsof`，为的是让「退回 `ss`」这条备选路径真的被执行到。

### 5. 设计约定

详见 `.impeccable.md`。三条最关键：

- **颜色只编码状态，不做装饰** —— 每种着色对应一个用户需要区分的语义
- **破坏性操作要有视觉分量** —— 终止（实心红）> 刷新（实心蓝）> 详情/复制（描边）
- **对比度必须达 WCAG AA** —— 已落成测试，两套主题上界面真实出现的每个前景/背景组合都会被断言

改配色后跑 `python theme.py` 看数值。

## 已经踩过的坑（别重复）

这些都是真实发生过、已修复的问题。改相关代码时留意别退化：

| 坑 | 症状 | 现在的处理 |
|---|---|---|
| POSIX 僵尸进程 | 杀完进程后误报「仍在运行」。`os.kill(pid, 0)` 对僵尸依然成功 | `_is_zombie_process()` 查 `/proc/<pid>/stat` 或 `ps -o state=` |
| cp1252 控制台 | 英文版 Windows / CI runner 上打印中文抛 `UnicodeEncodeError`，构建直接失败 | 每个有中文输出的入口都调 `allow_non_ascii_output()` 之类的函数先切 UTF-8 |
| 无控制台闪黑窗 | GUI 里调 `netstat` 每次闪一个黑框，开自动刷新时疯狂闪 | `subprocess` 一律传 `creationflags=SUBPROCESS_NO_WINDOW_FLAGS` |
| 中文 exe 文件名 | cmd 用 GBK 解析 UTF-8 的 `.bat`，`if exist "dist\中文.exe"` 永远判假 | exe 名用 ASCII `PortKit.exe`，中文名走 `version_info.txt` |
| clam 主题 Combobox | 暗色下 `readonly` 状态退回默认配色，文字与底色撞成一片像空白框 | `style.map` 里显式映射 `readonly` 的前景/背景 |
| DPI 感知下布局挤压 | 打包成 exe 后中文字体变宽，`pack` 压缩最后放入的元素，文字消失 | 控件按语义分栏，别往一栏里堆；`LayoutFitTests` 自动断言 |
| tkinter 回调在窗口销毁后触发 | 关窗后 `after` 回调访问已销毁控件抛 `TclError` | `handle_window_close()` 幂等地取消所有排程 |
| 内联脚本进 YAML | 无法本地验证，且享受不到源码里的统一编码处理 | 抽成 `.github/scripts/*.py` |

## 常用命令

```bash
# 测试（53 项，约 40 秒）
python tests/smoke_test.py

# 看配色对比度数值
python theme.py

# 打包 exe（自动建 venv、装 PyInstaller、生成图标）
build_exe.bat            # 产物 dist\PortKit.exe

# 发版：打 tag 即触发 CI 构建并创建 Release
git tag v1.2.2 && git push origin v1.2.2
```

## 环境事实

- 本机 Windows + PowerShell（**不支持 heredoc**，写多行文本用 `Set-Content` + `[System.IO.File]::WriteAllText`）
- Python `D:\python\python.exe`，GUI 无窗口版 `D:\python\pythonw.exe`
- `gh` 已登录账号 `gzytop`，有 `repo` + `workflow` 权限
- 控制台是 GBK，中文输出常显示为乱码——**这通常只是显示问题，不代表程序出错**，判断成败看退出码
- 跑 GUI 测试或截图后，记得清理遗留的 `http.server` 测试服务与 `PortKit`/`pythonw` 进程

## 提交与发布习惯

- 提交信息写「为什么」而非「改了什么」，说明根因和权衡
- 用户没明确要求就不要提交
- **UI 或功能改动后，如果需要用户能下载到，必须打 tag** —— 只推 main 不会更新 Release
- 仓库不含构建产物：`.venv/`、`build/`、`dist/`、`app_icon.ico` 都在 `.gitignore` 里
