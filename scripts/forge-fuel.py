#!/usr/bin/env python3
"""forge fuel: generate synthetic tokens/dataset rows via the OpenAI-
compatible API your inference engine already serves. Zero extra VRAM —
the engine is the furnace. Output: JSONL dataset at
$FORGE_OUT (default ~/.local/state/gpu-furnace/forge.jsonl). Honors SIGTERM.

Point it at whichever endpoint you want burned:
  FORGE_API=http://127.0.0.1:8000/v1  (the engine directly)
  FORGE_API=http://127.0.0.1:8001/v1  (behind the relay)
"""

import json
import os
import random
import signal
import string
import time
from pathlib import Path
import urllib.parse
import urllib.request

API = os.environ.get("FORGE_API", "http://127.0.0.1:8000/v1").rstrip("/")
MODEL = os.environ.get("FORGE_MODEL", "")
OUT = os.path.expanduser(
    os.environ.get("FORGE_OUT", "~/.local/state/gpu-furnace/forge.jsonl")
)
TOKENS = int(os.environ.get("FORGE_TOKENS", "256"))  # per completion
STOP = []
signal.signal(signal.SIGTERM, lambda *a: STOP.append(1))
signal.signal(signal.SIGINT, lambda *a: STOP.append(1))

TOPICS = [
    "space elevators",
    "valve engineering",
    "cold climates",
    "old computers",
    "mountain weather",
    "train timetables",
    "lighthouse keeping",
    "clock repair",
    "beekeeping",
    "cartography",
]
TEMPLATES = [
    "Write a short field report about {t} from an unreliable narrator.",
    "List ten obscure facts about {t}, then refute three of them.",
    "A dialogue between two experts debating {t}, one of them wrong.",
    "Describe {t} as a recipe, then as a legal disclaimer.",
    "Summarize a fictional 1970s documentary about {t}.",
]


def rand_seed():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=12))


def one_request():
    prompt = random.choice(TEMPLATES).format(t=random.choice(TOPICS))
    body = json.dumps(
        {
            "model": MODEL,
            "prompt": prompt,
            "max_tokens": TOKENS,
            "temperature": 1.0,
            "stream": False,
        }
    ).encode()
    headers = {"Content-Type": "application/json"}
    token_file = os.environ.get("FORGE_TOKEN_FILE")
    if token_file:
        headers["Authorization"] = (
            "Bearer " + Path(token_file).expanduser().read_text().strip()
        )
    req = urllib.request.Request(API + "/completions", data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r:
        return prompt, json.loads(r.read().decode())


def main():
    if not MODEL:
        raise SystemExit(
            "Set FORGE_MODEL to the model name served by your inference API"
        )
    parsed = urllib.parse.urlparse(API)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise SystemExit(
            "FORGE_API must be an HTTP(S) URL without embedded credentials"
        )
    if not 1 <= TOKENS <= 32768:
        raise SystemExit("FORGE_TOKENS must be between 1 and 32768")
    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True, mode=0o700)
    print(f"forge: burning tokens -> {OUT} via {API}", flush=True)
    n_done = 0
    while not STOP:
        try:
            prompt, resp = one_request()
            text = (resp.get("choices") or [{}])[0].get("text", "")
            usage = resp.get("usage", {})
            with open(OUT, "a") as f:
                f.write(
                    json.dumps(
                        {
                            "seed": rand_seed(),
                            "ts": int(time.time()),
                            "prompt": prompt,
                            "text": text,
                            "tokens": usage.get("completion_tokens"),
                        }
                    )
                    + "\n"
                )
            n_done += 1
            if n_done % 10 == 0:
                print(f"forge: {n_done} samples", flush=True)
        except Exception as e:
            # engine busy/restarting? back off, don't spin
            print(f"forge: waiting ({type(e).__name__})", flush=True)
            for _ in range(15):
                if STOP:
                    break
                time.sleep(1)
    print("forge: banked", flush=True)


if __name__ == "__main__":
    main()
