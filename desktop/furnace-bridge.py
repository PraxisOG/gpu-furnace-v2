#!/usr/bin/env python3
"""Optional TCP bridge to a GPU host on another reachable network.

Set BRIDGE_TARGET explicitly. Authentication remains enforced by heatd.
Use an SSH tunnel or HTTPS reverse proxy for untrusted networks.
"""

import os, socket, threading, select

LISTEN = int(os.environ.get("BRIDGE_PORT", "8787"))
TARGET = os.environ.get("BRIDGE_TARGET", "")
HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
TARGET_PORT = int(os.environ.get("BRIDGE_TARGET_PORT", "8787"))


def splice(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 300)
            if not r:
                break
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                other = b if s is a else a
                other.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()


def main():
    if not TARGET:
        raise SystemExit("Set BRIDGE_TARGET to your GPU host address")
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, LISTEN))
    srv.listen(16)
    print(f"furnace-bridge: {HOST}:{LISTEN} -> {TARGET}:{TARGET_PORT}", flush=True)
    while True:
        conn, _ = srv.accept()
        try:
            up = socket.create_connection((TARGET, TARGET_PORT), timeout=5)
        except OSError:
            conn.close()
            continue
        threading.Thread(target=splice, args=(conn, up), daemon=True).start()


if __name__ == "__main__":
    main()
