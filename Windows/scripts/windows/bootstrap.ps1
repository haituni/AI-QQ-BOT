# Only verified binaries are downloaded. No downloaded PowerShell is evaluated.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
Set-Location -LiteralPath $root
$runtime = Join-Path $root 'runtime'
foreach ($folder in @('bin', 'downloads', 'tmp', 'cache', 'python')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $runtime $folder) | Out-Null
}
$env:UV_CACHE_DIR = Join-Path $runtime 'cache\uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $runtime 'python'
$env:UV_PYTHON_BIN_DIR = Join-Path $runtime 'bin'
$env:UV_NO_CONFIG = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$env:TEMP = Join-Path $runtime 'tmp'
$env:TMP = $env:TEMP
$env:PYTHONPYCACHEPREFIX = Join-Path $runtime 'cache\pycache'
$python = Join-Path $root '.venv\Scripts\python.exe'
$lock = $null
try {
    # Native launchers and script callers serialize the Python-free bootstrap too.
    $lock = [IO.File]::Open((Join-Path $runtime 'native-install.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    if (-not (Test-Path -LiteralPath $python)) {
        if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
            throw '此 Windows 版本需要 x64 系统。'
        }
        Write-Output '阶段：准备安装工具'
        $uv = Join-Path $runtime 'bin\uv.exe'
        if (-not (Test-Path -LiteralPath $uv)) {
            $metadata = Invoke-RestMethod -Uri 'https://api.github.com/repos/astral-sh/uv/releases/tags/0.13.0' -Headers @{'User-Agent'='Qi-Desktop-Installer'} -TimeoutSec 60
            $asset = @($metadata.assets | Where-Object { $_.name -eq 'uv-x86_64-pc-windows-msvc.zip' })
            if ($asset.Count -ne 1 -or $asset[0].digest -notmatch '^sha256:([a-fA-F0-9]{64})$') {
                throw '官方发行版未提供 SHA256 校验摘要。'
            }
            $expected = $asset[0].digest.Substring(7)
            $archive = Join-Path $runtime 'downloads\uv-x86_64-pc-windows-msvc.zip'
            Write-Output '阶段：下载并校验安装工具'
            Invoke-WebRequest -UseBasicParsing -Uri $asset[0].browser_download_url -OutFile $archive -TimeoutSec 900
            if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $expected) { throw '下载校验失败，请重试。' }
            $extract = Join-Path $runtime 'uv-native-dist'
            Expand-Archive -LiteralPath $archive -DestinationPath $extract -Force
            $binary = @(Get-ChildItem -LiteralPath $extract -Filter 'uv.exe' -File -Recurse)
            if ($binary.Count -ne 1) { throw '安装工具压缩包结构无效。' }
            Copy-Item -LiteralPath $binary[0].FullName -Destination $uv -Force
        }
        Write-Output '阶段：准备项目内 Python'
        & $uv python install 3.12 --no-bin
        if ($LASTEXITCODE -ne 0) { throw 'Python 下载未完成，请检查网络后重试。' }
        $installed = @(Get-ChildItem -LiteralPath (Join-Path $runtime 'python') -Directory -Filter 'cpython-3.12.*-windows-*' | Sort-Object Name -Descending)
        if ($installed.Count -eq 0) { throw '未找到项目内 Python。' }
        $python = Join-Path $installed[0].FullName 'python.exe'
    }
    & $python -X utf8 (Join-Path $root 'scripts\bootstrap.py')
    if ($LASTEXITCODE -ne 0) { throw '运行环境安装未完成。' }
}
catch {
    # Do not echo third-party exception text, which can include proxy credentials.
    Write-Output '阶段：安装未完成，请检查网络与文件夹权限后重试'
    exit 1
}
finally {
    if ($null -ne $lock) { $lock.Dispose() }
}
