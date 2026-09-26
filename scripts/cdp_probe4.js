// 真实鼠标点击 → 若主线程卡死，用 Debugger.pause 抓取卡住的调用栈
const { spawn } = require("child_process");
const http = require("http");
const fs = require("fs");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 9224;
const TARGET = process.env.TARGET || "http://127.0.0.1:7860/";
const LABEL = process.env.LABEL || "批量评测";

function get(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => { let d = ""; res.on("data", (c) => (d += c)); res.on("end", () => resolve(d)); }).on("error", reject);
  });
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const edge = spawn(EDGE, ["--headless=new", `--remote-debugging-port=${PORT}`, "--disable-gpu",
    "--no-first-run", "--no-default-browser-check", `--user-data-dir=${process.env.TEMP}\\edge-cdp4`, "about:blank"],
    { stdio: "ignore" });

  let version = null;
  for (let i = 0; i < 60; i++) { try { version = JSON.parse(await get(`http://127.0.0.1:${PORT}/json/version`)); break; } catch (e) { await sleep(400); } }
  if (!version) { console.log("CDP_FAIL"); edge.kill(); return; }

  const list = JSON.parse(await get(`http://127.0.0.1:${PORT}/json/list`));
  const target = list.find((t) => t.type === "page") || list[0];
  const ws = new WebSocket(target.webSocketDebuggerUrl);

  let id = 0; const pending = new Map();
  const logs = []; const errors = []; const reqs = new Map();
  let paused = null;

  ws.onmessage = (evt) => {
    const m = JSON.parse(evt.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
    if (m.method === "Runtime.consoleAPICalled") {
      logs.push(`[${m.params.type}] ` + (m.params.args || []).map((a) => a.value ?? a.description ?? a.type).join(" ").slice(0, 300));
    }
    if (m.method === "Log.entryAdded") {
      const e = m.params.entry;
      errors.push(`[log:${e.level}] ${(e.text || "").slice(0, 200)} ${e.url || ""}`);
    }
    if (m.method === "Runtime.exceptionThrown") {
      const d = m.params.exceptionDetails;
      errors.push(`[exception] ${(d.exception?.description || d.text || "").slice(0, 300)}`);
    }
    if (m.method === "Network.requestWillBeSent") reqs.set(m.params.requestId, { url: m.params.request.url, t: Date.now(), done: false });
    if (m.method === "Network.loadingFinished" || m.method === "Network.loadingFailed") { const r = reqs.get(m.params.requestId); if (r) { r.done = true; r.ms = Date.now() - r.t; } }
    if (m.method === "Debugger.paused") paused = m.params;
  };

  const send = (method, params = {}) => new Promise((res) => { const my = ++id; pending.set(my, res); ws.send(JSON.stringify({ id: my, method, params })); });
  const fire = (method, params = {}) => ws.send(JSON.stringify({ method, params }));
  await new Promise((r) => ws.addEventListener("open", r));
  await send("Runtime.enable"); await send("Page.enable"); await send("Log.enable");
  await send("Network.enable"); await send("Debugger.enable");

  const evRace = async (expr, ms = 4000) => {
    const t0 = Date.now();
    const p = send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true });
    const timeout = sleep(ms).then(() => "__TIMEOUT__");
    const r = await Promise.race([p, timeout]);
    if (r === "__TIMEOUT__") return { ms: Date.now() - t0, v: "__HANG__" };
    return { ms: Date.now() - t0, v: r.result?.result?.value };
  };

  console.log("navigate…");
  await send("Page.navigate", { url: TARGET });
  await sleep(7000);
  console.log("基线 ping:", JSON.stringify(await evRace("1+1")));

  const box = await evRace(`
    (() => {
      const el = Array.from(document.querySelectorAll('[role=tab]')).find(e => (e.textContent||'').includes(${JSON.stringify(LABEL)}));
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2)};
    })()
  `);
  console.log("tab 坐标:", JSON.stringify(box.v));

  if (box.v && box.v.x) {
    fire("Input.dispatchMouseEvent", { type: "mousePressed", x: box.v.x, y: box.v.y, button: "left", buttons: 1, clickCount: 1 });
    fire("Input.dispatchMouseEvent", { type: "mouseReleased", x: box.v.x, y: box.v.y, button: "left", buttons: 0, clickCount: 1 });
  }

  let hung = false;
  for (let i = 1; i <= 6; i++) {
    const r = await evRace("1+1", 3000);
    console.log(`点击后 ${i * 1}s ping: ${JSON.stringify(r)}`);
    if (r.v === "__HANG__") { hung = true; break; }
    await sleep(1000);
  }

  if (hung) {
    console.log(">>> 主线程疑似卡死，尝试 Debugger.pause 抓栈 …");
    fire("Debugger.pause");
    for (let i = 0; i < 30 && !paused; i++) await sleep(300);
    if (paused) {
      console.log("paused reason:", paused.reason);
      (paused.callFrames || []).slice(0, 12).forEach((f, i) => {
        console.log(`  #${i} ${f.functionName || "(anonymous)"} @ ${f.url}:${f.location.lineNumber}:${f.location.columnNumber}`);
      });
      for (const f of (paused.callFrames || []).slice(0, 3)) {
        try {
          const src = await send("Debugger.getScriptSource", { scriptId: f.location.scriptId });
          const lines = (src.result?.scriptSource || "").split("\n");
          const l = f.location.lineNumber;
          const snippet = lines.slice(l, l + 6).map((x) => x.slice(0, 160)).join("\n    ");
          console.log(`  --- 源码 @${f.url}:${l} ---\n    ${snippet}`);
        } catch (e) { /* ignore */ }
      }
    } else {
      console.log("Debugger.pause 未返回暂停事件");
    }
  }

  console.log("--- console ---");
  logs.slice(-25).forEach((l) => console.log("  " + l));
  console.log("--- errors ---");
  errors.slice(-25).forEach((l) => console.log("  " + l.replace(/\n/g, " | ")));
  console.log("--- 请求（未完成优先） ---");
  Array.from(reqs.values()).sort((a, b) => (a.done ? 1 : 0) - (b.done ? 1 : 0))
    .slice(0, 20).forEach((r) => console.log(`  ${r.done ? "OK " : "PENDING"} ${r.ms || "-"}ms ${r.url.slice(0, 110)}`));

  ws.close(); edge.kill();
  setTimeout(() => process.exit(0), 300);
}
main().catch((e) => { console.log("SCRIPT_ERROR:", e.message); process.exit(1); });
