"""啟動本機 War-Room 網頁版。

使用方式：
    pip install fastapi uvicorn        # 第一次先裝
    python run_webgui.py               # 預設 127.0.0.1:7860，自動開瀏覽器
    python run_webgui.py --host 0.0.0.0 --port 8000  # 想開放區網時
"""
import argparse
import sys
import threading
import time
import webbrowser


def open_browser(host: str, port: int):
    time.sleep(1.0)
    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    webbrowser.open(f"http://{target}:{port}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="綁定 IP（預設 127.0.0.1，只本機可連）")
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--no-browser", action="store_true",
                    help="不要自動開瀏覽器")
    args = ap.parse_args()

    try:
        import uvicorn  # noqa: F401
    except ImportError:
        print("缺套件，請先執行：pip install fastapi uvicorn",
              file=sys.stderr)
        sys.exit(1)

    if not args.no_browser:
        threading.Thread(target=open_browser, args=(args.host, args.port),
                         daemon=True).start()

    import uvicorn
    uvicorn.run(
        "webgui.server:app",
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
