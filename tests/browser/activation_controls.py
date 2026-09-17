"""Check activation UI with mocked mutations; never change live arm settings."""
import asyncio
import copy
import os
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

BASE = os.environ.get('CAPABILITY_TEST_URL', 'http://127.0.0.1:18000')
REACH = os.environ.get('REACH_TEST_URL', 'http://127.0.0.1:18001')
CHROME = os.environ.get('CHROME_PATH', '/home/robot/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome')
expect.set_options(timeout=15000)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, headless=True, args=['--no-sandbox'])
        try:
            page = await browser.new_page(viewport={'width':1800,'height':1200})
            errors, posts, blocked = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            baseline = await (await page.request.get(REACH + '/api/dual/status')).json()
            capability = await (await page.request.get(BASE + '/api/capability/registry')).json()
            model = copy.deepcopy(baseline)
            model['shared_control_active'] = False
            for entry in model['arms'].values():
                entry['enabled'] = entry['selection']['enabled'] = True
            hold_next, reject_next = False, False
            held, release_poll = asyncio.Event(), asyncio.Event()

            async def intercept(route):
                nonlocal hold_next, reject_next
                request = route.request
                path = urlsplit(request.url).path
                if request.method == 'OPTIONS':
                    await route.fulfill(status=204,headers={'Access-Control-Allow-Origin':'*','Access-Control-Allow-Methods':'GET,POST','Access-Control-Allow-Headers':'content-type'})
                    return
                if path == '/api/dual/status':
                    value = copy.deepcopy(model)
                    if hold_next:
                        hold_next = False; held.set(); await release_poll.wait()
                    await route.fulfill(json=value,headers={'Access-Control-Allow-Origin':'*'})
                elif path.startswith('/api/capability/arms/'):
                    arm = path.rsplit('/',1)[-1]
                    body = request.post_data_json
                    posts.append((arm,body))
                    if reject_next:
                        reject_next = False
                        await route.fulfill(status=409,json={'ok':False,'error':'模拟保存失败'},headers={'Access-Control-Allow-Origin':'*'})
                        return
                    entry = model['arms'][arm]
                    entry['selection'].update(body)
                    if body.get('enabled') and not entry['selection'].get('gravity_file'):
                        entry['selection']['gravity_file'] = '/home/robot/yx/project/IK_replay/config/gravity_compensation.json'
                    entry['enabled'] = body['enabled']
                    capability['arm_workspace'] = copy.deepcopy(model)
                    capability['arm_workspace']['runtime_available'] = False
                    await route.fulfill(json=capability,headers={'Access-Control-Allow-Origin':'*'})
                elif path.startswith('/api/dual/reload-config/'):
                    await route.fulfill(json=model,headers={'Access-Control-Allow-Origin':'*'})
                elif request.method == 'POST':
                    blocked.append(path); await route.abort()
                else:
                    await route.continue_()

            await page.route('**/api/**',intercept)
            await page.goto(BASE,wait_until='domcontentloaded')
            left,right = [page.locator('#active-'+arm) for arm in ('left_arm','right_arm')]
            for card in (left,right):
                await expect(card.get_by_role('button',name='已激活（未改动）',exact=True)).to_be_disabled()
                await expect(card.get_by_role('button',name='取消激活',exact=True)).to_be_enabled()
            left_backend = model['arms']['left_arm']['selection']['motion_backend']
            changed_backend = 'pink' if left_backend != 'pink' else 'legacy_timed'
            await left.get_by_label('运动后端').select_option(changed_backend)
            await expect(left.get_by_role('button',name='保存更改',exact=True)).to_be_enabled()
            await page.wait_for_timeout(2200)
            await expect(left.get_by_label('运动后端')).to_have_value(changed_backend)
            await left.get_by_label('运动后端').select_option(left_backend)
            await expect(left.get_by_role('button',name='已激活（未改动）',exact=True)).to_be_disabled()
            await left.get_by_label('运动后端').select_option(changed_backend)
            await left.get_by_role('button',name='保存更改',exact=True).click()
            await expect(left.get_by_role('button',name='已激活（未改动）',exact=True)).to_be_disabled()
            assert model['arms']['left_arm']['selection']['motion_backend'] == changed_backend
            print('Unchanged, edited, reverted, and saved button states passed.',flush=True)

            right_saved = copy.deepcopy(model['arms']['right_arm'])
            right_backend = right_saved['selection']['motion_backend']
            right_draft = 'pink' if right_backend != 'pink' else 'legacy_timed'
            await right.get_by_label('运动后端').select_option(right_draft)
            hold_next = True
            await asyncio.wait_for(held.wait(),timeout=6)
            await left.get_by_role('button',name='取消激活',exact=True).click()
            await expect(left.get_by_role('button',name='激活本侧',exact=True)).to_be_enabled()
            assert posts[-1] == ('left_arm',{'enabled':False})
            assert model['arms']['right_arm'] == right_saved
            await expect(right.get_by_label('运动后端')).to_have_value(right_draft)
            release_poll.set()
            await page.wait_for_timeout(2200)
            await expect(left.get_by_role('button',name='激活本侧',exact=True)).to_be_enabled()
            await page.reload(wait_until='domcontentloaded')
            await expect(left.get_by_role('button',name='激活本侧',exact=True)).to_be_enabled()
            await expect(right.get_by_role('button',name='已激活（未改动）',exact=True)).to_be_disabled()
            print('Left deactivation persists; right stays active; stale polls and other drafts are handled.',flush=True)

            await left.get_by_role('button',name='激活本侧',exact=True).click()
            await expect(left.get_by_role('button',name='已激活（未改动）',exact=True)).to_be_disabled()
            await right.get_by_role('button',name='取消激活',exact=True).click()
            await expect(right.get_by_role('button',name='激活本侧',exact=True)).to_be_enabled()
            assert model['arms']['left_arm']['enabled'] and not model['arms']['right_arm']['enabled']
            await page.screenshot(path='/tmp/activation-controls-single-arm.png')
            await right.get_by_role('button',name='激活本侧',exact=True).click()
            await expect(right.get_by_role('button',name='已激活（未改动）',exact=True)).to_be_disabled()
            reject_next = True
            await left.get_by_role('button',name='取消激活',exact=True).click()
            await expect(page.get_by_role('status')).to_contain_text('模拟保存失败')
            await expect(left.get_by_role('button',name='已激活（未改动）',exact=True)).to_be_disabled()
            model['shared_control_active'] = True
            for card in (left,right):
                await expect(card.get_by_role('button',name='取消激活',exact=True)).to_be_disabled()
            assert not blocked,blocked
            assert not errors,errors
            print('Right-only/left-only, reactivation, failed-save and controlled-state checks passed. No real settings changed.',flush=True)
        finally:
            await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
