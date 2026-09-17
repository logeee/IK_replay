"""Cabinet-relative endpoint planning; all sensing mocked, no hardware commands."""
import json
import time
from copy import deepcopy
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from adapters.reach import cabinet_waypoints as c, orientation as o, waypoint_rgbd as rgbd
from adapters.reach.execution import _resolve_exec_backend
from adapters.reach.state import ReachState, use_state
from core.robot_config import load_app_config
from core.robot_model import RobotModel
from ik.numerical_solver import NumericalIKSolver


@pytest.fixture
def scene(tmp_path, monkeypatch):
    config = load_app_config()
    s = ReachState()
    s.robot_model = RobotModel(config.robots['h2'])
    s.ik_solver = NumericalIKSolver(s.robot_model, config.ik)
    s.chain_id = 'left_arm'
    s.joint_names = s.robot_model.joint_names(s.chain_id)
    s.wrist_link = s.robot_model.end_link(s.chain_id)
    s.p_tool = [.18, -.05, .02]
    s.T_wrist2hand = np.eye(4).tolist()
    s.T_cam2torso = np.eye(4)
    s.handeye_ready = True
    s.active_combo = {'hand_id':'test', 'mount_profile_id':'measured', 'camera_role':'head'}
    s.waypoints_dir = tmp_path
    q = np.array([-1.45,.74,-.95,.19,1.95,.26,.58])
    s.provider_reader = lambda: q.copy()
    with use_state(s):
        pose = rgbd.pose_sample()
        pose = rgbd.paired_pose(pose, pose)
        _, jpeg = cv2.imencode('.jpg', np.zeros((16,16,3),np.uint8))
        config_frame = {'method':'method2_panel_edges','params':{}}
        capture = {'arm':'left_arm','capture_id':'record-B','source':{'robot_pose':pose},
                   'recording':{'cabinet_frame_config':config_frame},
                   'intrinsics':[100.,100.,8.,8.], 'distortion':[0.]*5}
        bundle = {'capture':capture, 'pose':pose, 'jpeg':jpeg.tobytes(),
                  'depth':np.ones((16,16),np.float32)*1000, 'cloud':b'test'}
        file = 'B_20260917_010000.json'
        (tmp_path / file.removesuffix('.json')).mkdir()
        link = rgbd.write_bundle(tmp_path / file.removesuffix('.json') / 'rgbd', bundle, file)
        (tmp_path / file).write_text(json.dumps({'name':'B','arm':'left_arm','rgbd':link}))
        saved_panel = np.eye(4)
        saved_panel[:3,3] = [.5,0,.5]
        fresh = deepcopy(bundle)
        current_q = q.copy()
        current_q[4] -= .35  # actual start is neither A nor B
        s.provider_reader = lambda: current_q.copy()
        fresh['pose'] = rgbd.pose_sample()
        fresh['pose'] = rgbd.paired_pose(fresh['pose'],fresh['pose'])
        fresh['capture']['source']['robot_pose'] = fresh['pose']
        fresh['capture']['capture_id'] = 'fresh'
        current_panel = saved_panel.copy()
        current_panel[:3,3] += [.003,.002,0]  # camera/robot moved relative to cabinet
        size = np.array([.24,.18])
        def frame(bundle, config):
            return (current_panel if bundle['capture']['capture_id']=='fresh' else saved_panel), size, {'rms_m':.001}
        monkeypatch.setattr(c,'panel_frame',frame)
        monkeypatch.setattr(c,'capture_bundle',lambda: fresh)
        c._saved_geometry.cache_clear()
        yield SimpleNamespace(s=s, file=file, q=q, current_q=current_q, fresh=fresh,
                              current_panel=current_panel, saved_panel=saved_panel, size=size)


def planned(scene, **kwargs):
    result = c.plan_waypoint({'file':scene.file, 'motion_backend':'legacy_timed', **kwargs})
    assert isinstance(result,dict), getattr(result,'body',result)
    path = np.array([[w['named_joints'][n] for n in scene.s.joint_names] for w in result['waypoints']])
    return result, path


