# run_all.py
import os
import signal
import subprocess
import sys
import time

def _popen(script_name: str) -> subprocess.Popen:
    py = sys.executable
    path = os.path.join(os.path.dirname(__file__), script_name)
    return subprocess.Popen([py, path])

def main():
    p1 = _popen("main.py")
    time.sleep(0.2)
    p2 = _popen("preview_server.py")

    try:
        while True:
            if p1.poll() is not None or p2.poll() is not None:
                break
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        for p in (p1, p2):
            if p.poll() is None:
                try:
                    p.send_signal(signal.SIGTERM)
                except Exception:
                    pass

if __name__ == "__main__":
    main()