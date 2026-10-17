#!/usr/bin/env python3
"""Last-resort heat element: hammer every GPU with matmul until SIGTERM.

No useful output. Pure space heater. Use when the volunteer queue is
empty or off and the room is still cold. Honors SIGTERM/SIGINT.
"""

import os
import signal
import sys
import time

import torch

STOP = False


def _stop(signum, frame):
    global STOP
    STOP = True


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    n = int(os.environ.get("HEATER_SIZE", "4096"))
    devs = [torch.device(f"cuda:{i}") for i in range(torch.cuda.device_count())]
    if not devs:
        sys.exit("no supported PyTorch GPU devices (CUDA or ROCm)")
    tensors = []
    for d in devs:
        a = torch.randn(n, n, device=d, dtype=torch.float16)
        b = torch.randn(n, n, device=d, dtype=torch.float16)
        tensors.append((a, b))
    print(f"heater: burning on {len(devs)} GPUs, {n}x{n} matmul", flush=True)
    while not STOP:
        for a, b in tensors:
            a @ b
        for d in devs:
            torch.cuda.synchronize(d)
    print("heater: banked", flush=True)


if __name__ == "__main__":
    main()
