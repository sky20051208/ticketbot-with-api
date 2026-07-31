"""音效警報：撞 403 / 進 checkout 時播音效。

固定路徑（放專案根目錄）：
  sounds/403.wav        撞 403 時播
  sounds/checkout.wav   進 checkout 終局時播

檔案不存在 → 靜默不播。播放走 winsound（Windows 內建），非阻塞，不影響搶票。
3 秒 debounce 避免短時間連續 403 把音效塞爆。

**跑在美東 VPS 時音效沒有意義**（無音效裝置、人也不在機器旁），所以非 Windows 平台
直接跳過不播，只留 print —— 真正的通知管道是 LINE 推播（LineBot/line_push.py）。
"""
import os
import sys
import time

# 專案根目錄（tixcraftapi/ 的上一層）
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOUND_403 = os.path.join(_HERE, "sounds", "403.wav")
SOUND_CHECKOUT = os.path.join(_HERE, "sounds", "checkout.wav")

_last_403_at = 0.0
_DEBOUNCE_403 = 3.0  # 秒


def _play(path: str):
    """非阻塞播放 wav。檔案不存在 / 非 Windows 都靜默跳過。"""
    if sys.platform != "win32":
        return                      # VPS 上沒有音效裝置，print 那行就是全部的通知
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
