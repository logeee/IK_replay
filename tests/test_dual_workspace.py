"""Offline dual-arm invariants: no DDS initialization or robot commands."""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import numpy as np
import pytest
from fastapi import FastAPI

from adapters.reach.state import ReachState, ReachThread, state, use_state, current_state
from adapters.reach.workspace import ArmWorkspace, ArmContextMiddleware, load_compensation
from adapters.reach.dual_controller import DualArmController, DDSTransport
from core.collision import ConfigurableCollisionChecker, _segment_distance
from core.robot_config import load_app_config
from core.robot_model import RobotModel
from api import pointcloud_viewer as pc


@pytest.fixture
def workspace():
    w = ArmWorkspace.__new__(ArmWorkspace)
    w.default_arm = 'left_arm'
    w.states = {}
    w.owner = DualArmController(transport=SimpleNamespace())
    w.lock = asyncio.Lock()
    for arm in ('left_arm', 'right_arm'):
        s = ReachState()
        s.chain_id = arm
        s.workspace_enabled = True
        s.handeye_ready = True
        s.workspace = w
        w.states[arm] = s
    w.refresh_collision = lambda entry: None
    return w


def app_for(w):
    app = FastAPI()
    app.add_middleware(ArmContextMiddleware, workspace=w)

    @app.get('/api/reach/probe')
    def probe():
        received = []
        worker = ReachThread(target=lambda: received.append(state.chain_id))
        worker.start()
        worker.join()
        return {'arm': state.chain_id, 'thread': received}

    @app.post('/api/reach/execute')
    async def execute(body: dict):
        await asyncio.sleep(.03)
        state.exec_running = True
        state.exec_message = body['value']
        return {'arm': state.chain_id}

    @app.post('/api/reach/hand_move')
    def hand_move(body: dict):
        from adapters.reach.execution import reach_hand_move
        return reach_hand_move(body)

    @app.post('/api/fk')
    @app.post('/api/collision/check')
    def preview_fk():
        return {'ok': True}

    @app.post('/api/reach/align_yaw')
    def stop_align(body: dict):
        state.align_cancel.set()
        return {'ok': body.get('stop')}

    return app


def test_requests_and_workers_retain_their_arm(workspace):
    baseline = current_state()
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(workspace)), base_url='http://test') as c:
            responses = await asyncio.gather(*[c.get(f'/api/arms/{arm}/reach/probe') for arm in workspace.states])
            for arm, response in zip(workspace.states, responses):
                assert response.json() == {'arm': arm, 'thread': [arm]}
            assert (await c.get('/api/reach/probe')).json()['arm'] == 'left_arm'
    asyncio.run(run())
    assert current_state() is baseline


def test_simultaneous_execute_accepts_exactly_one_arm(workspace):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(workspace)), base_url='http://test') as c:
            responses = await asyncio.gather(*[c.post(f'/api/arms/{arm}/reach/execute', json={'value': arm}) for arm in workspace.states])
            assert sorted(r.status_code for r in responses) == [200, 409]
    asyncio.run(run())
    assert sum(s.exec_running for s in workspace.states.values()) == 1
    for arm, s in workspace.states.items():
        if s.exec_running:
            assert s.exec_message == arm


def test_other_float_blocks_execute_and_running_blocks_unload(workspace):
    left, right = workspace.states.values()
    right.controller = SimpleNamespace(status=lambda: {'float': True})
    assert '卸力' in workspace.control_error(left, 'execute', {})
    right.exec_running = True
    assert workspace.control_error(left, 'hand_move', {'on': True})
    assert workspace.control_error(left, 'pink/anchor', {})
    assert workspace.control_error(right, 'align_yaw', {'stop': True}) is None


def test_stop_alignment_remains_reachable_while_running(workspace):
    right = workspace.states['right_arm']
    right.align_running = True
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(workspace)), base_url='http://test') as c:
            assert (await c.post('/api/arms/right_arm/reach/align_yaw', json={'stop': True})).status_code == 200
    asyncio.run(run())
    assert right.align_cancel.is_set()


