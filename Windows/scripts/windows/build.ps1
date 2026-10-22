# Build a native Windows GUI executable using the system .NET Framework compiler.
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { throw '需要 Windows x64 的 .NET Framework 4.x 编译器。' }
$source = Join-Path $PSScriptRoot 'Launcher.cs'
$output = Join-Path $root '栖.exe'
& $csc /nologo /target:winexe /platform:x64 /optimize+ /codepage:65001 /reference:System.Windows.Forms.dll /reference:System.Drawing.dll "/out:$output" $source
if ($LASTEXITCODE -ne 0) { throw '启动器编译失败。' }
Write-Output "已生成：$output"
