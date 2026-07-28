"""7002 人工确认台：给全流程里"还没部署"的步骤当临时大脑。

流程（api/flow.py）跑到 YOLO 判断、起手式选择、点位识别、拨动复核等
未部署步骤时，把问题 POST 到本服务，网页上人来回答，流程拿到答案继续。
以后某步的自动化部署好了，把流程里"问人"换成"问模型"即可，控制台不用改。

启动：
    python -m api.console                # 监听 0.0.0.0:7002
    python -m api.console --port 7002 --reach-port 8001

网页：http://<机器人IP>:7002/ —— 常驻显示相机画面；有问题时弹出问题卡片。
问题种类：
    yesno   是/否            → value: true / false
    choice  单选             → value: 选项字符串
    points  在画面上点点位   → value: [{"u": int, "v": int}, ...]
    confirm 确认继续         → value: "ok"
任何问题都可点「中止流程」→ value: {"__abort__": true}。

接口（全部 JSON）：
    POST /api/console/ask      {kind, prompt, options?}          → {ok, id}
    GET  /api/console/pending                                    → {questions: [...]}
    POST /api/console/answer   {id, value}                       → {ok}
    GET  /api/console/wait?id=&timeout_s=25   长轮询             → {done, value?}
    POST /api/console/cancel   {id}                              → {ok}
    GET  /api/console/config                                     → {reach_base}
"""

from __future__ import annotations

import argparse
import threading
import time
import uuid
from typing import Any

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

app = FastAPI(title="flow-console")

# 只连本机 reach_server，绝不走系统代理——终端里设了坏代理也不受影响
_http = requests.Session()
_http.trust_env = False

_cond = threading.Condition()
_questions: dict[str, dict[str, Any]] = {}   # id → 问题（含 answer）
_reach_base = "http://127.0.0.1:8001"        # 相机流的上游（服务器端代理）


@app.get("/cam")
def console_cam():
    """相机画面：服务器端代理 reach_server 的 MJPEG 流。

    网页用同源相对路径 <img src="/cam">（同 perp.html 的模式），
    浏览器不需要能直达 8001 端口。
    """
    try:
        upstream = _http.get(f"{_reach_base}/api/reach/stream",
                             stream=True, timeout=(3.0, None))
        upstream.raise_for_status()
    except requests.RequestException as exc:
        return Response(f"相机流不可达（{_reach_base}）: {exc}",
                        status_code=502, media_type="text/plain")

    def gen():
        try:
            yield from upstream.iter_content(chunk_size=65536)
        finally:
            upstream.close()

    return StreamingResponse(
        gen(),
        media_type=upstream.headers.get(
            "Content-Type", "multipart/x-mixed-replace; boundary=frame"),
        headers={"Cache-Control": "no-cache"})


@app.post("/api/console/ask")
def console_ask(body: dict):
    kind = str(body.get("kind") or "confirm")
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        return JSONResponse({"ok": False, "error": "需要 prompt"}, status_code=400)
    qid = uuid.uuid4().hex[:8]
    with _cond:
        _questions[qid] = {
            "id": qid, "kind": kind, "prompt": prompt,
            "options": list(body.get("options") or []),
            "created": time.time(), "answer": None, "answered": False,
        }
        _cond.notify_all()
    return {"ok": True, "id": qid}


@app.get("/api/console/pending")
def console_pending():
    with _cond:
        qs = [{k: q[k] for k in ("id", "kind", "prompt", "options", "created")}
              for q in sorted(_questions.values(), key=lambda q: q["created"])
              if not q["answered"]]
    return {"questions": qs}


@app.post("/api/console/answer")
def console_answer(body: dict):
    qid = str(body.get("id") or "")
    with _cond:
        q = _questions.get(qid)
        if q is None:
            return JSONResponse({"ok": False, "error": "问题不存在（可能已被回答或撤销）"},
                                status_code=404)
        if q["answered"]:
            return JSONResponse({"ok": False, "error": "已回答过"}, status_code=409)
        q["answer"] = body.get("value")
        q["answered"] = True
        _cond.notify_all()
    return {"ok": True}


