$ErrorActionPreference = 'Stop'
$version = '4.4.0'
if (-not [Environment]::Is64BitOperatingSystem) { throw 'Нужна 64-битная Windows. Для 32-битной системы используйте исходники.' }
$asset = "ConsoleLauncher-$version-Windows-AMD64.zip"
$base = "https://github.com/nullxdead/ConsoleLauncher/releases/download/v$version"
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$tempDir = Join-Path $tempRoot ('consolelauncher-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempDir | Out-Null
try {
    Write-Output 'Скачивание Console Launcher...'
    $archive = Join-Path $tempDir $asset
    Invoke-WebRequest -UseBasicParsing -Uri "$base/$asset" -OutFile $archive
    $sums = Join-Path $tempDir 'SHA256SUMS.txt'
    Invoke-WebRequest -UseBasicParsing -Uri "$base/SHA256SUMS.txt" -OutFile $sums
    $match = @(Get-Content -LiteralPath $sums | Where-Object { $_ -match ('^([a-fA-F0-9]{64})  ' + [regex]::Escape($asset) + '$') })
    if ($match.Count -ne 1) { throw 'Контрольная сумма не найдена.' }
    $expected = $match[0].Substring(0,64)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($archive)
    try { $actual = [BitConverter]::ToString($sha256.ComputeHash($stream)).Replace('-', '') }
    finally { $stream.Dispose(); $sha256.Dispose() }
    if ($actual -ine $expected) { throw 'Контрольная сумма не совпадает.' }
    $package = Join-Path $tempDir 'package'
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::ExtractToDirectory($archive, $package)
    $installer = [scriptblock]::Create([IO.File]::ReadAllText((Join-Path $package 'install-cli.ps1')))
    & $installer -PackageDirectory $package
    Write-Output 'Готово. Откройте новый терминал и запустите consolelauncher.'
} finally {
    $resolved = [IO.Path]::GetFullPath($tempDir)
    if ($resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and (Split-Path $resolved -Leaf).StartsWith('consolelauncher-')) {
        Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
    }
}
