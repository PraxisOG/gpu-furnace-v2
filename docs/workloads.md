# Choose your fuel

A furnace workload must be safe to interrupt, have an honest total power estimate, and leave your GPUs available when stopped. Configure commands you trust: they execute as the controller’s user.

## Four cards, one budget

The supplied example contains four disabled 150 W jobs, one per GPU. Enabling them gives a **600 W planned ceiling**. Each command uses `CUDA_VISIBLE_DEVICES` to choose a card; PyTorch uses the `cuda` API for both CUDA and ROCm. Confirm device ordering on your driver; ROCm users can set `ROCR_VISIBLE_DEVICES` instead where required.

`heater.py` runs matrix multiplication with no useful output. It requires a compatible GPU PyTorch installation and synchronizes every selected device. Set `HEATER_SIZE` to change matrix dimensions. Configure real hardware power caps separately: matrix size and `watts` do not enforce draw.

## Workload fields

| Field | Meaning |
| --- | --- |
| `name` | Unique display name |
| `cmd` | Shell command, run from the repository root |
| `unit` | Existing systemd unit; use exactly one of `cmd` / `unit` |
| `unit_user` | Use `systemctl --user` (default false) |
| `sudo` | Use `sudo -n systemctl` for one explicitly permitted system unit |
| `watts` | Estimated total watts for this entire workload |
| `fuel` | `fold`, `bench`, `forge`, or `any` (default) |
| `enabled` | Explicitly enable a job (default true if omitted) |
| `cleanup` | Optional trusted shell command to remove detached resources |

Jobs start in list order and stop in reverse order as demand falls. The first jobs must fit within `max_watts`; the controller does not reorder them to pack the budget. A fuel mode includes only jobs tagged with that fuel or `any`. An unavailable fuel never launches a different kind of work.

## Folding / BOINC / training

Install and configure the client independently, then add its actual service unit. Confirm that your chosen project supports your GPU and stops/checkpoints reliably. A service that spans all four 150 W cards should have `watts: 600`. Account for any CPU work separately when sizing total wall power.

For your own training, embedding, or rendering queue, point `cmd` at a runner that saves checkpoints and handles SIGTERM. The old fine-tune placeholder, which moved job files without actually executing them, has been removed.

## Container matrix workload

`scripts/bench-fuel.sh` uses **your** CUDA or ROCm PyTorch image. Set `FURNACE_IMAGE` and `FURNACE_GPU_BACKEND=amd` or `nvidia`. AMD device groups are resolved locally; NVIDIA requires Docker GPU support configured on the host. No image is downloaded or engine container stopped by heatd itself.

For a four-card container, use one 600 W workload:

```json
{
  "name": "All-GPU matrix workload",
  "fuel": "bench",
  "cmd": "bash scripts/bench-fuel.sh",
  "cleanup": "docker rm -f furnace-bench >/dev/null 2>&1 || true",
  "watts": 600,
  "enabled": true
}
```

The default container name is `furnace-bench`; set `FURNACE_CONTAINER` and the matching cleanup command if you run multiple instances. Container access grants substantial host privileges; use it only for trusted images/workloads.

## Forge a dataset

`scripts/forge-fuel.py` calls `/completions` on your own OpenAI-compatible endpoint. Set `FORGE_API`, `FORGE_MODEL`, and optionally `FORGE_TOKEN_FILE`, `FORGE_OUT`, and `FORGE_TOKENS`. The model name is required, and credentials must be in a private token file rather than the URL. The script makes one request at a time and stores samples as JSONL; review them before using them as training data.

The inference engine runs independently. Stopping or reserving the furnace stops its request generator; it does not unload the model, cancel an already accepted completion, or stop other clients. Configure your engine’s own limits. A stalled request can be terminated by the controller’s process-group timeout.

## Transcode a library

`scripts/transcode-farm.sh` is an optional NVIDIA AV1 example. Set `LIBRARY` and `OUT`, install an AV1-capable ffmpeg/GPU combination, and test on a small file first. Completed files are renamed from partial outputs so interruption does not create a falsely finished file. Encoder-only workloads may draw much less than their GPU’s maximum; measure before assigning `watts`.

## Check reclamation

Start a test job, choose **Off**, and confirm in your GPU tools that it exits. Repeat with **Reserve GPUs** and with a service shutdown. SIGTERM gets 30 seconds before process-group SIGKILL. An uncooperative system service or detached container may still require external cleanup. Never put irreplaceable, uncheckpointed work in the thermostat’s workload list.
