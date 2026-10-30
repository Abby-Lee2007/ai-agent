import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://127.0.0.1:8000";
const debugPort = 9231;
const profileDir = mkdtempSync(join(tmpdir(), "finagent-forecast-"));
const chromeCandidates = [
  process.env.CHROME_PATH,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);
const chromePath = chromeCandidates.find((path) => existsSync(path));
if (!chromePath) throw new Error("未找到 Chrome 或 Edge");

const browser = spawn(chromePath, [
  "--headless=new",
  "--no-sandbox",
  "--disable-gpu",
  "--disable-dev-shm-usage",
  `--remote-debugging-port=${debugPort}`,
  `--user-data-dir=${profileDir}`,
  "about:blank",
], { stdio: "ignore", windowsHide: true });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitForDebugger() {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${debugPort}/json/version`);
      if (response.ok) return;
    } catch {
      // Browser is still starting.
    }
    await sleep(100);
  }
  throw new Error("Chrome 调试端口未就绪");
}

class CdpClient {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (!message.id) return;
      const waiter = this.pending.get(message.id);
      if (!waiter) return;
      this.pending.delete(message.id);
      if (message.error) waiter.reject(new Error(message.error.message));
      else waiter.resolve(message.result);
    });
  }

  send(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }
}

async function connect() {
  const targets = await fetch(`http://127.0.0.1:${debugPort}/json/list`)
    .then((response) => response.json());
  const page = targets.find((target) => target.type === "page");
  if (!page) throw new Error("没有可用浏览器页面");
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  return new CdpClient(socket);
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description || "页面脚本失败");
  }
  return result.result.value;
}

async function waitFor(cdp, expression, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(cdp, expression)) return;
    await sleep(150);
  }
  throw new Error(`等待页面状态超时：${expression}`);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

try {
  await waitForDebugger();
  const cdp = await connect();
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Page.navigate", { url });
  await waitFor(cdp, `document.querySelector("#message-input") !== null`);
  await evaluate(cdp, `(() => {
    const input = document.querySelector("#message-input");
    input.value = "预测我未来90天的现金流";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    document.querySelector("#send-button").click();
    return true;
  })()`);
  await waitFor(cdp, `document.querySelector(".forecast-panel .forecast-guide") !== null`);
  const initial = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    const panel = bubble.querySelector(".forecast-panel");
    const guide = panel.querySelector(".forecast-guide");
    const primary = panel.querySelector(".forecast-action-primary");
    const actions = [...panel.querySelectorAll(".forecast-action")];
    const bubbleRect = bubble.getBoundingClientRect();
    const primaryRect = primary?.getBoundingClientRect();
    return {
      workflow: Boolean(bubble.querySelector(".workflow-strip")),
      guideOpen: guide.open,
      guideText: guide.textContent,
      guideBeforeHead: guide.getBoundingClientRect().top
        <= panel.querySelector(".forecast-head").getBoundingClientRect().top,
      primary: primary?.textContent.trim() || "",
      lastAction: actions.at(-1)?.textContent.trim() || "",
      insideViewport: bubbleRect.left >= 0 && bubbleRect.right <= innerWidth + 1,
      primaryInsidePanel: Boolean(primaryRect)
        && primaryRect.left >= bubbleRect.left - 1
        && primaryRect.right <= bubbleRect.right + 1,
      pageFits: document.documentElement.scrollWidth <= innerWidth
    };
  })()`);
  assert(!initial.workflow, "现金流顶部仍显示 Agent 工作流");
  assert(!initial.guideOpen, "使用说明不应默认展开");
  assert(initial.guideBeforeHead, "使用说明没有放在图表顶部");
  assert(initial.guideText.includes("确认应用此预测"), "说明缺少确认按钮指引");
  assert(initial.guideText.includes("蓝色实线"), "说明缺少蓝色实线");
  assert(initial.guideText.includes("灰色虚线"), "说明缺少灰色虚线");
  assert(initial.guideText.includes("黄色圆点"), "说明缺少最低余额点");
  assert(initial.guideText.includes("90%"), "说明缺少概率区间");
  assert(initial.primary === "确认应用此预测", "主按钮文案错误");
  assert(initial.lastAction === "确认应用此预测", "确认按钮不在操作区最后");
  assert(initial.insideViewport, "现金流面板超出视口");
  assert(initial.primaryInsidePanel, "确认按钮超出现金流面板");
  assert(initial.pageFits, "现金流面板导致横向滚动");
  await evaluate(cdp, `document.querySelector(".forecast-guide summary").click()`);
  const expanded = await evaluate(cdp, `document.querySelector(".forecast-guide").open`);
  assert(expanded, "使用说明无法展开");
  console.log(JSON.stringify({ ok: true, ...initial, expanded }, null, 2));
  await cdp.send("Browser.close");
} finally {
  if (browser.exitCode === null) browser.kill();
  await sleep(200);
  rmSync(profileDir, { recursive: true, force: true });
}
