import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const url = process.argv[2] || "http://127.0.0.1:8000";
const debugPort = 9232;
const profileDir = mkdtempSync(join(tmpdir(), "finagent-theme-"));
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

async function state(cdp) {
  return evaluate(cdp, `(() => {
    const selector = document.querySelector("#theme-select");
    const bats = document.querySelector(".halloween-bats");
    const classicMark = document.querySelector(".brand-mark-classic");
    const halloweenMark = document.querySelector(".brand-mark-halloween");
    const classicAvatar = document.querySelector(".message.agent .avatar-classic");
    const halloweenAvatar = document.querySelector(".message.agent .avatar-halloween");
    return {
      value: selector?.value || "",
      options: [...(selector?.options || [])].map((option) => option.value),
      activeTheme: document.documentElement.dataset.theme || "",
      halloweenClass: document.documentElement.classList.contains("theme-halloween"),
      stored: localStorage.getItem("finagent_theme"),
      batsVisible: bats ? getComputedStyle(bats).display !== "none" : false,
      classicMarkVisible: classicMark ? getComputedStyle(classicMark).display !== "none" : false,
      halloweenMarkVisible: halloweenMark ? getComputedStyle(halloweenMark).display !== "none" : false,
      classicAvatarVisible: classicAvatar ? getComputedStyle(classicAvatar).display !== "none" : false,
      halloweenAvatarVisible: halloweenAvatar ? getComputedStyle(halloweenAvatar).display !== "none" : false,
      pageFits: document.documentElement.scrollWidth <= innerWidth
    };
  })()`);
}

try {
  await waitForDebugger();
  const cdp = await connect();
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Page.navigate", { url });
  await waitFor(cdp, `document.querySelector("#theme-select") !== null`);

  const initial = await state(cdp);
  assert(initial.options.join(",") === "classic,halloween", "主题选项不完整");
  assert(initial.value === "classic", "默认主题不是经典");
  assert(initial.activeTheme === "classic", "默认主题状态错误");
  assert(!initial.halloweenClass, "默认不应启用万圣节主题");
  assert(!initial.batsVisible, "经典主题下仍显示万圣节装饰");
  assert(initial.classicMarkVisible && !initial.halloweenMarkVisible, "品牌标记未切换为经典");
  assert(initial.classicAvatarVisible && !initial.halloweenAvatarVisible, "头像未切换为经典");

  await evaluate(cdp, `(() => {
    const selector = document.querySelector("#theme-select");
    selector.value = "halloween";
    selector.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  })()`);
  const halloween = await state(cdp);
  assert(halloween.activeTheme === "halloween", "万圣节主题未启用");
  assert(halloween.halloweenClass, "万圣节主题类缺失");
  assert(halloween.stored === "halloween", "万圣节主题未保存");
  assert(halloween.batsVisible, "万圣节主题装饰未显示");
  assert(!halloween.classicMarkVisible && halloween.halloweenMarkVisible, "万圣节品牌标记未显示");
  assert(!halloween.classicAvatarVisible && halloween.halloweenAvatarVisible, "万圣节头像未显示");

  await cdp.send("Page.reload", { ignoreCache: true });
  await waitFor(cdp, `document.querySelector("#theme-select") !== null`);
  const reloaded = await state(cdp);
  assert(reloaded.value === "halloween", "刷新后没有保留万圣节皮肤");
  assert(reloaded.halloweenClass && reloaded.batsVisible, "刷新后万圣节皮肤未恢复");

  await evaluate(cdp, `(() => {
    const selector = document.querySelector("#theme-select");
    selector.value = "classic";
    selector.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  })()`);
  const classic = await state(cdp);
  assert(classic.activeTheme === "classic", "无法卸载万圣节皮肤");
  assert(classic.stored === "classic", "经典皮肤选择未保存");
  assert(!classic.batsVisible, "卸载后仍有万圣节装饰");
  assert(classic.classicAvatarVisible, "卸载后头像没有恢复经典样式");
  assert(classic.pageFits, "皮肤切换导致横向滚动");

  console.log(JSON.stringify({
    ok: true,
    initial,
    halloween,
    reloaded,
    classic,
  }, null, 2));
  await cdp.send("Browser.close");
} finally {
  if (browser.exitCode === null) browser.kill();
  await sleep(200);
  rmSync(profileDir, { recursive: true, force: true });
}
