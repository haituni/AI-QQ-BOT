# 栖 Windows 应用入口

双击 `栖.exe`，通过图形窗口自动准备项目内 Python 3.12、依赖和表情语义模型，不要求预装 Python。准备完成后自动打开浏览器控制台；QQ 接入的 Docker Desktop 仍需单独安装并启动。

应用使用 Windows 自带的 .NET Framework 4.8 / WinForms，托盘菜单提供打开控制台、查看启动进度、查看日志、停止机器人并退出。重复双击唤回同一安装目录的启动器。关闭进度窗口只隐藏窗口，不中断安装；关闭浏览器不会停止服务。

安装引导位于 `scripts/windows/bootstrap.ps1`，从 uv 官方固定版本元数据读取 SHA256，校验压缩包后才执行安装工具。所有下载和运行文件留在项目目录。安装完整成功后才保存完成标记，未完成时可重试。

原生启动器、Python 服务与其 uv / curl / Docker 子进程均使用隐藏控制台方式启动。原始诊断写入 `logs/desktop.log`；窗口展示安装阶段及程序提供的失败原因。停止失败时不会直接退出托盘，以免误以为后台已经停止。

`栖.exe` 必须与 `scripts/`、`README.md` 同级。Windows 需为 x64，系统需提供 .NET Framework 4.8 与 Windows PowerShell 5.1，下载依赖使用系统 `curl.exe`。Docker Desktop 需启用 WSL 2 和 Linux 容器模式。

开发者在 Windows 上执行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/windows/build.ps1`，使用系统编译器生成应用；或使用 .NET SDK 执行 `dotnet build scripts/windows/Launcher.csproj -c Release`。`--preview` 参数可预览窗口而不下载或启动服务。

GitHub 仓库已附带 Windows x64 的 `栖.exe`。开发者重建后，可用 Python 3 执行 `scripts/package_desktop.py` 生成不含配置和运行数据的 ZIP。跨平台构建后仍需在 Windows 上验收原生托盘、下载流程和 QQ 登录。

## 下载与启动排错

安装下载自动继承显式的 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY 环境变量；未设置时读取系统固定代理。系统代理开启后无需在脚本里写死端口。本机管理接口始终绕过代理。仅浏览器扩展中的代理不属于系统代理；PAC 自动配置也不保证适用于所有下载工具，此时应显式设置代理环境变量。

表情模型下载失败会自动重试两次，窗口显示模型下载或校验失败；成功文件仍按固定 SHA256 校验并复用。启动前检测本机 8765 端口，若被其他服务占用，窗口提示先退出旧版本或停止占用程序，不会自动终止其他进程。其他诊断见 `logs/desktop.log` 和 `logs/server.log`。

本轮代理和启动提示修复已更新源码及仓库附带的 `栖.exe`，使用 macOS 上的 .NET SDK 8.0.426 与 .NET Framework 4.8 参考程序集交叉编译，构建无错误。原生托盘、首次安装、系统代理和 QQ 登录仍需 Windows 实机验收。
