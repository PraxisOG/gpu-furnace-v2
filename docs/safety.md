# Before you leave the furnace running

GPU Furnace controls workloads. Hardware power limits, cooling, and electrical protection remain the job of your system and its physical installation.

## Measure the whole system

The example is **four GPUs at 150 W each: 600 W total GPU power**. CPU, memory, fans, and PSU losses are additional. A GPU telemetry value is not the wattage at the outlet. Use a suitable wall meter and check the ratings of the complete power path, including shared loads, cables, strips, and UPS equipment. Avoid overloaded or daisy-chained power strips.

`max_watts` only compares workload estimates. It does not program GPU power caps or impose a hard electrical limit. Set and verify board limits using your vendor’s supported tools. Unrelated processes can consume power outside the furnace’s workload budget.

## Keep cooling independent

Maintain clear intake/exhaust paths, clean filters, and functioning fans. Keep hot exhaust away from materials that cannot tolerate it. Retain firmware throttling/shutdown protections and use a smoke detector appropriate to the room. Do not wire a generic hobby relay into mains power; use properly rated, enclosed equipment for any independent cutoff.

The default software guard stops managed work at 85°C and releases below 75°C. Choose thresholds appropriate to the GPU and the actual sensor: NVIDIA core temperature and AMD edge/hotspot readings are different measurements. The AMD guard uses the hottest exposed temperature, not just the edge. Missing power/temperature telemetry pauses heating.

## Know the failure modes

- Automatic heating pauses after 120 seconds without a fresh room reading. Case/GPU temperatures are not reliable substitutes for a room sensor under load.
- Full heat bypasses room temperature, but preserves telemetry, thermal, budget, and reservation checks. It does not resume automatically after a restart.
- Off and Reserve stop managed jobs. A separately running inference engine remains running.
- SIGINT/SIGTERM requests workload shutdown. User-service cgroup cleanup also covers attached subprocesses. A crash, SIGKILL, detached container, or independently managed system service can prevent normal cleanup. Use independent protections where the consequences require them.
- Test shutdown and GPU reclamation with your actual workloads. Software can request a stop; it cannot guarantee a faulty external service obeys.

## Protect access and data

Keep the controller on loopback or a trusted network; use encrypted transport for remote access. The token controls every mutating endpoint. Anyone who can reach GET endpoints can read telemetry, reasons, sensor names, and workload labels. The token does not make arbitrary Internet exposure a deployment strategy.

Only configure trusted commands and images. Use an unprivileged account and narrow service permissions. Keep credentials out of Git and revoke any token that has been exposed; removing a file does not revoke its value or erase Git history.
