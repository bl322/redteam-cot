// 长时间观察：真实鼠标点击标签页后持续 60s 采样主线程状态 + 截图
const { spawn } = require("child_process");
const http = require("http");
const fs = require("fs");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 9225;
const TARGET = process.env.TARGET || "http://127.0.0.1:7860/";
const LABEL = process.env.LABEL || "批量评测";
const WINDOW = Number(process.env.WINDOW || 60);

function get(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => { let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => resolve(d)); }).on("error", reject);
  });
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const edge = spawn(EDGE, ["--headless=new", `--remote-debugging-port=${PORT}`, "--disable-gpu",
    "--no-first-run", "--no-default-browser-check", `--user-data-dir=${process.env.TEMP}\\edge-cdp5`, "about:blank"],
    { stdio: "ignore" });

  let version = null;
  for (let i = 0; i < 60; i++) { try { version = JSON.parse(await get(`http://127.0.0.1:${PORT}/json/version`)); break; } catch (e) { await sleep(400); } }
  if (!version) { console.log("CDP_FAIL"); edge.kill(); return; }

  const list = JSON.parse(await get(`http://127.0.0.1:${PORT}/json/list`));
  const target = list.find((t) => t.type === "page") || list[0];
  const ws = new WebSocket(target.webSocketDebuggerUrl);

  let id = 0; const pending = new Map(); const reqs = new Map(); const warns = [];
  ws.onmessage = (evt) => {
    const m = JSON.parse(evt.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
    if (m.method === "Network.requestWillBeSent") reqs.set(m.params.requestId, { url: m.params.request.url, t: Date.now(), done: false });
    if (m.method === "Network.loadingFinished" || m.method === "Network.loadingFailed") { const r = reqs.get(m.params.requestId); if (r) { r.done = true; r.ms = Date.now() - r.t; } }
    if (m.method === "Runtime.exceptionThrown") warns.push("[exc] " + (m.params.exceptionDetails.exception?.description || "").slice(0, 200));
  };
  const send = (method, params = {}) => new Promise((res) => { const my = ++id; pending.set(my, res); ws.send(JSON.stringify({ id: my, method, params })); });
  const fire = (method, params = {}) => ws.send(JSON.stringify({ method, params }));
  await new Promise((r) => ws.addEventListener("open", r));
  await send("Runtime.enable"); await send("Page.enable");
  await send("Network.enable"); await send("Performance.enable");

  const evRace = async (expr, ms = 4000) => {
    const t0 = Date.now();
    const r = await Promise.race([send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }), sleep(ms).then(() => "__T__")]);
    if (r === "__T__") return { ms: Date.now() - t0, v: "__HANG__" };
    return { ms: Date.now() - t0, v: r.result?.result?.value };
  };
  const metrics = async () => {
    const r = await Promise.race([send("Performance.getMetrics", {}), sleep(3000).then(() => null)]);
    const mm = r?.result?.metrics || [];
    const g = (n) => { const v = (mm.find((x) => x.name === n) || {}).value; return typeof v === "number" ? v : 0; };
    return { task: g("TaskDuration"), script: g("ScriptDuration"), layout: g("LayoutCount"), recalc: g("RecalcStyleCount"), nodes: g("Nodes") };
  };
  const state = `
    (() => {
      const tabs = Array.from(document.querySelectorAll('[role=tab]')).map(t => ((t.textContent||'').trim().slice(0,10) + ':' + (t.getAttribute('aria-selected')||'-')));
      const txt = document.body.innerText;
      return tabs.join(' | ') + ' || len=' + txt.length + ' || hasBatch=' + txt.includes('Dataset Path') + ' || hasArch=' + txt.includes('选择攻击策略');
    })()
  `;

  await send("Page.navigate", { url: TARGET });
  await sleep(7000);
  console.log("初始状态:", await evRace(state).then((r) => r.v));
  console.log("初始指标:", JSON.stringify(await metrics()));

  const box = await evRace(`
    (() => {
      const el = Array.from(document.querySelectorAll('[role=tab]')).find(e => (e.textContent||'').includes(${JSON.stringify(LABEL)}));
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)};
    })()
  `);
  console.log("坐标:", JSON.stringify(box.v));
  if (!box.v) { ws.close(); edge.kill(); setTimeout(() => process.exit(0), 300); return; }

  fire("Input.dispatchMouseEvent", { type: "mouseMoved", x: box.v.x, y: box.v.y, button: "none", buttons: 0 });
  await sleep(200);
  fire("Input.dispatchMouseEvent", { type: "mousePressed", x: box.v.x, y: box.v.y, button: "left", buttons: 1, clickCount: 1 });
  fire("Input.dispatchMouseEvent", { type: "mouseReleased", x: box.v.x, y: box.v.y, button: "left", buttons: 0, clickCount: 1 });
  const t0 = Date.now();

  const shot = async (n) => {
    try {
      const r = await Promise.race([send("Page.captureScreenshot", {}), sleep(6000).then(() => null)]);
      if (r?.result?.data) { fs.writeFileSync(`artifacts/shot_${n}.png`, Buffer.from(r.result.data, "base64")); return `artifacts/shot_${n}.png`; }
    } catch (e) { /* ignore */ }
    return "(截图失败)";
  };

  let prev = await metrics();
  for (let el = 0; el * 5 < WINDOW; el++) {
    await sleep(5000);
    const nowSecs = ((Date.now() - t0) / 1000).toFixed(0);
    const ping = await evRace("1+1", 3000);
    const m = await metrics();
    const st = await evRace(state, 3000);
    const dTask = (m.task - prev.task).toFixed(2);
    console.log(`+${nowSecs}s ping=${ping.ms}ms ${ping.v === "__HANG__" ? "HANG!" : "ok"} Δtask=${dTask}s layout+${m.layout - prev.layout} recalc+${m.recalc - prev.recalc} nodes=${m.nodes}`);
    console.log(`     ${st.v}`);
    prev = m;
    if (el === 0) console.log("     截图1:", await shot("after_click_2s"));
    if (el === 2) console.log("     截图2:", await shot("after_click_15s"));
  }
  console.log("     截图末:", await shot("after_click_end"));

  const pend = Array.from(reqs.values()).filter((r) => !r.done);
  console.log("未完成的请求:", pend.length);
  pend.slice(0, 10).forEach((r) => console.log("  PENDING", r.url.slice(0, 120), `${((Date.now() - r.t) / 1000).toFixed(1)}s`));
  console.log("异常:", warns.slice(0, 5));

  ws.close(); edge.kill();
  setTimeout(() => process.exit(0), 300);
}
main().catch((e) => { console.log("SCRIPT_ERROR:", e.message); process.exit(1); });
