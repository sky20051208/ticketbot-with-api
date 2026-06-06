"""打包朋友版 zip：TicketBot_朋友版.zip。

把 ticketbotWithApi 內 runtime 必要的檔案壓成 zip 給朋友用。
排除：訓練素材、profile、cache、git、venv 等。

跑法：python pack_friend_zip.py
輸出：TicketBot_朋友版.zip (覆蓋舊版)
"""
import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
ZIP_PATH = ROOT / "TicketBot_朋友版.zip"
PREFIX = "TicketBot_朋友版/"

# runtime 必要：朋友拿到後 `setup.bat → start.bat` 就能跑
FILES = [
    "main.py",
    "bot.py",
    "browser_login.py",
    "config.py",
    "create_profile.bat",
    "create_profile.py",
    "proxy_pool.py",
    "requirements.txt",
    "run_webgui.py",
    "setup.bat",
    "start.bat",
    "timeWatcher.py",
    "使用說明.txt",
    "captchaAI/__init__.py",
    "captchaAI/predict.py",
    "captchaAI/tixcraft_ocr.onnx",   # 7MB 自訓 model
    "webgui/__init__.py",
    "webgui/server.py",
]

# 整個目錄打包（會自動排除 __pycache__）
DIRS = [
    "tixcraftapi",      # API 模式整套（含 state.py / runner.py FSM / alerts.py）
    "kktix",
    "ticketplus",
    "webgui/static",
    "sounds",           # 音效檔（朋友放自己的 403.wav / checkout.wav 進來）
]

EXCLUDE_NAMES = {"__pycache__", ".pyc"}


def _should_skip(p: Path) -> bool:
    parts = p.parts
    return any(seg in EXCLUDE_NAMES or seg.endswith(".pyc") for seg in parts)


def main():
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()

    n_files = 0
    total_bytes = 0
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        # 個別檔
        for rel in FILES:
            src = ROOT / rel
            if not src.exists():
                print(f"  [WARN] 缺檔: {rel}")
                continue
            arc = PREFIX + rel
            z.write(src, arc)
            n_files += 1
            total_bytes += src.stat().st_size

        # 目錄 recursive
        for d in DIRS:
            base = ROOT / d
            if not base.exists():
                print(f"  [WARN] 缺目錄: {d}")
                continue
            for p in base.rglob("*"):
                if not p.is_file() or _should_skip(p.relative_to(ROOT)):
                    continue
                rel = p.relative_to(ROOT).as_posix()
                z.write(p, PREFIX + rel)
                n_files += 1
                total_bytes += p.stat().st_size

    print(f"\n[DONE] {ZIP_PATH.name}")
    print(f"  檔案數: {n_files}")
    print(f"  原始尺寸: {total_bytes / 1024 / 1024:.1f} MB")
    print(f"  壓縮後尺寸: {ZIP_PATH.stat().st_size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    main()
