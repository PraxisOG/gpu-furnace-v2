#!/usr/bin/env python3
"""Scan tracked files (optionally all reachable Git history), without values.

This heuristic catches common credentials; it cannot prove no secret exists.
"""

import argparse
from pathlib import Path
import re
import subprocess
import sys

PATTERNS = {
    "provider token": r"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{20,})",
    "JWT": r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    "private key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "credential assignment": r"""(?im)^\s*(?:[A-Z_]*(?:TOKEN|PASSWORD|API_KEY|SECRET)|token|password|api_key|secret)\s*[:=]\s*["']?([A-Za-z0-9_+/=-]{24,})""",
}
SENSITIVE = re.compile(
    r"(?:^|/)(?:control\.token|secrets\.ya?ml|\.env(?:\..+)?|[^/]+\.(?:pem|key))$"
)


def scan(name, data):
    findings = []
    if SENSITIVE.search(name) and not name.endswith((".example", ".sample")):
        findings.append((name, 0, "credential file must not be tracked"))
    for label, pattern in PATTERNS.items():
        for match in re.finditer(pattern, data):
            findings.append((name, data[: match.start()].count("\n") + 1, label))
    return findings


def git(*args):
    return subprocess.check_output(["git", *args])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    findings, count = [], 0
    if args.history:
        seen = set()
        for ref in git("rev-list", "--all").decode().splitlines():
            for entry in git("ls-tree", "-rz", ref).split(b"\0"):
                if not entry:
                    continue
                meta, name = entry.split(b"\t", 1)
                sha = meta.split()[2].decode()
                key = (sha, name)
                if key in seen:
                    continue
                seen.add(key)
                count += 1
                findings.extend(
                    scan(
                        name.decode(),
                        git("cat-file", "-p", sha).decode(errors="replace"),
                    )
                )
    else:
        for raw in git("ls-files", "-z").split(b"\0"):
            if raw and Path(raw.decode()).is_file():
                name = raw.decode()
                count += 1
                findings.extend(
                    scan(name, Path(name).read_bytes().decode(errors="replace"))
                )
    for name, line, label in sorted(set(findings)):
        print(f"{name}:{line}: {label} (value withheld)")
    print(
        f"Scanned {count} {'historical blobs' if args.history else 'tracked files'}; {len(set(findings))} findings."
    )
    return bool(findings)


if __name__ == "__main__":
    sys.exit(main())
