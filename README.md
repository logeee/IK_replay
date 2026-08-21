# IK Replay Debug Viewer

Offline web tool for generic robot IK visualization and trajectory replay.

The current version is intentionally offline. It loads a URDF, lets you edit joints and target TCP pose in the browser, calls a backend IK solver, plans a joint trajectory, and replays the result in a Three.js viewer. It does not connect to a robot, subscribe to robot state, run a data-recording workflow, use VR input, or send control commands.

## What Is Included

- URDF loading with relative mesh paths.
- Generic `RobotModel` with FK and TCP pose calculation.
- Replaceable IK solver interface.
- Numerical IK solver for the first working version.
- Replaceable trajectory planner interface.
- Linear and quintic joint-space planners.
- Browser 3D viewer with robot mesh, target marker, TCP marker, TCP path, replay controls, and debug output.
- Configurable simplified collision regions with sphere, box, and capsule primitives.
- YAML config for robot, chains, TCP, collision, solver, planner, and viewer defaults.
- G1-D and H2 as example robots, not as core framework logic.

## Run

```bash
pip install -r requirements.txt
python3 app.py
```

Open:

```text
http://localhost:8000
```

## Gravity calibration experiment bench (18002)

The gravity calibration UI keeps its waypoints and experiment records separate
under `data/gravity_calibration/`. It delegates planning and hardware execution
to an already-running reach service on port 18001; the 18002 process never opens
DDS or controls the arm directly.

```bash
./prepare-gravity.sh
# open http://<robot-ip>:18002/

./prepare-gravity.sh stop
```

Recommended workflow: save all manually dragged poses first, then choose and
order any subset for the experiment. Each confirmed run plans and previews the
trajectory, moves the arm, waits for settling, and records command/measured
joints, measured velocity, estimated motor torque, controller gains,
gravity/feedforward torque, TCP, and torso diagnostics.
The run settings can insert 0–8 intermediate holds; every hold keeps rigid
position/gravity support active, settles, and is stored as a separate static
sample point before the remaining trajectory continues.

Gravity parameters are immutable, rollback-safe profiles in
`config/gravity_compensation.json`. The initial active snapshot is
`0.0.0 · 未标定前的重力补偿版本`. Saving or activating a profile in the 18002
dashboard never changes live arm torque; restart 18001 to load the selected
version. Every experiment record stores the effective version and any CLI
overrides reported by 18001.

Experiment runs are physically partitioned by that version:
`data/gravity_calibration/runs/<version>/<run-id>.json`. Switching or rolling
back the active profile only affects future 18001 startups; historical version
directories and files are never moved or rewritten.

Completed runs can be opened in the 18002 dual-pose viewer. It reconstructs
commanded and measured arm FK as translucent cyan/orange link models, marks
every joint and both TCPs, draws displacement connectors, and reports per-joint
angle error plus TCP XYZ/Euclidean error for each intermediate or final sample.

The gravity waypoint library can also multi-select and copy existing
`data/waypoints/*.json` poses. Imports retain their source filename, never edit
the original waypoint, and skip a source file that has already been imported.

Planning preview uses the same H2 URDF/STL assets as the 18001 viewer. After a
collision-checked plan, 18002 loads the complete robot and replays every named
joint frame with play/pause, scrubbing, sampling-frame labels, orbit, zoom, pan,
and camera reset controls. The wrist-attached nine color calibration markers,
white TCP point, green flange plane, and dark hand collision capsule are carried
through every preview frame; the old TCP-only 2D preview is not used.
Completed gravity runs use the same URDF assets to overlay complete theoretical
and measured robots, with previous/next controls for every sampled trajectory
stop and per-stop joint/TCP error readouts.

## Reach RGB-D source

The production reach service is a read-only consumer of the external
teleimager RGB-D ZMQ stream. It does not start a local camera and it does not
modify the teleimager project.

Before first deployment (or after changing the camera/profile), temporarily
make the camera available for exclusive SDK access and export its calibration:

