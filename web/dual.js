const arms = ["left_arm", "right_arm"];
const labels = {left_arm:"机器人左手", right_arm:"机器人右手"};
const frames = new Map();
const previews = new Map();
let snapshot;
let polling = false;
let refreshVersion = 0;
const scene = document.querySelector("#scene");
function notice(message, error=false) {
  const node=document.querySelector("#notice");node.textContent=message;
  node.className=`visible ${error?'error':''}`;
  clearTimeout(notice.timer);notice.timer=setTimeout(()=>node.className="",7000);
}
async function api(path, body) {
  const response=await fetch(path, body===undefined?{cache:"no-store"}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const value=await response.json();if(!response.ok||value.ok===false)throw Error(value.error||"请求失败");return value;
}
function capabilityUrl(path){const url=new URL(location.href);url.port="18000";url.pathname=path;url.search=url.hash="";return url}
async function saveDesired(arm,body){
  const response=await fetch(capabilityUrl(`/api/capability/arms/${arm}`),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const value=await response.json();if(!response.ok||value.ok===false)throw Error(value.error||"配置保存失败");
  try{await api(`/api/dual/reload-config/${arm}`,{})}
  catch(error){notice(`配置已保存到 18000，执行窗口暂未应用：${error.message}`,true)}
  return value;
}
for(const arm of arms){
  const root=document.getElementById(arm);
  root.innerHTML=`<div class="arm-title"><strong>${labels[arm]}</strong><span class="status">未启用</span><button class="settings">配置</button><button class="collapse" aria-label="收起窗口">—</button></div>
  <form class="configuration"><label>手型号<select name="hand_id" required></select></label>
  <label>安装方案<select name="mount_profile_id"></select></label>
  <label>补偿文件<input name="gravity_file" placeholder="补偿 JSON 文件绝对路径" required></label>
  <label>补偿版本<input name="gravity_version" placeholder="留空使用文件默认版本"></label>
  <label>运动后端<select name="motion_backend"><option value="legacy">原方案</option><option value="legacy_timed">50Hz 时间轨迹</option><option value="pink">PINK 世界系跟踪</option></select></label>
  <label>灵巧手服务<input name="hand_service_url" placeholder="http://127.0.0.1:18089"></label>
  <label>独立手串口<input name="hand_port" placeholder="DDS / 第一只串口手可留空"></label>
  <p>修改配置前释放双臂。卸力沿用原有定义；无标定时可接管、卸力和录制关节位点，选点规划需先补齐该侧标定。</p>
  <footer><button type="submit">保存并启用本侧</button><button type="button" class="disable">停用本侧</button></footer></form>`;
  const form=root.querySelector("form");
  root.querySelector(".settings").onclick=()=>form.classList.toggle("hidden");
  root.querySelector(".collapse").onclick=()=>root.classList.toggle("collapsed");
  const updateMounts=()=>{
    const hand=snapshot?.hands.find(h=>h.id===form.elements.hand_id.value);
    form.elements.mount_profile_id.replaceChildren(...(hand?.mount_profiles||[]).map(p=>new Option(p.name,p.id)));
  };
  form.elements.hand_id.onchange=updateMounts;
  root.updateForm=entry=>{
    const selection=entry.selection||{};
    form.elements.hand_id.replaceChildren(...snapshot.hands.filter(h=>h.design_side===arm.replace("_arm","")).map(h=>new Option(h.name,h.id)));
    if(selection.hand_id)form.elements.hand_id.value=selection.hand_id;updateMounts();
    for(const key of ["mount_profile_id","gravity_file","gravity_version","motion_backend","hand_service_url","hand_port"])
      if(selection[key]!==undefined)form.elements[key].value=selection[key];
    form.classList.toggle("hidden",entry.enabled);
  };
  form.onsubmit=async event=>{
    event.preventDefault();const button=form.querySelector('[type="submit"]');button.disabled=true;
    try{await saveDesired(arm,{...Object.fromEntries(new FormData(form)),enabled:true});
      frames.get(arm)?.remove();frames.delete(arm);await refresh();form.classList.add("hidden");notice(`${labels[arm]}已启用`);
    }catch(e){notice(e.message,true)}finally{button.disabled=false}
  };
  root.querySelector(".disable").onclick=async()=>{try{await saveDesired(arm,{enabled:false});await refresh()}catch(e){notice(e.message,true)}};
}
function applySnapshot(value){
    snapshot=value;
    document.querySelector("#summary").textContent=snapshot.control_error||"独立卸力 · 自动轨迹串行";
    for(const arm of arms){const entry=snapshot.arms[arm],root=document.getElementById(arm),status=entry.status;
      root.querySelector(".status").textContent=!entry.enabled?"未启用":status?.armed?(status.hand_move?"已卸力":status.exec?.running?"执行中":"保持中"):"已启用 · 未接管";
      const selectionKey=JSON.stringify(entry.selection);
      if(root.selectionKey!==undefined&&root.selectionKey!==selectionKey){
        frames.get(arm)?.remove();frames.delete(arm);previews.delete(arm);
        root.updateForm(entry);
        scene.contentWindow.postMessage({type:"dual-reset",arm},location.origin);
      }
      root.selectionKey=selectionKey;
      if(!root.initialized){root.updateForm(entry);root.initialized=true}
      if(entry.enabled&&!frames.has(arm)){const frame=document.createElement("iframe");frame.title=`${labels[arm]}操作窗口`;frame.src=`/?workspace=panel&arm=${arm}`;root.append(frame);frames.set(arm,frame)}
      if(!entry.enabled&&frames.has(arm)){frames.get(arm).remove();frames.delete(arm);root.querySelector("form").classList.remove("hidden")}
      frames.get(arm)?.contentWindow.postMessage({type:"dual-control-status",arm,status},location.origin);
    }
}
async function refresh(force=false){
  if(polling&&!force)return;
  polling=true;const version=++refreshVersion;
  try{const value=await api("/api/dual/status");
    if(version===refreshVersion)applySnapshot(value);
  }catch(e){if(version===refreshVersion)notice(e.message,true)}
  finally{if(version===refreshVersion)polling=false}
}
for(const [id,path,body] of [["bothFloat","hand_move",{on:true}],["bothHold","hand_move",{on:false}],["stop","stop",{}],["release","disarm",{}]]){
  document.getElementById(id).onclick=async()=>{
    if(id==="release"&&!confirm("确认释放双臂并交还本体控制？请先扶稳手臂。"))return;
    if(id==="bothFloat"&&!confirm("确认将已接管的手臂同时卸力？沿用各侧原有阻尼和补偿设置。"))return;
    const button=document.getElementById(id);button.disabled=true;
    try{const value=await api(`/api/dual/${path}`,body);
      if(value.arms){
        // Discard any status response started before this completed action.
        ++refreshVersion;polling=false;applySnapshot(value);
      }else await refresh(true);
      notice("操作完成");
    }catch(e){notice(e.message,true)}finally{button.disabled=false}
  };
}
window.addEventListener("message",event=>{
  if(event.origin!==location.origin)return;
  if(event.source===scene.contentWindow&&event.data?.type==="dual-scene-ready"){
    for(const preview of previews.values())scene.contentWindow.postMessage(preview,location.origin);
    return;
  }
  if(event.source===scene.contentWindow&&event.data?.type==="dual-target"){
    frames.get(event.data.arm)?.contentWindow.postMessage(event.data,location.origin);return;
  }
  for(const [arm,frame] of frames){
    if(event.source===frame.contentWindow&&event.data?.type==="dual-control-changed"){
      refresh(true);return;
    }
    if(event.source===frame.contentWindow&&event.data?.type==="dual-preview"){
    const preview={...event.data,arm,frames:event.data.frames??previews.get(arm)?.frames};
    previews.set(arm,preview);
    scene.contentWindow.postMessage({...event.data,arm},location.origin);
  }}
});
await refresh();setInterval(refresh,1000);
