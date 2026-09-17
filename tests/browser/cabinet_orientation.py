"""18001 orientation selection and execution contract; all hardware writes mocked."""
import asyncio
import copy
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

BASE = os.environ.get('REACH_TEST_URL','http://127.0.0.1:18001')
CHROME = '/home/robot/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome'
ROOT = Path(__file__).resolve().parents[2]
REFERENCE = 'L-柜面末端朝向_20260917_024547'
expect.set_options(timeout=30000)


async def main():
    record = json.loads((ROOT/'data/orientation_references'/REFERENCE/'reference.json').read_text())
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME,headless=True,
            args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        try:
            page = await browser.new_page(viewport={'width':1800,'height':1300})
            baseline = await (await page.request.get(BASE+'/api/dual/status')).json()
            for entry in baseline['arms'].values():
                entry['status'].update(armed=True,hand_move=False,pink_available=True)
            errors, writes, blocked, plans = [], [], [], []
            entries = [{'file':'A.json','name':'L-HHY-L-D-V1','compatible':True},
                       {'file':'B.json','name':'L-HHY-R-D-V1','compatible':True},
                       {'file':'A2.json','name':'新的 A 点','compatible':True},
                       {'file':'bad.json','name':'旧 TCP 点','compatible':False,'error':'TCP 不匹配'}]
            old_point_server = False
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('dialog',lambda d:d.accept())
            pick = {'ok':True,'available':False,'revision':0}
            first_pick_poll = asyncio.Event()
            q = record['named_arm_joints_rad']
            points = [{'named_joints':q,'tcp_pose':{'xyz':[.45,.01,.6],'rpy':[0.,0.,0.]}},
                      {'named_joints':{k:v+.001 for k,v in q.items()},'tcp_pose':{'xyz':[.45,.01,.61],'rpy':[0.,0.,0.]}}]
            async def intercept(route):
                req = route.request
                path = urlsplit(req.url).path
                arm = 'right_arm' if '/right_arm/' in path else 'left_arm'
                if path == '/api/dual/status': await route.fulfill(json=baseline)
                elif path.endswith('/reach/status'): await route.fulfill(json=baseline['arms'][arm]['status'])
                elif path.endswith('/reach/orientation/references'):
                    refs = [{'id':REFERENCE,'name':record['name']}] if arm == 'left_arm' else []
                    await route.fulfill(json={'ok':True,'references':refs,'tolerance_deg':2})
                elif path.endswith('/reach/cabinet_waypoints'):
                    await route.fulfill(json={'ok':True,'waypoints':entries if arm == 'left_arm' else []})
                elif path.endswith('/reach/joints'): await route.fulfill(json={'ok':True,'named_joints':q})
                elif path.endswith('/reach/pink/body'): await route.fulfill(json={'available':False})
                elif path.endswith('/reach/latest_pick'):
                    await route.fulfill(json=copy.deepcopy(pick))
                    first_pick_poll.set()
                elif path.endswith('/reach/plan_orientation'):
                    plans.append(req.post_data_json)
                    await route.fulfill(json={'ok':True,'waypoints':points,'planner':'goal_pose/axis_last',
                        'goal_position_error_mm':.01,'goal_orientation_error_deg':.001,'collision':None,
                        'target_pose':{'xyz':[.45,.01,.61],'rpy':[.2,.3,.4]},
                        'orientation':{'id':'mock-proof','reference_id':req.post_data_json['reference_id'],
                            **({} if old_point_server else {'reference_kind':req.post_data_json.get('reference_kind','recorded_orientation')}),
                            'name':next((e['name'] for e in entries if e['file']==req.post_data_json['reference_id']),record['name']),
                            'tolerance_deg':2,'scope':'goal'}})
                elif path.endswith('/reach/execute'):
                    writes.append(req.post_data_json)
                    await route.fulfill(json={'ok':True})
                elif path.endswith('/reach/exec_status'):
                    await route.fulfill(json={'running':False,'message':'完成（目标位姿到达）','progress':1})
                elif req.method != 'GET' and '/api/' in path:
                    if path.endswith(('/fk','/ik/solve','/collision/check','/trajectory/plan','/demo/plan')):
                        await route.continue_()
                    else:
                        blocked.append(path)
                        await route.fulfill(status=409,json={'error':'Hardware writes blocked'})
                else: await route.continue_()
            await page.route('**/api/**',intercept)
            await page.goto(BASE+'/?arm=left_arm',wait_until='domcontentloaded')
            mode = page.locator('#reachOrientationMode')
            select = page.locator('#reachOrientationReference')
            backend = page.locator('#reachExecBackend')
            await expect(select.locator('option')).to_have_text([record['name']])
            await expect(mode).to_have_value('free')
            original_backend = await backend.input_value()
            await mode.select_option('cabinet')
            await expect(select).to_be_visible()
            await expect(backend).to_have_value(original_backend)
            await expect(backend).to_be_enabled()
            await backend.select_option('pink')
            await expect(page.locator('#reachOrientationHelp')).to_contain_text('先锚定')
            await backend.select_option('legacy_timed')
            await expect(page.locator('#reachOrientationHelp')).to_contain_text('原方案无需锚定')
            await expect(page.locator('#reachExecBtn')).to_be_disabled()
            await asyncio.wait_for(first_pick_poll.wait(),timeout=30)
            pick.update(available=True,revision=1,p_root=[.45,.01,.61],p_torso=[.45,.01,.1],
                        selection_mode='frozen_rgbd_pointcloud',selection_source='manual')
            await expect(page.locator('#reachInfo')).to_contain_text('目标点使用记录朝向')
            await expect(page.locator('#reachExecBtn')).to_be_enabled()
            await expect(backend).to_be_enabled()
            # Changing backend remains possible with a valid orientation plan.
            await backend.select_option('pink')
            await backend.select_option('legacy')
            await backend.select_option('legacy_timed')
            assert plans[-1]['reference_id'] == REFERENCE and plans[-1]['pick_revision'] == 1
            assert plans[-1]['kind'] == 'axis_last'
            await expect(page.locator('#reachInfo')).to_contain_text('终点朝向误差')
            await expect(page.locator('#reachInfo')).to_contain_text('中途朝向自由')
            await page.locator('#reachStepMode').check()
            await page.locator('#reachExecBtn').click()
            await expect(mode).to_be_enabled()
            assert len(writes) == 1 and writes[0]['motion_backend'] == 'legacy_timed'
            assert writes[0]['orientation_plan_id'] == 'mock-proof'
            # Orientation selection must respect the original segmented workflow.
            await expect(page.locator('#reachStepNext')).to_be_visible()
            await page.locator('#reachNextDoneBtn').click()
            await page.locator('#reachStepMode').uncheck()
            await page.locator('#reachStepLen').fill('0')
            await page.locator('#reachExecBtn').click()
            await expect(mode).to_be_enabled()
            await expect(page.locator('#reachStepNext')).to_be_hidden()
            assert len(writes) == 2
            await mode.select_option('free')
            await expect(backend).to_have_value('legacy_timed')
            await expect(backend).to_be_enabled()
            await expect(page.locator('#reachExecBtn')).to_be_disabled()
            await mode.select_option('cabinet')
            await expect(page.locator('#reachExecBtn')).to_be_disabled()
            # A/B orientation uses current picked XYZ and carries no A/B offset or force.
            await mode.select_option('cabinet_a')
            await expect(select).to_have_value('A.json')
            await expect(page.locator('#reachOrientationReferenceLabel')).to_have_text('A 点来源')
            await expect(page.locator('#reachOrientationHelp')).to_contain_text('相对柜面的朝向')
            await expect(page.locator('#reachDebugModeBtn')).to_have_text('调试方式：经典…')
            await expect(select.locator('option[value="bad.json"]')).to_be_disabled()
            await page.locator('#reachReplanBtn').click()
            await expect(page.locator('#reachExecBtn')).to_be_enabled()
            assert plans[-1]['reference_id']=='A.json'
            assert plans[-1]['reference_kind']=='cabinet_waypoint_orientation'
            assert plans[-1]['target_root']==pick['p_root']
            assert 'cabinet_assist' not in plans[-1] and 'cabinet_x_offset_mm' not in plans[-1]
            await expect(page.locator('#reachInfo')).to_contain_text('A 点柜面朝向')
            await mode.select_option('cabinet_b')
            await expect(select).to_have_value('B.json')
            await expect(page.locator('#reachExecBtn')).to_be_disabled()
            await page.locator('#reachPlanLeftBtn').click()
            await expect(page.locator('#reachExecBtn')).to_be_enabled()
            assert plans[-1]['reference_id']=='B.json' and plans[-1]['kind']=='axis_last'
            await page.locator('#reachExecBtn').click()
            await expect(mode).to_be_enabled()
            assert len(writes)==3 and writes[-1]['orientation_plan_id']=='mock-proof'
            assert 'cabinet_assist' not in writes[-1]
            await mode.select_option('cabinet_a')
            await select.select_option('A2.json')
            await expect(page.locator('#reachExecBtn')).to_be_disabled()
            await page.locator('#reachDebugModeBtn').click()
            await page.locator('#reachDebugAB').check()
            await expect(page.locator('#reachDebugA')).to_have_value('A2.json')
            await expect(page.locator('#reachDebugB')).to_have_value('B.json')
            await page.locator('#reachDebugCancel').click()
            # A/B settings and the source selector share the same saved A/B mapping.
            await page.locator('#reachDebugModeBtn').click()
            await page.locator('#reachDebugAB').check()
            await page.locator('#reachDebugA').select_option('A.json')
            await page.locator('#reachDebugSave').click()
            await expect(select).to_have_value('A.json')
            old_point_server = True
            await page.locator('#reachReplanBtn').click()
            await expect(page.locator('#reachMsg')).to_contain_text('未确认 A/B 柜面朝向来源')
            await expect(page.locator('#reachExecBtn')).to_be_disabled()
            old_point_server = False
            await page.locator('#reachReplanBtn').click()
            await expect(page.locator('#reachExecBtn')).to_be_enabled()
            await page.screenshot(path='/tmp/ab-cabinet-orientation-ui.png')
            assert not errors, errors
            assert not blocked, blocked
            assert len(writes) == 3
            await page.screenshot(path='/tmp/cabinet-orientation-ui.png')
            # The other arm never offers a left-arm reference.
            await page.goto(BASE+'/?arm=right_arm',wait_until='domcontentloaded')
            await expect(select.locator('option')).to_have_text(['本侧暂无匹配的朝向记录'])
            await mode.select_option('cabinet_a')
            await expect(select).to_have_value('')
            assert await select.locator('option').count()==1
            await page.goto(BASE+'/?arm=left_arm',wait_until='domcontentloaded')
            await mode.select_option('cabinet_a'); await expect(select).to_have_value('A.json')
            await mode.select_option('cabinet_b'); await expect(select).to_have_value('B.json')
            assert not errors, errors
            print('A/B cabinet orientation, unchanged picked XYZ, shared/persisted A/B selection, old-server rejection and original orientation/segmented workflow passed. All hardware writes mocked.')
        finally: await browser.close()


if __name__ == '__main__': asyncio.run(main())
