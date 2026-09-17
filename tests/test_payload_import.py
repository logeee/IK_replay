"""Payload library imports preserve data and never activate a compensation."""
import json
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from core import capability_registry as capability_registry
from core import gravity_profiles as profiles
from core.payload_import import MAX_JSON_BYTES, import_payload_profile
from tools import capability_server


@pytest.fixture
def payload():
    return {'arm':'right', 'mass_kg':1.477, 'com_m':[.2828,-.0006,.032],
            'alpha':1., 'with_hand':False, 'source_session':'20260915_145406_right'}


def body(payload, **kwargs):
    return {'arm':'right_arm', 'hand_id':'screwdriver', 'filename':'劳洛斯_2机械螺丝刀带相机.json',
            'content':payload, **kwargs}


def test_import_path_defaults_to_filename_and_preserves_current_version(tmp_path, payload):
    source = tmp_path / '劳洛斯_2机械螺丝刀带相机.json'
    source.write_text(json.dumps(payload))
    registry = tmp_path / 'gravity.json'
    item, created = import_payload_profile({'arm':'right_arm','hand_id':'screwdriver','source_path':str(source)},registry)
    assert created and item['label'] == source.name and item['version'] == '0.0.1'
    assert item['source'] == str(source) + '#20260915_145406_right'
    assert item['compatibility'] == {'arm':'right_arm','hand_id':'screwdriver'}
    assert item['parameters']['excluded_subtree_link'] == 'right_hand_link'
    assert item['parameters']['payload_kg'] == 1.477
    assert profiles.load_registry(registry)['active_version'] == '0.0.0'
    source.unlink()
    assert profiles.active_profile(profiles.load_registry(registry), item['version']) == item


def test_upload_custom_name_idempotency_and_changed_data_get_new_version(tmp_path, payload):
    registry = tmp_path / 'gravity.json'
    request = body(payload, label='螺丝刀带相机')
    first, created = import_payload_profile(request, registry)
    again, created_again = import_payload_profile(request, registry)
    assert created and not created_again and again == first
    changed = deepcopy(request)
    changed['content']['mass_kg'] = 1.5
    second, _ = import_payload_profile(changed, registry)
    assert second['version'] != first['version']
    assert second['label'] == '螺丝刀带相机'
    assert profiles.active_profile(profiles.load_registry(registry), first['version']) == first


@pytest.mark.parametrize('change', [
    {'arm':'left'}, {'with_hand':'false'}, {'mass_kg':-1}, {'com_m':[1,2]},
    {'alpha':0}, {'alpha':float('nan')}, {'mass_kg':float('inf')},
])
def test_invalid_import_does_not_write_library(tmp_path, payload, change):
    registry = tmp_path / 'gravity.json'
    with pytest.raises(ValueError):
        import_payload_profile(body({**payload, **change}), registry)
    assert not registry.exists()


def test_rejects_invalid_sources_and_ambiguous_input(tmp_path, payload):
    registry = tmp_path / 'gravity.json'
    source = tmp_path / 'bad.json'
    source.write_text('not JSON')
    for request in [body(payload, filename='secret.txt'), body(payload, source_path=str(source)),
                    {'arm':'right_arm','hand_id':'screwdriver','source_path':str(source)},
                    {'arm':'right_arm','hand_id':'screwdriver','source_path':'relative.json'}]:
        with pytest.raises(ValueError):
            import_payload_profile(request,registry)
    source.write_text(' ' * (MAX_JSON_BYTES + 1))
    with pytest.raises(ValueError,match='1 MiB'):
        import_payload_profile({'arm':'right_arm','hand_id':'screwdriver','source_path':str(source)},registry)
    assert not registry.exists()


def test_parallel_imports_keep_both_profiles(tmp_path, payload):
    registry = tmp_path / 'gravity.json'
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(import_payload_profile,body(payload,label=name),registry) for name in ('一号.json','二号.json')]
        items = [f.result()[0] for f in futures]
    assert len({p['version'] for p in items}) == 2
    assert len(profiles.load_registry(registry)['versions']) == 3


def test_import_endpoint_scopes_library_without_changing_arm_selection(tmp_path, payload):
    registry = tmp_path / 'gravity.json'
    registry_path = tmp_path / 'capability.json'
    arms_path = tmp_path / 'arms.json'
    seed = capability_registry.seed_registry()
    seed['hands'].append({
        'id':'screwdriver', 'name':'螺丝刀', 'design_side':'right',
        'tool_out_mm':0, 'notes':'',
    })
    capability_registry.save_registry(seed, registry_path)
    client = TestClient(capability_server.app)
    with patch.object(capability_server, 'REGISTRY_PATH', registry_path), \
         patch.object(capability_server, 'GRAVITY_PROFILES_PATH', registry), \
         patch.object(capability_server, 'ARMS_CONFIG_PATH', arms_path):
        response = client.post('/api/capability/gravity/import', json=body(payload))
        assert response.status_code == 200, response.text
        imported = response.json()['arm_workspace']['imported_profile']
        assert imported['label'] == body(payload)['filename']
        invalid = client.post(
            '/api/capability/gravity/import', json=body(payload, hand_id='unknown'))
        assert invalid.status_code == 400
    assert not arms_path.exists()
