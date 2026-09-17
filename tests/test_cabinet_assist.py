"""Force configuration, coordinate transforms and execution with simulated arms only."""
import json
from types import SimpleNamespace

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from adapters.reach import cabinet_assist as ca, cabinet_waypoints as c, execution, orientation as o
from adapters.reach.state import ReachState, use_state
from tests.test_cabinet_waypoints import scene, planned


def spec(direction='a_to_b', force=6.):
    return dict(direction=direction, force_n=force, ramp_s=.5, hold_s=.1, release_s=.3)


@pytest.mark.parametrize('key,value', [('force_n', -1), ('force_n', 40.01), ('force_n', True),
    ('force_n', float('nan')), ('force_n', float('inf')), ('force_n', '5'),
    ('ramp_s', 0), ('ramp_s', 5.1), ('hold_s', -.1), ('release_s', 0), ('direction', 'x')])
def test_bad_spec_rejected_before_sensing(scene, monkeypatch, key, value):
    bad = spec(); bad[key] = value
    monkeypatch.setattr(c, 'capture_bundle', lambda: pytest.fail('invalid force must not capture'))
    result = c.plan_waypoint({'file': scene.file, 'cabinet_assist': bad})
    assert result.status_code == 422 and '助力' in json.loads(result.body)['error']


@pytest.mark.parametrize('direction', ['a_to_b', 'b_to_a'])
def test_force_does_not_change_goal_and_is_bound_to_plan(scene, direction):
    scene.current_panel[:3, :3] = Rotation.from_euler('z', 2, degrees=True).as_matrix()
    before, _ = planned(scene)
    result, path = planned(scene, cabinet_assist=spec(direction))
    proof = o.execution_constraint(result['orientation']['id'], path, 'legacy_timed')
    for key in ('target_root', 'R_root_tcp'):
        np.testing.assert_array_equal(proof[key], before['orientation'][key])
    root_base = scene.s.robot_model.forward_kinematics({})[scene.s.base_link]
    np.testing.assert_allclose(proof['cabinet_x_axis_root'], (root_base @ scene.current_panel)[:3, 0])
    assert result['cabinet_assist'] == spec(direction)
    assert ca.validate_execution_assist(spec(direction), proof) == spec(direction)
    for value in (None, spec(direction, 7.), spec('b_to_a' if direction == 'a_to_b' else 'a_to_b')):
        with pytest.raises(ValueError, match='规划不一致'):
            ca.validate_execution_assist(value, proof)
    with pytest.raises(ValueError):
        ca.validate_execution_assist(spec(direction), proof, {'force_n': 1})
    with pytest.raises(ValueError):
        ca.validate_execution_assist(spec(direction), None)


@pytest.mark.parametrize('submitted', ['match', 'changed', 'missing'])
def test_execute_endpoint_cannot_drop_or_change_planned_force(scene, monkeypatch, submitted):
    import time
    from unittest.mock import Mock
    result, path = planned(scene, cabinet_assist=spec())
    scene.s.controller = SimpleNamespace(
        read_measured=lambda: scene.current_q.copy(), status=lambda: {'float': False},
        command_snapshot=lambda: {'q_rad': scene.current_q.tolist(), 'sequence': 1,
            'sent_at_monotonic': time.monotonic(), 'tau_ff_nm': [0.]*7})
    thread = Mock()
    monkeypatch.setattr(execution, 'ReachThread', thread)
    body = {'waypoints': [dict(zip(scene.s.joint_names, q)) for q in path],
        'orientation_plan_id': result['orientation']['id'], 'motion_backend': 'legacy_timed'}
    if submitted != 'missing': body['cabinet_assist'] = spec(force=6. if submitted == 'match' else 7.)
    response = execution.reach_execute(body)
    if submitted == 'match':
        assert response['ok']
        proof = thread.call_args.kwargs['kwargs']['execution_context']['orientation_constraint']
        assert proof['cabinet_assist'] == spec()
        assert thread.call_args.kwargs['kwargs']['push_tau'] is None
        thread.return_value.start.assert_called_once()
    else:
        assert response.status_code == 409 and '规划不一致' in json.loads(response.body)['error']
        thread.assert_not_called()


@pytest.fixture
def force_clock(monkeypatch):
    clock = SimpleNamespace(t=0.)
    def sleep(dt): clock.t += dt
    monkeypatch.setattr(ca, 'time', SimpleNamespace(monotonic=lambda: clock.t, sleep=sleep))
    s = ReachState(); s.joint_names = ['a', 'b', 'c']
    ctl = SimpleNamespace(q=np.array([1., 2., 3.]), torques=[])
    ctl.read_measured = lambda: ctl.q.copy()
    def set_tau(tau): ctl.torques.append(np.asarray(tau).copy()); return True
    ctl.set_tau_ff = set_tau
    with use_state(s): yield s, ctl, clock


