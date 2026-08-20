"""本機 War-Room 網頁版後端。

功能：
- 管理多個 instance（profiles/acc_{id}/）
- spawn `python -m tixcraftapi`（拓元）/ `python -m kktix_api`（KKTIX）子進程，依平台分派
- 透過 pause.lock 暫停/繼續
- WebSocket 推 stdout 即時 log
"""
import asyncio
import json
import os
import platform
import subprocess
import sys
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional, Set

import requests
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config  # LINE_WORKER_URL / LINE_WORKER_ADMIN_KEY（客人資料存 Cloudflare D1，這裡只是 proxy）

PROJECT_DIR = Path(__file__).resolve().parent.parent
PROFILES_DIR = PROJECT_DIR / "profiles"
STATIC_DIR = Path(__file__).resolve().parent / "static"
CHROME_PROFILES_DIR = PROJECT_DIR / "chrome_profiles"
# chrome profile 按平台分子資料夾：chrome_profiles/<平台>/<名字>/。這些名字是子資料夾，
# 不是 profile 本身（掃舊扁平 profile 時要排除）。
PLATFORM_SUBDIRS = {"tixcraft", "kktix", "ticketplus"}

DEFAULT_EXCLUDE = "輪椅;身障;身心;障礙;Restricted View;燈柱遮蔽;視線不完整;身障票"
# chrome profile 下拉選單裡代表「不用 user-data-dir、用手貼 COOKIE」的選項
MANUAL_COOKIE_OPTION = "(手貼COOKIE)"


class InstanceConfig(BaseModel):
    PLATFORM: str = "TIXCRAFT"
    COOKIE: str = ""
    ACTIVITY_SLUG: str = "26_mltr"
    TICKET_AMOUNT: str = "1"
    REQUIRE_FULL_AMOUNT: bool = False   # 剩餘量 < TICKET_AMOUNT 就跳過，不買少一點
    AREA_KEYWORD: str = ""
    AREA_AUTO_SELECT_MODE: str = "關鍵字優先"
    CLEAR_MODE: str = "寬鬆"
    EXCLUDE_AREA_KEYWORD: str = DEFAULT_EXCLUDE
    DATE_KEYWORD: str = ""
    PRESALE_CODE: str = ""
    LIVENATION_START_URL: str = ""
    TARGET_START_TIME: str = "12:00:00"
    ENABLE_TIME_WATCHER: bool = True
    TIME_WATCH_URL: str = ""
    ENABLE_PROXY_POOL: bool = False
    # 美國 VPS 多 IP 架構：填 OCI VNIC 上的次要私有 IP（例 10.0.0.88），每個 instance 一顆，
    # 取代 proxy 做多開隔離。curl_cffi 和 Chrome 都會綁它。空 = 走預設路由
    LOCAL_BIND_IP: str = ""
    LINE_USER_ID: str = ""   # 客人的 LINE userId（LINE 客服 bot 登記取得；空 = 不推播）
    TICKET_FEE: str = ""     # 搶票費（每個客人不同，寫進通知訊息）
    chrome_profile: str = MANUAL_COOKIE_OPTION  # 純前端用，「當前平台」選的 chrome profile 名
    # 純前端用，每平台各記一個 profile（{PLATFORM: profile名}）；切平台時前端自動套用對應的，
    # save_config 也以這份為準決定 COOKIE_SOURCE / CHROME_USER_DATA_DIR
    chrome_profile_map: Dict[str, str] = {}


class InstanceState:
    def __init__(self, id: int, config: InstanceConfig):
        self.id = id
        self.config = config
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.status = "待命"
        self.log_buffer: deque = deque(maxlen=1000)
        self.subscribers: Set[asyncio.Queue] = set()
        self.reader_task: Optional[asyncio.Task] = None


instances: Dict[int, InstanceState] = {}


def profile_dir(id: int) -> Path:
    return PROFILES_DIR / f"acc_{id}"


def config_path(id: int) -> Path:
    return profile_dir(id) / "config.json"


def pause_path(id: int) -> Path:
    return profile_dir(id) / "pause.lock"


def load_config(id: int) -> InstanceConfig:
    p = config_path(id)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            known = {k: data[k] for k in InstanceConfig.model_fields if k in data}
            return InstanceConfig(**known)
        except Exception:
            pass
    return InstanceConfig()


