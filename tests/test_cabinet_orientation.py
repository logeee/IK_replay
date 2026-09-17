"""Target XYZ + recorded cabinet orientation; intermediate orientation is free.

All execution uses simulated controllers. Nothing imports or publishes DDS.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from adapters.reach import orientation as o
from adapters.reach.state import ReachState, use_state
from control.reference_builder import build_reference_plan, build_recovery_plan
from core.robot_config import load_app_config
from core.robot_model import RobotModel
from core.types import Pose
from ik.numerical_solver import NumericalIKSolver

REFERENCE = 'L-柜面末端朝向_20260917_024547'


@pytest.fixture
def context():
    reference = json.loads((o.REFERENCES / REFERENCE / 'reference.json').read_text())
    config = load_app_config()
    s = ReachState()
    s.robot_model = RobotModel(config.robots['h2'])
    s.ik_solver = NumericalIKSolver(s.robot_model, config.ik)
    s.chain_id = 'left_arm'
    s.joint_names = s.robot_model.joint_names(s.chain_id)
    s.p_tool = reference['p_tool_wrist_m']
    s.T_wrist2hand = reference['T_wrist_from_hand']
    s.active_combo = {'hand_id':reference['hand_id']}
    r = np.asarray(reference['T_root_from_cabinet'])[:3,:3]
    s.plane = {'horizontal_axis_source':'wall_coordinate_x',
               'right_root':r[:,0].tolist(), 'wall_up_root':r[:,2].tolist()}
    s.pick_revision = 5
    s.pick_context = {'selection_mode':'frozen_rgbd_pointcloud'}
    # Goal-only planning needs no IMU snapshot or world anchor for legacy.
    s.pick_torso = None
    s.pink_runtime = SimpleNamespace(world_frame=SimpleNamespace(anchored=True,anchor_count=3),
                                    pick_world_frame_anchor=3,pick_world_T_root=np.eye(4))
    q = dict(reference['named_arm_joints_rad'])
    with use_state(s):
        p = o.joint_pose(q)
        s.pick_target_root = (p[:3,3] + [0.,0.,.01]).tolist()
        q['left_wrist_roll_joint'] += .5  # Start is 28.65 degrees away from the target orientation.
        yield s, reference, q, p


def make_plan(context, **over):
    s, _, q, _ = context
    body = {'reference_id':REFERENCE,'pick_revision':s.pick_revision,
            'target_root':s.pick_target_root,'start_joints':q,'kind':'direct','check_collision':False}
    body.update(over)
    result = o.plan_orientation(body)
    assert isinstance(result,dict), getattr(result,'body',result)
    return result, np.array([[w['named_joints'][n] for n in s.joint_names] for w in result['waypoints']])


def test_saved_reference_matches_locked_arm_and_is_side_scoped(context):
    s, r, _, p = context
    assert o.angle_deg(o.cabinet_rotation() @ r['tcp']['R_cabinet_from_frame'],p[:3,:3]) < 1e-6
    assert o.references()['references'][0]['id'] == REFERENCE
    s.chain_id = 'right_arm'
    assert o.references()['references'] == []
    with pytest.raises(ValueError,match='不一致'): o.load_reference(REFERENCE)


def test_mount_and_record_paths_validated(context):
    s, *_ = context
    for value in ('../outside','..','/etc/passwd','absent'):
        with pytest.raises(ValueError): o.load_reference(value)
    s.T_wrist2hand = np.eye(4)
    with pytest.raises(ValueError,match='安装'): o.load_reference(REFERENCE)


@pytest.mark.parametrize('kind',['direct','axis_last'])
def test_different_start_orientation_is_preserved_and_only_goal_is_constrained(context,kind):
    result, path = make_plan(context,kind=kind,lift_m=.005)
    s, r, q, _ = context
    desired = o.cabinet_rotation() @ r['tcp']['R_cabinet_from_frame']
    np.testing.assert_array_equal(path[0],[q[n] for n in s.joint_names])
    errors = [o.angle_deg(o.joint_pose(v)[:3,:3],desired) for v in path]
    assert errors[0] > 25.
    assert errors[len(errors)//2] > 2.  # Middle may differ from goal: do not lock the whole path.
    assert errors[-1] < .35
    assert np.max(np.abs(np.diff(path,axis=0))) < .05  # no initial snap or last-step rotation jump
    assert result['goal_position_error_mm'] < 2.
    assert result['orientation']['scope'] == 'goal'
    assert o.execution_constraint(result['orientation']['id'],path,'legacy_timed')
    assert o.execution_constraint(result['orientation']['id'],path,'pink')


def test_only_endpoint_ik_constrains_orientation(context):
    s, *_ = context
    with patch.object(s.ik_solver,'solve',wraps=s.ik_solver.solve) as solve:
        r_cab = o.cabinet_rotation()
        o.plan_path(context[2],np.array(s.pick_target_root),r_cab @ context[1]['tcp']['R_cabinet_from_frame'],
                    r_cab,'axis_last',.005,False)
    requests = [c.args[0] for c in solve.call_args_list]
    assert len(requests) > 5
    assert requests[0].solver_options['solve_orientation'] is True
    assert all(not r.solver_options['solve_orientation'] for r in requests[1:])


@pytest.mark.parametrize('kind',['direct','axis_last'])
def test_same_xyz_with_different_goal_orientation_still_plans(context,kind):
    s, _, q, _ = context
    s.pick_target_root = o.joint_pose(q)[:3,3].tolist()
    result, path = make_plan(context,kind=kind,lift_m=0.)
    assert len(path) > 2
    assert o.angle_deg(o.joint_pose(path[0])[:3,:3],o.joint_pose(path[-1])[:3,:3]) > 20
    np.testing.assert_allclose(o.joint_pose(path[-1])[:3,3],s.pick_target_root,atol=.002)


def test_reprojects_reference_into_new_cabinet_frame(context):
    s, r, _, _ = context
    cabinet = Rotation.from_euler('z',3,degrees=True).as_matrix() @ o.cabinet_rotation()
    s.plane['right_root'],s.plane['wall_up_root'] = cabinet[:,0].tolist(),cabinet[:,2].tolist()
    result, path = make_plan(context)
    desired = cabinet @ r['tcp']['R_cabinet_from_frame']
    assert o.angle_deg(o.joint_pose(path[-1])[:3,:3],desired) < .35


def test_via_joint_pose_can_have_any_orientation(context):
    s, _, q, _ = context
    via = dict(q);via['left_wrist_roll_joint'] -= .1
    _, path = make_plan(context,via_joints=[via])
    assert any(np.allclose(v,[via[n] for n in s.joint_names]) for v in path)


@pytest.mark.parametrize('change',['revision','anchor','tool','path','backend','missing','reference','scope'])
def test_execution_rejects_stale_or_changed_goal_plan(context,change):
    s, *_ = context
    result, path = make_plan(context)
    plan_id, backend = result['orientation']['id'], 'pink'
    if change == 'revision': s.pick_revision += 1
    elif change == 'anchor': s.pink_runtime.world_frame.anchor_count += 1
    elif change == 'tool': s.p_tool = [0.,0.,0.]
    elif change == 'path': path[-1,0] += .001
    elif change == 'backend': backend = 'unknown'
    elif change == 'missing': plan_id = None
    elif change == 'reference': s.orientation_plan['reference_key'] = 'old'
    elif change == 'scope': s.orientation_plan['scope'] = 'old_full_path'
    with pytest.raises(ValueError): o.execution_constraint(plan_id,path,backend)


@pytest.mark.parametrize('change',['source','target','axes','nan','unreachable'])
def test_invalid_goal_inputs_fail_without_execution(context,change):
    s, _, q, _ = context
    body = {'reference_id':REFERENCE,'pick_revision':5,'target_root':s.pick_target_root,
            'start_joints':dict(q),'kind':'direct'}
    if change == 'source': s.pick_context = {'selection_mode':'live_rgb_depth'}
    elif change == 'target': body['target_root'] = [0.,0.,0.]
    elif change == 'axes': s.plane = {}
    elif change == 'nan': body['start_joints'][s.joint_names[0]] = float('nan')
    elif change == 'unreachable': body['target_root'] = s.pick_target_root = [5.,5.,5.]
    result = o.plan_orientation(body)
    assert result.status_code == 422
    assert s.orientation_plan is None


@pytest.mark.parametrize('runtime',['missing','unanchored','oldpick'])
def test_original_backends_need_neither_pink_anchor_nor_torso_snapshot(context,runtime):
    s, *_ = context
    if runtime == 'missing': s.pink_runtime = None
    elif runtime == 'unanchored': s.pink_runtime.world_frame.anchored = False
    else: s.pink_runtime.pick_world_frame_anchor -= 1
    result, path = make_plan(context)
    for backend in ('legacy','legacy_timed'):
        assert o.execution_constraint(result['orientation']['id'],path,backend)
    with pytest.raises(ValueError,match='PINK'):
        o.execution_constraint(result['orientation']['id'],path,'pink')


def test_arrival_validation_rejects_wrong_final_position_or_orientation(context):
    _, _, q, _ = context
    result, path = make_plan(context)
    desired = o.joint_pose(path[-1])
    with pytest.raises(ValueError,match='终点位姿未达到'):
        o.check_goal_pose(o.joint_pose(q),desired[:3,3],desired[:3,:3])
    actual = desired.copy();actual[0,3] += .02
    with pytest.raises(ValueError,match='终点位姿未达到'):
        o.check_goal_pose(actual,desired[:3,3],desired[:3,:3])


def test_pink_reference_and_recovery_allow_changing_orientation():
    def fk(q):
        t = np.eye(4);t[:3,:3] = Rotation.from_euler('z',q[0]).as_matrix()
        t[:3,3] = [q[0],0.,0.]
        return t
    q0,q1 = np.zeros(7),np.ones(7)*.5
    for plan in (build_reference_plan([q0,q1],1.,fk=fk,world_T_root_ref=np.eye(4),max_qdot_rad_s=.4),
                 build_recovery_plan(q0,q1,fk=fk,world_T_root_ref=np.eye(4),max_qdot_rad_s=.4)):
        transforms = plan.reference.world_T_tcp
        assert o.angle_deg(transforms[0,:3,:3],transforms[-1,:3,:3]) > 20
        np.testing.assert_allclose(transforms[0],fk(q0))
        np.testing.assert_allclose(transforms[-1],fk(q1))


@pytest.mark.parametrize('backend',['legacy','legacy_timed','pink'])
def test_execute_all_backends_from_different_start_orientation(context,backend):
    from adapters.reach import execution
    from adapters.reach.execution_pink import PinkRuntime
    from adapters.reach.lowstate import MockLowStateSampler
    from tests.test_reach_pink_backend import FakeArmController
    s, _, q, _ = context
    q0 = np.array([q[n] for n in s.joint_names])
    ctl = FakeArmController(q0)
    s.controller,s.exec_running,s.settle_trim = ctl,True,'off'
    if backend == 'pink':
        sampler = MockLowStateSampler(arm_motor_indices=(15,16,17,18,19,20,21),arm_q_reader=ctl.read_measured)
        rt = PinkRuntime(arm_side='left',sampler=sampler,wrist_link='left_wrist_yaw_link')
        s.pink_runtime = rt
        rt.anchor();rt.capture_pick_frame()
    else: s.pink_runtime = None
    result,path = make_plan(context)
    proof = o.execution_constraint(result['orientation']['id'],path,backend)
    try:
        execution._exec_loop(list(path),1.5,speed=.4,label='主轨迹',command_start_q=q0,
                            motion_backend=backend,execution_context={'orientation_constraint':proof})
        assert s.exec_message.startswith('完成'),s.exec_message
        assert not s.exec_running
        assert len(ctl.targets) > 10
        np.testing.assert_allclose(ctl.targets[0],q0,atol=.015)
        rotations = [o.angle_deg(o.joint_pose(v)[:3,:3],proof['R_root_tcp']) for v in ctl.targets]
        assert max(rotations) > 20. # no en-route 2-degree guard
        assert rotations[-1] < 2.
        if backend != 'pink':
            o.check_goal_pose(o.joint_pose(ctl.read_measured()),proof['target_root'],proof['R_root_tcp'],10.)
        else:
            _,fb = rt.update_world()
            actual = fb.world_T_root @ rt.controller_for_tool(s.p_tool).root_T_tcp_actual(ctl.read_measured()).homogeneous
            goal = np.asarray(proof['world_T_root_ref']) @ rt.controller_for_tool(s.p_tool).root_T_tcp_actual(path[-1]).homogeneous
            o.check_goal_pose(actual,goal[:3,3],proof['R_world_tcp'],10.)
    finally: ctl.shutdown()


def test_axis_last_preserves_xyz_order_while_orientation_changes(context):
    s, _, q, _ = context
    result,path = make_plan(context,kind='axis_last',lift_m=.005)
    p0 = o.joint_pose(q)[:3,3]
    target = np.array(s.pick_target_root)
    mid = (np.array([p0[0],target[1],target[2]+.005]) if target[0] >= p0[0]
           else np.array([target[0],p0[1],p0[2]]))
    def distance(p,a,b):
        u = np.clip(np.dot(p-a,b-a)/max(np.dot(b-a,b-a),1e-12),0,1)
        return np.linalg.norm(p-(a+u*(b-a)))
    for v in path:
        p = o.joint_pose(v)[:3,3]
        assert min(distance(p,p0,mid),distance(p,mid,target)) < .003


def test_collision_reroute_does_not_skip_selected_via_point(context):
    from adapters.reach import planning
    s, r, q, _ = context
    via = dict(q);via['left_wrist_roll_joint'] -= .1
    rc = o.cabinet_rotation()
    with (patch.object(planning,'_attach_collision',side_effect=[{'status':'collision'},{'status':'collision'},{'status':'safe'},{'status':'safe'}]),
          patch.object(planning,'_axis_last_rrt_fallback',side_effect=lambda start,wps,c:(wps,{'status':'safe'},'axis_last+rrt')) as fallback):
        result = o.plan_path(q,np.array(s.pick_target_root),rc @ r['tcp']['R_cabinet_from_frame'],rc,'direct',0.,True,[via])
    assert result['planner'].endswith('+rrt')
    assert any(w['named_joints'] == via for w in result['waypoints'])
    assert fallback.call_args.args[1][-1]['named_joints'] == via


def test_execution_reports_wrong_actual_orientation_only_at_arrival(context):
    from adapters.reach import execution
    from tests.test_reach_pink_backend import FakeArmController
    s, _, q, _ = context
    q0 = np.array([q[n] for n in s.joint_names])
    ctl = FakeArmController(q0)
    # Simulate an actuator whose sent commands move but actual feedback stays put.
    ctl.read_measured = lambda: q0.copy()
    s.controller,s.pink_runtime,s.exec_running,s.settle_trim = ctl,None,True,'off'
    result,path = make_plan(context)
    proof = o.execution_constraint(result['orientation']['id'],path,'legacy_timed')
    try:
        execution._exec_loop(list(path),1.5,speed=.4,label='主轨迹',command_start_q=q0,
                            motion_backend='legacy_timed',execution_context={'orientation_constraint':proof})
        assert '终点位姿未达到' in s.exec_message,s.exec_message
        # No stop for the large start/intermediate angular difference.
        np.testing.assert_allclose(ctl.targets[-1],path[-1],atol=1e-9)
        assert len(ctl.targets) > len(path)
        assert not ctl.status()['jog_enabled']
    finally: ctl.shutdown()
