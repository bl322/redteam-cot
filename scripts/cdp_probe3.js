// 验证：切页到底有没有真正渲染（真实鼠标事件 + 可见性检查）
const { spawn } = require("child_process");
const http = require("http");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 9223;
const TARGET = process.env.TARGET || "http://127.0.0.1:7860/";

function get(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => { let d = ""; res.on("data", c => d += c); res.on("end", () => resolve(d)); }).on("error", reject);
  });
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function main() {
  const edge = spawn(EDGE, ["--headless=new", `--remote-debugging-port=${PORT}`, "--disable-gpu",
    "--no-first-run", "--no-default-browser-check", `--user-data-dir=${process.env.TEMP}\\edge-cdp3`, "about:blank"],
    { stdio: "ignore" });

  let version = null;
  for (let i = 0; i < 60; i++) { try { version = JSON.parse(await get(`http://127.0.0.1:${PORT}/json/version`)); break; } catch (e) { await sleep(500); } }
  if (!version) { console.log("CDP_FAIL"); edge.kill(); return; }

  const list = JSON.parse(await get(`http://127.0.0.1:${PORT}/json/list`));
  const target = list.find(t => t.type === "page") || list[0];
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  let id = 0; const pending = new Map(); const httpReqs = new Map();
  ws.onmessage = (evt) => {
    const m = JSON.parse(evt.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
    if (m.method === "Network.requestWillBeSent") httpReqs.set(m.params.requestId, { url: m.params.request.url, t: Date.now(), done: false });
    if (m.method === "Network.loadingFinished" || m.method === "Network.loadingFailed") { const r = httpReqs.get(m.params.requestId); if (r) { r.done = true; r.ms = Date.now() - r.t; } }
  };
  const send = (method, params = {}) => new Promise(res => { const my = ++id; pending.set(my, res); ws.send(JSON.stringify({ id: my, method, params })); });
  await new Promise(r => ws.addEventListener("open", r));
  await send("Runtime.enable"); await send("Page.enable"); await send("Network.enable");
  const ev = async (expr) => {
    const t0 = Date.now();
    const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    return { ms: Date.now() - t0, v: r.result?.result?.value };
  };

  await send("Page.navigate", { url: TARGET });
  await sleep(7000);

  const probeVisible = `
    (() => {
      const all = Array.from(document.querySelectorAll('*'));
      const find = (txt) => all.find(e => e.children.length === 0 && (e.textContent||'').includes(txt));
      const vis = (el) => { if (!el) return false; let n = el; while (n) { const s = getComputedStyle(n); if (s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false; n = n.parentElement; } return true; };
      const tabs = Array.from(document.querySelectorAll('[role=tab]')).map(t => ({
        text: (t.textContent||'').trim().slice(0,12),
        selected: t.getAttribute('aria-selected') || String(t.className).match(/selected[^ ]*/)?.[0] || ''
      }));
      return {
        tabs,
        batchCtl: vis(find('Dataset Path')),
        runBatch: vis(find('Run Batch')),
        archCtl: vis(find('架构与说明')),
        seedBox: vis(find('Seed Prompt')),
        bodyLen: document.body.innerText.length
      };
    })()
  `;

  console.log("初始:", JSON.stringify(await ev(probeVisible)));

  // 真实鼠标点击「批量评测」
  const box = await ev(`
    (() => {
      const el = Array.from(document.querySelectorAll('[role=tab]')).find(e => (e.textContent||'').includes('批量评测'));
      if (!el) return null;
      el.scrollIntoView({block:'center'});
      const r = el.getBoundingClientRect();
      return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)};
    })()
  `);
  console.log("坐标:", JSON.stringify(box.v));
  if (box.v) {
    await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: box.v.x, y: box.v.y, button: "none", buttons: 0 });
    await send("Input.dispatchMouseEvent", { type: "mousePressed", x: box.v.x, y: box.v.y, button: "left", buttons: 1, clickCount: 1 });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: box.v.x, y: box.v.y, button: "left", buttons: 0, clickCount: 1 });
  }
  await sleep(1500);
  console.log("点批量后1.5s:", JSON.stringify(await ev(probeVisible)));
  await sleep(4000);
  console.log("点批量后5.5s:", JSON.stringify(await ev(probeVisible)));

  // 真实鼠标点击「架构与说明」
  const box2 = await ev(`
    (() => {
      const el = Array.from(document.querySelectorAll('[role=tab]')).find(e => (e.textContent||'').includes('架构与说明'));
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)};
    })()
  `);
  if (box2.v) {
    await send("Input.dispatchMouseEvent", { type: "mousePressed", x: box2.v.x, y: box2.v.y, button: "left", buttons: 1, clickCount: 1 });
    await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: box2.v.x, y: box2.v.y, button: "left", buttons: 0, clickCount: 1 });
  }
  await sleep(3000);
  console.log("点架构后3s:", JSON.stringify(await ev(probeVisible)));

  const reqList = Array.from(httpReqs.values());
  console.log("--- 请求 ---");
  reqList.filter(r => r.url.includes('127.0.0.1')).forEach(r => console.log(`  ${r.done ? 'OK ' : 'PENDING'} ${r.ms || '-'}ms ${r.url.replace('http://127.0.0.1:7860','').slice(0,90)}`));
  console.log("外部请求:");
  reqList.filter(r => !r.url.includes('127.0.0.1')).forEach(r => console.log(`  ${r.done ? 'OK ' : 'PENDING'} ${r.url.slice(0,110)}`));

  await send("Page.captureScreenshot", {}).then(async (r) => {
    const fs = require("fs");
    fs.writeFileSync("artifacts/cdp_shot.png", Buffer.from(r.result.data, "base64"));
    console.log("截图已存 artifacts/cdp_shot.png");
  }).catch(e => console.log("截图失败", e.message));

  ws.close(); edge.kill();
  setTimeout(() => process.exit(0), 500);
}
main().catch(e => { console.log("SCRIPT_ERROR:", e.message); process.exit(1); });
