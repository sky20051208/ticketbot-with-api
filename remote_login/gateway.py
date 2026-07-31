"""遠端登入 session 管理：一個 session = 一隻 nodriver Chrome + 一條 screencast 串流。

事件迴圈：nodriver 是 async、綁在建立它的 loop。所有 session 都在 FastAPI/uvicorn 的
loop 裡 await 啟動，screencast 事件 handler 與 WebSocket 收發也都跑同一個 loop，天生同步。

畫面座標：用 Emulation.setDeviceMetricsOverride 把 Chrome 視窗模擬成手機視埠（VIEW_W×VIEW_H
CSS px），screencast 以 2x 解析度輸出（手機看得清楚）。客人端把觸控位置換算回 0..VIEW_W /
0..VIEW_H 這個 CSS px 空間再傳來，Input.* 就直接吃這組座標，不用再換算。
"""
import asyncio
import base64
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import nodriver
from nodriver import cdp

from browser_login import setup_proxy_bridge  # 複用 localhost proxy bridge（自動補 auth header）

PROJECT_DIR = Path(__file__).resolve().parent.parent
CHROME_PROFILES_DIR = PROJECT_DIR / "chrome_profiles"

# 客人手機視埠（CSS px）。輸入座標一律在這個空間；screencast 影像可以是它的 2x。
VIEW_W = 390
VIEW_H = 844
SCREENCAST_QUALITY = 72           # JPEG 品質（清晰度 vs 流量）
SCREENCAST_SCALE = 2              # 影像解析度倍率：force-device-scale-factor + screencast 上限都用它

# 各平台登入起始頁 / 首頁（跟 create_profile.py 同一組，profile 也落同一個資料夾結構，
# 這樣 GUI 的 chrome profile 下拉直接掃得到）
PLATFORM_LOGIN = {
    "tixcraft": "https://tixcraft.com/login",
    "kktix": "https://kktix.com/users/sign_in",
}
PLATFORM_COOKIE_DOMAIN = {
    "tixcraft": "tixcraft.com",
    "kktix": "kktix.com",
}

# 特殊鍵：一般字元走 Input.insertText，這些控制鍵要送 keyDown/keyUp
def _raw_get_cookies():
    """自寫 CDP generator 取 cookie，繞過 nodriver 的 typed 解析、直接拿原始 dict。
    坑（2026-07 花時間踩過）：新版 Chrome 的 Network.getCookies 回應不再送 sameParty 欄位，
    nodriver 的 cdp.network.Cookie.from_json 硬要這欄 → KeyError 在收訊迴圈（Connection._listener）
    炸掉、該請求的 future 永不 resolve → 連 storage/network.get_cookies、b.cookies.get_all() 全部 timeout。
    原始回應其實含完整 cookie（含 httpOnly 明文），直接讀 dict 一勞永逸、也不怕之後欄位再變。"""
    result = yield {"method": "Network.getCookies", "params": {}}
    return result


_SPECIAL_KEYS = {
    "Enter":     dict(key="Enter", code="Enter", windows_virtual_key_code=13, text="\r"),
    "Backspace": dict(key="Backspace", code="Backspace", windows_virtual_key_code=8),
    "Tab":       dict(key="Tab", code="Tab", windows_virtual_key_code=9),
}


