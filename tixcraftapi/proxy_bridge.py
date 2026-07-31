"""本機 HTTP CONNECT proxy bridge — Chrome 連 localhost (無 auth) → bridge 補 auth → CliProxy。

為什麼要它：
    Chrome 不認 cmdline 的 http://user:pass@host:port inline auth；MV3 extension 用
    webRequest.onAuthRequired 在 service worker 還沒暖機時會 race 到 dialog 跳出來。
    走 localhost bridge 最穩 — Chrome 不必處理 auth，bridge 自己塞 Proxy-Authorization。

效能影響：
    bridge 只 bind 127.0.0.1，OS 系統其他流量完全不經過。loopback 轉發開銷可忽略
    （比一次網路 RTT 小幾個數量級）。daemon thread，process 結束自動回收。
"""
import base64
import socket
import threading


def pipe_both(a: socket.socket, b: socket.socket):
    """雙向轉發直到任一邊斷。給 LocalProxyBridge 和 bind_proxy.BindingProxy 共用。"""
    def copy(src: socket.socket, dst: socket.socket):
        try:
            while True:
                data = src.recv(16384)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    t1 = threading.Thread(target=copy, args=(a, b), daemon=True)
    t2 = threading.Thread(target=copy, args=(b, a), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()


class LocalProxyBridge:
    """單一上游 proxy + 認證的 localhost forwarder。

    用法:
        bridge = LocalProxyBridge("sg2.cliproxy.io", 3010, "user", "pass")
        port = bridge.start()   # 拿到 127.0.0.1:{port}，給 Chrome --proxy-server 用
        # ... 用完 process 結束時 daemon thread 自動收
    """

    def __init__(self, upstream_host: str, upstream_port: int,
                 upstream_user: str, upstream_pass: str):
        self.upstream_host = upstream_host
        self.upstream_port = upstream_port
        token = f"{upstream_user}:{upstream_pass}".encode()
        self.auth_header = b"Proxy-Authorization: Basic " + base64.b64encode(token)
        self.server_sock: socket.socket | None = None
        self.local_port: int = 0
        self._stop = threading.Event()

    def start(self) -> int:
        """Bind 隨機 127.0.0.1 port，起 accept loop（daemon thread），回 port。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(32)
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
            # 讀第一個 request 的 header 區塊（CONNECT 或 GET ... HTTP/1.1）
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = client.recv(4096)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > 65536:
                    return  # 防 DoS

            head, _, body = buf.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            if not lines:
                return

            # 補上 Proxy-Authorization；如果客戶端已經有就覆蓋
            new_lines = [lines[0]]
            seen_auth = False
            for line in lines[1:]:
                if line.lower().startswith(b"proxy-authorization:"):
                    new_lines.append(self.auth_header)
                    seen_auth = True
                else:
                    new_lines.append(line)
            if not seen_auth:
                new_lines.append(self.auth_header)

            modified = b"\r\n".join(new_lines) + b"\r\n\r\n" + body

            # 連上游 CliProxy 並轉發
            upstream = socket.create_connection(
                (self.upstream_host, self.upstream_port), timeout=15
            )
            upstream.sendall(modified)

            # 雙向 pipe 直到任一邊斷
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