@app.get("/api/console/wait")
def console_wait(id: str, timeout_s: float = 25.0):
    """长轮询：等到该问题被回答或超时。回答被取走后问题即删除。"""
    deadline = time.monotonic() + min(max(timeout_s, 0.0), 60.0)
    with _cond:
        while True:
            q = _questions.get(id)
            if q is None:
                return {"done": False, "gone": True}
            if q["answered"]:
                _questions.pop(id, None)
                return {"done": True, "value": q["answer"]}
            remain = deadline - time.monotonic()
            if remain <= 0:
                return {"done": False}
            _cond.wait(remain)


@app.post("/api/console/cancel")
def console_cancel(body: dict):
    with _cond:
        _questions.pop(str(body.get("id") or ""), None)
        _cond.notify_all()
    return {"ok": True}


_PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>流程确认台 :7002</title>
<style>
  body { margin:0; background:#14171c; color:#dde3ea; font:14px/1.6 system-ui,"Noto Sans SC",sans-serif; }
  .wrap { max-width:960px; margin:0 auto; padding:16px; }
  h1 { font-size:17px; margin:4px 0 12px; color:#8ecbff; }
  .cols { display:flex; gap:16px; flex-wrap:wrap; }
  .cam { flex:1 1 480px; }
  .panel { flex:1 1 340px; }
  /* 边框放容器上：绝对定位的红圈原点 = 图像左上角，点击换算不吃边框偏移 */
  #imgbox { position:relative; display:inline-block; max-width:100%;
            border:1px solid #333a44; border-radius:6px; }
  #cam { display:block; width:100%; border-radius:5px; }
  #imgbox.picking { border-color:#e0a838; }
  #imgbox.picking #cam { cursor:crosshair; }
  .dot { position:absolute; width:14px; height:14px;
         transform:translate(-50%,-50%);   /* 连同描边一起精确居中到点击点 */
         border:2px solid #ff5c5c; border-radius:50%; background:rgba(255,92,92,.35);
         pointer-events:none; }
  .card { background:#1c2129; border:1px solid #333a44; border-radius:8px;
          padding:14px 16px; margin-bottom:12px; }
  .card.q { border-color:#e0a838; }
  .prompt { font-size:15px; white-space:pre-wrap; margin-bottom:12px; }
  button { background:#2a3340; color:#dde3ea; border:1px solid #3d4a5c;
           border-radius:6px; padding:8px 18px; margin:4px 8px 4px 0;
           font-size:14px; cursor:pointer; }
  button:hover { background:#354152; }
  button.primary { background:#2563eb; border-color:#2563eb; color:#fff; }
  button.danger  { background:#5c2a2a; border-color:#7a3a3a; }
  .muted { color:#8a93a0; font-size:13px; }
  #ptlist { font-size:13px; color:#9fd6a0; min-height:20px; }
  .badge { display:inline-block; background:#2a3340; border-radius:10px;
           padding:0 10px; font-size:12px; color:#9ab; margin-left:8px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>流程人工确认台 <span class="badge" id="queue">队列 0</span></h1>
  <div class="cols">
    <div class="cam">
      <div id="imgbox">
        <img id="cam" src="/cam" alt="相机画面加载中…">
      </div>
      <div class="muted" id="camnote">相机画面：确认台代理 reach_server 的流（断了会自动重连）</div>
    </div>
    <div class="panel">
      <div class="card q" id="qcard" style="display:none">
        <div class="prompt" id="prompt"></div>
        <div id="answers"></div>
        <div id="ptlist"></div>
        <div style="margin-top:8px">
          <button class="danger" onclick="abortFlow()">中止流程</button>
        </div>
      </div>
      <div class="card" id="idle">等待流程提问…（页面每秒自动刷新队列）</div>
      <div class="card"><div class="muted" id="history">还没有已回答的问题</div></div>
    </div>
  </div>
</div>
<script>
// 相机流断线自动重连（reach_server 重启后不用手动刷新页面）
document.getElementById('cam').addEventListener('error', () => {
  setTimeout(() => { document.getElementById('cam').src = '/cam?' + Date.now(); }, 2000);
});

let cur = null;        // 当前展示的问题
let pts = [];          // points 模式下已点的点
const hist = [];

function el(id) { return document.getElementById(id); }

async function poll() {
  try {
    const r = await fetch('/api/console/pending');
    const d = await r.json();
    const qs = d.questions || [];
    el('queue').textContent = '队列 ' + qs.length;
    const first = qs[0] || null;
    if (!first) { show(null); return; }
    if (!cur || cur.id !== first.id) show(first);
  } catch (e) { /* 服务重启间隙，忽略 */ }
}
setInterval(poll, 1000); poll();

function show(q) {
  cur = q; pts = [];
  el('imgbox').classList.toggle('picking', !!q && q.kind === 'points');
  clearDots(); renderPts();
  if (!q) { el('qcard').style.display = 'none'; el('idle').style.display = ''; return; }
  el('qcard').style.display = ''; el('idle').style.display = 'none';
  el('prompt').textContent = q.prompt;
  const box = el('answers'); box.innerHTML = '';
  if (q.kind === 'yesno') {
    addBtn(box, '是', 'primary', () => answer(true));
    addBtn(box, '否', '', () => answer(false));
  } else if (q.kind === 'choice') {
    (q.options || []).forEach(o => addBtn(box, o, 'primary', () => answer(o)));
  } else if (q.kind === 'confirm') {
    addBtn(box, '确认，继续', 'primary', () => answer('ok'));
  } else if (q.kind === 'points') {
    addBtn(box, '提交点位', 'primary', () => {
      if (!pts.length) { alert('请先在左侧画面上点击点位'); return; }
      answer(pts);
    });
    addBtn(box, '撤销上个点 (Z)', '', undoPt);
    addBtn(box, '清空重点', '', () => { pts = []; clearDots(); renderPts(); });
  }
}

function addBtn(box, label, cls, fn) {
  const b = document.createElement('button');
  b.textContent = label; if (cls) b.className = cls; b.onclick = fn;
  box.appendChild(b);
}

async function answer(value) {
  if (!cur) return;
  const q = cur;
  await fetch('/api/console/answer', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ id: q.id, value })
  });
  hist.unshift(q.prompt.split('\\n')[0] + ' → ' + JSON.stringify(value));
  el('history').textContent = hist.slice(0, 5).join('\\n');
  show(null); poll();
}

function abortFlow() {
  if (cur && confirm('确定中止整个流程？')) answer({ '__abort__': true });
}

// ---- 点位点击：换算到相机原始分辨率的像素坐标 ----
el('cam').addEventListener('click', ev => {
  if (!cur || cur.kind !== 'points') return;
  const img = el('cam');
  if (!img.naturalWidth) return;
  const rect = img.getBoundingClientRect();
  const fx = (ev.clientX - rect.left) / rect.width;
  const fy = (ev.clientY - rect.top) / rect.height;
  const u = Math.round(fx * img.naturalWidth);
  const v = Math.round(fy * img.naturalHeight);
  pts.push({ u, v });
  const dot = document.createElement('div');
  dot.className = 'dot';
  dot.style.left = (fx * 100) + '%';
  dot.style.top = (fy * 100) + '%';
  el('imgbox').appendChild(dot);
  renderPts();
});
function clearDots() { document.querySelectorAll('.dot').forEach(d => d.remove()); }
function undoPt() {
  pts.pop();
  const dots = document.querySelectorAll('.dot');
  if (dots.length) dots[dots.length - 1].remove();
  renderPts();
}
document.addEventListener('keydown', ev => {
  if (ev.key !== 'z' && ev.key !== 'Z') return;
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  if (/INPUT|TEXTAREA|SELECT/.test(ev.target.tagName)) return;
  if (cur && cur.kind === 'points') undoPt();
});
function renderPts() {
  el('ptlist').textContent = pts.length
    ? '已点 ' + pts.length + ' 个点位: ' + pts.map(p => `(${p.u},${p.v})`).join(' ')
    : '';
}
</script>
</body>
</html>
"""


@app.get("/")
def console_page():
    return HTMLResponse(_PAGE)


def _lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main() -> None:
    global _reach_base
    import uvicorn

    parser = argparse.ArgumentParser(description="流程人工确认台（7002）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7002)
    parser.add_argument("--reach-base", default="http://127.0.0.1:8001",
                        help="reach_server 地址（相机流经确认台服务器端代理）")
    args = parser.parse_args()
    _reach_base = args.reach_base.rstrip("/")
    print(f"[console] 确认台已启动（这个进程会一直挂着，属正常）")
    print(f"[console] 浏览器打开: http://{_lan_ip()}:{args.port}/")
    print(f"[console] 相机流上游: {_reach_base}/api/reach/stream")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
