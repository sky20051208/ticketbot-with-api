"""遠端登入 FastAPI router（掛進 webgui/server.py）。

owner 端（你，在 War-Room 網頁裡）：
    GET  /remote-admin                     管理頁：設定對外網址、開 session、拿連結、看狀態
    POST /api/remote/session               開一個新 session → 回 {token, link}
    GET  /api/remote/sessions              列出所有 session 狀態 + cookie
    POST /api/remote/session/{token}/close 手動收掉
    POST /api/remote/public_base          設定 cloudflared 對外網址

客人端（手機開連結）：
    GET  /remote/{token}                   手機操作頁（HTML）
    WS   /remote/ws/{token}                畫面串流（server→binary JPEG）+ 輸入（client→JSON）
"""
import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .gateway import registry

STATIC_DIR = Path(__file__).resolve().parent / "static"

router = APIRouter()


class CreateSessionReq(BaseModel):
    platform: str = "tixcraft"    # tixcraft / kktix
    name: str                     # 帳號名（= chrome profile 資料夾名）
    proxy_url: str = ""


class PublicBaseReq(BaseModel):
    public_base: str


# ---- owner ----------------------------------------------------------------
@router.get("/remote-admin", response_class=HTMLResponse)
async def remote_admin():
    return FileResponse(str(STATIC_DIR / "admin.html"))


@router.post("/api/remote/public_base")
async def set_public_base(req: PublicBaseReq):
    registry.public_base = req.public_base.strip()
    return {"public_base": registry.public_base}


@router.post("/api/remote/session")
async def create_session(req: CreateSessionReq):
    if not req.name.strip():
        raise HTTPException(400, "name 不可空白")
    if req.platform not in ("tixcraft", "kktix"):
        raise HTTPException(400, "platform 只能 tixcraft / kktix")
    s = await registry.create(req.platform, req.name.strip(), req.proxy_url.strip())
    return {"token": s.token, "link": registry.link_for(s.token),
            "platform": s.platform, "name": s.name}


@router.get("/api/remote/sessions")
async def list_sessions():
    out = []
    for tok, s in registry.sessions.items():
        out.append({
            "token": tok, "platform": s.platform, "name": s.name,
            "status": s.status, "has_cookie": bool(s.cookie_str),
            "cookie": s.cookie_str, "link": registry.link_for(tok),
            "created_at": s.created_at,
        })
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return {"public_base": registry.public_base, "sessions": out}


@router.post("/api/remote/session/{token}/close")
async def close_session(token: str):
    await registry.close(token)
    return {"ok": True}


# ---- 客人 -----------------------------------------------------------------
@router.get("/remote/{token}", response_class=HTMLResponse)
async def remote_page(token: str):
    if registry.get(token) is None:
        return HTMLResponse("<h2>連結已失效或尚未建立，請向店家重新索取。</h2>", status_code=404)
    return FileResponse(str(STATIC_DIR / "remote.html"))


@router.websocket("/remote/ws/{token}")
async def remote_ws(ws: WebSocket, token: str):
    s = registry.get(token)
    if s is None:
        await ws.close(code=4404)
        return
    await ws.accept()
    s.ws = ws
    await s.request_keyframe()   # 逼一張目前畫面，客人一連上就看得到（不然要等頁面變化）
    try:
        while True:
            raw = await ws.receive_text()
            try:
                m = json.loads(raw)
            except Exception:
                continue
            if m.get("t") == "done":
                await s.finish()                                  # 停 screencast + 撈 cookie（秒級）
                try:
                    await ws.send_text(json.dumps({"t": "finished"}))   # 立刻放行客人
                except Exception:
                    pass
                asyncio.create_task(s.close())                    # 關 Chrome 丟背景，不擋客人
                break
            await s.dispatch_input(m)
    except WebSocketDisconnect:
        pass
    finally:
        if s.ws is ws:
            s.ws = None
