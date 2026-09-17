"""A/B debug workflow. Every hardware mutation is intercepted."""
import asyncio
import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

BASE='http://127.0.0.1:18001'
CHROME='/home/robot/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome'
ROOT=Path(__file__).resolve().parents[2]
expect.set_options(timeout=30000)


async def main():
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=CHROME,headless=True,
            args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        try:
            page=await browser.new_page(viewport={'width':1800,'height':1300})
            baseline=await (await page.request.get(BASE+'/api/dual/status')).json()
            for entry in baseline['arms'].values(): entry['status'].update(armed=True,hand_move=False)
            q={f'left_{joint}_joint':v for joint,v in zip(
                ['shoulder_pitch','shoulder_roll','shoulder_yaw','elbow','wrist_roll','wrist_pitch','wrist_yaw'],
                [-1.45,.74,-.95,.19,1.95,.26,.58])}
            points=[{'named_joints':q,'tcp_pose':{'xyz':[.45,.01,.6],'rpy':[0.,0.,0.]}},
                    {'named_joints':{k:v+.001 for k,v in q.items()},'tcp_pose':{'xyz':[.45,.01,.61],'rpy':[0.,0.,0.]}}]
            entries=[{'file':'A.json','name':'L-HHY-L-D-V1','compatible':True},
                     {'file':'B.json','name':'L-HHY-R-D-V1','compatible':True}]
            pick={'ok':True,'available':False,'revision':0}
            writes,plans,blocked,errors,dialogs=[],[],[],[],[]
            started,finish=asyncio.Event(),asyncio.Event()
            first_pick=asyncio.Event()
            fail=False
            already=False
            old_server=False
            old_xyz_server=False
            old_force_server=False
            old_tolerance_server=False
            finish.set()
            async def dialog(d):
                dialogs.append(d.message)
                await d.accept()
            page.on('dialog',dialog)
            page.on('pageerror',lambda e:errors.append(str(e)))
            async def intercept(route):
                nonlocal fail, already, old_server, old_xyz_server, old_force_server, old_tolerance_server
                req,path=route.request,urlsplit(route.request.url).path
                arm='right_arm' if '/right_arm/' in path else 'left_arm'
                if path=='/api/dual/status': await route.fulfill(json=baseline)
                elif path.endswith('/reach/status'): await route.fulfill(json=baseline['arms'][arm]['status'])
                elif path.endswith('/reach/pink/body'): await route.fulfill(json={'available':False})
                elif path.endswith('/reach/joints'): await route.fulfill(json={'ok':True,'named_joints':q})
                elif path.endswith('/reach/cabinet_waypoints'): await route.fulfill(json={'ok':True,'waypoints':entries if arm=='left_arm' else []})
                elif path.endswith('/reach/waypoints'):
                    await route.fulfill(json={'waypoints':[{'file':'end.json','name':'结束','named_joints':q}],'total':1})
                elif path.endswith('/reach/latest_pick'):
                    await route.fulfill(json=copy.deepcopy(pick));first_pick.set()
                elif path.endswith('/reach/plan_cabinet_waypoint'):
                    assert '/api/arms/left_arm/reach/' in path
                    plans.append(req.post_data_json);started.set()
                    await finish.wait()
                    if fail: await route.fulfill(status=422,json={'error':'当前帧面板被遮挡'})
                    elif already: await route.fulfill(json={'ok':True,'already_at_target':True,'name':'B',
                        'goal_position_error_mm':.01,'goal_orientation_error_deg':.01,
                        'arrival_tolerance':req.post_data_json.get('arrival_tolerance'),
                        'cabinet_assist':req.post_data_json.get('cabinet_assist'),
                        **{f'cabinet_{axis}_offset_mm':req.post_data_json.get(f'cabinet_{axis}_offset_mm',0) for axis in 'xyz'}})
                    else: await route.fulfill(json={'ok':True,'waypoints':points,'planner':'goal_pose/direct',
                        **({} if old_tolerance_server else {'arrival_tolerance':req.post_data_json.get('arrival_tolerance')}),
                        **({} if old_force_server else {'cabinet_assist':req.post_data_json.get('cabinet_assist')}),
                        **({} if old_server else {'cabinet_x_offset_mm':req.post_data_json.get('cabinet_x_offset_mm',0)}),
                        **({} if old_xyz_server else {f'cabinet_{axis}_offset_mm':req.post_data_json.get(f'cabinet_{axis}_offset_mm',0) for axis in 'yz'}),
                        'target_pose':{'xyz':[.45,.01,.61],'rpy':[.2,.3,.4]},'collision':None,
                        'goal_position_error_mm':.01,'goal_orientation_error_deg':.01,
                        'orientation':{'id':'ab-proof','name':req.post_data_json['file']}})
                elif path.endswith('/reach/plan_axis_last'):
                    await route.fulfill(json={'ok':True,'waypoints':points,'planner':'axis_last',
                        'mode':'push_in','mid_root':[.45,.01,.62],'max_ik_error_mm':.01,'collision':None})
                elif path.endswith('/reach/execute'):
                    writes.append(req.post_data_json);await route.fulfill(json={'ok':True})
                elif path.endswith('/reach/stop'): await route.fulfill(json={'ok':True})
                elif path.endswith('/reach/exec_status'):
                    await route.fulfill(json={'running':False,'message':'完成（目标位姿到达）','progress':1})
                elif path.endswith('/trajectory/plan'):
                    await route.fulfill(json={'waypoints':points,'collision':None})
                elif req.method!='GET' and '/api/' in path:
                    if path.endswith(('/fk','/ik/solve','/collision/check','/demo/plan')): await route.continue_()
                    else:
                        blocked.append(path);await route.fulfill(status=409,json={'error':'Hardware write blocked'})
                else: await route.continue_()
            await page.route('**/api/**',intercept)
            await page.goto(BASE+'/?arm=left_arm',wait_until='domcontentloaded')
            button=page.locator('#reachDebugModeBtn')
            modal=page.locator('#reachDebugModal')
            popup=page.locator('#reachStepNext')
            await expect(button).to_have_text('调试方式：经典…')
            await asyncio.wait_for(first_pick.wait(),30)
            await button.click()
            await page.locator('#reachDebugAB').check()
            await expect(page.locator('#reachDebugA')).to_have_value('A.json')
            await expect(page.locator('#reachDebugB')).to_have_value('B.json')
            await page.locator('#reachDebugB').select_option('A.json')
            await page.locator('#reachDebugSave').click()
            await expect(page.locator('#reachDebugFeedback')).to_contain_text('两个不同')
            await page.locator('#reachDebugB').select_option('B.json')
            await expect(page.locator('#reachDebugOffsetAB')).to_have_value('0')
            await expect(page.locator('#reachDebugOffsetBA')).to_have_value('0')
            await page.locator('#reachDebugOffsetBA').fill('')
            await page.locator('#reachDebugSave').click()
            await expect(page.locator('#reachDebugFeedback')).to_contain_text('不偏移填 0')
            await page.locator('#reachDebugOffsetAB').fill('5')
            await page.locator('#reachDebugOffsetBA').fill('-10')
            for field in ('OffsetABY','OffsetABZ','OffsetBAY','OffsetBAZ'):
                await expect(page.locator('#reachDebug'+field)).to_have_value('0')
                await page.locator('#reachDebug'+field).fill('100.1')
                await page.locator('#reachDebugSave').click()
                await expect(page.locator('#reachDebugFeedback')).to_contain_text('−100～+100 mm')
                await expect(page.locator('#reachDebug'+field)).to_be_focused()
                await page.locator('#reachDebug'+field).fill('0')
            for field,value in [('OffsetABY','3'),('OffsetABZ','-2'),('OffsetBAY','-4'),('OffsetBAZ','6')]:
                await page.locator('#reachDebug'+field).fill(value)
            await expect(page.locator('#reachDebugOffsetSummary')).to_contain_text('A→B：B · X +5 mm / Y +3 mm / Z −2 mm')
            await expect(page.locator('#reachDebugOffsetSummary')).to_contain_text('B→A：A · X −10 mm / Y −4 mm / Z +6 mm')
            await expect(page.locator('#reachDebugForceAB')).to_have_value('0')
            await expect(page.locator('#reachDebugForceBA')).to_have_value('0')
            await page.locator('#reachDebugForceBA').fill('41')
            await page.locator('#reachDebugSave').click()
            await expect(page.locator('#reachDebugFeedback')).to_contain_text('0～40 N')
            await page.locator('#reachDebugForceAB').fill('6')
            await page.locator('#reachDebugForceBA').fill('8')
            await page.locator('#reachDebugRamp').fill('0')
            await page.locator('#reachDebugSave').click()
            await expect(page.locator('#reachDebugFeedback')).to_contain_text('0.1～5 秒')
            await page.locator('#reachDebugRamp').fill('0.2')
            await page.locator('#reachDebugHold').fill('0.1')
            await page.locator('#reachDebugRelease').fill('0.4')
            await modal.locator('form').screenshot(path='/tmp/cabinet-xyz-settings.png')
            await page.locator('#reachDebugSave').click()
            await expect(modal).to_be_hidden()
            assert not writes and not dialogs
            await page.locator('#reachStepMode').uncheck()
            await page.locator('#reachEndSel').select_option('end.json')
            pick.update(available=True,revision=1,p_root=[.45,.01,.61],p_torso=[.45,.01,.1],
                        selection_mode='frozen_rgbd_pointcloud',selection_source='manual')
            await expect(page.locator('#reachExecBtn')).to_be_enabled()
            await page.locator('#reachExecBtn').click()
            await expect(popup).to_be_visible()
            assert len(writes)==1 and writes[0]['orientation_plan_id'] is None
            await expect(page.locator('#reachNextSideBtn')).to_have_text('A→B · X +5 mm / Y +3 mm / Z −2 mm · +X 6 N')
            await expect(page.locator('#reachNextSideRBtn')).to_have_text('B→A · X −10 mm / Y −4 mm / Z +6 mm · −X 8 N')
            await expect(page.locator('#reachNextPickBtn')).to_be_hidden()
            await expect(page.locator('#reachNextDoneBtn')).to_have_text('关闭')
            # No classic sidestep or automatic return even when configured and not segmented.
            await page.wait_for_timeout(400)
            assert len(writes)==1 and not plans
            await page.locator('#reachNextSideBtn').click()
            await expect(page.locator('#reachStepNextHint')).to_contain_text('A→B 已完成')
            assert plans[-1]['file']=='B.json' and 'start_joints' not in plans[-1]
            assert plans[-1]['cabinet_x_offset_mm']==5
            assert plans[-1]['cabinet_y_offset_mm']==3 and plans[-1]['cabinet_z_offset_mm']==-2
            assert writes[-1]['orientation_plan_id']=='ab-proof'
            assert plans[-1]['cabinet_assist']==writes[-1]['cabinet_assist']=={
                'direction':'a_to_b','force_n':6,'ramp_s':.2,'hold_s':.1,'release_s':.4}
            await expect(page.locator('#reachExecBtn')).to_be_disabled()
            await page.locator('#reachNextSideRBtn').click()
            await expect(page.locator('#reachStepNextHint')).to_contain_text('B→A 已完成')
            assert plans[-1]['file']=='A.json'
            assert plans[-1]['cabinet_x_offset_mm']==-10
            assert plans[-1]['cabinet_y_offset_mm']==-4 and plans[-1]['cabinet_z_offset_mm']==6
            assert plans[-1]['cabinet_assist']==writes[-1]['cabinet_assist']=={
                'direction':'b_to_a','force_n':8,'ramp_s':.2,'hold_s':.1,'release_s':.4}
            count=len(writes)
            await page.locator('#reachNextDoneBtn').click()
            await expect(popup).to_be_hidden()
            assert len(writes)==count
            await page.locator('#reachABOpenBtn').click()
            await page.set_viewport_size({'width':360,'height':550})
            await expect(popup).to_be_visible()
            popup_box=await popup.bounding_box()
            assert popup_box['x']>=0 and popup_box['x']+popup_box['width']<=360, popup_box
            assert await popup.evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
            await popup.screenshot(path='/tmp/cabinet-xyz-actions-mobile.png')
            await page.set_viewport_size({'width':1800,'height':1300})
            old_server=True
            await page.locator('#reachNextSideBtn').click()
            await expect(page.locator('#reachStepNextHint')).to_contain_text('未确认目标偏移')
            assert len(writes)==count  # Old server must not silently execute without the requested offset.
            old_server=False
            old_xyz_server=True
            await page.locator('#reachNextSideBtn').click()
            await expect(page.locator('#reachStepNextHint')).to_contain_text('未确认目标偏移')
            assert len(writes)==count  # A server accepting X only must not ignore nonzero Y/Z.
            old_xyz_server=False
            old_force_server=True
            await page.locator('#reachNextSideBtn').click()
            await expect(page.locator('#reachStepNextHint')).to_contain_text('未确认柜面助力')
            assert len(writes)==count
            old_force_server=False
            fail=True
            await page.locator('#reachNextSideBtn').click()
            await expect(page.locator('#reachStepNextHint')).to_contain_text('面板被遮挡')
            await expect(popup).to_be_visible()
            assert len(writes)==count
            fail=False
            already=True
            await page.locator('#reachNextSideBtn').click()
            await expect(page.locator('#reachStepNextHint')).to_contain_text('无需运动')
            assert len(writes)==count
            already=False
            # Stop during slow RGBD/IK: late response must never execute.
            started.clear();finish.clear()
            await page.locator('#reachNextSideBtn').click()
            await asyncio.wait_for(started.wait(),10)
            await expect(page.locator('#reachNextDoneBtn')).to_be_disabled()
            await page.locator('#reachStopBtn').click()
            finish.set()
            await expect(button).to_be_enabled()
            await expect(popup).to_be_hidden()
            assert len(writes)==count
            await page.locator('#reachABOpenBtn').click()
            await page.locator('#reachNextReturnBtn').click()
            await expect(popup).to_be_hidden()
            assert writes[-1]['label']=='收回:结束' and writes[-1]['motion_backend']=='legacy'
            assert len(dialogs)==1, dialogs  # only the existing first-main-motion confirmation
            # Direct arrival is available before any selected target/main trajectory.
            saved=await page.evaluate("localStorage.getItem('reachDebugMode:h2:left_arm')")
            pick.update(available=False,revision=2)
            first_pick.clear()
            await page.reload(wait_until='domcontentloaded')
            await expect(button).to_have_text('调试方式：A/B 点位…')
            await asyncio.wait_for(first_pick.wait(),30)
            await page.locator('#reachABOpenBtn').click()
            for side,target,direction in [('A','A.json','b_to_a'),('B','B.json','a_to_b')]:
                prior_plans,prior_writes=len(plans),len(writes)
                await page.locator(f'#reachDirect{side}Btn').click()
                await expect(page.locator('#reachStepNextHint')).to_contain_text(f'直接到 {side} 已完成')
                assert len(plans)==prior_plans+1 and len(writes)==prior_writes+1
                assert plans[-1]['file']==target and 'start_joints' not in plans[-1]
                assert all(plans[-1][f'cabinet_{axis}_offset_mm']==0 for axis in 'xyz')
                assert plans[-1]['cabinet_assist']==writes[-1]['cabinet_assist']=={
                    'direction':direction,'force_n':0,'ramp_s':.2,'hold_s':.1,'release_s':.4}
                assert plans[-1]['arrival_tolerance']==writes[-1]['arrival_tolerance']=={'position_mm':10,'orientation_deg':2}
                assert writes[-1]['orientation_plan_id']=='ab-proof'
                assert writes[-1]['label']==f'柜面点位:直接到 {side}'
                assert await page.evaluate("localStorage.getItem('reachDebugMode:h2:left_arm')")==saved
            # Direct settings have their own modal and never inherit roundtrip offsets/force.
            count=len(writes)
            await page.locator('#reachDirectSettingsOpen').click()
            direct_modal=page.locator('#reachDirectSettingsModal')
            await expect(direct_modal).to_be_visible()
            for field,value in [('AX','100.1'),('APosition','0'),('AOrientation','45.1'),('BX','')]:
                old=await page.locator('#reachDirectSettings'+field).input_value()
                await page.locator('#reachDirectSettings'+field).fill(value)
                await page.locator('#reachDirectSettingsSave').click()
                await expect(page.locator('#reachDirectSettingsFeedback')).to_contain_text('须在')
                await expect(page.locator('#reachDirectSettings'+field)).to_be_focused()
                await page.locator('#reachDirectSettings'+field).fill(old)
            configured={'A':([-1,2,3],12,4),'B':([4,-5,6],8,3)}
            for side,(offsets,position,angle) in configured.items():
                for suffix,value in zip(['X','Y','Z','Position','Orientation'],[*offsets,position,angle]):
                    await page.locator('#reachDirectSettings'+side+suffix).fill(str(value))
            await direct_modal.screenshot(path='/tmp/cabinet-direct-settings-desktop.png',animations='disabled')
            await page.set_viewport_size({'width':360,'height':640})
            card=page.locator('#reachDirectSettingsForm')
            assert await card.evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
            await expect(page.locator('#reachDirectSettingsSave')).to_be_in_viewport()
            await direct_modal.screenshot(path='/tmp/cabinet-direct-settings-mobile.png',animations='disabled')
            await page.locator('#reachDirectSettingsSave').click()
            await expect(direct_modal).to_be_hidden()
            assert len(writes)==count
            updated=await page.evaluate("localStorage.getItem('reachDebugMode:h2:left_arm')")
            assert {k:v for k,v in json.loads(updated).items() if not k.startswith('direct')}=={
                k:v for k,v in json.loads(saved).items() if not k.startswith('direct')}
            saved=updated
            await page.set_viewport_size({'width':1800,'height':1300})
            first_pick.clear();await page.reload(wait_until='domcontentloaded')
            await asyncio.wait_for(first_pick.wait(),30)
            await page.locator('#reachABOpenBtn').click()
            await page.locator('#reachDirectSettingsOpen').click()
            await expect(page.locator('#reachDirectSettingsAX')).to_have_value('-1')
            await expect(page.locator('#reachDirectSettingsBPosition')).to_have_value('8')
            await page.locator('#reachDirectSettingsAX').fill('50')
            await page.locator('#reachDirectSettingsCancel').click()
            for side,(offsets,position,angle) in configured.items():
                await page.locator(f'#reachDirect{side}Btn').click()
                await expect(page.locator('#reachStepNextHint')).to_contain_text(f'直接到 {side} 已完成')
                assert [plans[-1][f'cabinet_{a}_offset_mm'] for a in 'xyz']==offsets
                assert plans[-1]['arrival_tolerance']==writes[-1]['arrival_tolerance']=={'position_mm':position,'orientation_deg':angle}
                assert writes[-1]['cabinet_assist']['force_n']==0
            await page.locator('#reachNextSideBtn').click()
            await expect(page.locator('#reachStepNextHint')).to_contain_text('A→B 已完成')
            assert 'arrival_tolerance' not in plans[-1] and 'arrival_tolerance' not in writes[-1]
            assert [plans[-1][f'cabinet_{a}_offset_mm'] for a in 'xyz']==[5,3,-2]
            assert writes[-1]['cabinet_assist']['force_n']==6
            count=len(writes);old_tolerance_server=True
            await page.locator('#reachDirectABtn').click()
            await expect(page.locator('#reachStepNextHint')).to_contain_text('未确认直接到点容差')
            assert len(writes)==count
            old_tolerance_server=False
            print('Direct settings, persistence/cancel, separate roundtrip parameters and narrow-screen modal passed.',flush=True)
            await expect(page.locator('#reachNextSideBtn')).to_contain_text('Y +3 mm / Z −2 mm · +X 6 N')
            await expect(page.locator('#reachNextSideRBtn')).to_contain_text('Y −4 mm / Z +6 mm · −X 8 N')
            await page.set_viewport_size({'width':360,'height':550})
            await expect(page.locator('#reachDirectPoints')).to_be_visible()
            assert await popup.evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
            await popup.screenshot(path='/tmp/cabinet-direct-actions-mobile.png')
            await page.set_viewport_size({'width':1800,'height':1300})
            await popup.screenshot(path='/tmp/cabinet-direct-actions.png')
            count=len(writes)
            old_force_server=True
            await page.locator('#reachDirectABtn').click()
            await expect(page.locator('#reachStepNextHint')).to_contain_text('未确认柜面助力')
            assert len(writes)==count
            old_force_server=False
            fail=True
            await page.locator('#reachDirectBBtn').click()
            await expect(page.locator('#reachStepNextHint')).to_contain_text('面板被遮挡')
            assert len(writes)==count
            fail=False
            already=True
            await page.locator('#reachDirectABtn').click()
            await expect(page.locator('#reachStepNextHint')).to_contain_text('无需运动')
            assert len(writes)==count
            already=False
            # Every action is locked during direct planning; stop discards the late plan.
            started.clear();finish.clear()
            await page.locator('#reachDirectBBtn').click()
            await asyncio.wait_for(started.wait(),10)
            for id in ('reachDirectABtn','reachDirectBBtn','reachNextSideBtn','reachNextSideRBtn','reachNextReturnBtn'):
                await expect(page.locator('#'+id)).to_be_disabled()
            await page.locator('#reachStopBtn').click()
            finish.set()
            await expect(button).to_be_enabled()
            await expect(popup).to_be_hidden()
            assert len(writes)==count
            # Unarmed direct arrival previews only, and a second click after arming replans.
            baseline['arms']['left_arm']['status']['armed']=False
            await page.reload(wait_until='domcontentloaded')
            await expect(button).to_have_text('调试方式：A/B 点位…')
            await page.locator('#reachABOpenBtn').click()
            prior_plans=len(plans)
            await page.locator('#reachDirectABtn').click()
            await expect(page.locator('#reachStepNextHint')).to_contain_text('未接管：已生成预演')
            assert len(plans)==prior_plans+1 and len(writes)==count
            baseline['arms']['left_arm']['status']['armed']=True
            await page.reload(wait_until='domcontentloaded')
            await expect(button).to_have_text('调试方式：A/B 点位…')
            await page.locator('#reachABOpenBtn').click()
            await page.locator('#reachDirectABtn').click()
            await expect(page.locator('#reachStepNextHint')).to_contain_text('直接到 A 已完成')
            assert len(plans)==prior_plans+2 and len(writes)==count+1
            assert await page.evaluate("localStorage.getItem('reachDebugMode:h2:left_arm')")==saved
            assert len(dialogs)==1,dialogs
            await page.set_viewport_size({'width':360,'height':550})
            await page.goto(BASE+'/?arm=left_arm&workspace=panel',wait_until='domcontentloaded')
            await expect(button).to_have_text('调试方式：A/B 点位…')
            await button.click()
            await expect(page.locator('#reachDebugA')).to_have_value('A.json')
            await expect(page.locator('#reachDebugB')).to_have_value('B.json')
            await expect(page.locator('#reachDebugOffsetAB')).to_have_value('5')
            await expect(page.locator('#reachDebugOffsetBA')).to_have_value('-10')
            for field,value in [('OffsetABY','3'),('OffsetABZ','-2'),('OffsetBAY','-4'),('OffsetBAZ','6')]:
                await expect(page.locator('#reachDebug'+field)).to_have_value(value)
            for field,value in [('ForceAB','6'),('ForceBA','8'),('Ramp','0.2'),('Hold','0.1'),('Release','0.4')]:
                await expect(page.locator('#reachDebug'+field)).to_have_value(value)
            for width,height in [(360,550),(480,650),(620,420),(480,300)]:
                await page.set_viewport_size({'width':width,'height':height})
                await page.locator('#reachDebugBody').evaluate('(el)=>el.scrollTop=0')
                await page.screenshot(path=f'/tmp/cabinet-xyz-settings-{width}-{height}.png')
                box=await modal.locator('form').bounding_box()
                assert box['x']>=0 and box['x']+box['width']<=width+1
                assert box['y']>=0 and box['y']+box['height']<=height+1
                for id in ('reachDebugClose','reachDebugCancel','reachDebugSave'):
                    rect=await page.locator('#'+id).bounding_box()
                    assert rect['y']>=0 and rect['y']+rect['height']<=height, (id,rect)
                metrics=await page.locator('#reachDebugBody').evaluate('(el)=>[el.clientWidth,el.scrollWidth]')
                assert metrics[1]<=metrics[0]+1, (width,height,metrics)
                for direction in ('AB','BA'):
                    rects=[await page.locator('#reachDebugOffset'+direction+axis).bounding_box() for axis in ('','Y','Z')]
                    assert all(rect['width']>=60 for rect in rects), rects
                    assert max(rect['y'] for rect in rects)-min(rect['y'] for rect in rects)<1, rects
                    assert all(a['x']+a['width']<b['x'] for a,b in zip(rects,rects[1:])), rects
                await page.locator('#reachDebugRelease').scroll_into_view_if_needed()
                await expect(page.locator('#reachDebugRelease')).to_be_in_viewport()
            await page.set_viewport_size({'width':360,'height':550})
            # Cancelling an edit preserves the saved offsets.
            await page.locator('#reachDebugOffsetBA').fill('-20')
            await page.locator('#reachDebugOffsetBAY').fill('20')
            await page.locator('#reachDebugOffsetBAZ').fill('-20')
            await page.locator('#reachDebugForceBA').fill('15')
            await page.locator('#reachDebugCancel').click()
            await button.click()
            await expect(page.locator('#reachDebugOffsetBA')).to_have_value('-10')
            await expect(page.locator('#reachDebugOffsetBAY')).to_have_value('-4')
            await expect(page.locator('#reachDebugOffsetBAZ')).to_have_value('6')
            await expect(page.locator('#reachDebugForceBA')).to_have_value('8')
            await page.locator('#reachDebugClassic').check()
            await page.locator('#reachDebugSave').click()
            await expect(button).to_have_text('调试方式：经典…')
            await page.locator('#reachABOpenBtn').click()
            await expect(page.locator('#reachDirectPoints')).to_be_hidden()
            await page.locator('#reachNextDoneBtn').click()
            await page.goto(BASE+'/?arm=right_arm&workspace=panel',wait_until='domcontentloaded')
            await expect(button).to_have_text('调试方式：经典…')
            await button.click();await page.locator('#reachDebugAB').check()
            await expect(page.locator('#reachDebugOffsetAB')).to_have_value('0')
            await expect(page.locator('#reachDebugOffsetBA')).to_have_value('0')
            for field in ('OffsetABY','OffsetABZ','OffsetBAY','OffsetBAZ'):
                await expect(page.locator('#reachDebug'+field)).to_have_value('0')
            await expect(page.locator('#reachDebugForceBA')).to_have_value('0')
            await page.locator('#reachDebugSave').click()
            await expect(page.locator('#reachDebugFeedback')).to_contain_text('两个不同')
            assert not errors,errors
            assert not blocked,blocked
            # Existing X offsets and forces survive the addition of Y/Z fields.
            await page.evaluate("localStorage.setItem('reachDebugMode:h2:left_arm', JSON.stringify({mode:'ab',a:'A.json',b:'B.json',offsetABmm:5,offsetBAmm:-10,forceABn:6,forceBAn:8}))")
            await page.goto(BASE+'/?arm=left_arm&workspace=panel',wait_until='domcontentloaded')
            await expect(button).to_have_text('调试方式：A/B 点位…')
            await button.click()
            for field,value in [('OffsetAB','5'),('OffsetBA','-10'),('OffsetABY','0'),('OffsetABZ','0'),('OffsetBAY','0'),('OffsetBAZ','0'),('ForceAB','6'),('ForceBA','8')]:
                await expect(page.locator('#reachDebug'+field)).to_have_value(value)
            # Older saved settings have no offset fields; they must migrate to zero.
            await page.evaluate("localStorage.setItem('reachDebugMode:h2:left_arm', JSON.stringify({mode:'ab',a:'A.json',b:'B.json'}))")
            await page.goto(BASE+'/?arm=left_arm&workspace=panel',wait_until='domcontentloaded')
            await expect(button).to_have_text('调试方式：A/B 点位…')
            await button.click()
            await expect(page.locator('#reachDebugOffsetAB')).to_have_value('0')
            await expect(page.locator('#reachDebugOffsetBA')).to_have_value('0')
            for field,value in [('ForceAB','0'),('ForceBA','0'),('Ramp','0.5'),('Hold','0'),('Release','0.65')]:
                await expect(page.locator('#reachDebug'+field)).to_have_value(value)
            print('Direct A/B independent offsets/tolerances, zero force, persistence/cancel, old-server guards, no initial pick, unarmed preview/replanning, stop/error guards, unchanged roundtrip settings and responsive layouts passed. Hardware writes mocked.')
        finally: await browser.close()


if __name__=='__main__': asyncio.run(main())
