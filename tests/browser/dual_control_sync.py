"""Browser regression for shared controls; all arm mutations are intercepted.

Run with a local reach server and Playwright installed:
  python tests/browser/dual_control_sync.py
No hardware command is sent. A real, read-only snapshot supplies model metadata.
"""
import asyncio
import copy
import os
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

expect.set_options(timeout=60000)

BASE = os.environ.get('REACH_TEST_URL', 'http://127.0.0.1:18001')
CHROME = os.environ.get('CHROME_PATH', '/home/robot/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome')

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, headless=True, args=['--no-sandbox'])
        try:
            page = await browser.new_page(viewport={'width':1800,'height':1120})
            page.set_default_timeout(60000)
            baseline = await (await page.request.get(BASE + '/api/dual/status')).json()
            snapshot = copy.deepcopy(baseline)
            for entry in snapshot['arms'].values():
                entry['enabled'] = True
                entry['status'].update(armed=True, hand_move=False)
            snapshot['shared_control_active'] = True
            errors, writes, blocked = [], [], []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.on('dialog', lambda dialog: dialog.accept())
            # Hold one old outer-window poll across release to check stale responses.
            hold_next = False
            poll_held = asyncio.Event()
            poll_return = asyncio.Event()

            async def intercept(route):
                nonlocal hold_next
                req = route.request
                path = urlsplit(req.url).path
                if path == '/api/dual/status':
                    saved = copy.deepcopy(snapshot)
                    if hold_next and req.frame == page.main_frame:
                        hold_next = False
                        poll_held.set()
                        await poll_return.wait()
                    await route.fulfill(json=saved)
                elif path == '/api/dual/hand_move' and req.method == 'POST':
                    writes.append(path)
                    for entry in snapshot['arms'].values():
                        entry['status']['hand_move'] = req.post_data_json['on']
                    await route.fulfill(json=snapshot)
                elif path == '/api/dual/disarm' and req.method == 'POST':
                    writes.append(path)
                    for entry in snapshot['arms'].values():
                        entry['status'].update(armed=False, hand_move=False)
                    snapshot['shared_control_active'] = False
                    await route.fulfill(json=snapshot)
                elif path.startswith('/api/arms/') and path.endswith('/reach/status'):
                    await route.fulfill(json=snapshot['arms'][path.split('/')[3]]['status'])
                elif req.method == 'POST' and '/api/' in path:
                    # Viewer FK and offline planning are read-only calculations.
                    if path.endswith(('/api/fk','/fk','/collision/check','/trajectory/plan','/demo/plan')):
                        await route.continue_()
                    else:
                        blocked.append(path)
                        await route.fulfill(status=409,json={'ok':False,'error':'Hardware writes blocked by browser test'})
                else:
                    await route.continue_()

            await page.route('**/api/**', intercept)
            await page.goto(BASE + '/arms', wait_until='domcontentloaded')
            panels = [page.frame_locator(f'#{arm} iframe') for arm in ('left_arm','right_arm')]
            for panel in panels:
                await expect(panel.locator('#reachArmBtn')).to_have_text('释放手臂')
                await expect(panel.locator('#reachArmBtn')).to_be_enabled()
            await panels[0].locator('#reachDuration').fill('9')
            frames = [f for f in page.frames if 'workspace=panel' in f.url]
            await page.locator('#bothFloat').click()
            for panel in panels:
                await expect(panel.locator('#reachHandMoveBtn')).to_have_text('恢复保持')
                await expect(panel.locator('#reachBadge')).to_have_text('已接管（卸力中）')
            await page.locator('#bothHold').click()
            for panel in panels:
                await expect(panel.locator('#reachHandMoveBtn')).to_have_text('卸力摆位')
                await expect(panel.locator('#reachBadge')).to_have_text('已接管手臂')
            print('Paired float and hold update both panels.', flush=True)
            hold_next = True
            await asyncio.wait_for(poll_held.wait(),timeout=5)
            await page.locator('#release').click()
            for panel in panels:
                await expect(panel.locator('#reachArmBtn')).to_have_text('接管手臂与灵巧手')
                await expect(panel.locator('#reachHandMoveBtn')).to_be_disabled()
                await expect(panel.locator('#reachExecBtn')).to_be_disabled()
                await expect(panel.locator('#reachBadge')).to_have_text('未接管（仅模拟）')
            poll_return.set()
            await page.wait_for_timeout(1500)
            for panel in panels:
                await expect(panel.locator('#reachArmBtn')).to_have_text('接管手臂与灵巧手')
            assert all(not frame.is_detached() for frame in frames)
            await expect(panels[0].locator('#reachDuration')).to_have_value('9')
            assert writes.count('/api/dual/disarm') == 1
            assert not blocked, blocked
            assert not errors, errors
            print('Release updates both panels; stale polls cannot undo it. Drafts and frames are preserved.', flush=True)
            print('All control requests were mocked; no hardware commands sent.', flush=True)
        finally:
            await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
