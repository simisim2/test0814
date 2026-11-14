"""서버 실행. 같은 와이파이의 폰에서도 접속할 수 있게 0.0.0.0 으로 연다."""
import socket
import sys
import threading
import webbrowser

import uvicorn

PORT = 8765


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


if __name__ == "__main__":
    ip = lan_ip()
    print("=" * 50)
    print("  쇼츠 편집기 실행 중")
    print(f"  PC 에서:  http://localhost:{PORT}")
    print(f"  폰에서:   http://{ip}:{PORT}   (같은 와이파이)")
    print("  종료: 이 창에서 Ctrl+C")
    print("=" * 50)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, log_level="warning")
