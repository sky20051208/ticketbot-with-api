# 拓元美東虛擬機 —— 開關面板。
#
# 桌面只放這一個捷徑，開機/關機都在裡面按。按鈕本身不做事，只是去叫
# vps_start.bat / vps_stop.bat（真正的邏輯都在那兩支，這裡不重複實作）。
#
# 用 WinForms 是因為 PowerShell 內建就有，不用裝任何東西。

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "vps.config.ps1")

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

function Get-VpsState {
    if (-not (Get-Command oci -ErrorAction SilentlyContinue)) { return "沒有 oci CLI" }
    try {
        $json = & oci compute instance get --instance-id $VpsInstanceOcid | ConvertFrom-Json
        return $json.data.'lifecycle-state'
    } catch {
        return "查詢失敗"
    }
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "拓元美東虛擬機"
$form.Size = New-Object System.Drawing.Size(360, 318)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.BackColor = [System.Drawing.Color]::FromArgb(38, 44, 56)

$icon = Join-Path $PSScriptRoot "assets\vps_panel.ico"
if (Test-Path $icon) { $form.Icon = New-Object System.Drawing.Icon($icon) }

$lblState = New-Object System.Windows.Forms.Label
$lblState.Text = "查詢中…"
$lblState.Font = New-Object System.Drawing.Font("Segoe UI", 14, [System.Drawing.FontStyle]::Bold)
$lblState.ForeColor = [System.Drawing.Color]::White
$lblState.TextAlign = "MiddleCenter"
$lblState.Location = New-Object System.Drawing.Point(20, 20)
$lblState.Size = New-Object System.Drawing.Size(305, 40)
$form.Controls.Add($lblState)

function New-Button($text, $color, $y) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $text
    $b.Font = New-Object System.Drawing.Font("Segoe UI", 12, [System.Drawing.FontStyle]::Bold)
    $b.ForeColor = [System.Drawing.Color]::White
    $b.BackColor = $color
    $b.FlatStyle = "Flat"
    $b.FlatAppearance.BorderSize = 0
    $b.Location = New-Object System.Drawing.Point(20, $y)
    $b.Size = New-Object System.Drawing.Size(305, 48)
    return $b
}

# 按鈕只負責叫 .bat —— 開新的主控台視窗跑，進度和錯誤都在那邊看得到，
# 面板本身不會被卡住（開機要等 2 分鐘）。
function Start-Bat($name) {
    $bat = Join-Path $PSScriptRoot $name
    if (-not (Test-Path $bat)) {
        [System.Windows.Forms.MessageBox]::Show("找不到 $name", "錯誤") | Out-Null
        return
    }
    Start-Process -FilePath $bat -WorkingDirectory $PSScriptRoot
}

$btnStart = New-Button "▶  開機並打開 War-Room" ([System.Drawing.Color]::FromArgb(34, 160, 79)) 75
$btnStart.Add_Click({ Start-Bat "vps_start.bat" })
$form.Controls.Add($btnStart)

$btnStop = New-Button "■  關機" ([System.Drawing.Color]::FromArgb(200, 55, 55)) 133
$btnStop.Add_Click({ Start-Bat "vps_stop.bat" })
$form.Controls.Add($btnStop)

# 遠端桌面主力走 H.264（Sunshine + Moonlight）。VNC 是「每張畫面各自壓縮」的靜態圖
# 編碼，實測捲一次頁面 131KB，在這條 2.1MB/s 的路上 16fps 就是天花板；H.264 做影格間
# 預測，同一段捲動 60fps 只吃 3.9Mbps。VNC 留在下面當備援 —— 它走 SSH 通道，
# 在只有 22 埠通得出去的網路（公司 / 飯店）是唯一還能用的路。
$btnRemote = New-Button "▣  遠端桌面" ([System.Drawing.Color]::FromArgb(58, 110, 175)) 191
$btnRemote.Add_Click({ Start-Bat "vps_moonlight.bat" })
$form.Controls.Add($btnRemote)

function New-SmallButton($text, $x, $w) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $text
    $b.Font = New-Object System.Drawing.Font("Segoe UI", 9)
    $b.ForeColor = [System.Drawing.Color]::White
    $b.BackColor = [System.Drawing.Color]::FromArgb(60, 68, 82)
    $b.FlatStyle = "Flat"
    $b.FlatAppearance.BorderSize = 0
    $b.Location = New-Object System.Drawing.Point($x, 248)
    $b.Size = New-Object System.Drawing.Size($w, 26)
    $form.Controls.Add($b)
    return $b
}

$btnRefresh = New-SmallButton "重新整理狀態" 20 178
$btnVnc     = New-SmallButton "VNC 備援" 205 120
$btnVnc.Add_Click({ Start-Bat "vps_vnc.bat" })

function Update-State {
    $lblState.Text = "查詢中…"
    $lblState.ForeColor = [System.Drawing.Color]::Gainsboro
    $form.Refresh()
    $s = Get-VpsState
    switch ($s) {
        "RUNNING" { $lblState.Text = "執行中"; $lblState.ForeColor = [System.Drawing.Color]::FromArgb(90, 220, 130) }
        "STOPPED" { $lblState.Text = "已關機"; $lblState.ForeColor = [System.Drawing.Color]::FromArgb(150, 160, 175) }
        default   { $lblState.Text = $s;       $lblState.ForeColor = [System.Drawing.Color]::FromArgb(240, 190, 90) }
    }
}

$btnRefresh.Add_Click({ Update-State })
$form.Add_Shown({ $form.Activate(); Update-State })

[void]$form.ShowDialog()
