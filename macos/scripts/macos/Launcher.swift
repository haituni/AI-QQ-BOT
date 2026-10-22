import AppKit
import CryptoKit
import Darwin

final class Launcher: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let root = Bundle.main.bundleURL.deletingLastPathComponent()
    var item: NSStatusItem!
    let state = NSMenuItem(title: "正在启动…", action: nil, keyEquivalent: "")
    let preview = ProcessInfo.processInfo.arguments.contains("--preview")
    var launcherLock: Int32 = -1
    var busy = false
    var timer: Timer?
    var window: NSWindow!
    let heading = NSTextField(labelWithString: "栖 · 正在准备")
    let phase = NSTextField(wrappingLabelWithString: "首次启动会下载运行环境，请保持网络连接。")
    let hint = NSTextField(wrappingLabelWithString: "准备完成后会自动打开浏览器控制台。")
    let progress = NSProgressIndicator()
    let retry = NSButton(title: "重试启动", target: nil, action: nil)
    let logs = NSButton(title: "查看日志", target: nil, action: nil)

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        makeWindow()
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "栖"
        let menu = NSMenu()
        menu.addItem(state)
        menu.addItem(.separator())
        for (title, action, key) in [
            ("打开控制台", #selector(openConsole), "o"),
            ("查看启动进度", #selector(showProgress), "p"),
            ("查看运行日志", #selector(openLogs), "l"),
            ("停止机器人并退出", #selector(quit), "q")
        ] {
            let entry = NSMenuItem(title: title, action: action, keyEquivalent: key)
            entry.target = self
            menu.addItem(entry)
        }
        item.menu = menu
        if !preview {
            do {
                let runtime = root.appendingPathComponent("runtime")
                try FileManager.default.createDirectory(at: runtime, withIntermediateDirectories: true)
                launcherLock = Darwin.open(runtime.appendingPathComponent("desktop-launcher.lock").path, O_CREAT | O_RDWR, 0o600)
                guard launcherLock >= 0 else { throw NSError(domain: "LauncherLock", code: 1) }
                if flock(launcherLock, LOCK_EX | LOCK_NB) != 0 {
                    let alert = NSAlert()
                    alert.messageText = "栖已在运行"
                    alert.informativeText = "请使用菜单栏中的「栖」打开控制台或查看进度。"
                    alert.runModal()
                    Darwin.exit(0)
                }
            } catch {
                fail("无法写入项目目录，请将整个文件夹放到可写位置后重试。")
                return
            }
        }
        guard FileManager.default.fileExists(atPath: root.appendingPathComponent("scripts/bootstrap.py").path) else {
            fail("找不到项目文件。请把栖.app放回与 scripts、README.md 同级的目录。")
            return
        }
        if preview {
            heading.stringValue = "栖 · 首次安装"
            phase.stringValue = "准备项目内 Python、依赖和表情语义模型…"
            hint.stringValue = "界面预览，不下载组件或启动机器人。实际安装完成后会打开浏览器控制台。"
            retry.isEnabled = false
            progress.startAnimation(nil)
            showProgress()
            return
        }
        openConsole()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func makeWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 280),
                          styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "栖 · 启动与安装"
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.center()
        heading.font = NSFont.systemFont(ofSize: 25, weight: .semibold)
        phase.font = NSFont.systemFont(ofSize: 15)
        phase.maximumNumberOfLines = 3
        hint.textColor = .secondaryLabelColor
        hint.maximumNumberOfLines = 3
        progress.style = .bar
        progress.isIndeterminate = true
        retry.target = self
        retry.action = #selector(openConsole)
        logs.target = self
        logs.action = #selector(openLogs)
        let buttons = NSStackView(views: [logs, retry])
        buttons.orientation = .horizontal
        buttons.spacing = 12
        let stack = NSStackView(views: [heading, phase, progress, hint, buttons])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 18
        stack.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 28),
            stack.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -28),
            stack.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 24),
            progress.widthAnchor.constraint(equalTo: stack.widthAnchor),
            phase.widthAnchor.constraint(equalTo: stack.widthAnchor),
            hint.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if busy { showProgress() } else { openConsole() }
        return false
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        // Closing this window hides progress; the menu-bar application stays alive.
        sender.orderOut(nil)
        return false
    }

    @objc func showProgress() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func updatePhase(_ text: String) {
        DispatchQueue.main.async {
            self.phase.stringValue = text
            self.state.title = text
        }
    }

    func fail(_ detail: String) {
        heading.stringValue = "准备未完成"
        phase.stringValue = detail
        hint.stringValue = "可重试启动。窗口关闭后仍可从菜单栏查看进度或退出。"
        progress.stopAnimation(nil)
        retry.isEnabled = true
        state.title = "启动失败 · 可重试"
        showProgress()
    }

    // Drain stdout while the process runs so downloads cannot block on a full pipe.
    func run(_ executable: URL, _ arguments: [String], capture: Bool = false) throws -> (Bool, String) {
        let fm = FileManager.default
        let folder = root.appendingPathComponent("logs")
        try fm.createDirectory(at: folder, withIntermediateDirectories: true)
        let path = folder.appendingPathComponent("desktop.log")
        if fm.fileExists(atPath: path.path),
           (try? fm.attributesOfItem(atPath: path.path)[.size] as? NSNumber)?.intValue ?? 0 > 2_000_000 {
            let previous = folder.appendingPathComponent("desktop.previous.log")
            try? fm.removeItem(at: previous)
            try? fm.moveItem(at: path, to: previous)
        }
        if !fm.fileExists(atPath: path.path) { fm.createFile(atPath: path.path, contents: nil) }
        let log = try FileHandle(forWritingTo: path)
        defer { try? log.close() }
        log.seekToEndOfFile()
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.currentDirectoryURL = root
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPYCACHEPREFIX"] = root.appendingPathComponent("runtime/cache/pycache").path
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONUTF8"] = "1"
        let runtime = root.appendingPathComponent("runtime")
        for folder in ["tmp", "bin", "downloads", "python", "cache/uv"] {
            try fm.createDirectory(at: runtime.appendingPathComponent(folder), withIntermediateDirectories: true)
        }
        env["UV_CACHE_DIR"] = runtime.appendingPathComponent("cache/uv").path
        env["UV_PYTHON_INSTALL_DIR"] = runtime.appendingPathComponent("python").path
        env["UV_PYTHON_BIN_DIR"] = runtime.appendingPathComponent("bin").path
        env["UV_NO_CONFIG"] = "1"
        env["TMPDIR"] = runtime.appendingPathComponent("tmp").path
        process.environment = env
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        var output = Data()
        var pending = Data()
        while true {
            let chunk = pipe.fileHandleForReading.availableData
            if chunk.isEmpty { break }
            if !capture { log.write(chunk) }
            if capture && output.count < 8192 { output.append(chunk) }
            pending.append(chunk)
            while let newline = pending.firstIndex(of: 10) {
                let line = String(data: pending[..<newline], encoding: .utf8) ?? ""
                pending.removeSubrange(...newline)
                if !capture && line.hasPrefix("阶段：") {
                    updatePhase(String(line.dropFirst(3).prefix(100)))
                }
            }
            if pending.count > 32768 { pending.removeAll() }
        }
        process.waitUntilExit()
        return (process.terminationStatus == 0, String(data: output, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "")
    }

    // Bootstrap directly from the native app, so end users need neither Python nor Xcode.
    func bootstrapPython() throws -> URL {
        let fm = FileManager.default
        let python = root.appendingPathComponent(".venv/bin/python")
        if fm.isExecutableFile(atPath: python.path) { return python }
        let runtime = root.appendingPathComponent("runtime")
        let uv = runtime.appendingPathComponent("bin/uv")
        func require(_ success: Bool) throws {
            if !success { throw NSError(domain: "NativeInstall", code: 1) }
        }
        if !fm.isExecutableFile(atPath: uv.path) {
            updatePhase("下载安装工具…")
            let metadata = runtime.appendingPathComponent("downloads/uv-native-release.json")
            try fm.createDirectory(at: metadata.deletingLastPathComponent(), withIntermediateDirectories: true)
            let response = try run(URL(fileURLWithPath: "/usr/bin/curl"), ["-fL", "--connect-timeout", "25", "--max-time", "120", "-o", metadata.path, "https://api.github.com/repos/astral-sh/uv/releases/tags/0.13.0"])
            try require(response.0)
            #if arch(arm64)
            let archiveName = "uv-aarch64-apple-darwin.tar.gz"
            #else
            let archiveName = "uv-x86_64-apple-darwin.tar.gz"
            #endif
            let value = try JSONSerialization.jsonObject(with: Data(contentsOf: metadata)) as? [String: Any]
            guard let assets = value?["assets"] as? [[String: Any]],
                  let asset = assets.first(where: { $0["name"] as? String == archiveName }),
                  let digest = asset["digest"] as? String, digest.hasPrefix("sha256:"), digest.count == 71,
                  let address = asset["browser_download_url"] as? String,
                  let addressURL = URL(string: address), addressURL.scheme == "https", addressURL.host == "github.com" else {
                throw NSError(domain: "ReleaseDigest", code: 1)
            }
            let archive = runtime.appendingPathComponent("downloads/" + archiveName)
            updatePhase("下载并校验安装工具…")
            try require(try run(URL(fileURLWithPath: "/usr/bin/curl"), ["-fL", "--connect-timeout", "25", "--max-time", "900", "-o", archive.path, address]).0)
            let hash = SHA256.hash(data: try Data(contentsOf: archive)).map { String(format: "%02x", $0) }.joined()
            try require(hash == String(digest.dropFirst(7)).lowercased())
            let unpacked = runtime.appendingPathComponent("uv-native-dist")
            try fm.createDirectory(at: unpacked, withIntermediateDirectories: true)
            try require(try run(URL(fileURLWithPath: "/usr/bin/tar"), ["-xzf", archive.path, "-C", unpacked.path]).0)
            let directoryName = String(archiveName.dropLast(7))
            try fm.createDirectory(at: uv.deletingLastPathComponent(), withIntermediateDirectories: true)
            if fm.fileExists(atPath: uv.path) { try fm.removeItem(at: uv) }
            try fm.copyItem(at: unpacked.appendingPathComponent(directoryName + "/uv"), to: uv)
            try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: uv.path)
        }
        updatePhase("准备项目内 Python…")
        try require(try run(uv, ["python", "install", "3.12", "--no-bin"]).0)
        let folder = runtime.appendingPathComponent("python")
        let candidates = try fm.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)
            .filter { $0.lastPathComponent.hasPrefix("cpython-3.12.") && $0.lastPathComponent.contains("-macos-") }
            .sorted { $0.lastPathComponent.compare($1.lastPathComponent, options: .numeric) == .orderedAscending }
        guard let installed = candidates.last?.appendingPathComponent("bin/python3.12"), fm.isExecutableFile(atPath: installed.path) else {
            throw NSError(domain: "ManagedPython", code: 1)
        }
        return installed
    }

    func execute(_ action: String, completion: @escaping (Bool, String) -> Void) {
        guard !busy else { return }
        busy = true
        retry.isEnabled = false
        DispatchQueue.global(qos: .userInitiated).async {
            var succeeded = false
            var detail = ""
            do {
                let fm = FileManager.default
                let python = self.root.appendingPathComponent(".venv/bin/python")
                if action == "start" {
                    let seed = try self.bootstrapPython()
                    self.updatePhase("检查运行环境；首次下载可能需要数分钟…")
                    let result = try self.run(seed, [self.root.appendingPathComponent("scripts/bootstrap.py").path])
                    if !result.0 { throw NSError(domain: "Bootstrap", code: 1) }
                    self.updatePhase("启动管理服务…")
                }
                if action == "stop" && !fm.fileExists(atPath: self.root.appendingPathComponent("runtime/server.pid").path)
                    && !fm.fileExists(atPath: self.root.appendingPathComponent("runtime/lima/qqbot").path) {
                    succeeded = true
                } else {
                    let result = try self.run(python, [self.root.appendingPathComponent("scripts/desktop.py").path, action], capture: action == "status")
                    succeeded = result.0
                    detail = result.1
                }
            } catch {
                detail = "请检查网络及 logs/desktop.log。可从菜单重试；首次安装需要联网。"
            }
            DispatchQueue.main.async {
                self.busy = false
                self.retry.isEnabled = true
                completion(succeeded, detail)
            }
        }
    }

    @objc func openConsole() {
        if preview { showProgress(); return }
        guard !busy else { showProgress(); return }
        heading.stringValue = "栖 · 正在准备"
        phase.stringValue = "检查运行环境…"
        hint.stringValue = "首次安装自动下载 Python、依赖与模型。完成后打开控制台；QQ 扫码在控制台内进行。"
        progress.startAnimation(nil)
        showProgress()
        execute("start") { ok, detail in
            self.progress.stopAnimation(nil)
            if ok {
                self.state.title = "服务已启动"
                self.heading.stringValue = "栖已就绪"
                self.phase.stringValue = "控制台已在浏览器打开。"
                self.hint.stringValue = "通过菜单栏「栖」查看状态、重新打开控制台或停止并退出。"
                self.window.orderOut(nil)
                self.refresh()
            } else {
                self.fail(detail.isEmpty ? "启动未完成，请查看日志后重试。" : detail)
            }
        }
    }

    func refresh() {
        guard !busy else { return }
        execute("status") { ok, detail in
            self.state.title = ok ? String(detail.prefix(100)) : "暂时无法获取状态 · 可打开控制台检查"
        }
    }

    @objc func openLogs() {
        let folder = root.appendingPathComponent("logs")
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        NSWorkspace.shared.open(folder)
    }

    @objc func quit() { NSApp.terminate(nil) }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if preview { return .terminateNow }
        if busy {
            showProgress()
            hint.stringValue = "正在安装或操作服务，请等待当前步骤完成后退出。"
            return .terminateCancel
        }
        timer?.invalidate()
        heading.stringValue = "正在停止栖"
        phase.stringValue = "等待机器人与 QQ 环境安全退出…"
        progress.startAnimation(nil)
        showProgress()
        execute("stop") { ok, _ in
            self.progress.stopAnimation(nil)
            if !ok {
                self.fail("停止未完成。请查看日志并重试退出，服务仍可能在运行。")
                self.timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.refresh() }
            }
            sender.reply(toApplicationShouldTerminate: ok)
        }
        return .terminateLater
    }
}

let app = NSApplication.shared
let delegate = Launcher()
app.delegate = delegate
app.run()