def test_actual_start_direct_to_relocalized_goal_pose(scene):
    result,path = planned(scene)
    expected = o.joint_pose(scene.q)
    expected[:3,3] += [.003,.002,0]
    np.testing.assert_allclose(path[0],scene.current_q,atol=0)
    np.testing.assert_allclose(result['orientation']['target_root'],expected[:3,3],atol=1e-10)
    assert o.angle_deg(o.joint_pose(path[0])[:3,:3],expected[:3,:3]) > 15
    o.check_goal_pose(o.joint_pose(path[-1]),expected[:3,3],expected[:3,:3])
    assert max(np.max(abs(q-scene.q)) for q in path[:len(path)//2]) > .1
    assert result['cabinet']['origin'] == 'panel_rectangle_center'
    assert scene.s.pick_context is None  # no old 7005 pick required or overwritten
    for backend in ('legacy','legacy_timed'):
        proof=o.execution_constraint(result['orientation']['id'],path,backend)
        assert proof['scope']=='goal'
        assert _resolve_exec_backend(backend,label='柜面点位:A→B',cabinet_plan=True)==(backend,None)


def test_direct_arrival_accepts_distant_actual_start_without_an_opposite_point(scene,monkeypatch):
    from unittest.mock import Mock
    from adapters.reach import execution
    # The archive contains only the destination. No opposite A/B point exists.
    scene.current_q[0]+=.5
    fresh_pose=rgbd.pose_sample()
    scene.fresh['pose']=rgbd.paired_pose(fresh_pose,fresh_pose)
    scene.fresh['capture']['source']['robot_pose']=scene.fresh['pose']
    assert np.linalg.norm(o.joint_pose(scene.current_q)[:3,3]-o.joint_pose(scene.q)[:3,3])>.15
    assist={'direction':'a_to_b','force_n':0.,'ramp_s':.5,'hold_s':0.,'release_s':.65}
    result,path=planned(scene,cabinet_assist=assist)
    np.testing.assert_array_equal(path[0],scene.current_q)
    target=o.joint_pose(scene.q)
    target[:3,3]+=[.003,.002,0]
    o.check_goal_pose(o.joint_pose(path[-1]),target[:3,3],target[:3,:3])
    scene.s.controller=SimpleNamespace(
        read_measured=lambda:scene.current_q.copy(),status=lambda:{'float':False},
        command_snapshot=lambda:{'q_rad':scene.current_q.tolist(),'sequence':1,
            'sent_at_monotonic':time.monotonic(),'tau_ff_nm':[0.]*7})
    thread=Mock()
    monkeypatch.setattr(execution,'ReachThread',thread)
    response=execution.reach_execute({'waypoints':[dict(zip(scene.s.joint_names,q)) for q in path],
        'orientation_plan_id':result['orientation']['id'],'cabinet_assist':assist,
        'motion_backend':'legacy_timed','label':'柜面点位:直接到 B'})
    assert response['ok']
    thread.return_value.start.assert_called_once()
    assert thread.call_args.kwargs['kwargs']['push_tau'] is None


def test_path_and_id_cannot_be_forged(scene):
    result,path=planned(scene)
    with pytest.raises(ValueError,match='规划编号'): o.execution_constraint(None,path,'legacy')
    with pytest.raises(ValueError,match='失效'): o.execution_constraint('fake',path,'legacy')
    path[-1,0] += .02
    with pytest.raises(ValueError,match='失效'): o.execution_constraint(result['orientation']['id'],path,'legacy')
    assert _resolve_exec_backend('legacy_timed',label='柜面点位:A→B')[1]


def test_relocalization_rotates_goal_orientation_with_cabinet(scene):
    from scipy.spatial.transform import Rotation
    scene.current_panel[:3,:3]=Rotation.from_euler('z',2,degrees=True).as_matrix()
    result,path=planned(scene)
    root_base=scene.s.robot_model.forward_kinematics({})[scene.s.base_link]
    base_tcp=np.linalg.inv(root_base)@o.joint_pose(scene.q)
    expected=root_base@scene.current_panel@np.linalg.inv(scene.saved_panel)@base_tcp
    np.testing.assert_allclose(result['orientation']['R_root_tcp'],expected[:3,:3],atol=1e-10)
    np.testing.assert_allclose(result['orientation']['target_root'],expected[:3,3],atol=1e-10)
    o.check_goal_pose(o.joint_pose(path[-1]),expected[:3,3],expected[:3,:3])


@pytest.mark.parametrize('offset_mm',[-10.,7.5])
def test_x_offset_moves_only_destination_in_cabinet_axes_without_accumulation(scene,offset_mm):
    from scipy.spatial.transform import Rotation
    scene.current_panel[:3,:3]=Rotation.from_euler('z',2,degrees=True).as_matrix()
    baseline,_=planned(scene)
    result,path=planned(scene,cabinet_x_offset_mm=offset_mm)
    root_base=scene.s.robot_model.forward_kinematics({})[scene.s.base_link]
    axis=(root_base@scene.current_panel)[:3,0]
    np.testing.assert_allclose(np.array(result['orientation']['target_root'])-
        baseline['orientation']['target_root'],axis*offset_mm/1000.,atol=1e-12)
    np.testing.assert_array_equal(result['orientation']['R_root_tcp'],baseline['orientation']['R_root_tcp'])
    np.testing.assert_array_equal(path[0],scene.current_q)
    assert result['cabinet_x_offset_mm']==offset_mm
    proof=o.execution_constraint(result['orientation']['id'],path,'legacy_timed')
    assert proof['cabinet_x_offset_mm']==offset_mm
    o.check_goal_pose(o.joint_pose(path[-1]),proof['target_root'],proof['R_root_tcp'])
    reset,_=planned(scene,cabinet_x_offset_mm=0)
    np.testing.assert_array_equal(reset['orientation']['target_root'],baseline['orientation']['target_root'])
    assert reset['cabinet_x_offset_mm']==0


@pytest.mark.parametrize('axis',list('xyz'))
@pytest.mark.parametrize('offset',[float('nan'),float('inf'),-100.1,100.1,True,'10',None,{}])
def test_invalid_offset_rejected_before_capture(scene,monkeypatch,axis,offset):
    monkeypatch.setattr(c,'capture_bundle',lambda:pytest.fail('invalid offset must not capture'))
    result=c.plan_waypoint({'file':scene.file,f'cabinet_{axis}_offset_mm':offset})
    assert result.status_code==422 and '偏移' in json.loads(result.body)['error']
    assert scene.s.orientation_plan is None


@pytest.mark.parametrize('offset_xyz',[[0.,6.,0.],[0.,0.,-4.],[-3.,5.,2.]])
def test_xyz_offsets_follow_rotated_cabinet_frame_and_preserve_record(scene,offset_xyz):
    from scipy.spatial.transform import Rotation
    scene.current_panel[:3,:3]=Rotation.from_euler('xyz',[2,-3,4],degrees=True).as_matrix()
    baseline,_=planned(scene)
    record_before=(scene.s.waypoints_dir/scene.file).read_bytes()
    offsets={f'cabinet_{axis}_offset_mm':v for axis,v in zip('xyz',offset_xyz)}
    result,path=planned(scene,**offsets)
    root_base=scene.s.robot_model.forward_kinematics({})[scene.s.base_link]
    delta=(root_base@scene.current_panel)[:3,:3]@np.asarray(offset_xyz)/1000.
    np.testing.assert_allclose(np.asarray(result['orientation']['target_root'])-
        baseline['orientation']['target_root'],delta,atol=1e-12)
    np.testing.assert_array_equal(result['orientation']['R_root_tcp'],baseline['orientation']['R_root_tcp'])
    np.testing.assert_array_equal(path[0],scene.current_q)
    proof=o.execution_constraint(result['orientation']['id'],path,'legacy_timed')
    for axis in 'xyz':
        key=f'cabinet_{axis}_offset_mm'
        assert result[key]==proof[key]==result['cabinet'][f'{axis}_offset_mm']==offsets[key]
    o.check_goal_pose(o.joint_pose(path[-1]),proof['target_root'],proof['R_root_tcp'])
    repeated,_=planned(scene,**offsets)
    np.testing.assert_array_equal(repeated['orientation']['target_root'],result['orientation']['target_root'])
    reset,_=planned(scene)
    np.testing.assert_array_equal(reset['orientation']['target_root'],baseline['orientation']['target_root'])
    assert (scene.s.waypoints_dir/scene.file).read_bytes()==record_before


@pytest.mark.parametrize('change',['tcp','record','pick','expired','stop'])
def test_stale_plan_rejected(scene,change):
    result,path=planned(scene)
    if change=='tcp': scene.s.p_tool[0]+=.001
    if change=='record': (scene.s.waypoints_dir/scene.file).write_text('{}')
    if change=='pick': scene.s.pick_revision+=1
    if change=='expired': scene.s.orientation_plan['created_at_unix']-=100
    if change=='stop': scene.s.cabinet_plan_revision+=1
    with pytest.raises(ValueError): o.execution_constraint(result['orientation']['id'],path,'legacy_timed')


def test_pink_uses_this_capture_anchor_and_does_not_require_previous_pick(scene):
    scene.fresh['pose']['pink_world']={'world_T_root':np.eye(4).tolist(),'anchor_count':4}
    scene.s.pink_runtime=SimpleNamespace(world_frame=SimpleNamespace(anchored=True,anchor_count=4))
    result,path=planned(scene,motion_backend='pink')
    assert o.execution_constraint(result['orientation']['id'],path,'pink')
    assert _resolve_exec_backend('pink',label='柜面点位',cabinet_plan=True)==('pink',None)
    scene.s.pink_runtime.world_frame.anchor_count+=1
    with pytest.raises(ValueError,match='锚定'): o.execution_constraint(result['orientation']['id'],path,'pink')


def test_pink_without_capture_anchor_rejected(scene):
    result=c.plan_waypoint({'file':scene.file,'motion_backend':'pink'})
    assert result.status_code==422 and '锚定' in json.loads(result.body)['error']


@pytest.mark.parametrize('change',['wrong_arm','tool','mount','size','stale_capture','moving','unsafe_path'])
def test_invalid_input_does_not_plan(scene,change,monkeypatch):
    if change=='wrong_arm': scene.s.chain_id='right_arm'
    if change=='tool': scene.s.p_tool[0]+=.01
    if change=='mount': scene.s.T_wrist2hand[0][3]=.01
    if change=='size':
        original=c.panel_frame
        monkeypatch.setattr(c,'panel_frame',lambda b,cfg:(lambda t,s,f:(t,s*2 if b['capture']['capture_id']=='fresh' else s,f))(*original(b,cfg)))
    if change=='stale_capture': scene.fresh['pose']['sample_finished_unix_ns']=time.time_ns()-20_000_000_000
    if change=='moving': scene.s.exec_running=True
    monkeypatch.setattr(c,'solve',lambda args: pytest.fail('invalid input reached planner'))
    result=c.plan_waypoint({'file':'../outside.json' if change=='unsafe_path' else scene.file})
    assert result.status_code==422
    assert scene.s.orientation_plan is None


def test_registry_lists_only_rgbd_points_and_reports_mismatch(scene):
    assert c.list_waypoints()['waypoints'][0]['compatible']
    scene.s.p_tool[0]+=.001
    entries=c.list_waypoints()['waypoints']
    assert not entries[0]['compatible'] and 'TCP' in entries[0]['error']
    scene.s.chain_id='right_arm'
    assert c.list_waypoints()['waypoints']==[]


def test_concurrent_planning_is_rejected(scene):
    with scene.s.cabinet_plan_lock:
        assert c.plan_waypoint({'file':scene.file}).status_code==409


@pytest.mark.parametrize('offset_xyz',[[0.,0.,0.],[3.,-4.,5.]])
def test_already_at_endpoint_does_not_plan_or_execute(scene,monkeypatch,offset_xyz):
    scene.current_panel[:]=scene.saved_panel
    root_base=scene.s.robot_model.forward_kinematics({})[scene.s.base_link]
    actual=np.linalg.inv(root_base)@o.joint_pose(scene.q)
    actual[:3,3]+=np.asarray(offset_xyz)/1000.
    scene.fresh['pose']['T_base_from_tcp']=actual.tolist()
    monkeypatch.setattr(c,'solve',lambda args:pytest.fail('no unnecessary movement'))
    offsets={f'cabinet_{axis}_offset_mm':v for axis,v in zip('xyz',offset_xyz)}
    result=c.plan_waypoint({'file':scene.file,**offsets})
    assert result['already_at_target'] and result['goal_position_error_mm']<1e-6
    assert all(result[key]==value for key,value in offsets.items())
    assert scene.s.orientation_plan is None


def test_stop_during_capture_cannot_issue_executable_plan(scene,monkeypatch):
    def stopped_capture():
        scene.s.cabinet_plan_revision+=1
        return scene.fresh
    monkeypatch.setattr(c,'capture_bundle',stopped_capture)
    monkeypatch.setattr(c,'solve',lambda args:pytest.fail('stop must invalidate before IK'))
    result=c.plan_waypoint({'file':scene.file})
    assert result.status_code==422 and '急停' in json.loads(result.body)['error']
    assert scene.s.orientation_plan is None


@pytest.mark.parametrize('backend',['legacy','legacy_timed','pink'])
@pytest.mark.parametrize('arrival',[None, {'position_mm':6., 'orientation_deg':3.}])
def test_actual_controller_loops_accept_ab_proof_without_old_pick(scene,backend,arrival,monkeypatch):
    from adapters.reach import execution
    from adapters.reach.execution_pink import PinkRuntime
    from adapters.reach.lowstate import MockLowStateSampler
    from tests.test_reach_pink_backend import FakeArmController
    ctl=FakeArmController(scene.current_q)
    s=scene.s
    s.controller,s.settle_trim=ctl,'off'
    try:
        if backend=='pink':
            sampler=MockLowStateSampler(arm_motor_indices=(15,16,17,18,19,20,21),arm_q_reader=ctl.read_measured)
            rt=PinkRuntime(arm_side='left',sampler=sampler,wrist_link='left_wrist_yaw_link')
            s.pink_runtime=rt
            rt.anchor()
            _,fb=rt.update_world()
            scene.fresh['pose']['pink_world']={'world_T_root':fb.world_T_root.tolist(),
                                            'anchor_count':rt.world_frame.anchor_count}
            if arrival:
                closest_ik_endpoint(scene,monkeypatch,5.18,.38)
        result,path=planned(scene,motion_backend=backend,**({'arrival_tolerance':arrival} if arrival else {}))
        proof=o.execution_constraint(result['orientation']['id'],path,backend)
        checked=[]
        original=o.check_arrival_pose
        def check(actual,position,rotation,constraint):
            checked.append(constraint.get('arrival_tolerance'))
            expected=np.asarray(proof['target_root'])
            if backend=='pink':
                model_root=s.robot_model.forward_kinematics({})['torso_link']
                expected=(np.asarray(proof['world_T_root_ref'])@np.linalg.inv(model_root)@np.r_[expected,1.])[:3]
            np.testing.assert_allclose(position,expected,rtol=0,atol=1e-12)
            errors=original(actual,position,rotation,constraint)
            if backend=='pink' and arrival:
                assert errors['position_error_mm']==pytest.approx(5.18,abs=.3)
            return errors
        monkeypatch.setattr(o,'check_arrival_pose',check)
        s.exec_running=True
        execution._exec_loop(list(path),1.5,speed=.4,label='柜面点位:A→B',
            command_start_q=scene.current_q,motion_backend=backend,
            execution_context={'orientation_constraint':proof})
        assert s.exec_message.startswith('完成'),s.exec_message
        assert not s.exec_running
        assert checked==[arrival]
        assert len(ctl.targets)>10
        np.testing.assert_allclose(ctl.targets[0],scene.current_q,atol=.015)
        assert o.angle_deg(o.joint_pose(ctl.read_measured())[:3,:3],proof['R_root_tcp'])<2
    finally: ctl.shutdown()


@pytest.mark.parametrize('value',[None,{},True,{'position_mm':10},
    {'position_mm':True,'orientation_deg':2}, {'position_mm':'10','orientation_deg':2},
    {'position_mm':float('nan'),'orientation_deg':2}, {'position_mm':100.1,'orientation_deg':2},
    {'position_mm':0,'orientation_deg':2}, {'position_mm':10,'orientation_deg':45.1},
    {'position_mm':10,'orientation_deg':.09}, {'position_mm':10,'orientation_deg':float('inf')}])
def test_invalid_arrival_tolerance_rejected_before_capture(scene,monkeypatch,value):
    monkeypatch.setattr(c,'capture_bundle',lambda:pytest.fail('invalid limits must not capture'))
    result=c.plan_waypoint({'file':scene.file,'arrival_tolerance':value})
    assert result.status_code==422 and '容差' in json.loads(result.body)['error']


@pytest.mark.parametrize('position_mm,angle_deg,within',[(9,1.5,True),(11,1.5,False),(9,2.5,False)])
def test_direct_no_motion_requires_both_limits_around_offset_goal(scene,monkeypatch,position_mm,angle_deg,within):
    from scipy.spatial.transform import Rotation
    scene.current_panel[:]=scene.saved_panel
    root_base=scene.s.robot_model.forward_kinematics({})[scene.s.base_link]
    actual=np.linalg.inv(root_base)@o.joint_pose(scene.q)
    offsets=np.array([3.,-4.,5.])
    actual[:3,3]+=(offsets+[position_mm,0,0])/1000.
    actual[:3,:3]=actual[:3,:3]@Rotation.from_euler('x',angle_deg,degrees=True).as_matrix()
    scene.fresh['pose']['T_base_from_tcp']=actual.tolist()
    def must_plan(args): raise ValueError('outside arrival limits: motion required')
    monkeypatch.setattr(c,'solve',must_plan)
    limits={'position_mm':10,'orientation_deg':2}
    result=c.plan_waypoint({'file':scene.file,'arrival_tolerance':limits,
        **{f'cabinet_{a}_offset_mm':v for a,v in zip('xyz',offsets)}})
    if within:
        assert result['already_at_target'] and result['arrival_tolerance']==limits
        assert result['goal_position_error_mm']==pytest.approx(position_mm)
        assert result['goal_orientation_error_deg']==pytest.approx(angle_deg)
        assert scene.s.orientation_plan is None
    else: assert result.status_code==422 and 'motion required' in json.loads(result.body)['error']


def test_strict_direct_tolerance_is_met_by_ik_and_does_not_leak_to_roundtrip(scene):
    limits={'position_mm':.2,'orientation_deg':.1}
    result,path=planned(scene,arrival_tolerance=limits)
    proof=o.execution_constraint(result['orientation']['id'],path,'legacy_timed')
    assert proof['arrival_tolerance']==limits
    o.check_arrival_pose(o.joint_pose(path[-1]),proof['target_root'],proof['R_root_tcp'],proof)
    regular,_=planned(scene)
    assert 'arrival_tolerance' not in regular['orientation']


def closest_ik_endpoint(scene,monkeypatch,position_mm,angle_deg):
    from scipy.spatial.transform import Rotation
    from ik import numerical_solver
    # Reproduce an optimizer's closest solution using real FK and the actual
    # NumericalIKSolver acceptance check, without commanding any hardware.
    target=o.joint_pose(scene.q)
    target[:3,3]+=[position_mm/1000.,0,0]
    target[:3,:3]=target[:3,:3]@Rotation.from_euler('x',angle_deg,degrees=True).as_matrix()
    root_base=scene.s.robot_model.forward_kinematics({})[scene.s.base_link]
    saved=np.linalg.inv(root_base@scene.current_panel)@target
    original_geometry=c._saved_geometry
    def geometry(*args):
        _,size,config,details=original_geometry(*args)
        return saved,size,config,details
    monkeypatch.setattr(c,'_saved_geometry',geometry)
    monkeypatch.setattr(c,'solve',lambda args:o.plan_path(*args))
    monkeypatch.setattr(numerical_solver,'least_squares',lambda *a,**kw:
        SimpleNamespace(success=True,x=scene.q.copy(),nfev=1))


@pytest.mark.parametrize('position_mm,angle_deg,limits,accepted',[
    (5.18,.38,{'position_mm':10.,'orientation_deg':2.},True),
    (8.,3.,{'position_mm':10.,'orientation_deg':5.},True),
    (10.01,.38,{'position_mm':10.,'orientation_deg':2.},False),
    (5.18,2.01,{'position_mm':10.,'orientation_deg':2.},False),
    (5.18,.1,None,False),  # Original A→B/B→A IK still requires 2 mm.
    (1.,.38,None,False),  # Original A→B/B→A IK still requires 0.35°.
])
def test_approximate_ik_endpoint_uses_configured_direct_limits(
        scene,monkeypatch,position_mm,angle_deg,limits,accepted):
    closest_ik_endpoint(scene,monkeypatch,position_mm,angle_deg)
    result=c.plan_waypoint({'file':scene.file,'motion_backend':'legacy_timed',
        **({'arrival_tolerance':limits} if limits else {})})
    if accepted:
        assert isinstance(result,dict),getattr(result,'body',result)
        path=[[w['named_joints'][n] for n in scene.s.joint_names] for w in result['waypoints']]
        assert result['goal_position_error_mm']==pytest.approx(position_mm)
        assert result['goal_orientation_error_deg']==pytest.approx(angle_deg)
        proof=o.execution_constraint(result['orientation']['id'],path,'legacy_timed')
        assert proof['arrival_tolerance']==limits
        o.check_arrival_pose(o.joint_pose(path[-1]),proof['target_root'],proof['R_root_tcp'],proof)
    else:
        assert result.status_code==422
        assert 'IK' in json.loads(result.body)['error']
        assert scene.s.orientation_plan is None


@pytest.mark.parametrize('submitted',['match','missing','changed'])
def test_execute_cannot_change_or_drop_direct_arrival_limits(scene,monkeypatch,submitted):
    from unittest.mock import Mock
    from adapters.reach import execution
    limits={'position_mm':15.,'orientation_deg':5.}
    result,path=planned(scene,arrival_tolerance=limits)
    scene.s.controller=SimpleNamespace(read_measured=lambda:scene.current_q.copy(),
        status=lambda:{'float':False},command_snapshot=lambda:{'q_rad':scene.current_q.tolist(),
        'sequence':1,'sent_at_monotonic':time.monotonic(),'tau_ff_nm':[0.]*7})
    thread=Mock();monkeypatch.setattr(execution,'ReachThread',thread)
    body={'waypoints':[dict(zip(scene.s.joint_names,q)) for q in path],
          'orientation_plan_id':result['orientation']['id'],'motion_backend':'legacy_timed'}
    if submitted!='missing': body['arrival_tolerance']=limits if submitted=='match' else dict(limits,position_mm=50.)
    response=execution.reach_execute(body)
    if submitted=='match':
        assert response['ok'];thread.return_value.start.assert_called_once()
    else:
        assert response.status_code==409 and '容差与规划不一致' in json.loads(response.body)['error']
        thread.assert_not_called()


def test_measured_arrival_uses_custom_limits_and_requires_both():
    from scipy.spatial.transform import Rotation
    actual=np.eye(4);actual[:3,3]=[.012,0,0]
    actual[:3,:3]=Rotation.from_euler('z',4,degrees=True).as_matrix()
    constraint={'arrival_tolerance':{'position_mm':15.,'orientation_deg':5.}}
    assert o.check_arrival_pose(actual,[0,0,0],np.eye(3),constraint)['position_error_mm']==pytest.approx(12)
    for limits in ({'position_mm':10.,'orientation_deg':5.},{'position_mm':15.,'orientation_deg':2.}):
        with pytest.raises(ValueError,match='终点位姿未达到'):
            o.check_arrival_pose(actual,[0,0,0],np.eye(3),{'arrival_tolerance':limits})
