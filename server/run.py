"""Start the ExamGuard server.

    python server/run.py              # wait for the real Pico on :8080
    python server/run.py --sim        # also start a virtual Pico (no hardware)
"""
import argparse
import asyncio
import os
import socket
import sys
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")  # live single-frame inference

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import pico_link, simulator  # noqa: E402
from app.main import app, hub  # noqa: E402


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


async def main(args):
    await pico_link.serve(hub, "0.0.0.0", args.pico_port)
    print(f"[pico] listening on 0.0.0.0:{args.pico_port}  ->  set SERVER_IP = \"{lan_ip()}\" in firmware/main.py")
    if args.sim:
        asyncio.create_task(simulator.run_virtual_pico("127.0.0.1", args.pico_port))
        print("[sim] virtual Pico started")
    print(f"[web] dashboard on http://localhost:{args.port}")
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--pico-port", type=int, default=8080)
    p.add_argument("--sim", action="store_true", help="run a virtual Pico for demos/testing")
    asyncio.run(main(p.parse_args()))
