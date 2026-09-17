"""Waypoint dialog and arm-scoped writes; all API mutations intercepted."""
import asyncio
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
            for arm, embedded, width, height in (
                ('left_arm', False, 1800, 1300), ('right_arm', True, 480, 650),
                ('left_arm', True, 360, 550),
            ):
                page = await browser.new_page(viewport={'width':width, 'height':height})
                entries, writes, errors, blocked, dialogs = [], [], [], [], []
                fail = False
                pending, finish = asyncio.Event(), asyncio.Event()
                async def dialog(d):
                    dialogs.append(d.message)
                    await d.dismiss()  # every system dialog is unexpected
                page.on('dialog', dialog)
                page.on('pageerror', lambda e: errors.append(str(e)))
                async def route_request(route):
                    req, path = route.request, urlsplit(route.request.url).path
                    if path.endswith('/reach/waypoints') and req.method == 'GET':
                        await route.fulfill(json={'waypoints':entries, 'total':len(entries)})
                    elif req.method == 'POST' and path.endswith(('/waypoints', '/waypoints/record_rgbd')):
                        assert f'/api/arms/{arm}/reach/' in path, path
                        writes.append((path, req.post_data_json))
                        pending.set()
                        await finish.wait()
                        if fail:
                            await route.fulfill(status=503, json={'error':'RGBD 点位采集失败，未保存路点: 相机断开'})
                        else:
                            prefix = 'L' if arm == 'left_arm' else 'R'
                            item = {'name':f'{prefix}-RGBD-测试', 'file':f'{prefix}-RGBD-测试_{len(entries)}.json',
                                    'arm':arm, 'named_joints':{}, 'created_at':'2026-09-17'}
                            if req.post_data_json['capture_rgbd']:
                                item['rgbd'] = {'directory':f'{prefix}-RGBD-测试_{len(entries)}/rgbd'}
                            entries.append(item)
                            await route.fulfill(json={'ok':True, 'waypoint':item})
                    elif req.method != 'GET' and '/api/' in path:
                        if path.endswith(('/fk', '/ik/solve', '/collision/check', '/trajectory/plan')):
                            await route.continue_()  # numerical preview only
                        else:
                            blocked.append(path)
                            await route.fulfill(status=409, json={'error':'Test blocks hardware/API writes'})
                    else: await route.continue_()
                await page.route('**/api/**', route_request)
                await page.goto(f'{BASE}/?arm={arm}' + ('&workspace=panel' if embedded else ''), wait_until='domcontentloaded')
                button = page.locator('#reachRecordBtn')
                modal = page.locator('#reachRecordModal')
                name = page.locator('#reachRecordName')
                yes, no = page.locator('#reachRecordRgbdYes'), page.locator('#reachRecordRgbdNo')
                save, cancel = page.locator('#reachRecordSaveBtn'), page.locator('#reachRecordCancelBtn')
                feedback = page.locator('#reachRecordFeedback')
                await expect(button).to_be_enabled()
                await button.click()
                await expect(modal).to_be_visible()
                await expect(yes).to_be_checked()
                assert (await yes.bounding_box())['y'] < (await name.bounding_box())['y']
                await page.locator('#reachRecordCloseBtn').focus()
                await page.keyboard.press('Shift+Tab')
                await expect(save).to_be_focused()
                await cancel.click()
                await expect(modal).to_be_hidden()
                assert not writes
                await button.click()
                await name.fill('   ')
                await save.click()
                await expect(feedback).to_have_text('请输入点位名称。')
                assert not writes
                await name.fill('../A')
                await save.click()
                await expect(feedback).to_contain_text('不能包含')
                assert not writes
                await name.fill('RGBD-测试')
                await expect(feedback).to_have_text('')
                await page.screenshot(path=f'/tmp/waypoint-dialog-{arm}-{width}.png')
                await save.click()
                await asyncio.wait_for(pending.wait(), timeout=10)
                await expect(button).to_be_disabled()
                await expect(save).to_be_disabled()
                await expect(cancel).to_be_disabled()
                await expect(button).to_have_text('采集 RGBD…')
                await page.keyboard.press('Escape')
                await expect(modal).to_be_visible()
                await page.wait_for_timeout(1200)  # status polling must not re-enable it
                await expect(button).to_be_disabled()
                finish.set()
                await expect(modal).to_be_hidden()
                await expect(button).to_be_enabled()
                await expect(page.locator('#reachMsg')).to_contain_text('RGBD → data/waypoints/')
                assert writes[0][1] == {'name':'RGBD-测试', 'capture_rgbd':True}
                assert await page.locator('option').filter(has_text='RGBD 已录制').count() >= 1
                await button.click()
                await no.check()
                await name.fill('RGBD-测试')
                await name.press('Enter')
                await expect(modal).to_be_hidden()
                assert writes[-1][0].endswith('/waypoints')
                assert writes[-1][1]['capture_rgbd'] is False
                before = len(writes)
                await button.click()
                await page.keyboard.press('Escape')
                await expect(modal).to_be_hidden()
                await button.click()
                await page.locator('#reachRecordCloseBtn').click()
                await expect(modal).to_be_hidden()
                assert len(writes) == before
                fail = True
                await button.click()
                await yes.check()
                await name.fill('RGBD-测试')
                await save.click()
                await expect(feedback).to_contain_text('未保存路点')
                await expect(modal).to_be_visible()
                await expect(name).to_have_value('RGBD-测试')
                await expect(yes).to_be_checked()
                await expect(save).to_be_enabled()
                await expect(button).to_be_enabled()
                assert len(entries) == 2
                fail = False
                await save.click()
                await expect(modal).to_be_hidden()
                assert len(entries) == 3
                assert not dialogs, dialogs
                assert not errors, errors
                assert not blocked, blocked
                await page.close()
                print(f'{arm}, {width}px: single dialog / RGBD / normal / cancel / validation / retry / pending / keyboard passed')
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
