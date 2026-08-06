# 把 AWS 金鑰寫進 ~/.aws/credentials（東京機用）。
#
# 這支存在的意義是「金鑰不要經過聊天視窗」—— 你在自己的終端機貼，直接寫進檔案。
#
# 金鑰怎麼拿（一次性）：
#   1. AWS Console 右上角你的帳號名 → Security credentials
#   2. 往下找 Access keys → Create access key
#   3. Use case 選 Command Line Interface (CLI)，勾下面那個確認框 → Next → Create
#   4. 把 Access key ID 和 Secret access key 複製下來（**Secret 只會顯示這一次**）

$ErrorActionPreference = "Stop"

Write-Host "==> 設定 AWS 金鑰（東京機用）" -ForegroundColor Cyan
Write-Host "    金鑰在 AWS Console 右上角帳號名 → Security credentials → Access keys"
Write-Host ""

$keyId = Read-Host "Access key ID"
if (-not $keyId) { Write-Host "沒有輸入，取消" -ForegroundColor Yellow; exit 1 }
# 用 AsSecureString 讓 secret 不會顯示在畫面上，也不會進 PowerShell 的歷史紀錄
$secure = Read-Host "Secret access key（輸入時不會顯示）" -AsSecureString
$secret = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
if (-not $secret) { Write-Host "沒有輸入，取消" -ForegroundColor Yellow; exit 1 }

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
} else {
    Write-Host "  ❌ $check" -ForegroundColor Red
    Write-Host "     金鑰可能貼錯，或這組金鑰還沒生效（新建的偶爾要等幾秒）"
}
