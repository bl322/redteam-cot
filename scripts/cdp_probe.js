// 用本机 Edge（headless）通过 CDP 复现 Gradio 页面「切换标签页卡死」
// 用法: node scripts/cdp_probe.js
const { spawn } = require("child_process");
const http = require("http");

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const PORT = 9222;
const TARGET = "http://127.0.0.1:7860/";

function get(url) {
  return new Promise((resolve, reject) => {
    http
      .get(url, (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve(data));
      })
      .on("error", reject);
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  let version = null;
  let edge = null;
  if (process.env.EDGE_SPAWN === "1") {
    const profile = process.env.TEMP + "\\edge-cdp-profile";
    edge = spawn(
      EDGE,
      [
        "--headless=new",
        `--remote-debugging-port=${PORT}`,
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        `--user-data-dir=${profile}`,
        "about:blank",
      ],
      { detached: false, stdio: "ignore" }
    );
  }

  // 等待调试端口就绪
  for (let i = 0; i < 40; i++) {
    try {
      version = JSON.parse(await get(`http://127.0.0.1:${PORT}/json/version`));
      break;
    } catch (e) {
      await sleep(500);
    }
  }
  if (!version) {
    console.log("EDGE_CDP_FAIL: 调试端口未就绪");
    if (edge) edge.kill();
    return;
  }
  console.log("Edge:", version.Browser);

  const list = JSON.parse(await get(`http://127.0.0.1:${PORT}/json/list`));
  const target = list.find((t) => t.type === "page") || list[0];
  console.log("复用目标:", target.url);
  const ws = new WebSocket(target.webSocketDebuggerUrl);

  const logs = [];
  const errors = [];
  let msgId = 0;
  const pending = new Map();

  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    if (msg.id && pending.has(msg.id)) {
      pending.get(msg.id)(msg);
      pending.delete(msg.id);
      return;
    }
    if (msg.method === "Runtime.consoleAPICalled") {
      const text = (msg.params.args || [])
        .map((a) => a.value ?? a.description ?? a.type)
        .join(" ");
      logs.push(`[${msg.params.type}] ${text.slice(0, 200)}`);
    }
    if (msg.method === "Log.entryAdded") {
      const e = msg.params.entry;
      errors.push(`[${e.level}] ${e.text} ${e.url || ""}`.slice(0, 200));
    }
    if (msg.method === "Runtime.exceptionThrown") {
      const d = msg.params.exceptionDetails;
      errors.push(`[exception] ${d.exception?.description || d.text}`.slice(0, 200));
    }
  };

  const send = (method, params = {}) =>
    new Promise((resolve) => {
      const id = ++msgId;
      pending.set(id, resolve);
      ws.send(JSON.stringify({ id, method, params }));
    });

  await new Promise((r) => ws.addEventListener("open", r));
  await send("Runtime.enable");
  await send("Log.enable");
  await send("Page.enable");

  const evaluate = async (expr) => {
    const res = await send("Runtime.evaluate", {
      expression: expr,
      awaitPromise: true,
      returnByValue: true,
    });
    return res.result?.result?.value;
  };

  console.log("打开页面，等待 12s …");
  await send("Page.navigate", { url: TARGET });
  await sleep(12000);

  const tabInfo = await evaluate(`
    Array.from(document.querySelectorAll('[role=tab], button'))
      .map(el => (el.textContent || '').trim())
      .filter(t => t.length > 0 && t.length < 30)
      .slice(0, 12)
  `);
  console.log("可见标签/按钮:", JSON.stringify(tabInfo));

  const clickByText = (label) => `
    (() => {
      const el = Array.from(document.querySelectorAll('[role=tab], button'))
        .find(e => (e.textContent || '').includes(${JSON.stringify(label)}));
      if (!el) return 'not-found';
      el.click();
      return 'clicked';
    })()
  `;

  for (const label of ["批量评测", "架构与说明"]) {
    const t0 = Date.now();
    const r = await evaluate(clickByText(label));
    await sleep(6000);
    const alive = await evaluate("1 + 1");
    const spinner = await evaluate(`
      (document.querySelectorAll('.wrap.default.svelte, [class*=loading], [class*=spinner]').length)
    `);
    console.log(
      `点击「${label}」: ${r} | JS 响应 ${Date.now() - t0}ms | 存活=${alive} | loading 元素=${spinner}`
    );
  }

  console.log("--- console 日志(前15) ---");
  logs.slice(0, 15).forEach((l) => console.log("  " + l));
  console.log("--- 错误/异常(前15) ---");
  errors.slice(0, 15).forEach((l) => console.log("  " + l));

  ws.close();
  edge.kill();
}

main().catch((e) => console.log("SCRIPT_ERROR:", e.message));
