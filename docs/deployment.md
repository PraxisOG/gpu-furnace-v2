# Deploy your own furnace

## User service (recommended)

Clone the repository at `~/gpu-furnace`, run `bash scripts/setup.sh`, configure your workloads, and validate with `python3 heatd/heatd.py --config ~/.config/gpu-furnace/workloads.json --check`.

```bash
mkdir -p ~/.config/systemd/user
cp systemd/heatd.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now heatd
journalctl --user -u heatd -f
```

If the checkout is elsewhere, edit `WorkingDirectory` and `ExecStart` in the copied unit. To keep the user service running after logout, enable lingering for your user with `loginctl enable-linger` (your system may require an administrator).

Both AMD and NVIDIA hosts start without a driver-specific wait loop. The daemon needs Python 3.10+; only your selected workload needs GPU libraries. The recommended service does not run as root. Its commands inherit the environment in `~/.config/gpu-furnace/furnace.env`.

## Phone / LAN access

Set `FURNACE_HOST` to the GPU host’s LAN address in `furnace.env`, then restart heatd. All POST requests still require the token. GET endpoints reveal telemetry and workload names to anyone who can reach that port.

For access over an untrusted network, put the controller behind HTTPS and access controls or use an SSH tunnel. `0.0.0.0` explicitly binds all IPv4 interfaces; the default is `127.0.0.1`. An optional TCP bridge is included for segmented networks, but is not encryption or authentication.

The bridge requires `BRIDGE_TARGET`; `BRIDGE_HOST` defaults to loopback. Its user service reads `~/.config/gpu-furnace/bridge.env`, which you create with your own address and optional ports. A bridge on a different machine needs heatd bound to an address reachable by that machine.

## Token lifecycle

The first real start generates a random token at the configured token path, with mode `0600`. A missing token is generated; an empty, short, or broadly readable token is rejected. No token is returned by the HTTP API or written to logs. The dashboard stores it only in sessionStorage and removes persistent storage left by older versions.

To rotate, stop heatd, delete the private token file, and restart. Enter the new token in your browser and update sensor credentials. Keep tokens, `.env` files, and ESPHome `secrets.yaml` outside Git. The supplied ignore rules cover common local secrets; run the scanner before pushing changes.

## Existing installations

When migrating an existing installation, keep local settings outside the Git checkout:

1. Back up your ignored workload config, `control.token`, and state files to a private location.
2. Copy any customized tracked `heatd/workloads.yaml` into your private config directory. The public example is now JSON. YAML still works when PyYAML is installed; set `FURNACE_CONFIG` explicitly to keep using it.
3. Set `FURNACE_STATE` to your previous state directory if you want existing setpoints/history, or migrate `furnace_state.json` and `readings.jsonl` to `~/.local/state/gpu-furnace/`. Previously calibrated SYSTIN offsets are retained when you use that state directory.
4. Preserve `FURNACE_TOKEN_FILE` and secure its permissions with `chmod 600`. Sensor clients must send the token as a Bearer header; query-string tokens no longer work.
5. Set `FURNACE_HOST` explicitly if LAN/bridge clients depend on the previous all-interface listener. The new default is loopback.
6. Replace custom image names, GPU groups, service permissions, and workload commands with your local settings. Unavailable fuel selections now stay empty instead of falling back to unrelated jobs. Configure full-rig workloads at their total planned watts.
7. Validate the local config, stop the old service, update/reload the copied unit if needed, and start the new one. Recheck sensor freshness, per-card power limits, Off, and Reserve before leaving jobs unattended.

This edition starts with a fresh Git history containing only the portable source files. Clone it into a new directory instead of importing or merging history from a previous repository. Keep private deployment notes, tokens, sensor secrets, and runtime state outside Git.

## Optional system service

`systemd/gpu-furnace.service` is a template for an unprivileged `gpu-furnace` account, `/opt/gpu-furnace` checkout, `/etc/gpu-furnace` configuration, and `/var/lib/gpu-furnace` state. Create the account, install its private config/token, and grant only the GPU/device or service permissions needed for your commands before enabling it. Do not run both controller services at once.

For externally managed system services (such as Folding@home), configure one explicit unit and the minimum start/stop permissions. Do not give the controller unrestricted passwordless sudo. Stopping heatd requests shutdown of these units; abrupt controller failure cannot guarantee that a separate service or Docker container stops.
