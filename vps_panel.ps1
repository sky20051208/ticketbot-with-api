# 搶票虛擬機 —— 開關面板（兩台）。
#
# 桌面只放這一個捷徑。兩台機器是刻意分開的，不是重複：
#   拓元 → Oracle 美東（tixcraft 的 origin 在美東，而且 eps 只放行特定 ASN）
#   遠大 → AWS 東京  （TicketPlus 的 origin 在 AWS 東京，實測 queue 2.1ms vs 台灣 66ms）
# 位置剛好相反，所以不能共用一台。
#
# 按鈕本身不做事，只是去叫對應的 .bat（真正的邏輯都在那些腳本裡，這裡不重複實作）。
# 用 WinForms 是因為 PowerShell 內建就有，不用裝任何東西。

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "vps.config.ps1")

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# 不用 oci 的 --query：那是 JMESPath，`data."lifecycle-state"` 裡的雙引號在 PowerShell
# 的參數解析下會被吃掉。整包 JSON 拉回來自己解最穩。
function Get-OracleState {
    if (-not (Get-Command oci -ErrorAction SilentlyContinue)) { return "沒有 oci CLI" }
    try {
        return (& oci compute instance get --instance-id $VpsInstanceOcid | ConvertFrom-Json).data.'lifecycle-state'
    } catch { return "查詢失敗" }
}

function Get-TokyoState {
    try {
        Push-Location $PSScriptRoot
        try { $s = (python aws_tokyo.py state 2>$null) } finally { Pop-Location }
        if (-not $s) { return "查詢失敗" }
        return $s.Trim()
    } catch { return "查詢失敗" }
}

$form = New-Object System.Windows.Forms.Form
$form.Text = "搶票虛擬機"
$form.Size = New-Object System.Drawing.Size(700, 352)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.BackColor = [System.Drawing.Color]::FromArgb(38, 44, 56)

$icon = Join-Path $PSScriptRoot "assets\vps_panel.ico"
if (Test-Path $icon) { $form.Icon = New-Object System.Drawing.Icon($icon) }

function Start-Bat($name) {
    $bat = Join-Path $PSScriptRoot $name
    if (-not (Test-Path $bat)) {
        [System.Windows.Forms.MessageBox]::Show("找不到 $name", "錯誤") | Out-Null
        return
    }
    # 開新的主控台視窗跑，進度和錯誤都看得到，面板本身不會被卡住（開機要等 2 分鐘）
    Start-Process -FilePath $bat -WorkingDirectory $PSScriptRoot
}

function New-Label($text, $x, $y, $w, $size, $color, $bold) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text = $text
    $style = if ($bold) { [System.Drawing.FontStyle]::Bold } else { [System.Drawing.FontStyle]::Regular }
    $l.Font = New-Object System.Drawing.Font("Segoe UI", $size, $style)
    $l.ForeColor = $color
    $l.TextAlign = "MiddleCenter"
    $l.Location = New-Object System.Drawing.Point($x, $y)
    $l.Size = New-Object System.Drawing.Size($w, 26)
    $form.Controls.Add($l)
    return $l
}

function New-Button($text, $color, $x, $y, $w, $h, $size) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $text
    $b.Font = New-Object System.Drawing.Font("Segoe UI", $size, [System.Drawing.FontStyle]::Bold)
    $b.ForeColor = [System.Drawing.Color]::White
    $b.BackColor = $color
    $b.FlatStyle = "Flat"
    $b.FlatAppearance.BorderSize = 0
    $b.Location = New-Object System.Drawing.Point($x, $y)
    $b.Size = New-Object System.Drawing.Size($w, $h)
    $form.Controls.Add($b)
    return $b
}

$GREEN = [System.Drawing.Color]::FromArgb(34, 160, 79)
$RED   = [System.Drawing.Color]::FromArgb(200, 55, 55)
$BLUE  = [System.Drawing.Color]::FromArgb(58, 110, 175)
$GREY  = [System.Drawing.Color]::FromArgb(60, 68, 82)
$DIM   = [System.Drawing.Color]::FromArgb(150, 160, 175)

$COLW = 320
$LX = 20
$RX = 360

# ── 左：拓元・美東 ──────────────────────────────────────────
New-Label "拓元　Oracle 美東" $LX 14 $COLW 11 ([System.Drawing.Color]::FromArgb(190, 200, 215)) $true | Out-Null
$lblOracle = New-Label "查詢中…" $LX 42 $COLW 14 ([System.Drawing.Color]::Gainsboro) $true
(New-Button "▶  開機並打開 War-Room" $GREEN $LX 78 $COLW 44 11).Add_Click({ Start-Bat "vps_start.bat" })
(New-Button "■  關機" $RED $LX 128 $COLW 44 11).Add_Click({ Start-Bat "vps_stop.bat" })
(New-Button "▣  遠端桌面" $BLUE $LX 178 $COLW 44 11).Add_Click({ Start-Bat "vps_moonlight.bat" })
(New-Button "VNC 備援" $GREY $LX 228 $COLW 24 9).Add_Click({ Start-Bat "vps_vnc.bat" })

# ── 右：遠大・東京 ──────────────────────────────────────────
New-Label "遠大　AWS 東京" $RX 14 $COLW 11 ([System.Drawing.Color]::FromArgb(190, 200, 215)) $true | Out-Null
$lblTokyo = New-Label "查詢中…" $RX 42 $COLW 14 ([System.Drawing.Color]::Gainsboro) $true
(New-Button "▶  開機並打開 War-Room" $GREEN $RX 78 $COLW 44 11).Add_Click({ Start-Bat "tokyo_start.bat" })
(New-Button "■  關機" $RED $RX 128 $COLW 44 11).Add_Click({ Start-Bat "tokyo_stop.bat" })
(New-Button "▣  遠端桌面" $BLUE $RX 178 $COLW 44 11).Add_Click({ Start-Bat "tokyo_moonlight.bat" })

$btnRefresh = New-Button "重新整理狀態" $GREY $LX 268 660 26 9

function Set-StateLabel($label, $state) {
    # OCI 用大寫（RUNNING/STOPPED），AWS 用小寫（running/stopped）
    switch -Regex ($state) {
        '^(RUNNING|running)$' {
            $label.Text = "執行中"
            $label.ForeColor = [System.Drawing.Color]::FromArgb(90, 220, 130) }
        '^(STOPPED|stopped)$' {
            $label.Text = "已關機"
            $label.ForeColor = $DIM }
        '^none$' {
            $label.Text = "尚未建立"
            $label.ForeColor = $DIM }
        default {
            $label.Text = $state
            $label.ForeColor = [System.Drawing.Color]::FromArgb(240, 190, 90) }
    }
}

function Update-State {
    $lblOracle.Text = "查詢中…"; $lblOracle.ForeColor = [System.Drawing.Color]::Gainsboro
    $lblTokyo.Text  = "查詢中…"; $lblTokyo.ForeColor  = [System.Drawing.Color]::Gainsboro
    $form.Refresh()
    Set-StateLabel $lblOracle (Get-OracleState)
    $form.Refresh()
    Set-StateLabel $lblTokyo (Get-TokyoState)
}

$btnRefresh.Add_Click({ Update-State })
$form.Add_Shown({ $form.Activate(); Update-State })

[void]$form.ShowDialog()
