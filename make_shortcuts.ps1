# 在桌面建立「搶票VPS 開機 / 關機」兩個捷徑。跑一次就好。
#
# 捷徑指向 vps_start.bat / vps_stop.bat（不是 .ps1）—— .bat 那層負責
# -ExecutionPolicy Bypass，不然點下去會被「指令碼執行已停用」擋掉。

$ErrorActionPreference = "Stop"

$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell

# 桌面只放一個 —— 開機/關機都在面板裡按，不用兩個圖示擠在桌面上也不怕按錯。
$items = @(
    @{ Name = "拓元美東虛擬機"; Target = "vps_panel.bat"; Icon = "assets\vps_panel.ico"; Desc = "開關美東搶票 VPS" }
)

foreach ($item in $items) {
    $target = Join-Path $PSScriptRoot $item.Target
    if (-not (Test-Path $target)) {
        Write-Host "!!  找不到 $target，跳過" -ForegroundColor Yellow
        continue
    }

    $linkPath = Join-Path $desktop ($item.Name + ".lnk")
    $link = $shell.CreateShortcut($linkPath)
    $link.TargetPath = $target
    $link.WorkingDirectory = $PSScriptRoot     # .bat 用 %~dp0 找 .ps1，這裡設不設都行，設了比較保險
    $link.Description = $item.Desc

    $icon = Join-Path $PSScriptRoot $item.Icon
    if (Test-Path $icon) { $link.IconLocation = "$icon,0" }

    $link.Save()
    Write-Host "==> 已建立：$linkPath" -ForegroundColor Cyan
}

Write-Host ""
Write-Host "  桌面上點「拓元美東虛擬機」就會跳出開關面板。" -ForegroundColor Green
Write-Host "  圖示沒馬上更新的話，在桌面按 F5 重新整理一次。"
