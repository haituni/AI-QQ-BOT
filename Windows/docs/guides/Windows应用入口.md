# 栖 Windows 应用入口

双击 `栖.exe`，通过图形窗口自动准备项目内 Python 3.12、依赖和表情语义模型，不要求预装 Python。准备完成后自动打开浏览器控制台；QQ 接入的 Docker Desktop 仍需单独安装并启动。

应用使用 Windows 自带的 .NET Framework 4.8 / WinForms，托盘菜单提供打开控制台、查看启动进度、查看日志、停止机器人并退出。重复双击唤回同一安装目录的启动器。关闭进度窗口只隐藏窗口，不中断安装；关闭浏览器不会停止服务。

安装引导位于 `scripts/windows/bootstrap.ps1`，从 uv 官方固定版本元数据读取 SHA256，校验压缩包后才执行安装工具。所有下载和运行文件留在项目目录。安装完整成功后才保存完成标记，未完成时可重试。

原生启动器、Python 服务与其 uv / curl / Docker 子进程均使用隐藏控制台方式启动。原始诊断写入 `logs/desktop.log`；窗口只展示安装阶段。停止失败时不会直接退出托盘，以免误以为后台已经停止。

`栖.exe` 必须与 `scripts/`、`README.md` 同级。Windows 需为 x64，系统需提供 .NET Framework 4.8 与 Windows PowerShell 5.1，下载依赖使用系统 `curl.exe`。Docker Desktop 需启用 WSL 2 和 Linux 容器模式。

开发者在 Windows 上执行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/windows/build.ps1`，使用系统编译器生成应用；或使用 .NET SDK 执行 `dotnet build scripts/windows/Launcher.csproj -c Release`。`--preview` 参数可预览窗口而不下载或启动服务。

构建完成后，可用 Python 3 执行 `scripts/package_desktop.py` 生成不含配置和运行数据的 ZIP。构建成品不纳入 Git。源码目录不附带可执行文件；构建后仍需在 Windows 上验收原生托盘、下载流程和 QQ 登录。
