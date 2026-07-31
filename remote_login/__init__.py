"""遠端登入閘道：客人用手機開一條連結，就能操作跑在你機器上的 Chrome 完成登入。

為什麼要這樣：
    - 客人常用手機、沒有 DevTools，bookmarklet 又被 httpOnly cookie 擋死，客人端自己
      匯出登入資料基本不可能。
    - Chrome 在 Windows 的 cookie 用 DPAPI 綁「該 Windows 使用者帳號」加密，整包 profile
      複製到你電腦解不開 → 直接搬 profile 這條路死。
    - 解法：讓登入「發生在你的機器上」。客人只是透過網頁遠端操作，cookie 天生落在你機器，
      搶票 bot（userdata 模式）現成就能用；同機器同 IP，風控也最一致。

架構：
    remote_login/gateway.py  ── 每個 session 用 nodriver 開一隻獨立 profile 的 Chrome，
                                CDP Page.startScreencast 把畫面串成 JPEG，Input.* 把客人
                                的觸控/鍵盤打回去；登入完 storage.getCookies 撈明文 cookie。
    remote_login/routes.py   ── FastAPI router（掛進 webgui/server.py），發連結 / 客人頁 /
                                WebSocket 串流 / owner 管理頁。
    remote_login/static/     ── 客人手機頁 + owner 管理頁（純前端）。

對外網址走 cloudflared quick tunnel（見 tunnel_start.bat），不用網域、不用開 port。
"""
