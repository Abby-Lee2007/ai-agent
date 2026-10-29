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
  await waitFor(cdp, `document.querySelectorAll(".capability-category").length === 6`);
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
      capabilityLabels: [...document.querySelectorAll(".capability-category")].map(
        (button) => button.textContent.replace(/\s+/g, " ").trim()
      ),
      globalActions: [...document.querySelectorAll(".capability-global-action")].map(
        (button) => button.textContent.trim()
      ),
      expandedCategories: [...document.querySelectorAll(".capability-category")]
        .filter((button) => button.getAttribute("aria-expanded") === "true")
        .map((button) => button.textContent.trim()),
    };
  })()`);
  assert(desktop.title === "AI Banking Agent", "H5 标题不正确");
  assert(desktop.scrollWidth <= desktop.viewport[0], "桌面页面出现横向滚动");
  assert(desktop.composerOverlap === false, "输入框与发送按钮重叠");
  assert(desktop.capabilityLabels.length === 6, "业务分类数量不是 6 个");
  assert(desktop.capabilityLabels[0].includes("智能转账"), "缺少智能转账分类");
  assert(desktop.capabilityLabels[5].includes("人际礼物"), "缺少人际礼物分类");
  assert(desktop.globalActions.includes("每日简报"), "缺少全局每日简报入口");
  assert(desktop.expandedCategories.length === 0, "默认界面不应展开任何业务分类");
  const shortcutPrompts = await evaluate(cdp, `(async () => {
    const response = await fetchWithAuth("/v1/capabilities");
    const payload = await response.json();
    return [
      ...payload.categories.flatMap((category) => category.actions),
      ...payload.global_actions
    ]
      .filter((action) => action.input_mode === "prompt")
      .map((action) => ({
        id: action.id,
        label: action.label,
        prompt: action.prompt
      }));
  })()`);
  assert(shortcutPrompts.length > 0, "没有加载可发送的快捷指令");
  assert(
    shortcutPrompts.every((action) => action.prompt === action.label),
    "快捷指令仍带有具体人、金额或平台示例",
  );
  await evaluate(cdp, `(() => {
    const button = [...document.querySelectorAll(".capability-category")]
      .find((item) => item.textContent.includes("智能转账"));
    if (button?.getAttribute("aria-expanded") !== "true") button?.click();
    return Boolean(button);
  })()`);
  await waitFor(
    cdp,
    `[...document.querySelectorAll(".capability-action")].some(
      (button) => button.textContent.trim() === "给他人转账"
    )`,
  );
  await evaluate(cdp, `(() => {
    const button = [...document.querySelectorAll(".capability-action")]
      .find((item) => item.textContent.trim() === "给他人转账");
    button?.click();
    return Boolean(button);
  })()`);
  await waitFor(
    cdp,
    `[...document.querySelectorAll(".message.agent .bubble")].at(-1)?.textContent.includes("转给谁")`,
  );
  const transferShortcut = await evaluate(cdp, `(() => ({
    userText: [...document.querySelectorAll(".message.user .bubble")].at(-1)?.textContent.trim(),
    agentText: [...document.querySelectorAll(".message.agent .bubble")].at(-1)?.textContent || ""
  }))()`);
  assert(transferShortcut.userText === "给他人转账", "转账快捷指令没有只发送卡片简称");
  assert(!transferShortcut.agentText.includes("转给他人"), "转账快捷指令把“他人”误当成收款人");
  await sendPrompt(cdp, "取消", "本次操作已取消");
  await evaluate(cdp, `(() => {
    const button = [...document.querySelectorAll(".capability-category")]
      .find((item) => item.textContent.includes("人际礼物"));
    button?.click();
    return Boolean(button);
  })()`);
  await waitFor(
    cdp,
    `[...document.querySelectorAll(".capability-action")].some(
      (button) => button.textContent.trim() === "生日惊喜"
    )`,
  );
  await evaluate(cdp, `(() => {
    const button = [...document.querySelectorAll(".capability-action")]
      .find((item) => item.textContent.trim() === "生日惊喜");
    button?.click();
    return Boolean(button);
  })()`);
  await waitFor(
    cdp,
    `[...document.querySelectorAll(".message.agent .bubble")].at(-1)?.textContent.includes("生日惊喜要送给谁")`,
  );
  const birthdayShortcut = await evaluate(cdp, `(() => ({
    userText: [...document.querySelectorAll(".message.user .bubble")].at(-1)?.textContent.trim(),
    agentText: [...document.querySelectorAll(".message.agent .bubble")].at(-1)?.textContent || ""
  }))()`);
  assert(birthdayShortcut.userText === "生日惊喜", "生日惊喜快捷指令没有只发送卡片简称");
  assert(!birthdayShortcut.agentText.includes("老婆"), "生日惊喜仍预设了妻子");
  assert(!birthdayShortcut.agentText.includes("对象"), "生日惊喜仍预设了对象");
  await sendPrompt(cdp, "老师", "生日");
  await sendPrompt(cdp, "11月20日", "礼物偏好");
  await sendPrompt(cdp, "没有", "过敏");
  await sendPrompt(cdp, "对花粉和奶制品过敏", "预算");
  await sendPrompt(cdp, "70", "没有可预约礼物");
  await sendPrompt(cdp, "预算1000，没有禁忌", "选择方案 1");
  const giftLayout = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    const grid = bubble?.querySelector(".gift-option-grid");
    const cards = [...(bubble?.querySelectorAll(".gift-option-card") || [])];
    const taobaoLinks = cards
      .map((card) => card.querySelector(".gift-taobao-link"))
      .filter(Boolean);
    const actionBlocks = cards
      .map((card) => card.querySelector(".gift-option-actions"))
      .filter(Boolean);
    const cardHeights = cards.map((card) => card.getBoundingClientRect().height);
    const actionBottoms = actionBlocks.map(
      (actions) => actions.getBoundingClientRect().bottom
    );
    const bubbleRect = bubble?.getBoundingClientRect();
    return {
      count: cards.length,
      hasSummary: Boolean(bubble?.querySelector(".gift-summary")),
      selectButtonCount: cards.filter((card) => card.querySelector(".gift-option-select")).length,
      pricePendingCount: cards.filter(
        (card) => card.querySelector(".gift-option-price")?.textContent === "价格待核实"
      ).length,
      taobaoLinkCount: taobaoLinks.length,
      taobaoLinksSafe: taobaoLinks.every((link) =>
        link.target === "_blank"
        && link.rel.includes("noopener")
        && link.rel.includes("noreferrer")
      ),
      taobaoQueries: taobaoLinks.map((link) =>
        new URL(link.href).searchParams.get("q") || ""
      ),
      taobaoHosts: taobaoLinks.map((link) => new URL(link.href).hostname),
      priceCaptions: cards.map(
        (card) => card.querySelector(".gift-price-caption")?.textContent || ""
      ),
      uniformCardHeight: cardHeights.length > 0
        && Math.max(...cardHeights) - Math.min(...cardHeights) <= 1,
      alignedActions: actionBottoms.length > 0
        && Math.max(...actionBottoms) - Math.min(...actionBottoms) <= 1,
      columns: grid ? getComputedStyle(grid).gridTemplateColumns.split(" ").length : 0,
      allInside: Boolean(bubbleRect) && cards.every((card) => {
        const rect = card.getBoundingClientRect();
        return rect.left >= bubbleRect.left - 1 && rect.right <= bubbleRect.right + 1;
      })
    };
  })()`);
  assert(giftLayout.count === 3, "生日礼物候选没有渲染 3 张方案卡");
  assert(giftLayout.hasSummary, "生日礼物候选缺少条件摘要");
  assert(giftLayout.selectButtonCount === 3, "礼物方案缺少选择按钮");
  assert(giftLayout.pricePendingCount === 0, "目录内礼物方案不应显示价格待核实");
  assert(giftLayout.taobaoLinkCount === 3, "生日礼物方案缺少淘宝搜索入口");
  assert(giftLayout.taobaoLinksSafe, "淘宝搜索链接缺少安全窗口属性");
  assert(giftLayout.taobaoHosts.every((host) => host === "s.taobao.com"), "淘宝搜索链接域名不正确");
  assert(
    giftLayout.priceCaptions.every((caption) => ["方案价", "市场参考价"].includes(caption)),
    "礼物卡价格标签不统一",
  );
  assert(giftLayout.uniformCardHeight, "礼物方案卡高度不一致");
  assert(giftLayout.alignedActions, "礼物方案卡底部按钮没有对齐");
  assert(
    giftLayout.taobaoQueries.every((query) =>
      query.includes("生日礼物")
      && !query.includes("老师")
      && !query.includes("没有禁忌")
      && !query.includes("沿用资料")
    ),
    "淘宝搜索链接传递了不必要的个人条件",
  );
  assert(giftLayout.columns === 3, "桌面端礼物方案没有按三列排版");
  assert(giftLayout.allInside, "礼物方案卡超出消息气泡");
  await screenshot(cdp, "gift-options.png");
  await waitFor(
    cdp,
    `document.querySelector("#service-status")?.textContent === "接口就绪"
      && document.querySelector("#send-button")?.disabled === false`,
  );
  await evaluate(cdp, `(() => {
    const button = [...document.querySelectorAll(".capability-category")]
      .find((item) => item.textContent.includes("智能转账"));
    button?.click();
    return Boolean(button);
  })()`);
  await waitFor(
    cdp,
    `[...document.querySelectorAll(".capability-action")].some(
      (button) => button.textContent.trim() === "给他人转账"
    )`,
  );
  await evaluate(cdp, `(() => {
    const button = [...document.querySelectorAll(".capability-action")]
      .find((item) => item.textContent.trim() === "给他人转账");
    button?.click();
    return Boolean(button);
  })()`);
  await waitFor(
    cdp,
    `[...document.querySelectorAll(".message.agent .bubble")].at(-1)?.textContent.includes("转给谁")`,
  );
  const giftChoiceInterrupted = await evaluate(cdp, `(() => ({
    userText: [...document.querySelectorAll(".message.user .bubble")].at(-1)?.textContent.trim(),
    interfaceText: document.querySelector("#service-status")?.textContent
  }))()`);
  assert(giftChoiceInterrupted.userText === "给他人转账", "待选礼物状态下其他快捷指令被锁住");
  assert(giftChoiceInterrupted.interfaceText === "接口就绪", "待选礼物切换后界面仍处于忙碌状态");
  await sendPrompt(cdp, "取消", "本次操作已取消");
  await evaluate(cdp, `(() => {
    const button = [...document.querySelectorAll(".capability-category")]
      .find((item) => item.textContent.includes("人际礼物"));
    button?.click();
    return Boolean(button);
  })()`);
  await waitFor(
    cdp,
    `[...document.querySelectorAll(".capability-action")].some(
      (button) => button.textContent.trim() === "伴手礼/礼物预约"
    )`,
  );
  await evaluate(cdp, `(() => {
    const button = [...document.querySelectorAll(".capability-action")]
      .find((item) => item.textContent.trim() === "伴手礼/礼物预约");
    button?.click();
    return Boolean(button);
  })()`);
  await waitFor(
    cdp,
    `[...document.querySelectorAll(".message.agent .bubble")].at(-1)?.textContent.includes("这份礼物要送给谁")`,
  );
  const souvenirShortcut = await evaluate(cdp, `(() => ({
    userText: [...document.querySelectorAll(".message.user .bubble")].at(-1)?.textContent.trim(),
    agentText: [...document.querySelectorAll(".message.agent .bubble")].at(-1)?.textContent || ""
  }))()`);
  assert(souvenirShortcut.userText === "伴手礼/礼物预约", "伴手礼快捷指令没有只发送卡片简称");
  assert(!souvenirShortcut.agentText.includes("对象"), "伴手礼仍预设了对象");
  await sendPrompt(cdp, "妹妹", "什么礼物");
  await sendPrompt(cdp, "鲜花饼", "日期");
  const giftItemState = await evaluate(cdp, `(() => ({
    agentText: [...document.querySelectorAll(".message.agent .bubble")].at(-1)?.textContent || "",
    interfaceText: document.querySelector("#service-status")?.textContent
  }))()`);
  assert(giftItemState.agentText.includes("日期"), "礼物名称没有进入日期询问");
  assert(!giftItemState.agentText.includes("生日惊喜要送给谁"), "礼物名称被误判为生日意图");
  assert(giftItemState.interfaceText === "接口就绪", "礼物名称提交后界面仍处于忙碌状态");
  await sendPrompt(cdp, "取消", "本次操作已取消");
  await evaluate(cdp, `(() => {
    const button = [...document.querySelectorAll(".capability-category")]
      .find((item) => item.textContent.includes("订阅管理"));
    button?.click();
    return Boolean(button);
  })()`);
  await waitFor(
    cdp,
    `[...document.querySelectorAll(".capability-action")].some((button) => button.textContent.includes("自动续费检测"))`,
  );
  await evaluate(cdp, `(() => {
    const button = [...document.querySelectorAll(".capability-category")]
      .find((item) => item.textContent.includes("订阅管理"));
    button?.click();
    return Boolean(button);
  })()`);
  await waitFor(
    cdp,
    `(() => {
      const button = [...document.querySelectorAll(".capability-category")]
        .find((item) => item.textContent.includes("订阅管理"));
      const panel = button?.closest(".capability-group")?.querySelector(".capability-actions");
      return button?.getAttribute("aria-expanded") === "false" && panel?.hidden === true;
    })()`,
  );
  const collapsedSubscription = await evaluate(cdp, `(() => {
    const button = [...document.querySelectorAll(".capability-category")]
      .find((item) => item.textContent.includes("订阅管理"));
    return {
      expanded: button?.getAttribute("aria-expanded"),
      actionCount: button
        ?.closest(".capability-group")
        ?.querySelectorAll(".capability-action").length || 0
    };
  })()`);
  assert(collapsedSubscription.expanded === "false", "重复点击分类后没有收起");
  assert(collapsedSubscription.actionCount === 0, "分类收起后操作列表仍然存在");
  await evaluate(cdp, `(() => {
    const button = [...document.querySelectorAll(".capability-category")]
      .find((item) => item.textContent.includes("订阅管理"));
    button?.click();
    return Boolean(button);
  })()`);
  await waitFor(
    cdp,
    `[...document.querySelectorAll(".capability-action")].some((button) => button.textContent.includes("自动续费检测"))`,
  );
  const categoryExpansion = await evaluate(cdp, `(() => {
    const category = [...document.querySelectorAll(".capability-category")]
      .find((button) => button.textContent.includes("订阅管理"));
    const group = category?.closest(".capability-group");
    const panel = group?.querySelector(".capability-actions");
    const categoryRect = category?.getBoundingClientRect();
    const panelRect = panel?.getBoundingClientRect();
    return {
      nested: Boolean(group && panel && group.contains(panel)),
      directlyBelow: Boolean(categoryRect && panelRect)
        && panelRect.top >= categoryRect.bottom
        && panelRect.top - categoryRect.bottom <= 14
    };
  })()`);
  assert(categoryExpansion.nested, "分类操作没有嵌套在对应分类中");
  assert(categoryExpansion.directlyBelow, "分类操作没有在对应分类下方展开");
  const subscriptionActions = await evaluate(cdp, `[...document.querySelectorAll(".capability-action")].map((button) => button.textContent.trim())`);
  assert(subscriptionActions.includes("自动续费检测"), "订阅分类缺少自动续费检测");
  assert(subscriptionActions.includes("会员办理/充值"), "订阅分类缺少会员办理");
  await evaluate(cdp, `(() => {
    const button = [...document.querySelectorAll(".capability-action")]
      .find((item) => item.textContent.includes("自动续费检测"));
    button.click();
    return true;
  })()`);
  await waitFor(cdp, `document.querySelectorAll(".subscription-table").length > 0`, 15_000);
  await waitFor(cdp, `!document.querySelector("#typing-message")`);
  const subscriptionTable = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    const table = bubble?.querySelector(".subscription-table");
    const wrapper = bubble?.querySelector(".subscription-table-wrap");
    const rows = [...(table?.querySelectorAll("tbody tr") || [])];
    const headers = [...(table?.querySelectorAll("thead th") || [])].map((cell) => cell.textContent.trim());
    const columnPositions = rows.map((row) =>
      [...row.querySelectorAll("td")].map((cell) => Math.round(cell.getBoundingClientRect().left))
    );
    const rowHeights = rows.map((row) => Math.round(row.getBoundingClientRect().height));
    const cellHeightsConsistent = rows.every((row) => {
      const rowHeight = row.getBoundingClientRect().height;
      return [...row.querySelectorAll("td")].every(
        (cell) => Math.abs(cell.getBoundingClientRect().height - rowHeight) <= 1
      );
    });
    const firstRow = rows[0];
    const amountCell = firstRow?.querySelector("td:nth-child(2)");
    const bubbleRect = bubble?.getBoundingClientRect();
    return {
      structured: Boolean(table),
      rowCount: rows.length,
      headers,
      cellCounts: rows.map((row) => row.querySelectorAll("td").length),
      columnsConsistent: columnPositions.length > 1
        && columnPositions.every((positions) =>
          positions.every((left, index) => Math.abs(left - columnPositions[0][index]) <= 1)
        ),
      rowHeights,
      uniformRowHeights: rowHeights.length > 1
        && Math.max(...rowHeights) - Math.min(...rowHeights) <= 1,
      cellHeightsConsistent,
      amountRightAligned: amountCell ? getComputedStyle(amountCell).textAlign === "right" : false,
      bubbleInsideViewport: bubbleRect
        ? bubbleRect.left >= 0 && bubbleRect.right <= innerWidth + 1
        : false,
      hasScrollWrapper: Boolean(wrapper),
      fitsDesktop: Boolean(wrapper) && table.scrollWidth <= wrapper.clientWidth + 1,
      pageFits: document.documentElement.scrollWidth <= innerWidth
    };
  })()`);
  assert(subscriptionTable.structured, "自动续费结果没有生成结构化表格");
  assert(subscriptionTable.rowCount > 0, "自动续费表格没有数据行");
  assert(subscriptionTable.headers.includes("到期时间"), "自动续费表格缺少到期时间列");
  assert(subscriptionTable.headers.includes("自动续费"), "自动续费表格缺少自动续费列");
  assert(subscriptionTable.cellCounts.every((count) => count === 6), "自动续费表格列数不一致");
  assert(subscriptionTable.columnsConsistent, "自动续费表格各行列未对齐");
  assert(subscriptionTable.uniformRowHeights, "自动续费表格行高不一致");
  assert(subscriptionTable.cellHeightsConsistent, "自动续费列单元格未填满行高");
  assert(subscriptionTable.amountRightAligned, "自动续费金额列没有右对齐");
  assert(subscriptionTable.bubbleInsideViewport, "自动续费表格气泡超出桌面视口");
  assert(subscriptionTable.hasScrollWrapper, "自动续费表格缺少横向滚动容器");
  assert(subscriptionTable.fitsDesktop, "桌面端自动续费表格不应依赖横向滚动");
  assert(subscriptionTable.pageFits, "自动续费表格导致页面横向滚动");
  await screenshot(cdp, "desktop-subscriptions.png");
  const qqExpiryBefore = await evaluate(cdp, `(() => {
    const bubbles = [...document.querySelectorAll(".message.agent .bubble")];
    const bubble = [...bubbles].reverse().find((item) => item.querySelector(".subscription-table"));
    const row = [...(bubble?.querySelectorAll(".subscription-table tbody tr") || [])]
      .find((item) => item.textContent.includes("QQ音乐绿钻"));
    return row?.querySelector("td:nth-child(3)")?.textContent.trim() || "";
  })()`);
  assert(qqExpiryBefore === "2026-11-12", "QQ音乐购买前到期时间不正确");
  await sendPrompt(cdp, "充一个QQ音乐的年度会员", "请补充本次购买价格");
  await sendPrompt(cdp, "148元", "自动续费");
  await sendPrompt(cdp, "不自动续费", "会员充值订单");
  await sendPrompt(cdp, "确认", "会员充值已执行");
  await sendPrompt(cdp, "帮我看看有哪些自动续费", "共识别到");
  const qqExpiryAfter = await evaluate(cdp, `(() => {
    const bubbles = [...document.querySelectorAll(".message.agent .bubble")];
    const bubble = [...bubbles].reverse().find((item) => item.querySelector(".subscription-table"));
    const row = [...(bubble?.querySelectorAll(".subscription-table tbody tr") || [])]
      .find((item) => item.textContent.includes("QQ音乐绿钻"));
    return {
      expiry: row?.querySelector("td:nth-child(3)")?.textContent.trim() || "",
      autoRenew: row?.querySelector(".subscription-renewal")?.textContent.trim() || ""
    };
  })()`);
  assert(qqExpiryAfter.expiry === "2027-11-12", "购买年会后检测结果未刷新到期时间");
  assert(qqExpiryAfter.autoRenew === "否", "购买一次性年会后自动续费状态不正确");
  await sendPrompt(cdp, "取消腾讯视频会员", "腾讯视频VIP");
  await sendPrompt(cdp, "确认", "已取消");
  await sendPrompt(cdp, "我的订阅都有什么", "已取消");
  const resumeAction = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    const rows = [...(bubble?.querySelectorAll(".subscription-table tbody tr") || [])];
    const row = rows.find((item) => item.textContent.includes("腾讯视频VIP"));
    const button = row?.querySelector(".subscription-resume");
    return {
      hasButton: Boolean(button),
      buttonText: button?.textContent.trim() || "",
      cancellationVisible: row?.textContent.includes("已取消") || false
    };
  })()`);
  assert(resumeAction.hasButton, "已取消订阅缺少恢复续费按钮");
  assert(resumeAction.buttonText === "恢复续费", "恢复续费按钮文案不正确");
  assert(resumeAction.cancellationVisible, "已取消订阅状态没有显示");
  await evaluate(cdp, `(() => {
    const rows = [...document.querySelectorAll(".message.agent .subscription-table tbody tr")];
    const row = rows.reverse().find((item) => item.textContent.includes("腾讯视频VIP"));
    row?.querySelector(".subscription-resume")?.click();
    return true;
  })()`);
  await waitFor(
    cdp,
    `[...document.querySelectorAll(".message.agent .bubble")].at(-1)?.textContent.includes("恢复「腾讯视频VIP」")`,
  );
  await sendPrompt(cdp, "确认", "已恢复");
  await sendPrompt(cdp, "我的订阅都有什么", "自动续费");
  const resumedSubscription = await evaluate(cdp, `(() => {
    const bubbles = [...document.querySelectorAll(".message.agent .bubble")];
    const bubble = [...bubbles].reverse().find((item) => item.querySelector(".subscription-table"));
    const rows = [...(bubble?.querySelectorAll(".subscription-table tbody tr") || [])];
    const row = rows.find((item) => item.textContent.includes("腾讯视频VIP"));
    return {
      found: Boolean(row),
      active: row?.textContent.includes("生效中") || false,
      autoRenew: row?.querySelector(".subscription-renewal")?.textContent.trim() === "是",
      hasResumeButton: Boolean(row?.querySelector(".subscription-resume"))
    };
  })()`);
  assert(resumedSubscription.found, "恢复后订阅列表缺少腾讯视频VIP");
  assert(resumedSubscription.active, "恢复后订阅状态没有变为生效中");
  assert(resumedSubscription.autoRenew, "恢复后自动续费状态没有变为是");
  assert(!resumedSubscription.hasResumeButton, "恢复后仍显示恢复续费按钮");
  await sendPrompt(cdp, "把腾讯视频改成12个月续费，续费价288元", "下期服务：12 个月");
  await sendPrompt(cdp, "确认", "下期周期：12 个月");
  await sendPrompt(cdp, "我的订阅都有什么", "订阅共");
  const pendingPlan = await evaluate(cdp, `(() => {
    const bubbles = [...document.querySelectorAll(".message.agent .bubble")];
    const bubble = [...bubbles].reverse().find((item) => item.querySelector(".subscription-table"));
    const rows = [...(bubble?.querySelectorAll(".subscription-table tbody tr") || [])];
    const row = rows.find((item) => item.textContent.includes("腾讯视频VIP"));
    const pending = row?.querySelector(".subscription-pending");
    return {
      visible: Boolean(pending),
      text: pending?.textContent.replace(/\\s+/g, " ").trim() || "",
      currentExpiryPreserved: row?.textContent.includes("2026-11-08") || false
    };
  })()`);
  assert(pendingPlan.visible, "订阅列表没有显示下期计划");
  assert(pendingPlan.text.includes("下期：12 个月"), "下期计划周期显示不正确");
  assert(pendingPlan.text.includes("288.00"), "下期计划续费价显示不正确");
  assert(pendingPlan.currentExpiryPreserved, "下期计划覆盖了当前到期时间");
  await screenshot(cdp, "desktop-subscription-resume.png");
  await sendPrompt(
    cdp,
    "帮我充腾讯视频3个月60元、Keep12个月199元",
    "请逐项确认是否默认开启自动续费",
  );
  await sendPrompt(cdp, "1自动续费，续费价25元；2不自动续费", "会员充值订单");
  const membershipReview = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    const text = bubble?.textContent || "";
    return {
      hasTwoItems: text.includes("1. 腾讯视频 3 个月") && text.includes("2. Keep 12 个月"),
      hasTotal: text.includes("259.00"),
      hasAutoChoices: text.includes("自动续费，续费价 25.00") && text.includes("不自动续费"),
      hasQuoteNotice: text.includes("未联网核验"),
      insideViewport: (() => {
        const rect = bubble?.getBoundingClientRect();
        return Boolean(rect && rect.left >= 0 && rect.right <= innerWidth + 1);
      })()
    };
  })()`);
  assert(membershipReview.hasTwoItems, "批量会员确认缺少完整项目");
  assert(membershipReview.hasTotal, "批量会员确认缺少合计金额");
  assert(membershipReview.hasAutoChoices, "批量会员确认缺少逐项自动续费设置");
  assert(membershipReview.hasQuoteNotice, "批量会员确认缺少用户报价提示");
  assert(membershipReview.insideViewport, "批量会员确认超出桌面视口");
  await sendPrompt(cdp, "取消", "没有执行任何业务动作");
  await sendPrompt(cdp, "我还有多少钱", "总资产");
  await evaluate(cdp, `document.querySelector("#profile-settings-button").click()`);
  await waitFor(cdp, `document.querySelector("#profile-settings-overlay")?.classList.contains("show")`);
  await waitFor(cdp, `document.querySelector('[name="name"]')?.value === "陈晓明"`);
  const profilePanel = await evaluate(cdp, `(() => ({
    name: document.querySelector('[name="name"]')?.value,
    carePeople: document.querySelectorAll(".care-person").length,
    firstCareName: document.querySelector('.care-person [data-field="name"]')?.value,
    careContactFields: ["phone", "wechat", "alipay", "bank_name", "bank_card"].every(
      (field) => Boolean(document.querySelector(\`.care-person [data-field="\${field}"]\`))
    ),
    careContactLabels: [...document.querySelectorAll(".care-person .settings-field")]
      .map((label) => label.textContent)
      .filter((text) => ["手机号", "微信号", "支付宝账号", "开户银行", "银行卡号"].some((name) => text.includes(name)))
      .length,
    careBankHint: document.querySelector(".care-person .bank-card-hint")?.textContent || "",
    careBankPlaceholder: document.querySelector('.care-person [data-field="bank_card"]')?.placeholder || "",
    careBankLabel: document.querySelector('.care-person [data-field="bank_card"]')
      ?.closest(".settings-field")?.textContent || "",
    ownBirthdayMax: document.querySelector('[name="birthday"]')?.max || "",
    careBirthdayMax: document.querySelector('.care-person [data-field="birthday"]')?.max || "",
    careWideFieldsAligned: (() => {
      const grid = document.querySelector(".care-person .settings-grid");
      if (!grid) return false;
      const gridWidth = grid.getBoundingClientRect().width;
      return ["bank_card", "favorite_gift", "avoid"].every((field) => {
        const label = document.querySelector(
          \`.care-person [data-field="\${field}"]\`
        )?.closest(".settings-field");
        if (!label) return false;
        const rect = label.getBoundingClientRect();
        return label.classList.contains("wide")
          && Math.abs(rect.width - gridWidth) <= 1;
      });
    })(),
    cardEditors: document.querySelectorAll(".cards-settings-section .card-editor").length,
    cardDeletes: document.querySelectorAll(".cards-settings-section .card-editor .history-delete").length,
    cardDeleteText: document.querySelector(".cards-settings-section .card-editor .history-delete")?.textContent,
    goalEditors: document.querySelectorAll(".goal-editor").length,
    goalDeletes: document.querySelectorAll(".goal-editor .goal-delete").length,
    addGoalButton: Boolean(document.querySelector("#add-goal")),
    newGoalForm: Boolean(document.querySelector("#new-goal-form")),
    firstGoalName: document.querySelector('.goal-editor [data-field="name"]')?.value || "",
    firstGoalTarget: document.querySelector('.goal-editor [data-field="target_amount"]')?.value || "",
    firstGoalLocked: document.querySelector('.goal-editor [data-field="locked_amount"]')?.value || "",
    deleteAtRight: (() => {
      const actions = document.querySelector(".cards-settings-section .card-editor .settings-actions");
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
  assert(profilePanel.careContactLabels === 5, "关心的人转账联系方式字段名不完整");
  assert(profilePanel.careBankHint.includes("16-19 位"), "银行卡号缺少格式提示");
  assert(profilePanel.careBankHint.includes("完整卡号"), "银行卡提示没有说明需要完整卡号");
  assert(profilePanel.careBankPlaceholder.includes("完整"), "银行卡输入框仍暗示只输入尾号");
  assert(profilePanel.careBankLabel.includes("完整卡号"), "银行卡字段名没有说明需要完整卡号");
  const today = await evaluate(cdp, `(() => {
    const now = new Date();
    const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 10);
  })()`);
  assert(profilePanel.ownBirthdayMax === today, "个人生日没有禁止选择未来日期");
  assert(profilePanel.careBirthdayMax === today, "关心的人生日没有禁止选择未来日期");
  assert(profilePanel.careWideFieldsAligned, "关心的人长字段没有按整行对齐");
  assert(profilePanel.cardEditors >= 3, "银行卡设置没有加载预设卡片");
  assert(profilePanel.cardDeletes >= 3, "银行卡设置没有删除按钮");
  assert(profilePanel.goalEditors >= 1, "资料页没有加载资金目标");
  assert(profilePanel.goalDeletes === profilePanel.goalEditors, "资金目标缺少删除按钮");
  assert(profilePanel.addGoalButton, "资金目标缺少新增按钮");
  assert(profilePanel.newGoalForm, "资金目标缺少新增表单");
  assert(profilePanel.firstGoalName, "资金目标名称没有加载");
  assert(profilePanel.firstGoalTarget === "8000", "资金目标金额没有加载");
  assert(profilePanel.firstGoalLocked === "8000", "资金目标锁定金额没有加载");
  assert(profilePanel.cardDeleteText === "删除这张卡", "银行卡删除文案不正确");
  assert(profilePanel.deleteAtRight, "删除这张卡没有位于操作行最右侧");
  assert(profilePanel.profileCardsGap >= 20, "保存偏好与银行卡栏之间的间距不足");
  await evaluate(cdp, `(() => {
    const goal = document.querySelector(".goal-editor");
    goal.querySelector('[data-field="name"]').value = "UI毕业旅行基金";
    goal.querySelector('[data-field="target_amount"]').value = "9000";
    goal.querySelector('[data-field="deadline"]').value = "2026-12-31";
    goal.querySelector('[data-field="status"]').value = "active";
    goal.querySelector('[data-field="note"]').value = "毕业后出发";
    goal.querySelector(".goal-save").click();
    return true;
  })()`);
  await waitFor(
    cdp,
    `document.querySelector(".goal-editor .goal-save-status")?.textContent.includes("已保存")`,
  );
  const savedGoal = await evaluate(cdp, `(async () => {
    const response = await fetchWithAuth(
      \`/v1/profile?session_id=\${encodeURIComponent(sessionId)}\`
    );
    const payload = await response.json();
    return payload.goals?.[0] || {};
  })()`);
  assert(savedGoal.name === "UI毕业旅行基金", "资金目标名称没有保存");
  assert(savedGoal.target_amount === 9000, "资金目标金额没有保存");
  assert(savedGoal.deadline === "2026-12-31", "资金目标日期没有保存");
  assert(savedGoal.note === "毕业后出发", "资金目标备注没有保存");
  assert(savedGoal.locked_amount === 8000, "锁定金额被资料页错误修改");
  await evaluate(cdp, `document.querySelector("#add-goal").click()`);
  await waitFor(cdp, `document.querySelector("#new-goal-form")?.hidden === false`);
  await evaluate(cdp, `(() => {
    const form = document.querySelector("#new-goal-form");
    form.querySelector('[data-new-goal="name"]').value = "UI新电脑基金";
    form.querySelector('[data-new-goal="target_amount"]').value = "12000";
    form.querySelector('[data-new-goal="deadline"]').value = "2027-06-01";
    form.querySelector('[data-new-goal="status"]').value = "active";
    form.querySelector('[data-new-goal="note"]').value = "毕业前攒够";
    form.querySelector(".goal-create-submit").click();
    return true;
  })()`);
  await waitFor(
    cdp,
    `document.querySelector(".goal-create-status")?.textContent.includes("已创建")`,
  );
  const createdGoal = await evaluate(cdp, `(async () => {
    const response = await fetchWithAuth(
      \`/v1/profile?session_id=\${encodeURIComponent(sessionId)}\`
    );
    const payload = await response.json();
    return payload.goals?.find((goal) => goal.name === "UI新电脑基金") || {};
  })()`);
  assert(createdGoal.goal_id, "资料页没有新增资金目标");
  assert(createdGoal.locked_amount === 0, "资料页新增目标不应锁定资金");
  assert(
    await evaluate(cdp, `Boolean(document.querySelector("#goal-delete-dialog"))`),
    "资金目标缺少删除确认弹窗",
  );
  await evaluate(cdp, `(() => {
    const editor = [...document.querySelectorAll(".goal-editor")]
      .find((row) => row.dataset.goalId === ${JSON.stringify(createdGoal.goal_id)});
    editor?.querySelector(".goal-delete")?.click();
    return Boolean(editor);
  })()`);
  await waitFor(cdp, `document.querySelector("#goal-delete-dialog")?.open === true`);
  const goalStillPresentBeforeConfirm = await evaluate(cdp, `(
    [...document.querySelectorAll(".goal-editor")].some(
      (row) => row.dataset.goalId === ${JSON.stringify(createdGoal.goal_id)}
    )
  )`);
  assert(goalStillPresentBeforeConfirm, "确认删除前资金目标不应消失");
  await evaluate(cdp, `document.querySelector("#goal-delete-cancel").click()`);
  await waitFor(cdp, `document.querySelector("#goal-delete-dialog")?.open === false`);
  const goalStillPresentAfterCancel = await evaluate(cdp, `(
    [...document.querySelectorAll(".goal-editor")].some(
      (row) => row.dataset.goalId === ${JSON.stringify(createdGoal.goal_id)}
    )
  )`);
  assert(goalStillPresentAfterCancel, "取消确认后资金目标不应被删除");
  await evaluate(cdp, `(() => {
    const editor = [...document.querySelectorAll(".goal-editor")]
      .find((row) => row.dataset.goalId === ${JSON.stringify(createdGoal.goal_id)});
    editor?.querySelector(".goal-delete")?.click();
    return Boolean(editor);
  })()`);
  await waitFor(cdp, `document.querySelector("#goal-delete-dialog")?.open === true`);
  await evaluate(cdp, `document.querySelector("#goal-delete-confirm").click()`);
  await waitFor(
    cdp,
    `![...document.querySelectorAll(".goal-editor")].some(
      (row) => row.dataset.goalId === ${JSON.stringify(createdGoal.goal_id)}
    )`,
  );
  const deletedGoalCount = await evaluate(cdp, `(async () => {
    const response = await fetchWithAuth(
      \`/v1/profile?session_id=\${encodeURIComponent(sessionId)}\`
    );
    const payload = await response.json();
    return payload.goals?.filter(
      (goal) => goal.goal_id === ${JSON.stringify(createdGoal.goal_id)}
    ).length || 0;
  })()`);
  assert(deletedGoalCount === 0, "资料页没有删除资金目标");
  await evaluate(cdp, `(() => {
    const person = document.querySelector(".care-person");
    const values = {
      phone: "13800138000",
      wechat: "ui_smoke_wechat",
      alipay: "ui_smoke@example.com",
      bank_name: "工商银行",
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
  assert(savedContacts.bank_name === "工商银行", "联系人开户银行没有保存");
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
  await sendPrompt(cdp, "给王强转1块，用微信", "请核对关键信息");
  await sendPrompt(cdp, "确认", "请输入付款银行卡");
  await sendPrompt(cdp, "246810", "转账成功");
  await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    bubble?.querySelector(".message-action")?.click();
    return true;
  })()`);
  await waitFor(cdp, `document.querySelector("#detail-dialog")?.open === true`);
  const transactionDetail = await evaluate(cdp, `({
    text: document.querySelector("#detail-dialog-content")?.textContent || "",
    rows: document.querySelectorAll("#detail-dialog-content .transaction-detail-row").length
  })`);
  assert(transactionDetail.text.includes("银行处理流水号"), "交易凭证缺少银行流水号");
  assert(transactionDetail.text.includes("生命周期"), "交易凭证缺少生命周期");
  assert(transactionDetail.rows >= 8, "交易凭证字段不完整");
  await evaluate(cdp, `document.querySelector("#detail-dialog-close").click()`);
  await sendPrompt(cdp, "预测我未来90天的现金流", "未来 90 天现金流");
  const forecastLayout = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    const panel = bubble?.querySelector(".forecast-panel");
    const chart = panel?.querySelector(".forecast-chart svg");
    const line = chart?.querySelector(".line-balance");
    const baseline = chart?.querySelector(".line-baseline");
    const band50 = chart?.querySelector(".band-50");
    const band90 = chart?.querySelector(".band-90");
    const workflow = bubble?.querySelector(".workflow-strip");
    const rect = bubble?.getBoundingClientRect();
    return {
      panel: Boolean(panel),
      chart: Boolean(chart),
      hasLine: Boolean(line?.getAttribute("d")),
      hasBaseline: Boolean(baseline?.getAttribute("d")),
      hasBand50: Boolean(band50?.getAttribute("d")),
      hasBand90: Boolean(band90?.getAttribute("d")),
      controls: panel?.querySelectorAll(".forecast-control").length || 0,
      workflow: workflow?.textContent || "",
      insideViewport: rect ? rect.left >= 0 && rect.right <= innerWidth + 1 : false,
      pageFits: document.documentElement.scrollWidth <= innerWidth
    };
  })()`);
  assert(forecastLayout.panel, "现金流预测没有生成数字孪生面板");
  assert(forecastLayout.chart && forecastLayout.hasLine, "现金流预测图没有生成折线");
  assert(forecastLayout.hasBaseline, "现金流预测图没有生成基准对比线");
  assert(forecastLayout.hasBand50, "现金流预测图没有生成 50% 区间");
  assert(forecastLayout.hasBand90, "现金流预测图没有生成 90% 区间");
  assert(forecastLayout.controls >= 5, "现金流预测缺少交互情景滑块");
  assert(forecastLayout.workflow.includes("顾问 Agent"), "工作流没有显示多 Agent 角色");
  assert(forecastLayout.insideViewport, "现金流预测面板超出视口");
  assert(forecastLayout.pageFits, "现金流预测面板导致页面横向滚动");
  await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    bubble?.querySelector(".workflow-open")?.click();
    return true;
  })()`);
  await waitFor(cdp, `document.querySelector("#detail-dialog")?.open === true`);
  const workflowDetails = await evaluate(cdp, `({
    rows: document.querySelectorAll("#detail-dialog-content .transaction-detail-row").length,
    text: document.querySelector("#detail-dialog-content")?.textContent || ""
  })`);
  assert(workflowDetails.rows >= 3, "Agent 操作中心没有展示工作流步骤");
  assert(workflowDetails.text.includes("理解 Agent"), "工作流缺少理解 Agent 信息");
  await evaluate(cdp, `document.querySelector("#detail-dialog-close").click()`);
  await screenshot(cdp, "desktop-forecast.png");
  await sendPrompt(cdp, "主动财务管家", "个人 CFO");
  const proactive = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    return {
      panel: Boolean(bubble?.querySelector(".proactive-panel")),
      items: bubble?.querySelectorAll(".insight-item").length || 0
    };
  })()`);
  assert(proactive.panel, "主动财务管家没有生成结构化简报");
  assert(proactive.items > 0, "主动财务管家没有生成提醒项");
  await sendPrompt(cdp, "今天有什么要注意的", "材料");
  const dailyIntake = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    return {
      panel: Boolean(bubble?.querySelector(".daily-material-list")),
      materials: bubble?.querySelectorAll(".daily-material").length || 0,
      channels: bubble?.querySelectorAll(".daily-channel").length || 0,
      hasUpload: [...(bubble?.querySelectorAll(".daily-channel strong") || [])]
        .some((item) => item.textContent.includes("上传")),
      hasConfirm: [...(bubble?.querySelectorAll(".daily-channel strong") || [])]
        .some((item) => item.textContent.includes("已接入数据"))
    };
  })()`);
  assert(dailyIntake.panel, "每日简报没有先展示材料清单");
  assert(dailyIntake.materials >= 4, "每日简报材料清单不完整");
  assert(dailyIntake.channels >= 3, "每日简报缺少提交通道");
  assert(dailyIntake.hasUpload, "每日简报缺少上传入口");
  assert(dailyIntake.hasConfirm, "每日简报缺少使用已接入数据的确认入口");
  await evaluate(cdp, `(() => {
    const buttons = [...document.querySelectorAll(".daily-channel .forecast-action")];
    buttons.at(-1)?.click();
    return buttons.length;
  })()`);
  await waitFor(
    cdp,
    `[...document.querySelectorAll(".message.agent .bubble")].at(-1)?.textContent.includes("数据口径：")`,
    15_000,
  );
  await sendPrompt(cdp, "我的财务健康分", "个人财务健康分");
  const health = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    return {
      panel: Boolean(bubble?.querySelector(".health-overview")),
      dimensions: bubble?.querySelectorAll(".health-dimension").length || 0,
      advice: bubble?.querySelectorAll(".forecast-warning").length || 0,
      adviceSteps: bubble?.querySelectorAll(".health-advice-steps li").length || 0
    };
  })()`);
  assert(health.panel, "财务健康分没有生成评分面板");
  assert(health.dimensions === 7, "财务健康分维度数量异常");
  assert(health.advice === 3, "财务健康分没有生成三条优先建议");
  assert(health.adviceSteps >= 3, "财务健康建议缺少可执行步骤");
  await sendPrompt(cdp, "比较一下基金产品", "产品横向对比");
  const wealthCompare = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    const rows = bubble?.querySelectorAll(".comparison-table-wrap tbody tr") || [];
    return {
      table: Boolean(bubble?.querySelector(".comparison-table-wrap")),
      products: bubble?.querySelectorAll(".comparison-table-wrap thead th").length
        ? bubble.querySelectorAll(".comparison-table-wrap thead th").length - 1
        : 0,
      rows: rows.length,
      conclusions: bubble?.querySelectorAll(".comparison-conclusion span").length || 0
    };
  })()`);
  assert(wealthCompare.table, "产品对比没有生成横向表格");
  assert(wealthCompare.products >= 2, "产品对比候选数量不足");
  assert(wealthCompare.rows >= 6, "产品对比维度不足");
  assert(wealthCompare.conclusions >= 3, "产品对比缺少差异结论");
  await sendPrompt(cdp, "帮我做账务对账", "同源");
  const reconcilePanel = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    return {
      panel: Boolean(bubble?.querySelector(".reconcile-panel")),
      purpose: bubble?.querySelector(".insight-subtitle")?.textContent || "",
      note: bubble?.querySelector(".insight-note")?.textContent || ""
    };
  })()`);
  assert(reconcilePanel.panel, "账务对账没有生成结构化面板");
  assert(reconcilePanel.purpose.includes("核对"), "账务对账没有解释用途");
  assert(reconcilePanel.note.includes("同源"), "账务对账缺少口径说明");
  await evaluate(cdp, `document.querySelector("#event-center-button").click()`);
  await waitFor(cdp, `document.querySelector("#event-center-dialog")?.open === true`);
  const eventCenter = await evaluate(cdp, `({
    items: document.querySelectorAll("#event-center-list .event-center-item").length,
    summary: document.querySelectorAll("#event-center-list .event-center-summary-item").length,
    levels: document.querySelectorAll("#event-center-list .event-level").length,
    meta: document.querySelectorAll("#event-center-list .event-center-meta").length,
    badge: document.querySelector("#event-badge")?.textContent || "0"
  })`);
  assert(eventCenter.items > 0, "主动事件中心没有事件");
  assert(eventCenter.summary === 4, "主动事件中心缺少统计概览");
  assert(eventCenter.levels === eventCenter.items, "主动事件缺少优先级标签");
  assert(eventCenter.meta === eventCenter.items, "主动事件缺少来源与状态标签");
  assert(Number(eventCenter.badge) > 0, "主动事件角标没有更新");
  await evaluate(cdp, `(() => {
    document.querySelector("#event-center-list .event-center-item .event-center-actions button:nth-child(2)")?.click();
    return true;
  })()`);
  await waitFor(
    cdp,
    `document.querySelectorAll("#event-center-list .event-center-item").length < ${eventCenter.items}`,
  );
  const eventAfterSnooze = await evaluate(cdp, `({
    items: document.querySelectorAll("#event-center-list .event-center-item").length,
    badge: document.querySelector("#event-badge")?.textContent || "0"
  })`);
  assert(eventAfterSnooze.items < eventCenter.items, "稍后提醒后事件没有从列表隐藏");
  assert(Number(eventAfterSnooze.badge) < Number(eventCenter.badge), "稍后提醒后角标没有减少");
  await evaluate(cdp, `document.querySelector("#event-center-close").click()`);
  await sendPrompt(cdp, "给我最近异常交易的风险处置方案", "风险处置");
  const playbook = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    return {
      panel: Boolean(bubble?.querySelector(".playbook-panel")),
      steps: bubble?.querySelectorAll(".playbook-step").length || 0,
      actions: bubble?.querySelectorAll(".playbook-action").length || 0,
      actionTexts: [...(bubble?.querySelectorAll(".playbook-action") || [])]
        .map((button) => button.textContent.trim())
    };
  })()`);
  assert(playbook.panel, "风险处置链没有生成结构化面板");
  assert(playbook.steps === 5, "风险处置链步骤数量异常");
  assert(playbook.actions === 5, "风险处置链缺少步骤操作按钮");
  assert(
    playbook.actionTexts.every((text) => text && text !== "打开步骤"),
    "风险处置步骤仍在使用无动作含义的“打开步骤”按钮",
  );
  assert(
    playbook.actionTexts.some((text) => text.includes("确认")),
    "风险处置链缺少可直接确认的高风险动作",
  );
  await evaluate(cdp, `document.querySelector(".playbook-action")?.click()`);
  await waitFor(cdp, `document.querySelector("#playbook-action-dialog")?.open === true`);
  const playbookDialog = await evaluate(cdp, `({
    title: document.querySelector("#playbook-action-title")?.textContent || "",
    message: document.querySelector("#playbook-action-message")?.textContent || ""
  })`);
  assert(playbookDialog.title.includes("确认"), "风险处置确认弹窗标题异常");
  assert(playbookDialog.message.includes("身份验证"), "风险处置确认弹窗没有说明验证要求");
  await evaluate(cdp, `document.querySelector("#playbook-action-confirm").click()`);
  await waitFor(
    cdp,
    `document.querySelector("#playbook-action-dialog")?.open === false
      && [...document.querySelectorAll(".message.agent .bubble")].at(-1)?.textContent.includes("已冻结")`,
    15_000,
  );
  const playbookDirectAction = await evaluate(cdp, `({
    text: [...document.querySelectorAll(".message.agent .bubble")].at(-1)?.textContent || "",
    dialogClosed: document.querySelector("#playbook-action-dialog")?.open === false
  })`);
  assert(playbookDirectAction.dialogClosed, "确认执行后风险处置弹窗没有关闭");
  assert(playbookDirectAction.text.includes("已冻结"), "确认按钮没有直接完成冻结动作");
  await screenshot(cdp, "desktop.png");

  await openChat(cdp, 390, 844, true);
  await sendPrompt(cdp, "帮我看看有哪些自动续费", "到期时间");
  const mobileSubscription = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    const table = bubble?.querySelector(".subscription-table");
    const wrapper = bubble?.querySelector(".subscription-table-wrap");
    const bubbleRect = bubble?.getBoundingClientRect();
    const wrapperRect = wrapper?.getBoundingClientRect();
    return {
      structured: Boolean(table),
      hasScrollWrapper: Boolean(wrapper),
      pageFits: document.documentElement.scrollWidth <= innerWidth,
      bubbleInsideViewport: bubbleRect
        ? bubbleRect.left >= 0 && bubbleRect.right <= innerWidth + 1
        : false,
      wrapperInsideBubble: Boolean(bubbleRect && wrapperRect)
        && wrapperRect.left >= bubbleRect.left - 1
        && wrapperRect.right <= bubbleRect.right + 1,
      canScroll: Boolean(wrapper) && table.scrollWidth > wrapper.clientWidth
    };
  })()`);
  assert(mobileSubscription.structured, "移动端自动续费结果没有生成结构化表格");
  assert(mobileSubscription.hasScrollWrapper, "移动端自动续费表格缺少横向滚动容器");
  assert(mobileSubscription.pageFits, "移动端自动续费表格导致页面横向滚动");
  assert(mobileSubscription.bubbleInsideViewport, "移动端自动续费气泡超出视口");
  assert(mobileSubscription.wrapperInsideBubble, "移动端自动续费表格容器超出气泡");
  assert(mobileSubscription.canScroll, "移动端自动续费表格无法横向查看完整列");
  await screenshot(cdp, "mobile-subscriptions.png");
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
      categoryCount: document.querySelectorAll(".capability-category").length,
      categoryBarDisplay: getComputedStyle(document.querySelector(".capability-categories")).display,
      categoryBarOverflow: getComputedStyle(document.querySelector(".capability-categories")).overflowX,
    };
  })()`);
  assert(mobile.scrollWidth <= mobile.viewport[0], "移动端页面出现横向滚动");
  assert(mobile.bubblesInside, "移动端消息气泡超出视口");
  assert(mobile.composerVisible, "移动端输入区不在视口内");
  assert(mobile.categoryCount === 6, "移动端业务分类数量不是 6 个");
  assert(mobile.categoryBarDisplay === "flex", "移动端业务分类未切换为横向布局");
  assert(["auto", "scroll"].includes(mobile.categoryBarOverflow), "移动端业务分类不可横向滚动");
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
  const ocrQuality = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    return {
      panel: Boolean(bubble?.querySelector(".forecast-panel")),
      hasQuality: bubble?.textContent.includes("识别质量") || false,
      hasConfidence: bubble?.querySelectorAll(".forecast-metric").length >= 3,
      editorRows: bubble?.querySelectorAll(".receipt-editor tbody tr").length || 0,
      hasCorrection: Boolean(bubble?.querySelector(".receipt-editor-actions"))
    };
  })()`);
  assert(ocrQuality.panel, "账单识别没有生成质量面板");
  assert(ocrQuality.hasQuality, "账单识别没有展示质量评分");
  assert(ocrQuality.hasConfidence, "账单识别没有展示置信度指标");
  assert(ocrQuality.editorRows > 0, "账单识别没有生成可编辑核对表");
  assert(ocrQuality.hasCorrection, "账单识别缺少人工校正操作");

  console.log(JSON.stringify({
    ok: true,
    desktop,
    mobile,
    mobileBill,
    profileSettings: profilePanel,
    savedContacts,
    billTable: tableLayout,
    anomalyTable: anomalyLayout,
    forecastLayout,
    proactive,
    dailyIntake,
    playbook,
    playbookDirectAction,
    health,
    wealthCompare,
    reconcilePanel,
    mobileAnomaly,
    staleSessionRetry: true,
    fileUpload: true,
    ocrQuality,
    accessPasswordTested: Boolean(accessPassword),
    artifacts: outDir,
  }, null, 2));
  await cdp.send("Browser.close");
} finally {
  if (browser.exitCode === null) browser.kill();
  await sleep(200);
  rmSync(profileDir, { recursive: true, force: true });
}
