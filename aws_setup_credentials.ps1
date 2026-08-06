# 把 AWS 金鑰寫進 ~/.aws/credentials（東京機用）。
#
# 這支存在的意義是「金鑰不要經過聊天視窗」—— 檔案讀取或你在自己的終端機貼，
# 兩種都不會讓 secret 跑到別的地方去。
#
# 金鑰怎麼拿（一次性）：
#   1. AWS Console 右上角你的帳號名 → Security credentials
#   2. 往下找 Access keys → Create access key
#   3. Use case 選 Command Line Interface (CLI)，勾下面那個確認框 → Next → Create
#   4. 按 Download .csv file（Secret 只會顯示這一次，下載最保險）
#
# 用法：直接執行就會自動去「下載」資料夾找那個 csv；找不到才叫你手動貼。
#       也可以自己指定：.\aws_setup_credentials.ps1 -Csv "C:\path\to\xxx.csv"

param([string]$Csv = "")

$ErrorActionPreference = "Stop"

Write-Host "==> 設定 AWS 金鑰（東京機用）" -ForegroundColor Cyan

$keyId = ""
$secret = ""

# --- 先試著讀 AWS 下載的 csv ---
if (-not $Csv) {
    # 新版 Console 存成 xxx_accessKeys.csv，舊版 IAM 使用者是 credentials.csv
    $Csv = Get-ChildItem "$env:USERPROFILE\Downloads" -Filter "*.csv" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match 'accessKey|credential' } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if ($Csv -and (Test-Path $Csv)) {
    Write-Host "    讀取 $Csv"
    $row = Import-Csv $Csv | Select-Object -First 1
    # 兩種格式的欄位名不同，用「包含」比對比較保險
    $idCol  = $row.PSObject.Properties.Name | Where-Object { $_ -match 'Access key ID' } | Select-Object -First 1
    $secCol = $row.PSObject.Properties.Name | Where-Object { $_ -match 'Secret access key' } | Select-Object -First 1
    if ($idCol -and $secCol) {
        $keyId  = $row.$idCol
        $secret = $row.$secCol
        Write-Host "    找到金鑰 $($keyId.Substring(0,[Math]::Min(8,$keyId.Length)))…（secret 不顯示）"
    } else {
        Write-Host "    這個 csv 沒有預期的欄位，改用手動輸入" -ForegroundColor Yellow
    }
}

# --- csv 讀不到就手動貼 ---
if (-not $keyId -or -not $secret) {
    Write-Host "    金鑰在 AWS Console 右上角帳號名 → Security credentials → Access keys"
    Write-Host ""
    $keyId = Read-Host "Access key ID"
    if (-not $keyId) { Write-Host "沒有輸入，取消" -ForegroundColor Yellow; exit 1 }
    # 用 AsSecureString 讓 secret 不會顯示在畫面上，也不會進 PowerShell 的歷史紀錄
    $secure = Read-Host "Secret access key（輸入時不會顯示）" -AsSecureString
    $secret = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    if (-not $secret) { Write-Host "沒有輸入，取消" -ForegroundColor Yellow; exit 1 }
}

$dir = Join-Path $env:USERPROFILE ".aws"
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }

# 不用 BOM：boto3 讀這個檔會把 BOM 當成 section 名稱的一部分，然後說找不到 [default]
$cred = "[default]`naws_access_key_id = $keyId`naws_secret_access_key = $secret`n"
[IO.File]::WriteAllText((Join-Path $dir "credentials"), $cred, [Text.UTF8Encoding]::new($false))
$conf = "[default]`nregion = ap-northeast-1`noutput = json`n"
[IO.File]::WriteAllText((Join-Path $dir "config"), $conf, [Text.UTF8Encoding]::new($false))

Write-Host ""
Write-Host "  已寫入 $dir\credentials（region = ap-northeast-1 東京）" -ForegroundColor Green
Write-Host "  驗證中…"
$check = python -c @"
import boto3
try:
    who = boto3.client('sts').get_caller_identity()
    print('OK ' + who['Arn'])
except Exception as e:
    print('FAIL ' + type(e).__name__ + ': ' + str(e)[:160])
"@
if ($check -like "OK *") {
    Write-Host "  ✅ $check" -ForegroundColor Green
    # 那個 csv 是明文的 secret 躺在「下載」資料夾，設定完就沒有留著的理由了
    if ($Csv -and (Test-Path $Csv)) {
        Write-Host ""
        Write-Host "  ⚠ $Csv 裡是明文的 secret，留在「下載」資料夾不安全。" -ForegroundColor Yellow
        if ((Read-Host "    現在刪掉嗎？(Y/n)") -notmatch '^[nN]') {
            Remove-Item $Csv -Force
            Write-Host "    已刪除" -ForegroundColor Green
        } else {
            Write-Host "    保留 —— 記得自己找時間刪" -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "  ❌ $check" -ForegroundColor Red
    Write-Host "     金鑰可能貼錯，或這組金鑰還沒生效（新建的偶爾要等幾秒）"
}
