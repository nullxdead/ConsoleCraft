param([switch]$Uninstall, [string]$PackageDirectory = $PSScriptRoot)
$ErrorActionPreference = 'Stop'
$installDirectory = Join-Path $env:LOCALAPPDATA 'ConsoleLauncher\bin'
$wrapperPath = Join-Path $installDirectory 'consolelauncher.cmd'
$statePath = Join-Path $installDirectory 'install-state.json'
$installedExecutable = Join-Path $installDirectory 'consolelauncher-cli.exe'
$marker = '@rem Console Launcher CLI wrapper'
function Publish-EnvironmentChange {
    if (-not ('ConsoleLauncher.EnvironmentNotification' -as [type])) {
        Add-Type @'
using System;
using System.Runtime.InteropServices;
namespace ConsoleLauncher {
    public static class EnvironmentNotification {
        [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern IntPtr SendMessageTimeout(IntPtr window, uint message, UIntPtr value, string text, uint flags, uint timeout, out UIntPtr result);
    }
}
'@
    }
    $notificationResult = [UIntPtr]::Zero
    [ConsoleLauncher.EnvironmentNotification]::SendMessageTimeout([IntPtr]0xffff, 0x001A, [UIntPtr]::Zero, 'Environment', 2, 2000, [ref]$notificationResult) | Out-Null
}
$environmentKey = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment')
$pathOriginallyPresent = @($environmentKey.GetValueNames()) -contains 'Path'
$pathKind = if ($pathOriginallyPresent) { $environmentKey.GetValueKind('Path') } else { [Microsoft.Win32.RegistryValueKind]::ExpandString }
if ($pathKind -notin @([Microsoft.Win32.RegistryValueKind]::String, [Microsoft.Win32.RegistryValueKind]::ExpandString)) { throw 'Необычный тип пользовательского PATH. Изменения не выполнены.' }
$userPath = [string]$environmentKey.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
$pathParts = @($userPath.Split(';'))
$existingWrapper = Test-Path -LiteralPath $wrapperPath
if ($existingWrapper -and (Get-Content -LiteralPath $wrapperPath -TotalCount 1) -ne $marker) {
    throw "Команда $wrapperPath принадлежит другой программе и не будет изменена."
}
$state = if (Test-Path -LiteralPath $statePath) { Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } else { $null }
if ($state -and $state.owner -ne 'ConsoleLauncher') { throw 'Каталог установщика занят другой программой.' }
if ($Uninstall) {
    if (-not $state) { Write-Output 'Собственная установка команды не найдена.'; exit 0 }
    if ($existingWrapper) { Remove-Item -LiteralPath $wrapperPath }
    if ($state.executable -eq $installedExecutable -and (Test-Path -LiteralPath $installedExecutable)) { Remove-Item -LiteralPath $installedExecutable }
    if ($state.pathAdded) {
        $remaining = @($pathParts | Where-Object { $_.TrimEnd('\') -ine $installDirectory.TrimEnd('\') })
        $remainingPath = $remaining -join ';'
        if (-not $state.pathOriginallyPresent -and $remainingPath -eq '') { $environmentKey.DeleteValue('Path', $false) }
        else { $environmentKey.SetValue('Path', $remainingPath, $pathKind) }
        Publish-EnvironmentChange
    }
    Remove-Item -LiteralPath $statePath
    if (@(Get-ChildItem -LiteralPath $installDirectory -Force).Count -eq 0) { Remove-Item -LiteralPath $installDirectory }
    Write-Output 'Команда consolelauncher удалена. Игры, аккаунты и настройки сохранены.'
    exit 0
}
$sourceExecutable = Join-Path $PackageDirectory 'consolelauncher.exe'
if (-not (Test-Path -LiteralPath $sourceExecutable -PathType Leaf)) { throw 'Рядом с установщиком отсутствует consolelauncher.exe.' }
$pathPresent = @($pathParts | Where-Object { [Environment]::ExpandEnvironmentVariables($_).TrimEnd('\') -ieq $installDirectory.TrimEnd('\') }).Count -gt 0
if (-not (Test-Path -LiteralPath $installDirectory)) { New-Item -ItemType Directory -Path $installDirectory | Out-Null }
$executable = $installedExecutable
if ((Test-Path -LiteralPath $executable) -and (-not $state -or $state.executable -ne $executable)) { throw 'Файл команды занят другой программой.' }
Copy-Item -LiteralPath $sourceExecutable -Destination $executable -Force
$escapedExecutable = $executable.Replace('%', '%%')
$wrapper = @($marker, '@echo off', '@chcp 65001 >nul', 'setlocal DisableDelayedExpansion', ('"{0}" %*' -f $escapedExecutable), 'exit /b %errorlevel%') -join "`r`n"
[IO.File]::WriteAllText($wrapperPath, $wrapper + "`r`n", [Text.UTF8Encoding]::new($false))
@{owner='ConsoleLauncher'; pathAdded=((-not $pathPresent) -or ($state -and $state.pathAdded)); pathOriginallyPresent=$(if ($state) { $state.pathOriginallyPresent } else { $pathOriginallyPresent }); executable=$executable} | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
if (-not $pathPresent) {
    $updatedPath = if ($userPath.Length -gt 0) { $userPath + ';' + $installDirectory } else { $installDirectory }
    $environmentKey.SetValue('Path', $updatedPath, $pathKind)
    Publish-EnvironmentChange
}
Write-Output 'Команда consolelauncher добавлена в пользовательский PATH. Откройте новый терминал.'
Write-Output 'Консольное меню: consolelauncher cli. Удаление: .\install-cli.ps1 -Uninstall'
