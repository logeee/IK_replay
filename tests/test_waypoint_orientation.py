"""A/B orientation sources: cabinet-relative rotation with independent picked XYZ."""
import json
from copy import deepcopy

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from adapters.reach import cabinet_waypoints as c, orientation as o
from tests.test_cabinet_waypoints import scene


def prepare(scene, *, kind='direct'):
    s = scene.s
    s.pick_revision = 5
    s.pick_context = {'selection_mode': 'frozen_rgbd_pointcloud'}
    rotation = Rotation.from_euler('z', 3, degrees=True).as_matrix()
    s.plane = {'horizontal_axis_source': 'wall_coordinate_x',
        'right_root': rotation[:, 0].tolist(), 'wall_up_root': rotation[:, 2].tolist()}
    s.pick_target_root = (o.joint_pose(scene.q)[:3, 3] + [.003, .002, .007]).tolist()
    return {'reference_id': scene.file, 'reference_kind': 'cabinet_waypoint_orientation',
        'pick_revision': s.pick_revision, 'target_root': s.pick_target_root,
        'start_joints': dict(zip(s.joint_names, scene.current_q)), 'kind': kind, 'lift_m': .005}


@pytest.mark.parametrize('kind', ['direct', 'axis_last'])
def test_reprojects_saved_cabinet_orientation_and_keeps_picked_position(scene, kind):
    body = prepare(scene, kind=kind)
    result = o.plan_orientation(body)
    assert isinstance(result, dict), getattr(result, 'body', result)
    q = [[w['named_joints'][n] for n in scene.s.joint_names] for w in result['waypoints']]
    actual_goal = o.joint_pose(q[-1])
    _, pose, directory, digest = c.archive(scene.file)
    recorded_panel_tcp = c._saved_geometry(str(directory), digest)[0]
    expected = o.cabinet_rotation() @ recorded_panel_tcp[:3, :3]
    np.testing.assert_allclose(result['target_pose']['xyz'], body['target_root'], atol=1e-12)
    assert np.linalg.norm(actual_goal[:3, 3] - o.joint_pose(scene.q)[:3, 3]) > .004
    assert o.angle_deg(actual_goal[:3, :3], expected) < .35
    assert o.angle_deg(actual_goal[:3, :3], np.asarray(pose['T_base_from_tcp'])[:3, :3]) > 2.
    assert o.angle_deg(o.joint_pose(q[0])[:3, :3], expected) > 10.
    assert result['orientation']['reference_kind'] == 'cabinet_waypoint_orientation'
    for backend in ('legacy', 'legacy_timed'):
        proof = o.execution_constraint(result['orientation']['id'], q, backend)
        assert proof['scope'] == 'goal'
        assert 'cabinet_assist' not in proof and 'cabinet_x_offset_mm' not in proof


def test_source_rotation_stays_cabinet_relative_when_robot_frame_changes(scene):
    source = o.load_orientation_source(scene.file, 'cabinet_waypoint_orientation')
    saved = np.asarray(source['tcp']['R_cabinet_from_frame'])
    _, original, directory, _ = c.archive(scene.file)
    original_capture = json.loads((directory / 'capture.json').read_text())
    for angle in (0, 25, -60):
        # Re-express the same recorded physical scene in a changed base frame.
        # A loader that simply returns T_base_from_tcp would fail this test.
        base_change = np.eye(4)
        base_change[:3, :3] = Rotation.from_euler('z', angle, degrees=True).as_matrix()
        base_change[:3, 3] = [.1, -.2, .05]
        pose = deepcopy(original)
        pose['T_base_from_tcp'] = (base_change @ np.asarray(original['T_base_from_tcp'])).tolist()
        pose['calibration']['T_base_from_camera'] = (base_change @ np.asarray(original['calibration']['T_base_from_camera'])).tolist()
        capture = deepcopy(original_capture); capture['source']['robot_pose'] = pose
        (directory / 'robot_pose.json').write_text(json.dumps(pose))
        (directory / 'capture.json').write_text(json.dumps(capture))
        changed = o.load_orientation_source(scene.file, 'cabinet_waypoint_orientation')
        np.testing.assert_allclose(changed['tcp']['R_cabinet_from_frame'], saved, atol=1e-12)


@pytest.mark.parametrize('change', ['record', 'tcp', 'arm', 'pick', 'stop', 'axes', 'kind', 'rgbd_missing'])
def test_stale_or_incompatible_point_orientation_is_rejected(scene, change):
    body = prepare(scene)
    if change in ('axes', 'kind', 'rgbd_missing'):
        if change == 'axes': scene.s.plane = {}
        if change == 'kind': body['reference_kind'] = 'unknown'
        if change == 'rgbd_missing': (scene.s.waypoints_dir / scene.file.removesuffix('.json') / 'rgbd/depth_mm.npy').unlink()
        result = o.plan_orientation(body)
        assert result.status_code == 422
        assert scene.s.orientation_plan is None
        return
    result = o.plan_orientation(body)
    assert isinstance(result, dict), getattr(result, 'body', result)
    q = [[w['named_joints'][n] for n in scene.s.joint_names] for w in result['waypoints']]
    if change == 'record':
        p = scene.s.waypoints_dir / scene.file
        raw = json.loads(p.read_text()); raw['name'] = 'changed'; p.write_text(json.dumps(raw))
    if change == 'tcp': scene.s.p_tool[0] += .01
    if change == 'arm': scene.s.chain_id = 'right_arm'
    if change == 'pick': scene.s.pick_revision += 1
    if change == 'stop': scene.s.cabinet_plan_revision += 1
    with pytest.raises(ValueError): o.execution_constraint(result['orientation']['id'], q, 'legacy_timed')


@pytest.mark.parametrize('backend', ['legacy', 'legacy_timed', 'pink'])
def test_point_orientation_executes_with_existing_goal_checks(scene, backend):
    from adapters.reach import execution
    from adapters.reach.execution_pink import PinkRuntime
    from adapters.reach.lowstate import MockLowStateSampler
    from tests.test_reach_pink_backend import FakeArmController
    body = prepare(scene)
    ctl = FakeArmController(scene.current_q)
    s = scene.s
    s.controller, s.settle_trim = ctl, 'off'
    try:
        if backend == 'pink':
            rt = PinkRuntime(arm_side='left', wrist_link='left_wrist_yaw_link',
                sampler=MockLowStateSampler(arm_motor_indices=(15,16,17,18,19,20,21), arm_q_reader=ctl.read_measured))
            s.pink_runtime = rt; rt.anchor(); rt.capture_pick_frame()
        result = o.plan_orientation(body)
        assert isinstance(result, dict), getattr(result, 'body', result)
        path = [np.asarray([w['named_joints'][n] for n in s.joint_names]) for w in result['waypoints']]
        proof = o.execution_constraint(result['orientation']['id'], path, backend)
        s.exec_running = True
        execution._exec_loop(path, 1.2, speed=.4, command_start_q=scene.current_q,
            motion_backend=backend, execution_context={'orientation_constraint': proof})
        assert s.exec_message.startswith('完成'), s.exec_message
        assert not s.exec_running
        assert o.angle_deg(o.joint_pose(ctl.read_measured())[:3, :3], proof['R_root_tcp']) < 2.
    finally: ctl.shutdown()
