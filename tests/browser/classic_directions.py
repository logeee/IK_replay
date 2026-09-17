"""Classic arrival popup and explicit left/right actions; hardware writes intercepted."""
import asyncio
import copy
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

BASE = 'http://127.0.0.1:18001'
CHROME = '/home/robot/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome'
expect.set_options(timeout=30000)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, headless=True,
            args=['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        try:
            page = await browser.new_page(viewport={'width':1800, 'height':1300})
            baseline = await (await page.request.get(BASE + '/api/dual/status')).json()
            plane = {'right_root':[0,1,0], 'left_root':[0,-1,0], 'wall_up_root':[0,0,1],
                     'normal_root':[-1,0,0], 'center_root':[.45,0,.6], 'horizontal_axis_source':'wall_coordinate_x'}
            for arm in baseline['arms'].values(): arm['status'].update(armed=True, hand_move=False, plane=plane, joints_available=True)
            q = dict(zip([f'left_{j}_joint' for j in ('shoulder_pitch','shoulder_roll','shoulder_yaw','elbow','wrist_roll','wrist_pitch','wrist_yaw')],
                         [-1.45,.74,-.95,.19,1.95,.26,.58]))
            points = [{'named_joints':q, 'tcp_pose':{'xyz':[.45,.01,.6], 'rpy':[0,0,0]}},
                      {'named_joints':{k:v+.001 for k,v in q.items()}, 'tcp_pose':{'xyz':[.45,.01,.61], 'rpy':[0,0,0]}}]
            entries = [{'file':'A.json','name':'L-HHY-L-D-V1','compatible':True},
                       {'file':'B.json','name':'L-HHY-R-D-V1','compatible':True}]
            pick = {'ok':True,'available':False,'revision':0}
            writes, plans, errors, blocked = [], [], [], []
            ready = asyncio.Event()
            page.on('dialog', lambda d: d.accept())
            page.on('pageerror', lambda e: errors.append(str(e)))
            async def intercept(route):
                req, path = route.request, urlsplit(route.request.url).path
                arm = 'right_arm' if '/right_arm/' in path else 'left_arm'
                if path == '/api/dual/status': await route.fulfill(json=baseline)
                elif path.endswith('/reach/status'): await route.fulfill(json=baseline['arms'][arm]['status'])
                elif path.endswith('/reach/joints'): await route.fulfill(json={'ok':True,'named_joints':q})
                elif path.endswith('/reach/pink/body'): await route.fulfill(json={'available':False})
                elif path.endswith('/reach/cabinet_waypoints'): await route.fulfill(json={'ok':True,'waypoints':entries})
                elif path.endswith('/reach/sidesteps'): await route.fulfill(json={'sidesteps':[]})
                elif path.endswith('/reach/latest_pick'):
                    await route.fulfill(json=copy.deepcopy(pick)); ready.set()
                elif path.endswith(('/reach/plan_axis_last','/reach/plan_cartesian')):
                    plans.append(req.post_data_json)
                    await route.fulfill(json={'ok':True,'waypoints':points,'planner':'axis_last', 'mode':'push_in',
                        'mid_root':[.45,.01,.62],'steps':1,'max_ik_error_mm':.01,'collision':None})
                elif path.endswith('/reach/execute'):
                    writes.append(req.post_data_json); await route.fulfill(json={'ok':True})
                elif path.endswith('/reach/exec_status'):
                    await route.fulfill(json={'running':False,'message':'完成（模拟）','progress':1})
                elif req.method != 'GET' and '/api/' in path:
                    if path.endswith(('/fk','/ik/solve','/collision/check','/trajectory/plan','/demo/plan')):
                        await route.continue_()
                    else:
                        blocked.append(path); await route.fulfill(status=409,json={'error':'Hardware writes blocked'})
                else: await route.continue_()
            await page.route('**/api/**', intercept)
            await page.goto(BASE+'/?arm=left_arm', wait_until='domcontentloaded')
            mode = page.locator('#reachDebugModeBtn')
            pause = page.locator('#reachStepMode')
            popup = page.locator('#reachStepNext')
            operations = page.locator('#reachABOpenBtn')
            await expect(mode).to_have_text('调试方式：经典…')
            await expect(pause).to_be_checked()
            await expect(operations).to_have_text('左右操作')
            await asyncio.wait_for(ready.wait(), 30)
            pick.update(available=True, revision=1, p_root=[.45,.01,.61], p_root_surface=[.45,.01,.61], p_torso=[.45,.01,.1], plane=plane,
                        selection_mode='frozen_rgbd_pointcloud', selection_source='manual', record='mock-record')
            await expect(page.locator('#reachExecBtn')).to_be_enabled()
            await page.locator('#reachExecBtn').click()
            await expect(popup).to_be_visible()
            await expect(page.locator('#reachNextSideBtn')).to_have_text('向左 10 cm')
            await expect(page.locator('#reachNextSideRBtn')).to_have_text('向右 10 cm')
            await page.wait_for_timeout(300)
            assert len(writes)==1  # arrival must not automatically sidestep
            await page.locator('#reachNextSideBtn').click()
            await expect(page.locator('#reachNextSideBtn')).to_be_enabled()
            assert len(writes)==2 and writes[-1]['label'].startswith('左移')
            assert writes[-1]['push']['direction_root'][1] < 0
            await page.locator('#reachNextSideRBtn').click()
            await expect(page.locator('#reachNextSideRBtn')).to_be_enabled()
            assert len(writes)==3 and writes[-1]['label'].startswith('右移')
            assert writes[-1]['push']['direction_root'][1] > 0
            # A negative signed automatic distance must not swap manual button meanings.
            await page.locator('#reachStepLen').fill('-7')
            await page.locator('#reachStepLen').dispatch_event('change')
            await expect(page.locator('#reachNextSideBtn')).to_have_text('向左 7 cm')
            await page.locator('#reachNextSideBtn').click()
            await expect(page.locator('#reachNextSideBtn')).to_be_enabled()
            assert writes[-1]['label'].startswith('左移7') and writes[-1]['push']['direction_root'][1] < 0
            n = len(writes)
            await page.locator('#reachNextDoneBtn').click()
            await operations.click()
            await expect(popup).to_be_visible()
            await expect(page.locator('#reachStepNextTitle')).to_have_text('经典左右操作')
            await page.locator('#reachNextDoneBtn').click()
            assert len(writes)==n  # opening/closing never executes
            await pause.uncheck()
            await page.reload(wait_until='domcontentloaded')
            await expect(pause).not_to_be_checked()  # explicit opt-out is remembered
            await mode.click(); await page.locator('#reachDebugAB').check(); await page.locator('#reachDebugSave').click()
            await expect(operations).to_have_text('A/B 操作')
            await mode.click(); await page.locator('#reachDebugClassic').check(); await page.locator('#reachDebugSave').click()
            await expect(pause).to_be_checked()
            await expect(operations).to_have_text('左右操作')
            await page.reload(wait_until='domcontentloaded')
            await expect(pause).to_be_checked()
            await operations.click()
            await expect(page.locator('#reachNextSideBtn')).to_have_text('向左 10 cm')
            await page.screenshot(path='/tmp/classic-direction-choice.png')
            assert len(writes)==n and not errors and not blocked, (writes,errors,blocked)
            print('Classic arrival popup, left/right force signs, negative-distance labels, mode-switch/persistence and no execution on open/close passed. All hardware writes mocked.')
        except Exception:
            print('Page diagnostics:', errors, blocked, await page.locator('#reachMsg').inner_text(),
                  await page.locator('#reachInfo').inner_text(), 'plans:', plans, flush=True)
            await page.screenshot(path='/tmp/classic-direction-failure.png')
            raise
        finally: await browser.close()


if __name__ == '__main__': asyncio.run(main())
