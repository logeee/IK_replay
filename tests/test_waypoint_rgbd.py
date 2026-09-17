"""Same-frame waypoint recording, using real FK and fake camera/HTTP only."""
import io
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from adapters.reach import recordings as rec, pointcloud_source as source, waypoint_rgbd as rgbd
from adapters.reach.state import ReachState, use_state
from api import pointcloud_viewer as pc
from api.pointcloud_client import PointcloudClient
from core.robot_config import load_app_config
from core.robot_model import RobotModel


@pytest.fixture(params=['left_arm', 'right_arm'])
def context(request, tmp_path, monkeypatch):
    s = ReachState()
    s.chain_id = request.param
    s.robot_model = RobotModel(load_app_config().robots['h2'])
    s.joint_names = s.robot_model.joint_names(s.chain_id)
    s.wrist_link = s.robot_model.end_link(s.chain_id)
    s.p_tool = [.18, -.05, .02]
    s.T_cam2torso = np.eye(4)
    s.T_cam2root = s.robot_model.forward_kinematics({})[s.base_link]
    s.T_wrist2hand = np.eye(4).tolist()
    s.handeye_ready = True
    s.active_combo = {'arm': s.chain_id, 'hand_id': 'test-hand', 'mount_profile_id': 'test'}
    s.waypoints_dir = tmp_path / 'waypoints'
    s.provider_reader = lambda: [.1] * 7
    s.torso_reader = lambda: {'waist_names': ['waist_yaw', 'waist_roll', 'waist_pitch'],
                              'waist_rad': [.1, .02, -.03], 'imu_quat': [1., 0., 0., 0.]}
    _, jpeg = cv2.imencode('.jpg', np.zeros((8, 10, 3), dtype=np.uint8))
    depth = np.full((8, 10), 1000.25, dtype=np.float32)
    depth[0, 0], depth[0, 1] = 0, 5000  # raw values outside viewer range survive
    frame = {'jpeg': jpeg.tobytes(), 'depth_mm': depth, 'intrinsics': (100., 100., 5., 4.),
             'distortion': np.zeros(5), 'metadata': {'frame_id': 'record-frame'}}
    reads = []
    def read(*, fresh=False):
        reads.append(fresh)
        return deepcopy(frame)
    s.camera = SimpleNamespace(rgbd_snapshot=read)
    calls = []
    def fetch(url, **kwargs):
        calls.append(url)
        assert f'/api/arms/{s.chain_id}/reach/rgbd_snapshot?include_robot_pose=true' in url
        response = source.reach_rgbd_snapshot(include_robot_pose=True)
        if response.status_code != 200:
            raise RuntimeError(json.loads(response.body)['error'])
        return SimpleNamespace(content=response.body, status_code=200, raise_for_status=lambda: None)
    monkeypatch.setattr(pc._http, 'get', fetch)
    monkeypatch.setattr(pc, '_model', None)
    monkeypatch.setattr(pc, '_latest', None)
    monkeypatch.setattr(pc, '_latest_by_arm', {})
    def transport(_client, arm):
        response = pc.recording_capture({'arm': arm})
        if response.status_code != 200:
            raise RuntimeError(json.loads(response.body)['error'])
        return response.body
    monkeypatch.setattr(PointcloudClient, 'recording_capture', transport)
    with use_state(s):
        yield SimpleNamespace(s=s, frame=frame, reads=reads, calls=calls)


def test_full_record_preserves_frame_depth_pose_and_7005_defaults(context):
    c = context
    latest = object()
    pc._latest_by_arm[c.s.chain_id] = latest
    result = rec.reach_record_rgbd_waypoint({'name': 'A'})
    assert result['ok']
    wp = result['waypoint']
    assert c.reads == [True]
    assert pc._latest_by_arm[c.s.chain_id] is latest
    path = c.s.waypoints_dir / wp['file']
    saved = json.loads(path.read_text())
    assert saved['named_joints'] == dict.fromkeys(c.s.joint_names, .1)
    assert saved['arm'] == c.s.chain_id
    directory = c.s.waypoints_dir / saved['rgbd']['directory']
    assert directory == path.with_suffix('') / 'rgbd'
    assert (directory / 'rgb.jpg').read_bytes() == c.frame['jpeg']
    np.testing.assert_array_equal(np.load(directory / 'depth_mm.npy'), c.frame['depth_mm'])
    cap = json.loads((directory / 'capture.json').read_text())
    assert [cap[k] for k in ['stride', 'z_min_m', 'z_max_m', 'conf']] == [4, .15, 3., .25]
    pose = json.loads((directory / 'robot_pose.json').read_text())
    assert pose['synchronization']['hardware_synchronized'] is False
    assert pose['before']['sample_finished_unix_ns'] < pose['sample_started_unix_ns']
    transforms = c.s.robot_model.forward_kinematics(saved['named_joints'])
    expected = np.linalg.inv(transforms[c.s.base_link]) @ transforms[c.s.wrist_link]
    expected[:3, 3] += expected[:3, :3] @ c.s.p_tool
    np.testing.assert_allclose(pose['T_base_from_tcp'], expected, atol=1e-12)
    assert pose['tcp']['p_tool_wrist_m'] == c.s.p_tool
    manifest = json.loads((directory / 'manifest.json').read_text())
    assert manifest['waypoint_file'] == path.name
    assert manifest['depth_unit'] == 'mm'
    assert manifest['frame_id'] == 'record-frame'
    assert rec.reach_waypoints()['waypoints'][0]['rgbd'] == wp['rgbd']