@dataclass
class RemoteSession:
    token: str
    platform: str            # "tixcraft" / "kktix"
    name: str                # 帳號名（= chrome_profiles/<platform>/<name>/）
    proxy_url: str = ""
    status: str = "created"  # created → live → done → closed
    cookie_str: str = ""
    created_at: float = field(default_factory=time.time)

    browser: Optional[nodriver.Browser] = None
    tab: Optional[nodriver.Tab] = None
    ws: Optional[object] = None      # 目前連著的客人 WebSocket（同時只服務一個客人）
    _sending: bool = False           # 一次只送一張，送的當下來的新 frame 直接丟（降延遲）
    _screencast_on: bool = False
    _finishing: bool = False          # 收尾中：frame handler 立刻停 ack，Chrome 靠 backpressure 暫停送 frame，清空 CDP 通道

    @property
    def profile_dir(self) -> Path:
        return CHROME_PROFILES_DIR / self.platform / self.name

    # ---- 生命週期 -------------------------------------------------------
    async def start(self):
        """開 Chrome、模擬手機視埠、開 screencast。"""
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-blink-features=AutomationControlled",
            # 視窗寬度要「對齊」模擬視埠寬（VIEW_W）——headful 下若視窗比模擬視埠寬，Chrome
            # 會像 DevTools 手機模式那樣把頁面畫在左邊、右邊一大片留白，screencast 連留白一起截
            # → 客人看到「畫面跑掉」而且內容只佔 1/3 超糊。對齊後滿版、又清楚。
            f"--window-size={VIEW_W + 6},{VIEW_H + 40}",
            # 拉高 raster 解析度 → frame 像素變大、手機上更清晰（headful 不吃 emulation 的 dsf，
            # 要靠這個 flag 才會 2x）
            f"--force-device-scale-factor={SCREENCAST_SCALE}",
            # 注意：不能開在螢幕外或最小化 — Chrome 對沒被合成的視窗停止 render，
            # screencast 就收不到 frame（跟 LineBot 截圖那個坑同源）。固定開在左上角。
            "--window-position=40,40",
        ]
        # proxy：複用 browser_login 的 localhost bridge（Chrome 只看到 no-auth localhost proxy）
        local_port = setup_proxy_bridge(self.proxy_url) if self.proxy_url else None
        if local_port is not None:
            args.append(f"--proxy-server=http://127.0.0.1:{local_port}")
            args.append("--webrtc-ip-handling-policy=disable_non_proxied_udp")

        self.browser = await nodriver.start(
            user_data_dir=str(self.profile_dir),
            headless=False,
            browser_args=args,
            lang="zh-TW",
        )
        login_url = PLATFORM_LOGIN.get(self.platform, PLATFORM_LOGIN["tixcraft"])
        self.tab = await self.browser.get(login_url)

        await self.tab.send(cdp.emulation.set_device_metrics_override(
            width=VIEW_W, height=VIEW_H, device_scale_factor=SCREENCAST_SCALE, mobile=True))
        await self.tab.send(cdp.emulation.set_touch_emulation_enabled(
            enabled=True, max_touch_points=1))

        await self.tab.send(cdp.page.enable())   # 不 enable Page domain 收不到 screencast 事件
        self.tab.add_handler(cdp.page.ScreencastFrame, self._on_frame)
        await self._start_screencast()
        self._screencast_on = True
        self.status = "live"
        print(f"[REMOTE] session {self.token} 已開 Chrome：{self.platform}/{self.name}")

    async def _start_screencast(self):
        await self.tab.send(cdp.page.start_screencast(
            format_="jpeg", quality=SCREENCAST_QUALITY,
            max_width=VIEW_W * SCREENCAST_SCALE, max_height=VIEW_H * SCREENCAST_SCALE,
            every_nth_frame=1))

    async def request_keyframe(self):
        """客人剛連上時叫一張新畫面 — screencast 只在畫面有變化時送 frame，
        重下 startScreencast 會逼 Chrome 立刻吐一張目前畫面，避免初始空白。"""
        if self.tab is not None and self._screencast_on:
            try:
                await self._start_screencast()
            except Exception:
                pass

    async def _on_frame(self, ev, *args):
        """screencast 每張 frame：一定要 ack（不 ack Chrome 會停送），再轉發給客人。"""
        if self._finishing:
            return   # 收尾中故意不 ack → Chrome 暫停送 frame，清空通道好讓 getCookies 秒回
        try:
            await self.tab.send(cdp.page.screencast_frame_ack(session_id=ev.session_id))
        except Exception:
            return
        ws = self.ws
        if ws is None or self._sending:
            return
        # 用背景 task 送、送的期間來的 frame 直接丟 → 只送得出去的最新一張，延遲最低
        self._sending = True
        asyncio.create_task(self._send_frame(ws, ev.data))

    async def _send_frame(self, ws, data_b64: str):
        try:
            await ws.send_bytes(base64.b64decode(data_b64))
        except Exception:
            pass
        finally:
            self._sending = False

    # ---- 客人輸入 -------------------------------------------------------
    async def dispatch_input(self, m: dict):
        """處理客人端傳來的一則輸入事件（座標已在 VIEW_W×VIEW_H CSS px 空間）。"""
        if self.tab is None:
            return
        t = m.get("t")
        try:
            if t == "tap":
                x, y = float(m["x"]), float(m["y"])
                for phase in ("mousePressed", "mouseReleased"):
                    await self.tab.send(cdp.input_.dispatch_mouse_event(
                        phase, x=x, y=y, button=cdp.input_.MouseButton.LEFT, click_count=1))
            elif t == "scroll":
                await self.tab.send(cdp.input_.dispatch_mouse_event(
                    "mouseWheel", x=float(m.get("x", VIEW_W / 2)), y=float(m.get("y", VIEW_H / 2)),
                    delta_x=float(m.get("dx", 0)), delta_y=float(m.get("dy", 0))))
            elif t == "text":
                await self.tab.send(cdp.input_.insert_text(str(m.get("text", ""))))
            elif t == "key":
                await self._send_key(str(m.get("key", "")))
        except Exception as e:
            print(f"[REMOTE] input 失敗 {t}: {e}")

    async def _send_key(self, name: str):
        spec = _SPECIAL_KEYS.get(name)
        if not spec:
            return
        await self.tab.send(cdp.input_.dispatch_key_event("keyDown", **spec))
        up = {k: v for k, v in spec.items() if k != "text"}
        await self.tab.send(cdp.input_.dispatch_key_event("keyUp", **up))

    # ---- 收尾 -----------------------------------------------------------
    async def finish(self):
        """客人按「完成登入」：撈明文 cookie（含 httpOnly）。profile 已寫在磁碟，搶票 bot 用
        userdata 模式指同一個資料夾即可。**瀏覽器的關閉交給 close() 在背景做，不讓客人等。**

        坑：一定要先停 screencast 再撈 cookie。screencast 還開著時 frame 會狂灌那條 CDP
        websocket，getCookies 的指令/回應排在一大堆 frame 後面 → 客人「處理中」卡好幾分鐘。
        先停掉清空通道，撈 cookie 就變瞬間。"""
        if self.status in ("done", "closed"):
            return
        t0 = time.monotonic()
        self._finishing = True       # 先停 ack 讓 frame 洪流靠 backpressure 停下，通道清空
        self._screencast_on = False
        await asyncio.sleep(0.1)      # 給 in-flight 的 frame 事件排空一下
        try:
            await asyncio.wait_for(self.tab.send(cdp.page.stop_screencast()), timeout=5)
        except Exception as e:
            print(f"[REMOTE] stop_screencast: {e}")
        domain = PLATFORM_COOKIE_DOMAIN.get(self.platform, "")
        cookies = []
        try:
            resp = await asyncio.wait_for(self.tab.send(_raw_get_cookies()), timeout=15)
            cookies = resp.get("cookies", []) if isinstance(resp, dict) else []
        except Exception as e:
            print(f"[REMOTE] get_cookies 失敗: {type(e).__name__} {e}")
        picked = [c for c in cookies if domain in (c.get("domain") or "")]
        self.cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in picked)
        print(f"[REMOTE] session {self.token} 共 {len(cookies)} 個 cookie，符合 {domain} 的 {len(picked)} 個"
              f"（{time.monotonic()-t0:.2f}s）")
        self.status = "done"

    async def close(self):
        if self.browser is not None:
            try:
                if self._screencast_on and self.tab is not None:
                    await self.tab.send(cdp.page.stop_screencast())
            except Exception:
                pass
            try:
                self.browser.stop()
            except Exception:
                pass
            self.browser = None
            self.tab = None
        if self.status != "done":
            self.status = "closed"
        print(f"[REMOTE] session {self.token} 已關閉（status={self.status}）")


class SessionRegistry:
    """token → RemoteSession。webgui 全域共用一份。"""
    def __init__(self):
        self.sessions: Dict[str, RemoteSession] = {}
        self.public_base: str = ""   # cloudflared 對外網址，owner 在管理頁貼一次

    def get(self, token: str) -> Optional[RemoteSession]:
        return self.sessions.get(token)

    async def create(self, platform: str, name: str, proxy_url: str = "") -> RemoteSession:
        import secrets
        token = secrets.token_urlsafe(8)
        s = RemoteSession(token=token, platform=platform, name=name, proxy_url=proxy_url)
        self.sessions[token] = s
        await s.start()
        return s

    async def close(self, token: str):
        s = self.sessions.get(token)
        if s:
            await s.close()

    def link_for(self, token: str) -> str:
        base = self.public_base.rstrip("/")
        return f"{base}/remote/{token}" if base else f"/remote/{token}"


registry = SessionRegistry()
