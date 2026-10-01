"""Command line: `photoband` (desktop window) or `photoband serve` (browser mode)."""
from __future__ import annotations

import argparse
import logging
import multiprocessing
import socket
import sys
import threading
import time
import webbrowser

from . import __version__


class StartError(RuntimeError):
    pass


def bind_socket(port: int = 0) -> socket.socket:
    """Our own listening socket on 127.0.0.1, so no other process can already own the port
    we then hand the token to. Fails if another socket is listening there. On Windows,
    SO_EXCLUSIVEADDRUSE (never SO_REUSEADDR, which there allows stealing a port); elsewhere
    SO_REUSEADDR only lets a restart reuse a port in TIME_WAIT (a live listener still blocks
    the bind), and SO_REUSEPORT is never set."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform == "win32":
            s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", -5), 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.listen(128)
        s.set_inheritable(False)
    except OSError as e:
        s.close()
        raise StartError(f"Photoband could not listen on 127.0.0.1:{port} ({e}).") from e
    return s


def start_server(port: int = 0):
    """Start uvicorn on a socket we bound ourselves; raises StartError unless it is serving."""
    import uvicorn
    from .server import app
    sock = bind_socket(port)
    port = sock.getsockname()[1]
    from .logsetup import UVICORN_LOG_CONFIG
    # our own log config: uvicorn's default needs a TTY-capable sys.stdout (None in windowed builds)
    config = uvicorn.Config(app, log_config=UVICORN_LOG_CONFIG, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    for _ in range(500):
        if server.started or not t.is_alive():
            break
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        sock.close()
        raise StartError("Photoband's local server did not start.")
    return server, port


def main(argv=None) -> int:
    multiprocessing.freeze_support()
    ap = argparse.ArgumentParser(prog="photoband")
    ap.add_argument("--version", action="version", version=f"photoband {__version__}")
    ap.add_argument("command", nargs="?", default="app", choices=["app", "serve"])
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    from . import logsetup
    logsetup.setup()
    logging.getLogger(__name__).info("Photoband %s starting (%s)", __version__, args.command)
    from . import security
    if security.TOKEN_ERROR:
        print(security.TOKEN_ERROR, file=sys.stderr, flush=True)
        return 2
    if args.command == "serve":
        try:
            server, port = start_server(args.port)
        except StartError as e:
            print(e, file=sys.stderr, flush=True)
            return 1
        url = f"http://127.0.0.1:{port}/?t={security.TOKEN}"
        print(f"Photoband is running at {url}", flush=True)
        if not args.no_browser:
            webbrowser.open(security.launch_url(port))  # a one-time nonce, never the token, in argv
        try:
            while server.started and not server.should_exit:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        return 0
    from .desktop import run_desktop
    return run_desktop()


if __name__ == "__main__":
    sys.exit(main())