def test_disabled_arm_and_wrong_chain_rejected(workspace):
    workspace.states['right_arm'].workspace_enabled = False
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(workspace)), base_url='http://test') as c:
            assert (await c.post('/api/arms/right_arm/reach/execute', json={'value': 'x'})).status_code == 409
            assert (await c.post('/api/arms/left_arm/trajectory/plan', json={'chain_id': 'right_arm'})).status_code == 409
    asyncio.run(run())


def test_right_only_workspace_keeps_shared_scene_without_enabling_left(workspace):
    workspace.states['left_arm'].workspace_enabled = False
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(workspace)), base_url='http://test') as c:
            assert (await c.post('/api/fk', json={})).status_code == 200
            assert (await c.post('/api/collision/check', json={'chain_id':'right_arm'})).status_code == 200
            assert (await c.post('/api/collision/check', json={'chain_id':'left_arm'})).status_code == 200
            assert (await c.post('/api/arms/left_arm/collision/check', json={'chain_id':'right_arm'})).status_code == 409
            assert (await c.post('/api/reach/execute', json={'value':'left'})).status_code == 409
            assert (await c.post('/api/arms/right_arm/reach/execute', json={'value':'right'})).status_code == 200
    asyncio.run(run())
    assert not workspace.states['left_arm'].workspace_enabled
    assert not workspace.states['left_arm'].exec_running


def test_compensation_files_validate_side_and_remain_independent(tmp_path):
    outputs = []
    for arm, mass in [('left', .43), ('right', .85)]:
        p = tmp_path / f'{arm}.json'
        p.write_text(json.dumps({'arm': arm, 'mass_kg': mass, 'com_m': [.01, .02, .03], 'alpha': 1.0}))
        outputs.append(load_compensation(p, '', arm + '_arm', 'test')['effective_parameters'])
        with pytest.raises(ValueError, match='不一致'):
            load_compensation(p, '', ('right' if arm == 'left' else 'left') + '_arm', 'test')
    assert outputs[0]['payload_kg'] == .43
    assert outputs[1]['payload_kg'] == .85
    assert outputs[0]['payload_link'] == 'left_wrist_yaw_link'
    assert outputs[1]['payload_link'] == 'right_wrist_yaw_link'
    assert all(p['grav_in_float'] for p in outputs)


@pytest.mark.parametrize('side', ['left', 'right'])
@pytest.mark.parametrize('with_hand', [True, False, None])
def test_payload_file_honors_native_hand_model(tmp_path, side, with_hand):
    raw = {'arm': side, 'mass_kg': 1.477, 'com_m': [.2828, -.0006, .032], 'alpha': 1.0}
    if with_hand is not None:
        raw['with_hand'] = with_hand
    path = tmp_path / '劳洛斯_2机械螺丝刀带相机.json'
    path.write_text(json.dumps(raw))
    params = load_compensation(path, '', side + '_arm', '')['effective_parameters']
    assert params['excluded_subtree_link'] == (f'{side}_hand_link' if with_hand is False else None)
    assert params['payload_link'] == f'{side}_wrist_yaw_link'
    assert params['payload_kg'] == 1.477
    assert params['payload_com_m'] == [.2828, -.0006, .032]


@pytest.mark.parametrize('invalid', ['false', 0, None])
def test_payload_file_rejects_ambiguous_hand_flag(tmp_path, invalid):
    path = tmp_path / 'payload.json'
    path.write_text(json.dumps({'arm':'right', 'mass_kg':1.477, 'com_m':[.2828,-.0006,.032], 'alpha':1., 'with_hand':invalid}))
    with pytest.raises(ValueError, match='with_hand'):
        load_compensation(path, '', 'right_arm', '')


@pytest.fixture
def arm_law(monkeypatch):
    # Instantiate the existing control law without calling its hardware constructor.
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "calib/calib_workstation"))
    from calib_workstation.calib3d.arm import H2ArmController
    def make(side, compensation):
        c = H2ArmController.__new__(H2ArmController)
        c.arm, c.n = side, 7
        c._lock = threading.Lock()
        c._float = c._jog_enabled = False
        c._engaged = True
        c._cmd_q = np.zeros(7)
        c._desired_q = np.zeros(7)
        c._tau_push = np.zeros(7)
        c.max_speed = .4
        c.limits = np.column_stack((np.full(7, -3.), np.full(7, 3.)))
        c.hand_move_kd = 2.
        c.kp_vec, c.kd_vec = np.full(7, 50.), np.full(7, 3.)
        c._jog_indices = list(range(15 if side == 'left' else 22, 22 if side == 'left' else 29))
        c._last_sent_sequence = 0
        c.grav_alpha, c.grav_in_float = 1., True
        c._tau_cap = np.full(7, 100.)
        c._grav_model = SimpleNamespace(torque=lambda q, g_dir: q + compensation)
        c._gravity_dir = lambda: None
        c.measured = np.full(7, .3)
        c.read_measured = lambda: c.measured.copy()
        return c
    return make


