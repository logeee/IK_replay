"""7005 manual category UI; all API calls mocked, no robot requests."""
import asyncio
import os
import struct
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

BASE = os.environ.get('POINTCLOUD_TEST_URL', 'http://127.0.0.1:7005')
CHROME = os.environ.get('CHROME_PATH', '/home/robot/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome')


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, headless=True,
                                         args=['--no-sandbox', '--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
        try:
            page = await browser.new_page(viewport={'width':1280, 'height':1100})
            errors, requests, unexpected = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            metadata = {
                'ok':True, 'capture_id':'occluded-panel', 'arm':'left_arm',
                'data_url':'/api/pointcloud/data/occluded-panel', 'image_url':'/api/pointcloud/image/occluded-panel',
                'point_count':1, 'boxes':[{'name':'面板', 'cls':0, 'conf':.95, 'xyxy':[1,1,10,10]}],
                'model':'fake.pt', 'model_available':True, 'stride':1, 'box_padding_ratio':.1,
                'z_min_m':.15, 'z_max_m':3., 'capture_ms':1.,
                'T_cam2root':[[1.,0.,0.,0.],[0.,1.,0.,0.],[0.,0.,1.,0.],[0.,0.,0.,1.]],
            }
            binary = struct.pack('<4sIII3f6B2Hh', b'PCV1', 1, 1, 0, 0., 0., 1., 100,100,100,239,83,80,1,1,0)

            async def route_api(route):
                path = urlsplit(route.request.url).path
                if path == '/api/pointcloud/status':
                    await route.fulfill(json={'ok':True, 'latest_capture_id':metadata['capture_id'], 'model_available':True})
                elif path.startswith('/api/pointcloud/capture/') or path == '/api/pointcloud/capture':
                    await route.fulfill(json=metadata)
                elif path.startswith('/api/pointcloud/data/'):
                    await route.fulfill(body=binary,content_type='application/octet-stream')
                elif path == '/api/pointcloud/offset-presets':
                    await route.fulfill(json={'ok':True,'presets':[]})
                elif path.startswith('/api/pointcloud/auto-target/'):
                    body = route.request.post_data_json
                    requests.append(body)
                    category = body['knob_scene_override']
                    if category is None:
                        await route.fulfill(status=422,json={'ok':False,'error':"当前帧没有旋钮类实例，仅有 ['面板']"})
                    else:
                        slot = 1 if category == '旋钮右' else 3
                        await route.fulfill(json={
                            'ok':True,'target_camera_m':[.05 if slot == 1 else -.05,0.,1.],
                            'panel_center_camera_m':[0.,0.,1.], 'target_wall_m':[.05,0.,0.],
                            'wall_axes_camera':[[1.,0.,0.],[0.,0.,1.],[0.,-1.,0.]],
                            'target_point_slot':slot,'matched_detection_name':category,
                            'model_version':'1.0.0-pa','knob_scene_source':'manual','knob_scene_override':category,
                        })
                elif path.startswith('/api/pointcloud/confirm/'):
                    body = route.request.post_data_json
                    assert body['knob_scene_source'] == 'manual'
                    assert body['knob_scene_override'] == '旋钮左'
                    assert body['target_point_slot'] == 3
                    await route.fulfill(json={'ok':True,'p_root':[0.,0.,1.]})
                elif path.startswith('/api/pointcloud/capture-progress/'):
                    await route.fulfill(json={'ok':True,'done':True})
                else:
                    unexpected.append(path)
                    await route.abort()

            await page.route('**/api/**',route_api)
            await page.goto(BASE+'/?arm=left_arm',wait_until='domcontentloaded')
            category = page.locator('#knobSceneOverride')
            find = page.locator('#autoTarget')
            confirm = page.locator('#confirmTarget')
            await expect(find).to_be_enabled()
            await expect(category).to_have_value('')
            await find.click()
            await expect(page.locator('#status')).to_contain_text('没有旋钮类实例')
            await category.select_option('旋钮右')
            await find.click()
            await expect(page.locator('#status')).to_contain_text('旋钮右·点1（手动指定类别）')
            await expect(page.locator('#selection')).to_contain_text('旋钮右（手动指定）')
            await expect(confirm).to_be_enabled()
            await page.reload(wait_until='domcontentloaded')
            await expect(find).to_be_enabled()
            await expect(category).to_have_value('旋钮右')
            await expect(page.locator('#selection')).to_contain_text('旋钮右（手动指定）')
            await category.select_option('旋钮左')
            await expect(confirm).to_be_disabled()
            await page.reload(wait_until='domcontentloaded')
            await expect(find).to_be_enabled()
            await expect(category).to_have_value('旋钮左')
            await expect(confirm).to_be_disabled()
            await find.click()
            await expect(page.locator('#status')).to_contain_text('旋钮左·点3（手动指定类别）')
            await confirm.click()
            await expect(page.locator('#status')).to_contain_text('18001 已确认目标')
            await category.select_option('')
            await expect(confirm).to_be_disabled()
            await find.click()
            await expect(page.locator('#status')).to_contain_text('没有旋钮类实例')
            assert [v['knob_scene_override'] for v in requests] == [None,'旋钮右','旋钮左',None]
            assert not errors, errors
            assert not unexpected, unexpected
            print('7005 category selection, source labels, stale-point clearing, refresh persistence and confirmation metadata passed. All API calls mocked.')
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
