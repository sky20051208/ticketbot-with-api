"""音效警報：撞 403 / 進 checkout 時播音效。

固定路徑（放專案根目錄）：
  sounds/403.wav        撞 403 時播
  sounds/checkout.wav   進 checkout 終局時播

檔案不存在 → 靜默不播。播放走 winsound（Windows 內建），非阻塞，不影響搶票。
3 秒 debounce 避免短時間連續 403 把音效塞爆。
"""
import os
import time

# 專案根目錄（tixcraftapi/ 的上一層）
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOUND_403 = os.path.join(_HERE, "sounds", "403.wav")
SOUND_CHECKOUT = os.path.join(_HERE, "sounds", "checkout.wav")

_last_403_at = 0.0
_DEBOUNCE_403 = 3.0  # 秒


def _play(path: str):
    """非阻塞播放 wav。檔案不存在/平台不支援都靜默。"""
    if not path or not os.path.exists(path):
        return
    try:
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception as e:
        print(f"[ALERT] 播放 {os.path.basename(path)} 失敗: {e}")


def play_403(label: str = ""):
    """撞 403 時呼叫。3 秒內重複呼叫只播一次。"""
    global _last_403_at
    now = time.monotonic()
    if now - _last_403_at < _DEBOUNCE_403:
        return
    _last_403_at = now
    print(f"[ALERT] {label or ''} 撞 403，播警報")
    _play(SOUND_403)


def play_checkout():
    """進 checkout 終局時呼叫。"""
    print("[ALERT] 進 checkout，播成功音效")
    _play(SOUND_CHECKOUT)


class Blocked403(Exception):
    """Server 回 403 → FSM 攔下做 8s cooldown + 同 state 重試（不 fallback GAME）。"""
    pass
