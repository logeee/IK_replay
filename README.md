# G1-D Arm IK, Trajectory, and Collision Demo

Standalone offline demo for Unitree G1-D arm inverse kinematics, smooth trajectory planning, simplified collision checking, and browser-based 3D playback.

This project does not connect to a real robot and does not send robot control commands.

## Features

- Loads `assets/g1_d_description/g1_d.urdf` and local STL meshes.
- Supports left and right 7-DoF arm chains.
- Accepts `current_joints`, `target_xyz`, `tcp_offset`, and `arm`.
- Solves position-only numerical IK through a replaceable solver interface.
- Generates quintic joint-space trajectories.
- Checks each trajectory frame against simplified primitives:
  - torso box
  - head sphere
  - upper arm and forearm capsules
  - TCP sphere
- Plays the planned motion in a local Three.js UI.
- Uses green, yellow, and red status colors for safe, near, and collision.

## Run

```bash
pip install -r requirements.txt
python app.py
```

Open:

```text
http://localhost:8000
```

## API

- `GET /`
- `GET /api/robot/metadata`
- `POST /api/ik/solve`
- `POST /api/trajectory/plan`
- `POST /api/collision/check`
- `POST /api/demo/plan`

Example:

```bash
curl -X POST http://localhost:8000/api/demo/plan \
  -H "Content-Type: application/json" \
  -d "{\"arm\":\"left\",\"current_joints\":[0,0.25,0,0.85,0,-0.35,0],\"target_xyz\":[0.34,0.28,0.65],\"tcp_offset\":[0.08,0,0],\"steps\":80,\"duration\":4}"
```

## Replace the IK Module

The IK boundary is `core/ik_solver.py`.

Keep this call signature:

```python
solve(current_joints, target_xyz, tcp_offset, arm) -> IKResult
```

`IKResult` should continue to return:

- `success`
- `target_joints`
- `named_target_joints`
- `error_mm`
- `message`
- `tcp_position`

To replace with Pinocchio, MoveIt, or a custom solver, create another class that implements `IKSolver.solve(...)`, then swap the instance in `app.py`:

```python
ik_solver = MyPinocchioIKSolver(robot)
```

## Replace the URDF or Meshes

Put the new robot description under:

```text
assets/g1_d_description/
```

Required:

```text
g1_d.urdf
meshes/
```

Then update `core/config.py` if joint names, end links, or arm chains differ:

- `ARM_JOINTS`
- `ARM_END_LINKS`
- `ARM_LINKS`

The browser loader expects mesh paths in the URDF to resolve relative to `/assets/g1_d_description/`.

## Feed Real Robot Joints Later

This demo is offline only. To connect later, keep the frontend/API contract unchanged and add a separate adapter that reads real robot states into the same 7-value `current_joints` list:

```python
current_joints = [
    shoulder_pitch,
    shoulder_roll,
    shoulder_yaw,
    elbow,
    wrist_roll,
    wrist_pitch,
    wrist_yaw,
]
```

Do not send control commands from this demo. If you later add execution, put it in a separate module with explicit safety checks, command limits, and operator confirmation.

## Notes

- Collision is approximate and for visualization only.
- Mesh collision is intentionally not used in the first version.
- IK solves TCP position, not end-effector orientation.
- The Three.js runtime files are vendored in `web/vendor/` so the page does not need a CDN at runtime.
