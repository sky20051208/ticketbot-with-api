"""建立一個專用的 Chrome user-data-dir 給某個帳號登入用。

用法:
    python create_profile.py --name 帳號名

重點：這裡用「一般 Chrome」開（subprocess 直接啟動，不經 Selenium）。
Google 會擋 Selenium 控制的瀏覽器登入（"這個瀏覽器或應用程式可能有安全疑慮"），
一般 Chrome 沒有自動化旗標，Google 登入才正常。

流程:
    1. 在 chrome_profiles/帳號名/ 建一份獨立的 Chrome profile
    2. 開一般 Chrome 視窗，自動跳到 Tixcraft 登入頁
    3. 你手動登入 Tixcraft（Google 登入也 OK）
    4. 關閉 Chrome 視窗 → 回 terminal 按 Enter

之後 GUI 下拉選單就會看到這個帳號名，bot 用它開 Chrome 即為已登入狀態。
"""
import os
import argparse
import subprocess

import config

PROFILES_DIR = os.path.join(config.BASE_DIR, "chrome_profiles")

# Windows 上 Chrome 常見安裝位置
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]


def find_chrome() -> str:
    for p in CHROME_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError(
        "找不到 chrome.exe — 請把你的 Chrome 路徑加到 create_profile.py 的 CHROME_CANDIDATES"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True,
                        help="帳號名（會變成 chrome_profiles/ 底下的資料夾名）")
    args = parser.parse_args()

    profile_dir = os.path.join(PROFILES_DIR, args.name)
    os.makedirs(profile_dir, exist_ok=True)
    chrome = find_chrome()
    print(f"[PROFILE] 資料夾: {profile_dir}")
    print(f"[PROFILE] Chrome: {chrome}")

    # 用一般 Chrome 開（非 Selenium）→ Google 登入不會被擋
    proc = subprocess.Popen([
        chrome,
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "https://tixcraft.com/login",
    ])

    print("=" * 56)
    print("  1. 在開啟的 Chrome 視窗登入 Tixcraft（Google 登入也 OK）")
    print("  2. 登入完成後，關閉那個 Chrome 視窗")
    print("  3. 回到這裡按 Enter")
    print("=" * 56)
    input()

    # 確保 Chrome 真的關了 — profile 資料夾不能被鎖著，bot 才能用
    if proc.poll() is None:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=5)
        except Exception:
            pass

    print(f"[PROFILE] 登入狀態已存進 {profile_dir}")
    print(f"[PROFILE] 之後 GUI 下拉選單選 '{args.name}' 即可")


if __name__ == "__main__":
    main()
