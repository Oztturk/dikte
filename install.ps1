# Dikte'yi bu Windows kullanicisi icin kurar: Baslat Menusu kisayolu, istege
# bagli otomatik baslangic ve her yerden calisan bir `dikte` komutu.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1              # kur
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Autostart   # + oturum acilisinda baslat
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall   # kaldir
param(
    [switch]$Autostart,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$repo = $PSScriptRoot
$startMenu = [Environment]::GetFolderPath("Programs")
$startup = [Environment]::GetFolderPath("Startup")
$shortcut = Join-Path $startMenu "Dikte.lnk"
$autostartLink = Join-Path $startup "Dikte.lnk"
# WindowsApps kullanici PATH'inde hazir durur; oraya birakilan dikte.cmd her
# terminalden calisir.
$cmdShim = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\dikte.cmd"

if ($Uninstall) {
    foreach ($path in @($shortcut, $autostartLink, $cmdShim)) {
        if (Test-Path $path) { Remove-Item $path -Force; Write-Host "silindi: $path" }
    }
    Write-Host "Dikte kisayollari kaldirildi. Depo klasoru ve ayarlar duruyor."
    exit 0
}

# --- gereksinimler ----------------------------------------------------------
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "Python bulunamadi. Kurun: winget install Python.Python.3.12"
}
$version = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$version -lt [version]"3.11") {
    Write-Error "Python 3.11+ gerekli, bulunan: $version"
}
& python -c "import PyQt6.QtWidgets" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyQt6 kuruluyor..."
    & python -m pip install PyQt6
    if ($LASTEXITCODE -ne 0) { Write-Error "PyQt6 kurulamadi." }
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warning "ffmpeg bulunamadi. Ses kaydi icin gerekli: winget install Gyan.FFmpeg"
}

# pythonw.exe konsol penceresi acmadan calistirir.
$pythonw = Join-Path (Split-Path $python.Source) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python.Source }

# --- Baslat Menusu kisayolu -------------------------------------------------
$shell = New-Object -ComObject WScript.Shell
foreach ($path in @($shortcut) + $(if ($Autostart) { @($autostartLink) } else { @() })) {
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $pythonw
    $link.Arguments = "`"$repo\dikte.py`" --gui"
    $link.WorkingDirectory = $repo
    $link.Description = "Dikte: sesli dikte"
    $link.Save()
    Write-Host "kisayol: $path"
}

# --- dikte komutu -----------------------------------------------------------
$shimDir = Split-Path $cmdShim
if (Test-Path $shimDir) {
    "@echo off`r`npython `"$repo\dikte.py`" %*" | Out-File $cmdShim -Encoding ascii
    Write-Host "komut: dikte  ($cmdShim)"
}

Write-Host ""
Write-Host "Kurulum tamam. Baslat Menusu'nden 'Dikte' ile ya da terminalden 'dikte' yazarak baslatin."
Write-Host "Ilk acilista Ayarlar penceresi acilir: oradan model indirin ve kisayolu secin (varsayilan Ctrl+Space)."