def test_one_publish_contains_both_sides_and_independent_float(arm_law):
    writes = []
    owner = DualArmController(transport=SimpleNamespace(write=lambda outputs, weight: writes.append((outputs, weight))))
    left, right = arm_law('left', 1), arm_law('right', 2)
    owner.channels = {'left': left, 'right': right}
    left.enter_hand_move()
    right.enable_jog()
    right.set_target(np.full(7, .5))
    owner.tick()
    assert len(writes) == 1
    data, weight = writes[-1]
    assert set(data) == {'left', 'right'}
    assert np.all(data['left']['kp'] == 0)
    assert np.all(data['left']['kd'] == 2)
    assert np.all(data['right']['kp'] == 50)
    np.testing.assert_allclose(data['left']['tau'], (left.measured + 1) * weight)
    np.testing.assert_allclose(data['right']['tau'], (data['right']['q'] + 2) * weight)
    assert np.max(data['right']['q']) <= .4 * .02
    right_before = right._cmd_q.copy()
    left.measured[:] = .7
    left.stop()
    np.testing.assert_allclose(left._cmd_q, .7)
    np.testing.assert_allclose(right._cmd_q, right_before)
    right.stop()
    left.enter_hand_move()
    right.enter_hand_move()
    owner.tick()
    assert all(np.all(v['kp'] == 0) for v in writes[-1][0].values())
    right.grav_in_float = False
    owner.tick()
    assert np.all(writes[-1][0]['right']['tau'] == 0)


def test_releasing_one_side_does_not_drop_shared_weight(arm_law):
    owner = DualArmController(transport=object())
    owner.channels = {'left': arm_law('left', 1), 'right': arm_law('right', 2)}
    owner.active = {'left', 'right'}
    owner.weight = 1.
    owner.release('left')
    assert owner.active == {'right'}
    assert owner.weight == 1.
    assert not owner._stop.is_set()


def test_transport_rejects_nonfinite_before_publishing():
    writes = []
    transport = DDSTransport.__new__(DDSTransport)
    transport._command = SimpleNamespace(motor_cmd=[SimpleNamespace() for _ in range(35)])
    transport._publisher = SimpleNamespace(Write=writes.append)
    data = {k: np.zeros(7) for k in ('q', 'kp', 'kd', 'tau')}
    data['q'][0] = np.nan
    with pytest.raises(RuntimeError, match='非有限'):
        transport._publish({'left': {**data, 'indices': range(15,22)}}, 1.)
    assert not writes


def test_collision_checks_include_opposite_arm():
    model = RobotModel(load_app_config().robots['h2'])
    checker = ConfigurableCollisionChecker(model)
    checker.other_arm_provider = lambda _: ('right_arm', model.initial_joints('right_arm'), model.tcp_offset('right_arm'))
    result = checker.check_state(model.initial_joints('left_arm'), 'left_arm')
    pairs = [p for p in result['pairs'] if p['b'].startswith('right_arm_')]
    assert pairs
    assert all(np.isfinite(p['distance_m']) for p in pairs)
    assert _segment_distance(np.array([-1.,0,0]), np.array([1.,0,0]), np.array([0.,-1,0]), np.array([0.,1,0])) == 0


