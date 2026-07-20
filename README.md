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

## API

- `GET /`
- `GET /api/robot/metadata`
- `POST /api/fk`
- `POST /api/ik/solve`
- `POST /api/trajectory/plan`
- `POST /api/collision/check`
- `POST /api/demo/solve_and_plan`
- `POST /api/demo/h2_switch_operation` H2 right-arm offline selector-switch demonstration
- `POST /api/demo/plan` legacy-compatible alias for the original position-IK demo flow

## Project Shape

```text
IK_replay/
├── app.py
├── config/
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

## H2 Selector-Switch Demo

Select H2 and use the highlighted robot-right-arm panel to solve two switch keyframes. Input
the switch XYZ position in metres: X is forward from the robot, Y is left from the robot
(right is negative), and Z is height above the ground. The default ground height is 1.70 m.
Input the initial lever angle, final lever angle, and choose a Cartesian line or
lever-centered arc between them. Green and red markers show the initial and final fingertip
positions. Both keyframes and all intermediate samples use the built-in natural pointing
posture by default. The solver weights are position 1.0 (validated at 2 mm), soft wrist
orientation 0.08, posture 0.008, and adjacent-point regularization 0.002. The configured
self-collision check remains active, and no command is sent to a physical robot.

After planning, the panel can export two handoff artifacts:

- `h2_switch_two_keyframe_ik_task.json`: explicit initial/final fingertip keyframes, switch
  geometry, coordinate-frame convention, units, optional sampled path, recommended solver
  weights, and the reference solution.
- `h2_switch_two_keyframe_reference_joints.csv`: timestamped reference joint positions with
  units in every Cartesian and joint column, for
  comparison, plotting, or replay. These joints are not mandatory for a replacement solver.
