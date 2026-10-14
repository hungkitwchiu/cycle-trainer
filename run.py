"""Start CycleTrainer: python run.py [--no-browser] [--simulate]"""
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

from app import config  # noqa: E402


def main():
    args = sys.argv[1:]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    config.ensure_dirs()
    if "--simulate" in args:
        config.update_settings(simulate=True)
    url = f"http://localhost:{config.PORT}/"
    if "--no-browser" not in args:
        threading.Thread(target=lambda: (time.sleep(1.5), webbrowser.open(url)), daemon=True).start()
    print(f"\n  CycleTrainer running at {url}\n  Close this window to quit.\n")
    import uvicorn
    from app.main import app
    uvicorn.run(app, host="127.0.0.1", port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
