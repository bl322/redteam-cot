// 深入探测：检测「切换标签页后主线程是否被占满」
// 用法: node scripts/cdp_probe2.js [waitMs]
const { spawn } = require("child_process");
const http = require("http");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 9222;
const TARGET = process.env.TARGET || "http://127.0.0.1:7860/";

function get(url) {
  return new Promise((resolve, reject) => {
    http
      .get(url, (res) => {
        let d = "";
        res.on("data", (c) => (d += c));
        res.on("end", () => resolve(d));
      })
      .on("error", reject);
  });
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const edge = spawn(
    EDGE,
    [
      "--headless=new",
      `--remote-debugging-port=${PORT}`,
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      `--user-data-dir=${process.env.TEMP}\\edge-cdp2`,
      "about:blank",
    ],
    { stdio: "ignore" }
  );

  let version = null;
  for (let i = 0; i < 60; i++) {
    try {
      version = JSON.parse(await get(`http://127.0.0.1:${PORT}/json/version`));
      break;
    } catch (e) {
      await sleep(400);
    }
  }
  if (!version) {
    console.log("CDP_FAIL");
    edge.kill();
    return;
  }

  const list = JSON.parse(await get(`http://127.0.0.1:${PORT}/json/list`));
  const target = list.find((t) => t.type === "page") || list[0];
  const ws = new WebSocket(target.webSocketDebuggerUrl);

  let msgId = 0;
  const pending = new Map();
  const netReq = new Map();
  const exceptions = [];
  const consoleErrs = [];

  ws.onmessage = (evt) => {
    const m = JSON.parse(evt.data);
    if (m.id && pending.has(m.id)) {
      pending.get(m.id)(m);
      pending.delete(m.id);
      return;
    }
    if (m.method === "Network.requestWillBeSent") {
      const r = m.params.request;
      if (r && r.url) netReq.set(m.params.requestId, { url: r.url, done: false, t: Date.now() });
    }
    if (m.method === "Network.loadingFinished" || m.method === "Network.loadingFailed") {
      const rec = netReq.get(m.params.requestId);
      if (rec) rec.done = true;
    }
    if (m.method === "Runtime.exceptionThrown") {
      exceptions.push((m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text || "").slice(0, 300));
    }
    if (m.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(m.params.type)) {
      const t = (m.params.args || []).map((a) => a.value ?? a.description ?? "").join(" ").slice(0, 200);
      consoleErrs.push(`[${m.params.type}] ${t}`);
    }
  };

  const send = (method, params = {}) =>
    new Promise((resolve) => {
      const id = ++msgId;
      pending.set(id, resolve);
      ws.send(JSON.stringify({ id, method, params }));
    });
  const waitOpen = new Promise((r) => ws.addEventListener("open", r));
  await waitOpen;
  await send("Runtime.enable");
  await send("Page.enable");
  await send("Network.enable");
  await send("Performance.enable");
  const M = (mm, n) => { const v = (mm.find(x=>x.name===n)||{}).value; return typeof v === "number" ? v : 0; };

  const evaluate = async (expr) => {
    const t0 = Date.now();
    const res = await send("Runtime.evaluate", {
      expression: expr,
      awaitPromise: true,
      returnByValue: true,
    });
    return { ms: Date.now() - t0, value: res.result?.result?.value };
  };
  const metrics = async () => {
    const r = await send("Performance.getMetrics", {});
    const mm = r.result?.metrics || [];
    const g = (n) => M(mm, n);
    return {
      layout: g("LayoutCount"),
      recalc: g("RecalcStyleCount"),
      task: g("TaskDuration"),
      script: g("ScriptDuration"),
      nodes: g("Nodes"),
      listeners: g("JSEventListeners"),
    };
  };
  const report = async (label) => {
    const live = await evaluate("1+1");
    const m = await metrics();
    const busy = Array.from(netReq.values()).filter((r) => !r.done);
    const external = busy.filter((r) => !r.url.includes("127.0.0.1"));
    console.log(
      `${label}: evalRT=${live.ms}ms layout=${m.layout} recalc=${m.recalc} task=${m.task.toFixed(2)}s script=${m.script.toFixed(2)}s nodes=${m.nodes} listeners=${m.listeners} pending=${busy.length} extPending=${external.length}`
    );
    external.slice(0, 8).forEach((r) => console.log("    PENDING(ext):", r.url.slice(0, 120)));
    return m;
  };

  console.log("navigate…");
  await send("Page.navigate", { url: TARGET });
  console.log("page loaded, waiting 6s"); await sleep(6000);
  const base = await report("载入后基线");

  const click = async (label) => {
    const r = await evaluate(`
      (() => {
        const el = Array.from(document.querySelectorAll('[role=tab],button'))
          .find(e => (e.textContent||'').includes(${JSON.stringify(label)}));
        if (!el) return 'not-found';
        el.click(); return 'clicked';
      })()
    `);
    return r.value;
  };

  for (const label of ["批量评测", "架构与说明", "批量评测", "架构与说明"]) {
    const c = await click(label);
    await sleep(400);
    await report(`点「${label}」+0.5s (${c})`);
    await sleep(2500);
    const m2 = await report(`点「${label}」+4.5s`);
    const dLayout = m2.layout - (lastGlobal.layout || 0);
    void dLayout;
  }

  console.log("--- 异常 ---");
  exceptions.slice(0, 10).forEach((e) => console.log("  " + e.replace(/\n/g, " | ")));
  console.log("--- console error/warn ---");
  consoleErrs.slice(0, 10).forEach((e) => console.log("  " + e));

  ws.close();
  edge.kill();
}

let lastGlobal = {};
process.on("exit", ()=>console.log("EXITED"));
main().catch((e) => console.log("SCRIPT_ERROR:", e.message));