def test_normal_record_does_not_touch_camera_and_same_second_does_not_overwrite(context, monkeypatch):
    monkeypatch.setattr(rec.time, 'strftime', lambda *args: '20260917_040000')
    first = rec.reach_record_waypoint({'name': 'A'})['waypoint']
    assert rec.reach_record_waypoint({'name': 'A', 'capture_rgbd': False}).status_code == 409
    monkeypatch.setattr(rec.time, 'strftime', lambda *args: '20260917_040001')
    second = rec.reach_record_waypoint({'name': 'A'})['waypoint']
    assert first['file'] != second['file']
    assert 'rgbd' not in first
    assert not context.reads and not context.calls
    assert len(list(context.s.waypoints_dir.glob('*.json'))) == 2


@pytest.mark.parametrize('failure', ['camera', 'motion', 'running', 'depth', 'tcp_change', 'unavailable'])
def test_capture_failure_leaves_no_waypoint_or_partial_archive(context, monkeypatch, failure):
    c = context
    if failure == 'camera': c.s.camera.rgbd_snapshot = lambda **_: None
    elif failure == 'motion':
        samples = iter([[0.] * 7, [.2] * 7])
        c.s.provider_reader = lambda: next(samples)
    elif failure == 'running': c.s.exec_running = True
    elif failure == 'depth': c.frame['depth_mm'][:] = 0
    elif failure == 'tcp_change':
        original = c.s.camera.rgbd_snapshot
        def read(**kwargs):
            c.s.p_tool = [0., 0., 0.]
            return original(**kwargs)
        c.s.camera.rgbd_snapshot = read
    elif failure == 'unavailable':
        def fail(*args): raise RuntimeError('7005 unavailable')
        monkeypatch.setattr(PointcloudClient, 'recording_capture', fail)
    result = rec.reach_record_rgbd_waypoint({'name': 'B'})
    assert result.status_code == 503
    assert '未保存路点' in json.loads(result.body)['error']
    assert not c.s.waypoints_dir.exists()


def test_write_failure_cleans_staging_and_does_not_advertise_success(context, monkeypatch):
    def fail(*args, **kwargs): raise OSError('disk full')
    monkeypatch.setattr(rgbd, 'write_bundle', fail)
    response = rec.reach_record_rgbd_waypoint({'name': 'A'})
    assert response.status_code == 500
    assert not list(context.s.waypoints_dir.iterdir())


def test_failed_final_commit_rolls_back_archive(context, monkeypatch):
    def fail(*args): raise OSError('commit failed')
    monkeypatch.setattr(rec.os, 'link', fail)
    assert rec.reach_record_rgbd_waypoint({'name': 'A'}).status_code == 500
    assert not list(context.s.waypoints_dir.iterdir())


def test_cross_arm_archive_rejected(context, monkeypatch):
    bundle = pc.recording_capture({'arm': context.s.chain_id}).body
    with np.load(io.BytesIO(bundle), allow_pickle=False) as archive:
        values = {k: archive[k] for k in archive.files}
    meta = json.loads(values['capture_json'].tobytes())
    meta['arm'] = 'right_arm' if context.s.chain_id == 'left_arm' else 'left_arm'
    values['capture_json'] = np.frombuffer(json.dumps(meta).encode(), dtype=np.uint8)
    data = io.BytesIO()
    np.savez_compressed(data, **values)
    monkeypatch.setattr(PointcloudClient, 'recording_capture', lambda *args: data.getvalue())
    response = rec.reach_record_rgbd_waypoint({'name': 'A'})
    assert response.status_code == 503
    assert not context.s.waypoints_dir.exists()


def test_invalid_names_and_flag_do_not_capture(context):
    for body in [{'name': '../A'}, {'name': 'A', 'capture_rgbd': 'false'}]:
        assert rec.reach_record_waypoint(body).status_code == 400
    assert not context.calls


def test_plain_rgbd_snapshot_still_works_without_robot_state(context):
    context.s.provider_reader = None
    result = source.reach_rgbd_snapshot()
    assert result.status_code == 200
    assert context.reads == [False]


def test_rgbd_snapshot_binds_pink_world_pose_to_capture_id(context):
    calls = []
    runtime = SimpleNamespace(pick_world_frame_anchor=12)
    runtime.capture_pick_frame = lambda **kwargs: (
        calls.append(kwargs) or np.eye(4)
    )
    context.s.pink_runtime = runtime

    response = source.reach_rgbd_snapshot(pick_capture_id="capture_12345678")

    assert response.status_code == 200
    with np.load(io.BytesIO(response.body), allow_pickle=False) as archive:
        metadata = json.loads(archive["metadata_json"].tobytes())
    assert calls == [{
        "capture_id": "capture_12345678",
        "source_frame_id": "record-frame",
    }]
    assert metadata["pink_pick_world_frame"] == {
        "capture_id": "capture_12345678",
        "source_frame_id": "record-frame",
        "bound": True,
        "anchor_count": 12,
    }


def test_7005_rejects_unscoped_recording():
    assert pc.recording_capture({}).status_code == 400
