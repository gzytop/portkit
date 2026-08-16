专治 `Address already in use` / `端口已被占用`：查清是谁占着端口，一键释放。

## 下载即用

下方 **PortKit.exe** 下载后双击即可运行，**不需要安装 Python**。

建议放到固定位置后右键「固定到任务栏」，端口被占用时一键打开。

## 主要功能

- **快速释放端口** — 输入框敲端口号回车，直接终止占用它的进程
- **搜索与筛选** — 按端口号 / PID / 进程名过滤，可只看常用开发端口，默认隐藏系统进程噪音
- **多选批量终止** — Ctrl / Shift 选多行一次处理
- **进程详情** — 双击查看完整命令行，判断「这个 node 是我哪个项目」
- **自动刷新** — 2 / 5 / 10 / 30 秒可选，适合等端口释放
- **命令行同源** — 仓库里的 `portkit.py` 可写进脚本：`python portkit.py check 3000 || python portkit.py kill 3000 -y`

## 安全设计

- `System` / `lsass.exe` / `svchost.exe` 等系统关键进程**默认拒绝终止**
- 先请求进程优雅退出，2 秒内没退才强杀，避免数据丢失
- 终止后**复查端口是否真的释放**，不给假结论
- `TIME_WAIT` 等无主残留会标注「稍后自动释放」，而非让你去杀一个杀不掉的东西

## 说明

- 终止其他用户或系统服务的进程需要**以管理员身份运行**
- PyInstaller 打包程序偶被杀软误报（自解压 + 调用 taskkill 的行为特征），可加白名单，或直接用源码运行 `python portkit_gui.py`

本二进制由 GitHub Actions 自动构建，构建前已在 Windows / macOS / Linux 上通过全部测试，并验证过 exe 能真实启动。

环境：Windows 10 / 11 x64。完整教程见 [README](https://github.com/{{REPOSITORY}}#readme)。

```
SHA256(PortKit.exe) = {{SHA256}}
```