def save_config(id: int, cfg: InstanceConfig, tile: dict):
    profile_dir(id).mkdir(parents=True, exist_ok=True)
    out = cfg.model_dump()
    # chrome_profile（前端選的）→ 轉成 Python 端用的 COOKIE_SOURCE / CHROME_USER_DATA_DIR
    # 以「當前平台」對應的 profile 為準（chrome_profile_map），沒有再 fallback chrome_profile
    prof = cfg.chrome_profile_map.get(cfg.PLATFORM) or cfg.chrome_profile
    use_userdata = bool(prof) and prof != MANUAL_COOKIE_OPTION
    out["COOKIE_SOURCE"] = "userdata" if use_userdata else "string"
    if use_userdata:
        sub = CHROME_PROFILES_DIR / cfg.PLATFORM.lower() / prof   # 平台子資料夾（新）
        flat = CHROME_PROFILES_DIR / prof                          # 舊版扁平（相容）
        path = sub if (sub.exists() or not flat.exists()) else flat
        out["CHROME_USER_DATA_DIR"] = str(path)
    else:
        out["CHROME_USER_DATA_DIR"] = ""
    out.update({
        "ACC_ID": id,
        "WINDOW_X": tile["x"],
        "WINDOW_Y": tile["y"],
        "WINDOW_W": tile["w"],
        "WINDOW_H": tile["h"],
    })
    config_path(id).write_text(
        json.dumps(out, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def compute_tile(id: int, n: int, screen_w: int, screen_h: int) -> dict:
    cols = 1 if n <= 1 else 2 if n <= 4 else 3 if n <= 9 else 4
    rows = -(-n // cols)
    win_w = screen_w // cols
    win_h = screen_h // rows
    col = id % cols
    row = id // cols
    return {"x": col * win_w, "y": row * win_h, "w": win_w, "h": win_h}


def set_status(inst: InstanceState, s: str):
    inst.status = s
    broadcast(inst, f"[STATUS] {s}")


def broadcast(inst: InstanceState, line: str):
    inst.log_buffer.append(line)
    for q in list(inst.subscribers):
        try:
            q.put_nowait(line)
        except asyncio.QueueFull:
            pass


async def kill_tree(proc: asyncio.subprocess.Process):
    if proc.returncode is not None:
        return
    if platform.system() == "Windows":
        # taskkill /T 連同子進程一起殺（Chrome 之類）
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=5,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    else:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        pass


async def stop_proc(inst: InstanceState):
    if inst.proc is None or inst.proc.returncode is not None:
        return
    await kill_tree(inst.proc)
    set_status(inst, "已停止")


async def read_proc_output(inst: InstanceState):
    proc = inst.proc
    assert proc is not None and proc.stdout is not None
    try:
        async for line_bytes in proc.stdout:
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            broadcast(inst, line)
    except Exception as e:
        broadcast(inst, f"[ERR] reader: {e}")
    rc = await proc.wait()
    set_status(inst, f"已結束 (code {rc})")


@asynccontextmanager
async def lifespan(app: FastAPI):
    PROFILES_DIR.mkdir(exist_ok=True)
    # Auto-load 既有 profiles
    for d in sorted(PROFILES_DIR.glob("acc_*")):
        try:
            id = int(d.name.split("_", 1)[1])
        except (ValueError, IndexError):
            continue
        instances[id] = InstanceState(id, load_config(id))
    yield
    # 關機時殺光所有子進程
    for inst in list(instances.values()):
        await stop_proc(inst)


app = FastAPI(lifespan=lifespan)

# 遠端登入閘道（客人用手機開連結，操作跑在本機的 Chrome 完成登入）
from remote_login.routes import router as remote_router  # noqa: E402
app.include_router(remote_router)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


DEFAULT_BIND_OPTION = "(預設出口)"


@app.get("/api/local_ips")
async def list_local_ips():
    """這台機器上可以拿來當出口的次要私有 IP（多開時每張卡片綁一顆）。

    直接讀網卡而不是讀設定檔 —— IP 是 `rotate_ips.py --sync-os` 在開機時跟 OCI
    對帳掛上去的，在 OCI Console 增減之後這裡自動反映，不用再改任何設定。
    非 Linux（本機開發）或沒有次要 IP 時只回「(預設出口)」，卡片就跟以前一樣走主 IP。
    """
    result = [DEFAULT_BIND_OPTION]
    try:
        out = subprocess.run(["ip", "-4", "addr", "show"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return result
    for line in out.splitlines():
        line = line.strip()
        # `inet 10.0.0.40/24 scope global secondary ens3` —— 只收 secondary，
        # 主 IP 不能拿來綁（那是 SSH / 管理走的，而且它配的是 ephemeral public IP）
        if line.startswith("inet ") and "secondary" in line:
            result.append(line.split()[1].split("/")[0])
    return result


@app.get("/api/chrome_profiles")
async def list_chrome_profiles(platform: str = ""):
    """回某平台的 chrome profile 清單（含「(手貼COOKIE)」）。

    profile 放 chrome_profiles/<平台小寫>/<名字>/；
    舊版扁平 chrome_profiles/<名字>/ 視為 TIXCRAFT（向後相容）。
    """
    result = [MANUAL_COOKIE_OPTION]
    seen: Set[str] = set()
    plat = (platform or "").lower()

    if plat:
        sub = CHROME_PROFILES_DIR / plat
        if sub.exists():
            for d in sorted(sub.iterdir()):
                if d.is_dir() and d.name not in seen:
                    result.append(d.name)
                    seen.add(d.name)

    # 向後相容：扁平 profile（舊版只登拓元）→ 歸到 TIXCRAFT
    if not platform or plat == "tixcraft":
        if CHROME_PROFILES_DIR.exists():
            for d in sorted(CHROME_PROFILES_DIR.iterdir()):
                if d.is_dir() and d.name.lower() not in PLATFORM_SUBDIRS and d.name not in seen:
                    result.append(d.name)
                    seen.add(d.name)
    return result


# 客人資料存在 Cloudflare Workers + D1（LineBotWorker/），這裡的 /api/customers
# 全是 proxy：webgui 帶 ADMIN_KEY 打 Worker 的同名 API，本機不再存客人資料庫。
# 這樣客人用客服 bot 登記時（電腦關著也行）跟 GUI 看到的是同一份資料。

def _worker_headers() -> dict:
    return {"X-Admin-Key": config.LINE_WORKER_ADMIN_KEY, "Content-Type": "application/json"}


def _require_worker_url():
    if not config.LINE_WORKER_URL:
        raise HTTPException(400, "尚未設定 config.py 的 LINE_WORKER_URL（LineBotWorker 部署後填）")


class Customer(BaseModel):
    name: str
    user_id: str
    concert: str = ""


@app.get("/api/customers")
async def list_customers():
    if not config.LINE_WORKER_URL:
        return []  # 還沒部署 Worker 時讓 GUI 照常開，只是客人下拉是空的
    try:
        res = requests.get(f"{config.LINE_WORKER_URL}/api/customers",
                           headers=_worker_headers(), timeout=8)
        res.raise_for_status()
        return res.json()
    except requests.RequestException as e:
        raise HTTPException(502, f"連不到 LINE Worker: {e}")


@app.post("/api/customers")
async def add_customer(c: Customer):
    uid = c.user_id.strip()
    if not uid:
        raise HTTPException(400, "user_id 不能為空")
    _require_worker_url()
    res = requests.post(f"{config.LINE_WORKER_URL}/api/customers",
                        headers=_worker_headers(),
                        data=json.dumps({"name": c.name.strip(), "user_id": uid,
                                        "concert": c.concert.strip()}),
                        timeout=8)
    if res.status_code != 200:
        raise HTTPException(502, f"LINE Worker 回應 {res.status_code}: {res.text[:200]}")
    return {"ok": True}


@app.delete("/api/customers/{user_id}")
async def delete_customer(user_id: str):
    _require_worker_url()
    res = requests.delete(f"{config.LINE_WORKER_URL}/api/customers/{user_id}",
                          headers=_worker_headers(), timeout=8)
    if res.status_code == 404:
        raise HTTPException(404)
    if res.status_code != 200:
        raise HTTPException(502, f"LINE Worker 回應 {res.status_code}: {res.text[:200]}")
    return {"ok": True}


@app.get("/api/instances")
async def list_instances():
    return [
        {
            "id": i.id,
            "config": i.config.model_dump(),
            "status": i.status,
            "running": i.proc is not None and i.proc.returncode is None,
            "paused": pause_path(i.id).exists(),
        }
        for i in sorted(instances.values(), key=lambda x: x.id)
    ]


class InitReq(BaseModel):
    count: int


@app.post("/api/init")
async def init_instances(req: InitReq):
    if req.count < 1 or req.count > 64:
        raise HTTPException(400, "count must be 1..64")
    for inst in list(instances.values()):
        await stop_proc(inst)
    new: Dict[int, InstanceState] = {}
    for i in range(req.count):
        if i in instances:
            new[i] = instances[i]
        else:
            new[i] = InstanceState(i, load_config(i))
    instances.clear()
    instances.update(new)
    return {"ok": True, "count": len(instances)}


@app.put("/api/instances/{id}/config")
async def update_config(id: int, cfg: InstanceConfig):
    """改欄位時前端會呼叫這支，但**只更新記憶體**，不落地。
    要寫進 profiles/acc_N/config.json 得走 /save（或按 START，那條會順便存）。"""
    if id not in instances:
        raise HTTPException(404)
    instances[id].config = cfg
    return {"ok": True}


class SaveReq(BaseModel):
    screen_w: int = 1920
    screen_h: int = 1040


@app.post("/api/instances/{id}/save")
async def save_only(id: int, req: SaveReq):
    """只存設定、不啟動。

    以前只有 START 會呼叫 save_config，所以「設定好但先不跑」根本沒辦法留下來 ——
    關掉瀏覽器就沒了，也沒辦法給外部工具（bench / 診斷腳本）讀 profiles/acc_N/config.json。
    """
    if id not in instances:
        raise HTTPException(404)
    inst = instances[id]
    tile = compute_tile(id, len(instances), req.screen_w, req.screen_h)
    save_config(id, inst.config, tile)
    return {"ok": True, "path": str(config_path(id))}


class StartReq(BaseModel):
    screen_w: int = 1920
    screen_h: int = 1040


async def _start_one(id: int, req: StartReq) -> dict:
    inst = instances[id]
    if inst.proc is not None and inst.proc.returncode is None:
        return {"ok": False, "msg": "already running"}

    pp = pause_path(id)
    if pp.exists():
        pp.unlink()

    n = len(instances)
    tile = compute_tile(id, n, req.screen_w, req.screen_h)
    save_config(id, inst.config, tile)

    # 依平台分派，其餘（拓元）→ tixcraftapi
    module = {"KKTIX": "kktix_api",
              "TICKETPLUS": "ticketplus_api"}.get(inst.config.PLATFORM, "tixcraftapi")
    args = [
        sys.executable, "-u", "-m", module,
        "--config", str(config_path(id)),
    ]

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(PROJECT_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except Exception as e:
        set_status(inst, "啟動失敗")
        broadcast(inst, f"[ERROR] {e}")
        return {"ok": False, "msg": str(e)}

    inst.proc = proc
    inst.log_buffer.clear()
    set_status(inst, f"運行中 (PID {proc.pid})")
    inst.reader_task = asyncio.create_task(read_proc_output(inst))
    return {"ok": True, "pid": proc.pid}


@app.post("/api/instances/{id}/start")
async def start_instance(id: int, req: StartReq):
    if id not in instances:
        raise HTTPException(404)
    return await _start_one(id, req)


@app.post("/api/instances/{id}/stop")
async def stop_instance(id: int):
    if id not in instances:
        raise HTTPException(404)
    await stop_proc(instances[id])
    return {"ok": True}


@app.post("/api/instances/{id}/pause")
async def pause_instance(id: int):
    if id not in instances:
        raise HTTPException(404)
    inst = instances[id]
    pp = pause_path(id)
    profile_dir(id).mkdir(parents=True, exist_ok=True)
    if pp.exists():
        pp.unlink()
        broadcast(inst, "[PAUSE] 已繼續")
        return {"paused": False}
    pp.write_text("paused", encoding="utf-8")
    broadcast(inst, "[PAUSE] 已暫停")
    return {"paused": True}


@app.post("/api/start_all")
async def start_all(req: StartReq):
    return [{"id": i, **await _start_one(i, req)} for i in sorted(instances.keys())]


@app.post("/api/stop_all")
async def stop_all():
    for inst in list(instances.values()):
        await stop_proc(inst)
    return {"ok": True}


@app.websocket("/ws/{id}")
async def ws_logs(ws: WebSocket, id: int):
    await ws.accept()
    if id not in instances:
        await ws.close(code=1003)
        return
    inst = instances[id]
    queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
    inst.subscribers.add(queue)
    try:
        # 先送 backlog
        for line in list(inst.log_buffer):
            await ws.send_text(line)
        # 再送即時
        while True:
            line = await queue.get()
            await ws.send_text(line)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        inst.subscribers.discard(queue)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
