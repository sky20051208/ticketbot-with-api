"""本機 HTTP proxy — 把 Chrome 的流量從**指定的本機 IP** 送出去。

為什麼要它（2026-07-31 搬 Oracle Ashburn 時發現）：
    eps 發的 `eps_sid` **綁發放時的出口 IP**。多開時每個 instance 用
    `curl_cffi(interface=<次要私有IP>)` 從不同公網 IP 出去，但登入用的 Chrome 預設走主要
    IP —— 兩邊 IP 不同，瀏覽器辛苦過完挑戰拿到的 cookie 一交給 curl_cffi 就作廢，
    直接被打回 401。所以 Chrome 也必須綁同一顆。

    Chrome 沒有「指定來源 IP」的參數，只有 `--proxy-server`。所以起一個 localhost proxy，
    由它代為連線並在連線時 `bind()` 到指定的本機 IP。

跟 [proxy_bridge.LocalProxyBridge](proxy_bridge.py) 的差別：那支是「轉發給上游 proxy 並補
認證」，這支是「自己直連目標，只是換一個來源 IP」。兩者都只 bind 127.0.0.1，不影響系統
其他流量；daemon thread，process 結束自動收。
"""
import socket
import threading

from tixcraftapi.proxy_bridge import pipe_both

CONNECT_TIMEOUT = 15


class BindingProxy:
    """localhost HTTP proxy，所有對外連線都從 `source_ip` 發出。

    用法:
        proxy = BindingProxy("10.0.0.88")
        port = proxy.start()      # 127.0.0.1:{port}，給 Chrome --proxy-server 用
    """

    def __init__(self, source_ip: str):
        self.source_ip = source_ip
        self.server_sock: socket.socket | None = None
        self.local_port: int = 0
        self._stop = threading.Event()

    def start(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(64)
        self.server_sock = sock
        self.local_port = sock.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return self.local_port

    def stop(self):
        self._stop.set()
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass

    def _connect_from_source(self, host: str, port: int) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(CONNECT_TIMEOUT)
        s.bind((self.source_ip, 0))     # ← 整支模組存在的理由就是這一行
        s.connect((host, port))
        s.settimeout(None)
        return s

    def _accept_loop(self):
        assert self.server_sock is not None
        self.server_sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                client, _ = self.server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket):
        upstream: socket.socket | None = None
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = client.recv(4096)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > 65536:
                    return          # 防 DoS

            head, _, body = buf.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            parts = lines[0].split(b" ")
            if len(parts) < 3:
                return
            method, target = parts[0].upper(), parts[1]

            if method == b"CONNECT":
                # https：建隧道，之後純轉發加密流量
                host, _, port = target.decode().rpartition(":")
                upstream = self._connect_from_source(host, int(port or 443))
                client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                # 明文 http：request line 是絕對網址，要改回 origin-form 再送給目標主機
                url = target.decode()
                rest = url.split("://", 1)[1] if "://" in url else url
                hostport, _, path = rest.partition("/")
                host, _, port = hostport.rpartition(":")
                if not host:                 # 沒有 :port 時 rpartition 會把整段放右邊
                    host, port = hostport, "80"
                upstream = self._connect_from_source(host, int(port or 80))
                lines[0] = b" ".join([parts[0], b"/" + path.encode(), parts[2]])
                upstream.sendall(b"\r\n".join(lines) + b"\r\n\r\n" + body)

            pipe_both(client, upstream)
        except Exception:
            pass
        finally:
            for s in (client, upstream):
                if s is not None:
                    try:
                        s.close()
                    except Exception:
                        pass
