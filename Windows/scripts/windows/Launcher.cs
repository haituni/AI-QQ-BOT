// Native .NET Framework launcher. No external runtime or Python is required to show this UI.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        string id;
        using (var hash = SHA256.Create())
            id = BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(root.ToUpperInvariant()))).Replace("-", "").Substring(0, 24);
        bool created;
        using (var mutex = new Mutex(true, "Local\\QiLauncher-" + id, out created))
        using (var reopen = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\QiReopen-" + id))
        {
            if (!created) { reopen.Set(); return; }
            try { Application.Run(new Launcher(root, reopen)); }
            catch (Exception)
            {
                MessageBox.Show("启动器未能运行。请确认文件夹可写，且栖.exe与 scripts、README.md 在一起。", "栖", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
            finally { mutex.ReleaseMutex(); }
        }
    }
}

internal sealed class Launcher : Form
{
    private readonly string root;
    private readonly NotifyIcon tray;
    private readonly ToolStripMenuItem state;
    private readonly Label heading;
    private readonly Label phase;
    private readonly Label hint;
    private readonly ProgressBar progress;
    private readonly Button retry;
    private readonly System.Windows.Forms.Timer timer;
    private readonly RegisteredWaitHandle reopenWait;
    private readonly object logLock = new object();
    private readonly bool preview = Array.IndexOf(Environment.GetCommandLineArgs(), "--preview") >= 0;
    private bool busy;
    private bool allowClose;
    private string Python { get { return Path.Combine(root, ".venv", "Scripts", "python.exe"); } }

    internal Launcher(string directory, EventWaitHandle reopen)
    {
        root = directory;
        Text = "栖 · 启动与安装";
        Font = new Font("Microsoft YaHei UI", 10F);
        BackColor = Color.FromArgb(246, 245, 239);
        ForeColor = Color.FromArgb(45, 65, 54);
        ClientSize = new Size(540, 290);
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        AutoScaleMode = AutoScaleMode.Dpi;
        heading = new Label { Text = "栖 · 正在准备", Font = new Font(Font.FontFamily, 22F, FontStyle.Bold), Location = new Point(28, 24), Size = new Size(480, 42) };
        phase = new Label { Text = "检查运行环境…", Location = new Point(30, 82), Size = new Size(480, 44) };
        progress = new ProgressBar { Location = new Point(30, 138), Size = new Size(480, 14), Style = ProgressBarStyle.Marquee };
        hint = new Label { Text = "首次安装自动下载 Python、依赖与模型。完成后打开浏览器控制台。", ForeColor = Color.DimGray, Location = new Point(30, 172), Size = new Size(480, 48) };
        var logs = new Button { Text = "查看日志", Location = new Point(288, 230), Size = new Size(106, 34) };
        retry = new Button { Text = "重试启动", Location = new Point(404, 230), Size = new Size(106, 34) };
        logs.Click += delegate { OpenLogs(); };
        retry.Click += async delegate { await StartAsync(); };
        Controls.AddRange(new Control[] { heading, phase, progress, hint, logs, retry });
        var menu = new ContextMenuStrip();
        state = new ToolStripMenuItem("正在启动…") { Enabled = false };
        menu.Items.Add(state);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("打开控制台", null, async delegate { await StartAsync(); });
        menu.Items.Add("查看启动进度", null, delegate { Reveal(); });
        menu.Items.Add("查看运行日志", null, delegate { OpenLogs(); });
        menu.Items.Add("停止机器人并退出", null, async delegate { await QuitAsync(); });
        tray = new NotifyIcon { Text = "栖 · 正在准备", Icon = SystemIcons.Application, ContextMenuStrip = menu, Visible = true };
        tray.DoubleClick += async delegate { await StartAsync(); };
        timer = new System.Windows.Forms.Timer { Interval = 15000 };
        timer.Tick += async delegate { await RefreshAsync(); };
        reopenWait = ThreadPool.RegisterWaitForSingleObject(reopen, delegate {
            Post(async delegate { await StartAsync(); });
        }, null, Timeout.Infinite, false);
        Shown += async delegate {
            if (preview) {
                heading.Text = "栖 · 首次安装";
                phase.Text = "准备项目内 Python、依赖和表情语义模型…";
                hint.Text = "界面预览，不下载组件或启动机器人。实际安装完成后会打开控制台。";
                retry.Enabled = false;
            } else { timer.Start(); await StartAsync(); }
        };
        FormClosing += delegate(object sender, FormClosingEventArgs e) {
            if (!allowClose) { e.Cancel = true; Hide(); }
        };
    }

    private void Post(Action action)
    {
        if (!IsDisposed && IsHandleCreated)
        {
            try { BeginInvoke(action); }
            catch (InvalidOperationException) { }
        }
    }

    private void Reveal() { Show(); WindowState = FormWindowState.Normal; Activate(); }
    private void Status(string message)
    {
        state.Text = message;
        tray.Text = message.Length > 55 ? message.Substring(0, 55) : "栖 · " + message;
    }
    private void Failed(string message)
    {
        heading.Text = "准备未完成";
        phase.Text = message;
        hint.Text = "请查看日志后重试。关闭窗口只隐藏进度；退出请使用托盘菜单。";
        progress.Style = ProgressBarStyle.Blocks;
        progress.Value = 0;
        Status("操作失败 · 可查看日志并重试");
        Reveal();
    }

    // Standard Windows argv quoting, including trailing backslashes and embedded quotes.
    internal static string Quote(string value)
    {
        var text = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value)
        {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') { text.Append('\\', slashes * 2 + 1); text.Append('"'); }
            else { text.Append('\\', slashes); text.Append(c); }
            slashes = 0;
        }
        text.Append('\\', slashes * 2);
        return text.Append('"').ToString();
    }

    private Task<string> RunAsync(string executable, string[] args, bool capture, int timeout)
    {
        return Task.Run(delegate {
            Directory.CreateDirectory(Path.Combine(root, "logs"));
            string logPath = Path.Combine(root, "logs", "desktop.log");
            if (!capture && File.Exists(logPath) && new FileInfo(logPath).Length > 2000000)
            {
                string previous = Path.Combine(root, "logs", "desktop.previous.log");
                if (File.Exists(previous)) File.Delete(previous);
                File.Move(logPath, previous);
            }
            var start = new ProcessStartInfo(executable) {
                Arguments = String.Join(" ", Array.ConvertAll(args, Quote)),
                WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardOutput = true, RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8
            };
            start.EnvironmentVariables["PYTHONUTF8"] = "1";
            start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            start.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
            start.EnvironmentVariables["PYTHONPYCACHEPREFIX"] = Path.Combine(root, "runtime", "cache", "pycache");
            // curl/uv do not automatically inherit Windows Internet Settings.
            bool explicitProxy = false;
            foreach (string key in new[] { "http_proxy", "https_proxy", "all_proxy" })
                if (!String.IsNullOrEmpty(start.EnvironmentVariables[key])) explicitProxy = true;
            if (!explicitProxy && WebRequest.DefaultWebProxy != null)
            {
                foreach (string scheme in new[] { "http", "https" })
                {
                    var target = new Uri(scheme + "://huggingface.co");
                    var proxy = WebRequest.DefaultWebProxy.GetProxy(target);
                    if (proxy != null && proxy != target)
                        start.EnvironmentVariables[scheme + "_proxy"] = proxy.AbsoluteUri;
                }
            }
            start.EnvironmentVariables["NO_PROXY"] = (start.EnvironmentVariables["NO_PROXY"] ?? "") + ",localhost,127.0.0.1,::1";
            var output = new StringBuilder();
            string diagnostic = null;
            using (var log = new StreamWriter(logPath, true, new UTF8Encoding(false)))
            using (var process = new Process { StartInfo = start })
            {
                log.AutoFlush = true;
                DataReceivedEventHandler receive = delegate(object sender, DataReceivedEventArgs e) {
                    if (e.Data == null) return;
                    lock (logLock)
                    {
                        if (capture) { if (output.Length < 8192) output.AppendLine(e.Data); }
                        else log.WriteLine(e.Data);
                    }
                    if (!capture && e.Data.StartsWith("错误：", StringComparison.Ordinal))
                    {
                        string message = e.Data.Substring(3);
                        lock (logLock) { diagnostic = message.Length > 200 ? message.Substring(0, 200) : message; }
                    }
                    // Only explicit phase messages reach the window. Raw diagnostic output stays in local logs.
                    if (!capture && e.Data.StartsWith("阶段：", StringComparison.Ordinal))
                    {
                        string message = e.Data.Substring(3);
                        if (message.Length > 100) message = message.Substring(0, 100);
                        Post(delegate { phase.Text = message; Status(message); });
                    }
                };
                process.OutputDataReceived += receive;
                process.ErrorDataReceived += receive;
                process.Start();
                process.BeginOutputReadLine();
                process.BeginErrorReadLine();
                if (timeout > 0 && !process.WaitForExit(timeout))
                {
                    try { process.Kill(); } catch (InvalidOperationException) { }
                    process.WaitForExit();
                    throw new TimeoutException();
                }
                process.WaitForExit();
                if (process.ExitCode != 0) throw new InvalidOperationException(diagnostic ?? "启动失败，请查看 logs/desktop.log 和 logs/server.log。");
            }
            return output.ToString().Trim();
        });
    }

    private async Task StartAsync()
    {
        if (preview) { Reveal(); return; }
        if (busy) { Reveal(); return; }
        busy = true;
        retry.Enabled = false;
        heading.Text = "栖 · 正在准备";
        phase.Text = "检查运行环境；首次下载可能需要数分钟…";
        hint.Text = "自动准备 Python、依赖与模型。QQ 接入仍需 Docker Desktop，可在控制台内安装和扫码。";
        progress.Style = ProgressBarStyle.Marquee;
        Reveal();
        try
        {
            if (!File.Exists(Path.Combine(root, "scripts", "windows", "bootstrap.ps1")))
                throw new FileNotFoundException();
            string powershell = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe");
            await RunAsync(powershell, new[] { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", Path.Combine(root, "scripts", "windows", "bootstrap.ps1") }, false, 0);
            phase.Text = "启动管理服务…";
            await RunAsync(Python, new[] { Path.Combine(root, "scripts", "desktop.py"), "start" }, false, 0);
            heading.Text = "栖已就绪";
            phase.Text = "控制台已在浏览器打开。";
            hint.Text = "通过系统托盘图标查看状态、打开控制台或停止并退出。";
            Status("服务已启动");
            Hide();
        }
        catch (InvalidOperationException error) { Failed(error.Message); }
        catch (Exception) { Failed("启动未完成，请检查网络、目录权限和 logs/desktop.log。"); }
        finally { busy = false; retry.Enabled = true; progress.Style = ProgressBarStyle.Blocks; }
    }

    private async Task RefreshAsync()
    {
        if (busy || !File.Exists(Python)) return;
        busy = true;
        try {
            string result = await RunAsync(Python, new[] { Path.Combine(root, "scripts", "desktop.py"), "status" }, true, 15000);
            Status(result.Length > 100 ? result.Substring(0, 100) : result);
        }
        catch (Exception) { Status("暂时无法获取状态 · 可打开控制台检查"); }
        finally { busy = false; }
    }

    private async Task QuitAsync()
    {
        if (preview) { allowClose = true; tray.Visible = false; Close(); return; }
        if (busy) { hint.Text = "正在操作服务，请等待当前步骤完成后退出。"; Reveal(); return; }
        busy = true;
        timer.Stop();
        retry.Enabled = false;
        heading.Text = "正在停止栖";
        phase.Text = "等待机器人与 QQ 接入安全退出…";
        progress.Style = ProgressBarStyle.Marquee;
        Reveal();
        try
        {
            if (File.Exists(Path.Combine(root, "runtime", "server.pid")) || Directory.Exists(Path.Combine(root, "runtime", "napcat", "installed")))
            {
                await RunAsync(Python, new[] { Path.Combine(root, "scripts", "desktop.py"), "stop" }, false, 0);
            }
            allowClose = true;
            tray.Visible = false;
            Close();
        }
        catch (Exception) {
            Failed("停止未完成，服务仍可能在运行。请查看日志后从托盘重试退出。");
            timer.Start();
        }
        finally { busy = false; if (!IsDisposed) retry.Enabled = true; }
    }

    private void OpenLogs()
    {
        try {
            string folder = Path.Combine(root, "logs");
            Directory.CreateDirectory(folder);
            Process.Start(new ProcessStartInfo(folder) { UseShellExecute = true });
        }
        catch (Exception) { MessageBox.Show(this, "无法打开日志目录，请检查目录权限。", "栖"); }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) { timer.Dispose(); reopenWait.Unregister(null); tray.Dispose(); }
        base.Dispose(disposing);
    }
}