```bash
sudo systemctl stop teleimager-camera-capture.service
python tools/export_orbbec_rgbd_calibration.py \
  --serial CP0BB53000FS \
  --color-width 1920 --color-height 1080 \
  --depth-width 1280 --depth-height 800 \
  --sample-dir /tmp/orbbec_alignment_reference
sudo systemctl start teleimager-camera-capture.service
```

The generated `config/camera/orbbec_rgbd_calibration.json` contains the SDK
intrinsics, distortion, depth-to-color extrinsics, and depth scale. Production
then starts without SDK camera access:

```bash
python reach_server.py \
  --camera-source zmq \
  --camera-host 192.168.123.164 \
  --network-interface enp86s0
```

`reach_server.py` checks the ZMQ metadata dimensions against the local
calibration before software-aligning Z16 depth into the JPEG color frame.
Failure to connect or a profile mismatch is fatal; production never falls back
to opening the local Orbbec. Direct SDK access remains available only through
an explicit debug command:

```bash
python reach_server.py --camera-source orbbec --camera-serial CP0BB53000FS
```

Before hand-eye calibration is available, camera and YOLO debugging can run in
an explicitly safe preview mode. It connects only to ZMQ, skips DDS, and blocks
all robot-coordinate, planning, and execution endpoints:

```bash
python reach_server.py --camera-only --camera-host 127.0.0.1
```

When `--sample-dir` is used, the exporter also saves the same frame before and
after SDK `AlignFilter`. Compare that reference with the SDK-free implementation:

```bash
python tools/compare_rgbd_alignment.py \
  --raw-depth /tmp/orbbec_alignment_reference/raw_depth_z16.npy \
  --sdk-aligned /tmp/orbbec_alignment_reference/sdk_aligned_depth_mm.npy
```

## API

- `GET /`
- `GET /api/robot/metadata`
- `POST /api/fk`
- `POST /api/ik/solve`
- `POST /api/trajectory/plan`
- `POST /api/collision/check`
- `POST /api/demo/solve_and_plan`
- `POST /api/demo/plan` legacy-compatible alias for the original position-IK demo flow

## Project Shape

```text
IK_replay/
├── app.py
├── camera_sources/
├── config/
│   ├── camera/
│   ├── default.yaml
│   └── robots/
│       ├── g1_d.yaml
│       └── h2.yaml
├── assets/robots/
│   ├── g1_d/
│   │   ├── robot.urdf
│   │   └── meshes/
│   └── h2/
│       ├── robot.urdf
│       └── meshes/
├── core/
├── ik/
├── planners/
├── web/
└── examples/
```

## Replacing The Robot

Create a new robot YAML under `config/robots/`, then point `config/default.yaml` at it.

Required robot settings:

- `robot.urdf_path`
- `robot.mesh_root`
- `chain.base_link`
- `chain.end_link`
- `chain.joints`
- `tcp.offset_xyz`
- `tcp.offset_rpy`
- optional `collision.body` and `collision.chains.*.shapes`

Do not put robot-specific joint names in `app.py`, `web/main.js`, or solver code.

## Collision Regions

URDF collision tags are not required. Each robot YAML can define approximate collision primitives:

- `box`: linked oriented box with `half_extents`.
- `sphere`: linked sphere or TCP sphere.
- `capsule`: segment between two linked points.

The backend checks each configured arm primitive against configured body primitives and returns `safe`, `near`, `collision`, or `unconfigured`.

## Replacing IK Or Trajectory

Add a new solver under `ik/` that implements `BaseIKSolver.solve(IKRequest) -> IKResult`, then register it in `app.py` and select it in config or the UI.

Add a new planner under `planners/` that implements `BaseTrajectoryPlanner.plan(TrajectoryRequest) -> list[Waypoint]`, then register it in `app.py` and select it in config or the UI.

## Future Extensions

VR input, replay-file input, ROS adapters, robot-state providers, mesh-level collision, and command executors should be added as optional adapters around the current API contract. They are not implemented in this version.