def test_pointcloud_capture_is_retained_per_side_and_cross_confirm_rejected():
    left = SimpleNamespace(capture_id='left', metadata={'arm': 'left_arm'})
    right = SimpleNamespace(capture_id='right', metadata={'arm': 'right_arm'})
    with patch.object(pc, '_latest', right), patch.object(pc, '_latest_by_arm', {'left_arm':left, 'right_arm':right}), patch.object(pc._http, 'post') as post:
        assert pc._capture_by_id('left') is left
        assert pc.status('left_arm')['latest_capture_id'] == 'left'
        assert pc.status('right_arm')['latest_capture_id'] == 'right'
        assert pc.confirm_pointcloud_target('left', {'arm':'right_arm'}).status_code == 409
        post.assert_not_called()
    assert pc._reach_url('confirm_pointcloud_pick', 'left_arm').endswith('/api/arms/left_arm/reach/confirm_pointcloud_pick')


def test_managed_channels_create_no_additional_dds_publishers(arm_law):
    from unitree_sdk2py.core import channel
    from calib_workstation.calib3d import arm as legacy_arm
    sample = SimpleNamespace(motor_state=[SimpleNamespace(q=0., dq=0., tau_est=0.) for _ in range(35)])
    transport = SimpleNamespace(snapshot=lambda: sample)
    owner = DualArmController(transport=transport)
    owner.configure('left', {'grav_alpha': .8})
    owner.configure('right', {'grav_alpha': 1.1})
    with patch.object(legacy_arm, 'ensure_dds_initialized'), patch.object(channel, 'ChannelPublisher') as publisher, patch.object(channel, 'ChannelSubscriber') as subscriber:
        owner._initialize()
        publisher.assert_not_called()
        subscriber.assert_not_called()
        assert owner.channels['left'].grav_alpha == .8
        assert owner.channels['right'].grav_alpha == 1.1
        assert all(c._publisher is None for c in owner.channels.values())
        assert not owner.engaged


def test_payload_conversion_preserves_identification_torque(arm_law, monkeypatch, tmp_path):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'calib/arm_payload_gravity'))
    from gravity import ArmGravity
    from calib_workstation.calib3d.arm import _load_arm_model
    from calib_workstation.calib3d.gravity import ArmGravityModel
    for side in ['left', 'right']:
        raw = {'arm':side,'mass_kg':.849,'com_m':[.3718,-.009,.0918],'alpha':1.054}
        path = tmp_path / f'{side}.json'
        path.write_text(json.dumps(raw))
        params = load_compensation(path,'',side+'_arm','')['effective_parameters']
        model, chain = _load_arm_model(side)
        converted = ArmGravityModel(model,chain,**{k:params[k] for k in ['payload_kg','payload_com_m','payload_link','excluded_subtree_link']})
        original = ArmGravity(side)
        for q in [np.array([.3,.2,.1,1.,.05,.1,.01]), np.array([-.2,.4,.1,.5,-.1,.2,.1])]:
            np.testing.assert_allclose(params['grav_alpha'] * converted.torque(q),original.tau_total(q,raw['mass_kg'],raw['com_m'],raw['alpha']),rtol=0,atol=1e-10)


def test_capturing_another_arm_preserves_both_windows_and_legacy():
    import cv2
    _, jpeg = cv2.imencode('.jpg',np.zeros((2,2,3),dtype=np.uint8))
    snapshot={'jpeg':jpeg.tobytes(),'depth_mm':np.full((2,2),1000,dtype=np.float32),'intrinsics':(100.,100.,.5,.5),'metadata':{},'T_cam2root':None}
    with patch.object(pc,'_latest',None), patch.object(pc,'_latest_by_arm',{}), patch.object(pc,'_model',None), patch.object(pc,'_fetch_rgbd_snapshot',return_value=snapshot) as fetch:
        legacy=pc.capture({'stride':1})
        left=pc.capture({'stride':1,'arm':'left_arm'})
        right=pc.capture({'stride':1,'arm':'right_arm'})
        assert pc.status()['latest_capture_id'] == legacy['capture_id']
        assert pc.status('left_arm')['latest_capture_id'] == left['capture_id']
        assert pc.status('right_arm')['latest_capture_id'] == right['capture_id']
        for meta in (legacy,left,right):
            assert pc.capture_image(meta['capture_id']).status_code == 200
            assert pc.pointcloud_data(meta['capture_id']).status_code == 200
        assert fetch.call_args_list[-2].kwargs == {'arm':'left_arm'}
        assert fetch.call_args_list[-1].kwargs == {'arm':'right_arm'}
