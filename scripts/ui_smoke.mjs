import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://127.0.0.1:8000";
const outDir = process.argv[3] || "ui-artifacts";
const accessPassword = process.argv[4] || "";
const debugPort = 9229;
const profileDir = mkdtempSync(join(tmpdir(), "finagent-ui-"));

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
  "--hide-scrollbars",
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
  const targets = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then((r) => r.json());
  const page = targets.find((target) => target.type === "page");
  if (!page) throw new Error("没有可用的浏览器页面");
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
    throw new Error(result.exceptionDetails.text || "页面脚本执行失败");
  }
  return result.result.value;
}

async function waitFor(cdp, expression, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(cdp, expression)) return;
    await sleep(100);
  }
  throw new Error(`等待页面状态超时：${expression}`);
}

async function screenshot(cdp, name) {
  const result = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  mkdirSync(outDir, { recursive: true });
  writeFileSync(join(outDir, name), Buffer.from(result.data, "base64"));
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function openChat(cdp, width, height, mobile) {
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile,
  });
  await cdp.send("Page.navigate", { url });
  await waitFor(cdp, `document.readyState === "complete"`);
  await waitFor(cdp, `document.querySelector("#session-id")?.textContent !== "正在创建"`);
}

async function sendPrompt(cdp, prompt, expectedText) {
  await evaluate(cdp, `(() => {
    const input = document.querySelector("#message-input");
    input.value = ${JSON.stringify(prompt)};
    input.dispatchEvent(new Event("input", { bubbles: true }));
    document.querySelector("#composer").requestSubmit();
    return true;
  })()`);
  await waitFor(
    cdp,
    `(() => {
      const replies = [...document.querySelectorAll(".message.agent .bubble")];
      return replies.some((node) => node.textContent.includes(${JSON.stringify(expectedText)}));
    })()`,
    15_000,
  );
  await waitFor(cdp, `!document.querySelector("#typing-message")`);
}

let cdp;
try {
  await waitForDebugger();
  cdp = await connect();
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  if (accessPassword) {
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
      source: [
        `sessionStorage.setItem("finagent_access_token", ${JSON.stringify(accessPassword)});`,
        `window.prompt = () => ${JSON.stringify(accessPassword)};`,
      ].join("\n"),
    });
  }

  await openChat(cdp, 1440, 1000, false);
  let desktop = await evaluate(cdp, `(() => {
    const app = document.querySelector(".app").getBoundingClientRect();
    const prompt = document.querySelector("#message-input").getBoundingClientRect();
    const send = document.querySelector("#send-button").getBoundingClientRect();
    return {
      title: document.title,
      viewport: [innerWidth, innerHeight],
      scrollWidth: document.documentElement.scrollWidth,
      app: [app.left, app.top, app.right, app.bottom],
      composerOverlap: !(prompt.right <= send.left || send.right <= prompt.left),
    };
  })()`);
  assert(desktop.title === "AI Banking Agent", "H5 标题不正确");
  assert(desktop.scrollWidth <= desktop.viewport[0], "桌面页面出现横向滚动");
  assert(desktop.composerOverlap === false, "输入框与发送按钮重叠");
  await sendPrompt(cdp, "我还有多少钱", "总资产");
  await screenshot(cdp, "desktop.png");

  await openChat(cdp, 390, 844, true);
  await sendPrompt(cdp, "我有哪些卡", "信用卡");
  const mobile = await evaluate(cdp, `(() => {
    const bubbles = [...document.querySelectorAll(".bubble")].map((node) => node.getBoundingClientRect());
    const composer = document.querySelector(".composer-area").getBoundingClientRect();
    return {
      viewport: [innerWidth, innerHeight],
      scrollWidth: document.documentElement.scrollWidth,
      bubblesInside: bubbles.every((rect) => rect.left >= 0 && rect.right <= innerWidth),
      composerVisible: composer.top < innerHeight && composer.bottom <= innerHeight + 1,
      quickDisplay: getComputedStyle(document.querySelector(".quick-list")).display,
    };
  })()`);
  assert(mobile.scrollWidth <= mobile.viewport[0], "移动端页面出现横向滚动");
  assert(mobile.bubblesInside, "移动端消息气泡超出视口");
  assert(mobile.composerVisible, "移动端输入区不在视口内");
  assert(mobile.quickDisplay === "flex", "移动端快捷指令未切换为横向布局");
  await screenshot(cdp, "mobile.png");

  // A server restart invalidates in-memory sessions. The first message after
  // that should transparently create a session and be retried.
  await evaluate(cdp, `localStorage.setItem("finagent_session_id", "SESSION-invalid-restart"); location.reload()`);
  await waitFor(cdp, `document.readyState === "complete"`);
  await waitFor(cdp, `document.querySelector("#session-id")?.textContent !== "正在创建"`);
  await sendPrompt(cdp, "产品推荐", "第 1/7 题");

  // A new session should also expose the drag/upload channel.
  await evaluate(cdp, `localStorage.removeItem("finagent_session_id"); location.reload()`);
  await waitFor(cdp, `document.readyState === "complete"`);
  await waitFor(cdp, `document.querySelector("#session-id")?.textContent !== "正在创建"`);
  const documentNode = await cdp.send("DOM.getDocument", { depth: -1 });
  const fileNode = await cdp.send("DOM.querySelector", {
    nodeId: documentNode.root.nodeId,
    selector: "#file-input",
  });
  await cdp.send("DOM.setFileInputFiles", {
    nodeId: fileNode.nodeId,
    files: [join(process.cwd(), "samples", "bill_sample.txt")],
  });
  await waitFor(
    cdp,
    `[...document.querySelectorAll(".message.agent .bubble")].some((node) => node.textContent.includes("识别方式"))`,
    15_000,
  );

  console.log(JSON.stringify({
    ok: true,
    desktop,
    mobile,
    staleSessionRetry: true,
    fileUpload: true,
    accessPasswordTested: Boolean(accessPassword),
    artifacts: outDir,
  }, null, 2));
  await cdp.send("Browser.close");
} finally {
  if (browser.exitCode === null) browser.kill();
  await sleep(200);
  rmSync(profileDir, { recursive: true, force: true });
}
