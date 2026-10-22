# 栖 macOS 应用入口

双击项目目录中的 `栖.app`，首次用图形窗口下载经过 SHA256 校验的 uv，自动安装项目内 Python 3.12、依赖与表情模型，完成后打开浏览器控制台。使用发行应用不需要系统 Python 或 Xcode；构建源码才需要 Xcode Command Line Tools。

菜单栏「栖」提供打开控制台、查看启动进度、查看日志及停止机器人并退出。状态每 15 秒刷新。关闭窗口只隐藏进度，不中断安装；关闭浏览器不会停止服务。退出时等待当前操作完成，再停止本目录服务和 QQ 虚拟机；停止失败时保留应用供重试。

安装完整成功后才保存 `runtime/desktop-ready.json`；安装中断、标记损坏或依赖文件更新都会触发下次重新检查安装。日志位于 `logs/desktop.log`，超过大小后轮换。下载失败后选择「重试启动」，不会把半成品当作安装完成。

应用必须与 `scripts/`、`README.md` 同级，不能单独移入 Applications。当前应用针对构建机器的 CPU 架构生成，最低部署目标 macOS 12；Lima 虚拟机是否可用仍取决于系统虚拟化支持。程序仅做本地临时签名，未经过 Apple 公证。

开发者构建：双击 `构建桌面应用.command`，或运行 `python3 scripts/build_macos_app.py`。源码位于 `scripts/macos/Launcher.swift`，控制助手是 `scripts/desktop.py`。`--preview` 参数可预览安装窗口而不启动服务或下载；该模式不用于日常运行。

打包：`python3 scripts/package_desktop.py`，生成的 ZIP 不含个人配置或运行数据。源码仓库忽略构建应用及 ZIP，保留构建脚本。备用 `.command` 入口用于排错。
