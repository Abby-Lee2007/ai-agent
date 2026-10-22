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
  await evaluate(cdp, `document.querySelector("#profile-settings-button").click()`);
  await waitFor(cdp, `document.querySelector("#profile-settings-overlay")?.classList.contains("show")`);
  await waitFor(cdp, `document.querySelector('[name="name"]')?.value === "陈晓明"`);
  const profilePanel = await evaluate(cdp, `(() => ({
    name: document.querySelector('[name="name"]')?.value,
    carePeople: document.querySelectorAll(".care-person").length,
    firstCareName: document.querySelector('.care-person [data-field="name"]')?.value,
    careContactFields: ["phone", "wechat", "alipay", "bank_card"].every(
      (field) => Boolean(document.querySelector(\`.care-person [data-field="\${field}"]\`))
    ),
    careContactLabels: [...document.querySelectorAll(".care-person .settings-field")]
      .map((label) => label.textContent)
      .filter((text) => ["手机号", "微信号", "支付宝账号", "银行卡号"].some((name) => text.includes(name)))
      .length,
    cardEditors: document.querySelectorAll(".card-editor:not(.care-person)").length,
    cardDeletes: document.querySelectorAll(".card-editor:not(.care-person) .history-delete").length,
    cardDeleteText: document.querySelector(".card-editor:not(.care-person) .history-delete")?.textContent,
    deleteAtRight: (() => {
      const actions = document.querySelector(".card-editor:not(.care-person) .settings-actions");
      const button = actions?.querySelector(".history-delete");
      if (!actions || !button) return false;
      return Math.abs(actions.getBoundingClientRect().right - button.getBoundingClientRect().right) <= 1;
    })(),
    profileCardsGap: (() => {
      const save = document.querySelector(".profile-save-row");
      const cards = document.querySelector(".cards-settings-section");
      if (!save || !cards) return 0;
      return cards.getBoundingClientRect().top - save.getBoundingClientRect().bottom;
    })()
  }))()`);
  assert(profilePanel.name === "陈晓明", "个人资料没有加载姓名");
  assert(profilePanel.carePeople >= 1, "关心的人没有从预设资料生成");
  assert(profilePanel.firstCareName, "关心的人姓名没有加载");
  assert(profilePanel.careContactFields, "关心的人缺少转账联系方式输入框");
  assert(profilePanel.careContactLabels === 4, "关心的人转账联系方式字段名不完整");
  assert(profilePanel.cardEditors >= 3, "银行卡设置没有加载预设卡片");
  assert(profilePanel.cardDeletes >= 3, "银行卡设置没有删除按钮");
  assert(profilePanel.cardDeleteText === "删除这张卡", "银行卡删除文案不正确");
  assert(profilePanel.deleteAtRight, "删除这张卡没有位于操作行最右侧");
  assert(profilePanel.profileCardsGap >= 20, "保存偏好与银行卡栏之间的间距不足");
  await evaluate(cdp, `(() => {
    const person = document.querySelector(".care-person");
    const values = {
      phone: "13800138000",
      wechat: "ui_smoke_wechat",
      alipay: "ui_smoke@example.com",
      bank_card: "6222020000000007",
    };
    Object.entries(values).forEach(([field, value]) => {
      const input = person?.querySelector(\`[data-field="\${field}"]\`);
      if (input) input.value = value;
    });
    document.querySelector("#profile-settings-form").requestSubmit();
    return true;
  })()`);
  await waitFor(
    cdp,
    `document.querySelector("#profile-settings-status")?.textContent.includes("已保存")`,
  );
  const savedContacts = await evaluate(cdp, `(async () => {
    const response = await fetchWithAuth(
      \`/v1/profile?session_id=\${encodeURIComponent(sessionId)}\`
    );
    const payload = await response.json();
    return payload.preferences?.care_people?.[0] || {};
  })()`);
  assert(savedContacts.phone === "13800138000", "联系人手机号没有保存");
  assert(savedContacts.wechat === "ui_smoke_wechat", "联系人微信号没有保存");
  assert(savedContacts.alipay === "ui_smoke@example.com", "联系人支付宝没有保存");
  assert(savedContacts.bank_card_masked === "**** **** **** 0007", "联系人银行卡号没有保存");
  await evaluate(cdp, `document.querySelector("#profile-settings-close").click()`);
  await sendPrompt(cdp, "分析我这个月的账单", "消费分类排行");
  const tableLayout = await evaluate(cdp, `(() => {
    const bubbles = [...document.querySelectorAll(".message.agent .bubble")];
    const bubble = bubbles.at(-1);
    const content = bubble?.querySelector(".formatted-response");
    const rect = bubble?.getBoundingClientRect();
    return {
      formatted: Boolean(content),
      wide: bubble?.classList.contains("wide"),
      scrollWidth: content?.scrollWidth || 0,
      clientWidth: content?.clientWidth || 0,
      insideViewport: rect ? rect.left >= 0 && rect.right <= innerWidth + 1 : false
    };
  })()`);
  assert(tableLayout.formatted, "账单分析没有使用表格排版");
  assert(tableLayout.wide, "账单分析没有使用宽消息气泡");
  assert(tableLayout.scrollWidth >= tableLayout.clientWidth, "账单表格滚动宽度异常");
  assert(tableLayout.insideViewport, "账单分析气泡超出视口");
  await sendPrompt(cdp, "帮我看看有没有异常交易", "高危");
  const anomalyLayout = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    const items = [...(bubble?.querySelectorAll(".anomaly-item") || [])];
    const content = bubble?.querySelector(".bubble-content");
    const rect = bubble?.getBoundingClientRect();
    return {
      count: items.length,
      text: content?.textContent || "",
      hasRiskSummary: Boolean(bubble?.querySelector(".anomaly-summary")),
      hasActionHeading: Boolean(bubble?.querySelector(".anomaly-action-heading")),
      hasSuggestion: items.some((item) => item.querySelector(".anomaly-suggestion")?.textContent.includes("建议")),
      allItemsComplete: items.every((item) => {
        const reason = item.querySelector(".anomaly-reason");
        const suggestion = item.querySelector(".anomaly-suggestion");
        return Boolean(reason?.textContent.trim())
          && Boolean(suggestion?.textContent.trim())
          && reason.scrollHeight <= reason.clientHeight + 1
          && suggestion.scrollHeight <= suggestion.clientHeight + 1;
      }),
      horizontalScroll: bubble ? bubble.scrollWidth > bubble.clientWidth + 1 : true,
      insideViewport: rect ? rect.left >= 0 && rect.right <= innerWidth + 1 : false
    };
  })()`);
  assert(anomalyLayout.count > 0, "异常交易没有生成结构化列表");
  assert(/共发现 \d+ 笔值得注意的交易/.test(anomalyLayout.text), "异常交易缺少完整结果摘要");
  assert(anomalyLayout.hasRiskSummary, "异常交易缺少风险总览");
  assert(anomalyLayout.hasActionHeading, "异常交易缺少处理建议区标题");
  assert(anomalyLayout.hasSuggestion, "异常交易列表缺少处理建议");
  assert(anomalyLayout.allItemsComplete, "异常交易原因或建议被裁切");
  assert(!anomalyLayout.horizontalScroll, "异常交易列表仍然依赖横向滚动");
  assert(anomalyLayout.insideViewport, "异常交易列表超出视口");
  await screenshot(cdp, "desktop.png");

  await openChat(cdp, 390, 844, true);
  await sendPrompt(cdp, "我有哪些卡", "信用卡");
  await sendPrompt(cdp, "帮我看看有没有异常交易", "高危");
  const mobileAnomaly = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    const items = [...(bubble?.querySelectorAll(".anomaly-item") || [])];
    const content = bubble?.querySelector(".bubble-content");
    return {
      count: items.length,
      allInsideBubble: items.every((item) => {
        const itemRect = item.getBoundingClientRect();
        const contentRect = content?.getBoundingClientRect();
        return contentRect
          && itemRect.left >= contentRect.left - 1
          && itemRect.right <= contentRect.right + 1
          && item.scrollWidth <= item.clientWidth + 1;
      }),
      allReasonsVisible: items.every((item) => {
        const reason = item.querySelector(".anomaly-reason");
        return reason && reason.scrollHeight <= reason.clientHeight + 1;
      })
    };
  })()`);
  assert(mobileAnomaly.count > 0, "移动端异常交易没有生成结构化列表");
  assert(mobileAnomaly.allInsideBubble, "移动端异常交易卡片超出消息气泡");
  assert(mobileAnomaly.allReasonsVisible, "移动端异常交易原因被裁切");
  await sendPrompt(cdp, "分析我这个月的账单", "消费分类排行");
  const mobileBill = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    const content = bubble?.querySelector(".formatted-response");
    const rect = bubble?.getBoundingClientRect();
    return {
      formatted: Boolean(content),
      insideViewport: rect ? rect.left >= 0 && rect.right <= innerWidth + 1 : false,
      canScroll: content ? content.scrollWidth >= content.clientWidth : false
    };
  })()`);
  assert(mobileBill.formatted, "移动端账单分析没有使用表格排版");
  assert(mobileBill.insideViewport, "移动端账单分析气泡超出视口");
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
    mobileBill,
    profileSettings: profilePanel,
    savedContacts,
    billTable: tableLayout,
    anomalyTable: anomalyLayout,
    mobileAnomaly,
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