@pytest.mark.parametrize('direction,sign', [('a_to_b', 1), ('b_to_a', -1)])
def test_measured_jacobian_time_ramp_and_rotated_cabinet(force_clock, direction, sign):
    s, ctl, clock = force_clock
    samples = []
    def jac(named):
        samples.append(list(named.values()))
        return np.diag(list(named.values()))
    assist = ca.CabinetAssist(ctl, {'cabinet_assist': spec(direction),
        'cabinet_x_axis_root': [0., 1., 0.]}, jac)
    assist.update(); np.testing.assert_array_equal(ctl.torques[-1], np.zeros(3))
    clock.t = .25
    assist.update(); np.testing.assert_allclose(ctl.torques[-1], [0, sign*6, 0])
    clock.t = .5; ctl.q[1] = 3.
    assist.update(); np.testing.assert_allclose(ctl.torques[-1], [0, sign*18, 0])
    assert samples[-1][1] == 3.  # recompute at measured q, never reuse starting Jacobian
    start = len(ctl.torques)
    assist.spec['hold_s'] = 0
    assist.finish()
    magnitudes = [np.linalg.norm(t) for t in ctl.torques[start:]]
    assert all(a >= b-1e-10 for a, b in zip(magnitudes, magnitudes[1:]))
    assert magnitudes[-1] == 0 and clock.t >= .8


def test_world_rotation_pause_cancel_and_short_motion(force_clock):
    s, ctl, clock = force_clock
    world = np.eye(4); world[:3, :3] = Rotation.from_euler('z', 90, degrees=True).as_matrix()
    assist = ca.CabinetAssist(ctl, {'cabinet_assist': spec(),
        'cabinet_x_axis_root': [1., 0., 0.], 'world_T_root_ref': world.tolist()}, lambda q: np.eye(3))
    assist.update(np.eye(3)); clock.t = .5
    assist.update(np.eye(3)); np.testing.assert_allclose(ctl.torques[-1], [0, 6, 0], atol=1e-10)
    assist.update(world[:3, :3]); np.testing.assert_allclose(ctl.torques[-1], [6, 0, 0], atol=1e-10)
    assist.update(active=False); assert not np.any(ctl.torques[-1])
    clock.t = 1.; assist.update(); assert not np.any(ctl.torques[-1])  # resume starts at zero
    clock.t = 1.1; assist.update()
    before = np.linalg.norm(ctl.torques[-1]); assist.spec['hold_s'] = 0
    n = len(ctl.torques); assist.finish()
    assert max(np.linalg.norm(t) for t in ctl.torques[n:]) <= before+1e-10
    s.exec_cancel.set(); assist.update(); assert not np.any(ctl.torques[-1])


@pytest.mark.parametrize('backend', ['legacy', 'legacy_timed', 'pink'])
@pytest.mark.parametrize('stop_phase', [None, 'traj', 'release', 'miss_goal'])
def test_execution_keeps_goal_checks_and_clears_force(scene, monkeypatch, backend, stop_phase):
    from adapters.reach.execution_pink import PinkRuntime
    from adapters.reach.lowstate import MockLowStateSampler
    from tests.test_reach_pink_backend import FakeArmController
    ctl = FakeArmController(scene.current_q)
    s = scene.s
    s.controller, s.settle_trim = ctl, 'off'
    torques = []
    old_set = ctl.set_tau_ff
    old_read = ctl.read_measured
    slipped = False
    def set_tau(tau):
        nonlocal slipped
        torques.append(np.asarray(tau).copy())
        if stop_phase == s.exec_phase and np.linalg.norm(tau) > .01:
            s.exec_cancel.set()
        if stop_phase == 'miss_goal' and s.exec_phase == 'release' and not np.any(tau):
            slipped = True
        return old_set(tau)
    def read():
        q = old_read()
        if slipped: q[4] += .15
        return q
    ctl.set_tau_ff, ctl.read_measured = set_tau, read
    try:
        if backend == 'pink':
            sampler = MockLowStateSampler(arm_motor_indices=(15,16,17,18,19,20,21), arm_q_reader=ctl.read_measured)
            rt = PinkRuntime(arm_side='left', sampler=sampler, wrist_link='left_wrist_yaw_link')
            s.pink_runtime = rt; rt.anchor()
            _, fb = rt.update_world()
            scene.fresh['pose']['pink_world'] = {'world_T_root': fb.world_T_root.tolist(),
                'anchor_count': rt.world_frame.anchor_count}
        result, path = planned(scene, motion_backend=backend, cabinet_assist=spec())
        proof = o.execution_constraint(result['orientation']['id'], path, backend)
        s.exec_running = True
        execution._exec_loop(list(path), 1., speed=.4, command_start_q=scene.current_q,
            motion_backend=backend, execution_context={'orientation_constraint': proof})
        assert not s.exec_running
        assert any(np.linalg.norm(t) > .01 for t in torques)
        np.testing.assert_array_equal(torques[-1], np.zeros(7))
        np.testing.assert_array_equal(ctl._tau, np.zeros(7))
        if stop_phase == 'miss_goal':
            assert '终点位姿未达到' in s.exec_message, s.exec_message
        elif stop_phase:
            assert s.exec_message.startswith('已中止'), s.exec_message
        else:
            assert s.exec_message.startswith('完成'), s.exec_message
            o.check_goal_pose(o.joint_pose(ctl.read_measured()), proof['target_root'], proof['R_root_tcp'], position_tolerance_mm=10.)
            if backend != 'pink': np.testing.assert_allclose(ctl.targets[-1], path[-1])
    finally: ctl.shutdown()
