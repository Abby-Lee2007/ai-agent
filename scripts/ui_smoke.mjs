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

async function renderCreditApplicationFixtures(cdp) {
  return evaluate(cdp, `(() => {
    const append = (suffix, decision, extra = {}) => {
      const id = suffix ? "APP-" + suffix : "";
      const reply = {
        text: "【信用卡申请】内部评分 88/100；shadow_status=hidden；model_version=hidden",
        intent: "card.apply",
        ok: true,
        kind: "executed",
        trace: [],
        plan: ["card.apply"],
        data: {
          kind: "credit",
          application_id: id,
          card_id: id,
          decision,
          score: 88,
          reference_limit: extra.reference_limit,
          submitted_at: extra.submitted_at || "",
          statement_quality: "良好",
          shadow_status: "insufficient_data",
          model_version: "internal-only",
          coverage: { reliable: 1 },
          artifact_sha256: "hidden",
          diagnostic_code: "hidden_diagnostic",
          ...extra.data,
        },
        needs_input: false,
      };
      appendMessage("agent", reply.text, reply);
      return reply;
    };
    const approved = append("APPROVED", "通过", {
      reference_limit: 30000,
      submitted_at: "2026-10-30T09:00:00",
      data: {
        credit_risk_collection: {
          total_features: 23,
          collected_features: 23,
          missing_features: 0,
          invalid_features: 0,
          required_total_features: 5,
          required_collected_features: 5,
          required_missing_features: 0,
          required_complete: true,
          core_complete: true,
          policy_allowed_for_decision: true,
          model_status: "success",
          collected_values: [
            { key: "X1", label: "当前信用额度", display_value: "50,000.00 元" },
            { key: "X3", label: "教育程度", display_value: "本科/大专" },
            { key: "X6", label: "过去第 1 个月还款状态", display_value: "按时还清" },
          ],
        },
        credit_risk_model: {
          status: "success",
          executed: true,
          predicted_probability: 0.448976,
          probability: 0.448976,
          threshold: 0.5,
          risk_label: "较低模型风险",
        },
      },
    });
    const review = append("REVIEW", "转人工复核", { reference_limit: 12000 });
    const rejected = append("REJECTED", "未通过", { reference_limit: 0 });
    const missing = append("", "", {
      data: {
        application_id: "",
        card_id: "",
        decision: "",
        reference_limit: null,
      },
    });
    appendMessage("agent", approved.text, approved);
    appendMessage("agent", "请确认是否提交信用卡申请。", {
      intent: "card.apply",
      kind: "confirm",
      data: {},
    });
    appendMessage("agent", "请补充信用风险评估资料。", {
      intent: "card.apply",
      kind: "question",
      data: {
        credit_risk_collection: {
          total_features: 23,
          collected_features: 3,
          missing_features: 20,
          invalid_features: 0,
          required_total_features: 5,
          required_collected_features: 1,
          required_missing_features: 4,
          core_complete: false,
          required_complete: false,
          policy_allowed_for_decision: true,
          model_status: "collecting",
          collected_values: [
            { key: "X1", label: "当前信用额度", display_value: "50,000.00 元" },
          ],
        },
        credit_risk_model: {
          status: "collecting",
          executed: false,
          probability: null,
        },
      },
    });
    appendMessage("agent", "当前余额为 100.00 元", {
      intent: "account.balance",
      kind: "executed",
      data: { amount: 100 },
    });

    const cards = [...document.querySelectorAll(".credit-application-result")];
    const applicationCard = (id) =>
      cards.find((node) => node.dataset.applicationId === id);
    const approvedCard = applicationCard("APP-APPROVED");
    const reviewCard = applicationCard("APP-REVIEW");
    const rejectedCard = applicationCard("APP-REJECTED");
    const missingCard = cards.find((node) => !node.dataset.applicationId);
    const confirmBubble = [...document.querySelectorAll(".message.agent .bubble")].at(-3);
    const collectionBubble = [...document.querySelectorAll(".message.agent .bubble")].at(-2);
    const balanceBubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    const allText = cards.map((node) => node.textContent).join("\\n");
    return {
      count: cards.length,
      approved: {
        tone: approvedCard?.dataset.tone || "",
        text: approvedCard?.textContent || "",
      },
      review: {
        tone: reviewCard?.dataset.tone || "",
        text: reviewCard?.textContent || "",
      },
      rejected: {
        tone: rejectedCard?.dataset.tone || "",
        text: rejectedCard?.textContent || "",
      },
      missing: {
        tone: missingCard?.dataset.tone || "",
        text: missingCard?.textContent || "",
      },
      progressText: approvedCard?.querySelector(".credit-risk-progress")?.textContent || "",
      questionProgressShown: Boolean(
        collectionBubble?.querySelector(".credit-risk-progress")
      ),
      modelResultVisible: (approvedCard?.textContent || "").includes("模型风险评估"),
      modelDoesNotReplaceApproval: (
        approvedCard?.textContent || ""
      ).includes("不替代正式审批"),
      valuesVisible: Boolean(
        approvedCard?.querySelector(".credit-risk-values")
      ) && (approvedCard?.textContent || "").includes("查看已采集资料"),
      confirmHadCard: Boolean(confirmBubble?.querySelector(".credit-application-result")),
      nonApplyUnchanged: (balanceBubble?.textContent || "").includes("当前余额为 100.00 元")
        && !(balanceBubble?.querySelector(".credit-application-result")),
      shadowLeaked: /shadow_status|model_version|artifact_sha256|diagnostic_code|coverage/.test(allText),
      scoreLeaked: /88\\/100/.test(allText),
      pageFits: document.documentElement.scrollWidth <= innerWidth,
    };
  })()`);
}

async function exerciseRequestLifecycle(cdp) {
  return evaluate(cdp, `(async () => {
    const realFetch = window.fetch.bind(window);
    const streamRequestIds = [];
    const queryRequestIds = [];
    let mode = "normal";
    let replySequence = 0;

    const makeReply = (text) => ({
      ok: true,
      kind: "reply",
      text,
      intent: "test.request_lifecycle",
      data: {},
      trace: [],
      plan: [],
      needs_input: false
    });
    const completedPayload = (requestId, text) => ({
      session_id: sessionId,
      request_id: requestId,
      request_status: "completed",
      replayed: true,
      reply: makeReply(text)
    });

    window.fetch = async (url, options = {}) => {
      const parsed = new URL(String(url), location.origin);
      if (parsed.pathname === "/v1/messages/stream") {
        const requestBody = JSON.parse(options.body || "{}");
        streamRequestIds.push(requestBody.request_id);
        if (mode === "disconnect" || mode === "disconnect-unknown") {
          throw new TypeError("simulated SSE disconnect");
        }
        replySequence += 1;
        const text = "生命周期回复 " + replySequence;
        const payload = completedPayload(requestBody.request_id, text);
        return new Response(
          "event: start\\n"
          + "data: " + JSON.stringify({
            session_id: sessionId,
            request_id: requestBody.request_id
          }) + "\\n\\n"
          + "event: message\\n"
          + "data: " + JSON.stringify({
            ...payload,
            replayed: false
          }) + "\\n\\n"
          + "event: done\\n"
          + "data: {\\"ok\\":true,\\"request_status\\":\\"completed\\"}\\n\\n",
          {
            status: 200,
            headers: { "Content-Type": "text/event-stream" }
          }
        );
      }
      if (parsed.pathname.startsWith("/v1/requests/")) {
        const requestId = decodeURIComponent(
          parsed.pathname.slice("/v1/requests/".length)
        );
        queryRequestIds.push(requestId);
        const status = mode === "unknown" || mode === "disconnect-unknown"
          ? "outcome_unknown"
          : "completed";
        return new Response(JSON.stringify(
          status === "completed"
            ? completedPayload(requestId, "断线恢复原结果")
            : {
              session_id: sessionId,
              request_id: requestId,
              request_status: status,
              replayed: false
            }
        ), {
          status: 200,
          headers: { "Content-Type": "application/json" }
        });
      }
      return realFetch(url, options);
    };

    try {
      sessionStorage.removeItem(REQUEST_RECOVERY_KEY);
      busy = false;
      sendButton.disabled = false;

      await sendMessage("生命周期测试一");
      const firstRequestId = streamRequestIds.at(-1);
      const firstReplyCount = [...document.querySelectorAll(".message.agent")]
        .filter((node) => node.dataset.requestId === firstRequestId).length;

      await sendMessage("生命周期测试一", {
        requestId: firstRequestId,
        retry: true
      });
      const replayCount = [...document.querySelectorAll(".message.agent")]
        .filter((node) => node.dataset.requestId === firstRequestId).length;

      await sendMessage("生命周期测试二");
      const secondRequestId = streamRequestIds.at(-1);

      mode = "disconnect";
      const streamsBeforeDisconnect = streamRequestIds.length;
      await sendMessage("断线恢复测试");
      const disconnectRequestId = streamRequestIds[streamsBeforeDisconnect];
      mode = "normal";

      const refreshId = "refresh-request-001";
      sessionStorage.setItem(REQUEST_RECOVERY_KEY, JSON.stringify({
        request_id: refreshId,
        session_id: sessionId,
        status: "completed",
        message_fingerprint: "refresh-fingerprint",
        created_at: new Date().toISOString()
      }));
      await recoverPendingRequest();
      await recoverPendingRequest();
      const refreshBubbles = [...document.querySelectorAll(".message.agent")]
        .filter((node) => node.dataset.requestId === refreshId).length;

      mode = "unknown";
      const unknownId = "unknown-request-001";
      sessionStorage.setItem(REQUEST_RECOVERY_KEY, JSON.stringify({
        request_id: unknownId,
        session_id: sessionId,
        status: "outcome_unknown",
        message_fingerprint: await messageFingerprint("未知执行操作"),
        created_at: new Date().toISOString()
      }));
      await recoverPendingRequest();
      const unknownRecoveryRaw = sessionStorage.getItem(REQUEST_RECOVERY_KEY) || "";
      const streamsBeforeUnknownRetry = streamRequestIds.length;
      await sendMessage("未知执行操作");
      const streamsAfterUnknownRetry = streamRequestIds.length;
      const unknownNotice = [...document.querySelectorAll(".message.agent")]
        .find((node) => node.dataset.requestNoticeId === unknownId);

      sessionStorage.removeItem(REQUEST_RECOVERY_KEY);
      mode = "disconnect-unknown";
      awaitingSecret = true;
      await sendMessage("654321");
      awaitingSecret = false;
      const secretRecoveryRaw = sessionStorage.getItem(REQUEST_RECOVERY_KEY) || "";
      const secretRecovery = JSON.parse(secretRecoveryRaw || "{}");
      const secretFingerprintNotPersisted =
        Boolean(secretRecovery.request_id)
        && !secretRecovery.message_fingerprint
        && !secretRecoveryRaw.includes("654321");

      return {
        firstRequestId,
        secondRequestId,
        disconnectRequestId,
        differentOperationsUseDifferentIds:
          Boolean(firstRequestId && secondRequestId)
          && firstRequestId !== secondRequestId,
        noDuplicateOnReplay:
          firstReplyCount === 1 && replayCount === 1,
        disconnectQueryReusedId:
          Boolean(disconnectRequestId)
          && queryRequestIds.includes(disconnectRequestId),
        refreshRecoveredOnce: refreshBubbles === 1,
        unknownBlockedResubmit:
          streamsAfterUnknownRetry === streamsBeforeUnknownRetry,
        unknownNotice:
          unknownNotice?.textContent?.includes("不要重复提交相同操作")
          || false,
        secretFingerprintNotPersisted,
        recoveryStoresNoMessageBody:
          !unknownRecoveryRaw.includes("未知执行操作")
      };
    } finally {
      window.fetch = realFetch;
      sessionStorage.removeItem(REQUEST_RECOVERY_KEY);
      busy = false;
      sendButton.disabled = false;
    }
  })()`);
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
      marketDialogOpen: document.querySelector("#market-dialog")?.open === true,
      marketStripInChat: Boolean(document.querySelector(".chat > #market-overview")),
      expandedCategories: [...document.querySelectorAll(".capability-category")]
        .filter((button) => button.getAttribute("aria-expanded") === "true")
        .map((button) => button.textContent.trim()),
      halloweenEdition: document.querySelector(".halloween-edition")?.textContent || "",
      agentAvatar: Boolean(document.querySelector(".message.agent .avatar svg")),
      halloweenBats: Boolean(document.querySelector(".halloween-bats")),
      halloweenWeb: Boolean(document.querySelector(".halloween-web svg")),
      halloweenCandies: document.querySelectorAll(".halloween-candy").length,
      halloweenSkulls: document.querySelectorAll(".halloween-skull").length,
      pumpkinAccent: getComputedStyle(document.documentElement)
        .getPropertyValue("--halloween-orange").trim(),
    };
  })()`);
  assert(desktop.title === "AI Banking Agent", "H5 标题不正确");
  assert(desktop.scrollWidth <= desktop.viewport[0], "桌面页面出现横向滚动");
  assert(desktop.halloweenEdition.includes("Halloween"), "万圣节限定徽标缺失");
  assert(desktop.agentAvatar, "Agent 万圣节头像缺失");
  assert(desktop.halloweenBats, "顶部万圣节装饰缺失");
  assert(desktop.halloweenWeb, "蜘蛛网装饰缺失");
  assert(desktop.halloweenCandies === 7, "糖果装饰数量异常");
  assert(desktop.halloweenSkulls === 2, "骷髅头装饰数量异常");
  assert(desktop.pumpkinAccent, "万圣节主题色未加载");
  assert(desktop.composerOverlap === false, "输入框与发送按钮重叠");
  assert(desktop.capabilityLabels.length === 6, "业务分类数量不是 6 个");
  assert(desktop.capabilityLabels[0].includes("智能转账"), "缺少智能转账分类");
  assert(desktop.capabilityLabels[5].includes("人际礼物"), "缺少人际礼物分类");
  assert(desktop.globalActions.includes("每日简报"), "缺少全局每日简报入口");
  assert(!desktop.marketDialogOpen, "默认界面不应自动打开市场弹窗");
  assert(!desktop.marketStripInChat, "市场行情不应占用聊天区");
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
      .find((item) => item.textContent.includes("理财操作"));
    button?.click();
    return Boolean(button);
  })()`);
  await waitFor(
    cdp,
    `document.querySelector("#market-dialog")?.open === true
      && document.querySelectorAll("#market-overview-indices .market-index").length === 6`,
    15_000,
  );
  const marketDialog = await evaluate(cdp, `(() => {
    const dialog = document.querySelector("#market-dialog");
    const rect = dialog.getBoundingClientRect();
    return {
      indices: dialog.querySelectorAll(".market-index").length,
      available: dialog.querySelectorAll(".market-index:not(.unavailable)").length,
      actions: dialog.querySelectorAll(".market-dialog-action").length,
      status: dialog.querySelector("#market-overview-status")?.textContent || "",
      turnover: dialog.querySelector("#market-overview-turnover")?.textContent || "",
      source: dialog.querySelector("#market-overview-source")?.textContent || "",
      insideViewport: rect.left >= 0 && rect.right <= innerWidth
        && rect.top >= 0 && rect.bottom <= innerHeight,
    };
  })()`);
  assert(marketDialog.indices === 6, "市场弹窗指数数量不足");
  assert(marketDialog.available >= 4, "市场弹窗公开指数可用数量不足");
  assert(marketDialog.actions === 4, "市场弹窗缺少理财快捷入口");
  assert(marketDialog.status.includes("公开行情"), "市场弹窗缺少行情状态");
  assert(marketDialog.turnover.includes("成交额"), "市场弹窗缺少两市成交额");
  assert(marketDialog.source.includes("非模拟"), "市场弹窗缺少真实数据说明");
  assert(marketDialog.insideViewport, "桌面市场弹窗超出视口");
  await evaluate(cdp, `document.querySelector("#market-dialog-close").click()`);
  await waitFor(cdp, `document.querySelector("#market-dialog")?.open === false`);
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
    agentText: [...document.querySelectorAll(".message.agent .bubble")].at(-1)?.textContent || "",
    userAvatar: Boolean(document.querySelector(".message.user .avatar svg"))
  }))()`);
  assert(transferShortcut.userText === "给他人转账", "转账快捷指令没有只发送卡片简称");
  assert(!transferShortcut.agentText.includes("转给他人"), "转账快捷指令把“他人”误当成收款人");
  assert(transferShortcut.userAvatar, "用户南瓜头像缺失");
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
  await sendPrompt(cdp, "预算1000，没有禁忌", "按估算价选择 1");
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
    giftLayout.priceCaptions.every((caption) =>
      ["方案价", "估算市场价", "市场参考价"].includes(caption)
    ),
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
    `(() => {
      const text = document.querySelector("#profile-settings-status")?.textContent || "";
      return text.length > 0 && !text.includes("保存中");
    })()`,
  );
  const profileSaveStatus = await evaluate(
    cdp,
    `document.querySelector("#profile-settings-status")?.textContent || ""`,
  );
  assert(
    profileSaveStatus.includes("已保存"),
    `个人资料保存失败：${profileSaveStatus}`,
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
  const cardBeforeStepUp = await evaluate(cdp, `(async () => {
    const response = await fetchWithAuth(
      \`/v1/profile?session_id=\${encodeURIComponent(sessionId)}\`
    );
    const payload = await response.json();
    return payload.cards?.[0] || {};
  })()`);
  const requestedOnlineState = !Boolean(cardBeforeStepUp.online_enabled);
  await evaluate(cdp, `(() => {
    const editor = document.querySelector(".cards-settings-section .card-editor");
    const online = editor?.querySelector('.card-switches input');
    if (!editor || !online) return false;
    online.checked = ${JSON.stringify(requestedOnlineState)};
    editor.querySelector(".settings-save")?.click();
    return true;
  })()`);
  await waitFor(cdp, `document.querySelector("#card-step-up-dialog")?.open === true`);
  const stepUpDialog = await evaluate(cdp, `(() => ({
    title: document.querySelector("#card-step-up-title")?.textContent || "",
    passwordType: document.querySelector("#card-step-up-password")?.type || "",
    maxLength: document.querySelector("#card-step-up-password")?.maxLength || 0,
    message: document.querySelector("#card-step-up-message")?.textContent || ""
  }))()`);
  assert(stepUpDialog.title.includes("确认卡片安全设置"), "卡片敏感修改没有二次确认弹窗");
  assert(stepUpDialog.passwordType === "password", "卡片二次确认没有使用密码输入框");
  assert(stepUpDialog.maxLength === 6, "卡片交易密码输入框长度不正确");
  assert(stepUpDialog.message.includes("交易密码"), "卡片二次确认没有说明验证方式");
  await screenshot(cdp, "desktop-card-step-up.png");
  await evaluate(cdp, `(() => {
    document.querySelector("#card-step-up-password").value = "246810";
    document.querySelector("#card-step-up-form").requestSubmit();
    return true;
  })()`);
  await waitFor(cdp, `document.querySelector("#card-step-up-dialog")?.open === false`);
  await waitFor(
    cdp,
    `document.querySelector("#card-settings-status")?.textContent.includes("银行卡已保存")`,
  );
  const cardAfterStepUp = await evaluate(cdp, `(async () => {
    const response = await fetchWithAuth(
      \`/v1/profile?session_id=\${encodeURIComponent(sessionId)}\`
    );
    const payload = await response.json();
    return payload.cards?.find(
      (card) => card.card_id === ${JSON.stringify(cardBeforeStepUp.card_id)}
    ) || {};
  })()`);
  assert(
    cardAfterStepUp.online_enabled === requestedOnlineState,
    "通过交易密码确认后卡片设置没有同步",
  );
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
    const fan50 = chart?.querySelector(".fan-50");
    const fan90 = chart?.querySelector(".fan-90");
    const probabilityPanel = panel?.querySelector(".model-audit-panel");
    const band50 = chart?.querySelector(".band-50");
    const band90 = chart?.querySelector(".band-90");
    const workflow = bubble?.querySelector(".workflow-strip");
    const rect = bubble?.getBoundingClientRect();
    return {
      panel: Boolean(panel),
      chart: Boolean(chart),
      hasLine: Boolean(line?.getAttribute("d")),
      hasBaseline: Boolean(baseline?.getAttribute("d")),
      hasFan50: Boolean(fan50?.getAttribute("d")),
      hasFan90: Boolean(fan90?.getAttribute("d")),
      hasBand50: Boolean(band50?.getAttribute("d")),
      hasBand90: Boolean(band90?.getAttribute("d")),
      probabilityPanel: Boolean(probabilityPanel),
      probabilityRows: probabilityPanel?.querySelectorAll(".analytics-table tbody tr").length || 0,
      probabilityTitle: probabilityPanel?.querySelector(".model-audit-head strong")?.textContent || "",
      probabilityLegend: panel?.querySelector(".forecast-legend")?.textContent || "",
      modelQuality: panel?.querySelector(".data-quality-line")?.textContent || "",
      controls: panel?.querySelectorAll(".forecast-control").length || 0,
      horizons: panel?.querySelectorAll(".forecast-horizon").length || 0,
      metrics: panel?.querySelectorAll(".forecast-metric").length || 0,
      breakdownRows: panel?.querySelectorAll(".cashflow-breakdown-row").length || 0,
      actionPlanItems: panel?.querySelectorAll(".action-plan-item").length || 0,
      stressTests: panel?.querySelectorAll(".stress-test").length || 0,
      workflow: workflow?.textContent || "",
      insideViewport: rect ? rect.left >= 0 && rect.right <= innerWidth + 1 : false,
      pageFits: document.documentElement.scrollWidth <= innerWidth
    };
  })()`);
  assert(forecastLayout.panel, "现金流预测没有生成数字孪生面板");
  assert(forecastLayout.chart && forecastLayout.hasLine, "现金流预测图没有生成折线");
  assert(forecastLayout.hasBaseline, "现金流预测图没有生成基准对比线");
  assert(forecastLayout.hasFan50 && forecastLayout.hasFan90, "现金流概率图缺少 50%/90% 区间");
  assert(forecastLayout.hasBand50 && forecastLayout.hasBand90, "现金流预测图缺少 50%/90% 区间");
  assert(forecastLayout.probabilityPanel, "现金流面板缺少概率分位模块");
  assert(forecastLayout.probabilityRows === 5, "现金流概率分位表不完整");
  assert(forecastLayout.probabilityTitle.includes("概率现金流区间"), "现金流概率模块标题错误");
  assert(
    forecastLayout.probabilityLegend.includes("P25–P75")
      && forecastLayout.probabilityLegend.includes("P05–P95"),
    "现金流概率图例没有标出 50%/90% 区间",
  );
  assert(
    forecastLayout.modelQuality.includes("独立于账单 OCR"),
    "现金流模型质量与 OCR 质量没有明确分开",
  );
  assert(forecastLayout.controls >= 5, "现金流预测缺少交互情景滑块");
  assert(forecastLayout.horizons === 3, "财务数字孪生缺少 30/60/90 天切换");
  assert(forecastLayout.metrics >= 5, "财务数字孪生缺少可安排资金/日均预算指标");
  assert(forecastLayout.breakdownRows >= 6, "财务数字孪生缺少现金流构成");
  assert(forecastLayout.actionPlanItems >= 1, "财务数字孪生缺少可执行建议");
  assert(forecastLayout.stressTests === 3, "财务数字孪生缺少三项压力测试");
  assert(forecastLayout.workflow.includes("顾问 Agent"), "工作流没有显示多 Agent 角色");
  assert(forecastLayout.insideViewport, "现金流预测面板超出视口");
  assert(forecastLayout.pageFits, "现金流预测面板导致页面横向滚动");
  await evaluate(cdp, `(() => {
    const button = [...document.querySelectorAll(".forecast-action")]
      .find((item) => item.textContent.trim() === "试算收入下降20%");
    button?.click();
    return Boolean(button);
  })()`);
  await waitFor(
    cdp,
    `[...document.querySelectorAll(".message.agent .bubble")].at(-1)
      ?.querySelector(".scenario-summary")?.textContent.includes("收入变化 -20%")`,
    15_000,
  );
  const scenarioLayout = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    const incomeSlider = [...bubble.querySelectorAll(".forecast-control")]
      .find((item) => item.querySelector("strong")?.textContent === "收入下降")
      ?.querySelector('input[type="range"]');
    const metrics = [...bubble.querySelectorAll(".forecast-metric")]
      .map((item) => item.textContent);
    return {
      scenarioText: bubble.querySelector(".scenario-summary")?.textContent || "",
      incomeSlider: incomeSlider?.value || "",
      metricCount: metrics.length,
      hasDeltaMetric: metrics.some((text) => text.includes("余额变化")),
      hasBaseline: Boolean(bubble.querySelector(".line-baseline")?.getAttribute("d")),
      guidance: bubble.querySelector(".forecast-guide")?.textContent || ""
    };
  })()`);
  assert(scenarioLayout.scenarioText.includes("收入变化 -20%"), "收入下降情景没有显示情景摘要");
  assert(scenarioLayout.incomeSlider === "20", "收入下降情景没有同步回填滑块");
  assert(scenarioLayout.metricCount >= 7, "情景结果缺少基准差值指标");
  assert(scenarioLayout.hasDeltaMetric, "情景结果没有显示余额变化");
  assert(scenarioLayout.hasBaseline, "情景图缺少基准对比线");
  assert(scenarioLayout.guidance.includes("使用顺序"), "数字孪生缺少使用说明");
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

  await openChat(cdp, 319, 844, true);
  await evaluate(cdp, `(() => {
    const button = [...document.querySelectorAll(".capability-category")]
      .find((item) => item.textContent.includes("理财操作"));
    button?.click();
    return Boolean(button);
  })()`);
  await waitFor(
    cdp,
    `document.querySelector("#market-dialog")?.open === true
      && document.querySelectorAll("#market-overview-indices .market-index").length === 6`,
    15_000,
  );
  const mobileMarketDialog = await evaluate(cdp, `(() => {
    const dialog = document.querySelector("#market-dialog");
    const rect = dialog.getBoundingClientRect();
    const indexStrip = dialog.querySelector("#market-overview-indices");
    const actions = [...dialog.querySelectorAll(".market-dialog-action")];
    return {
      insideViewport: rect.left >= 0 && rect.right <= innerWidth
        && rect.top >= 0 && rect.bottom <= innerHeight + 1,
      pageFits: document.documentElement.scrollWidth <= innerWidth,
      indexStripScrolls: indexStrip.scrollWidth > indexStrip.clientWidth,
      actionsInside: actions.every((button) => {
        const item = button.getBoundingClientRect();
        return item.left >= rect.left && item.right <= rect.right;
      }),
    };
  })()`);
  assert(mobileMarketDialog.insideViewport, "移动端市场弹窗超出视口");
  assert(mobileMarketDialog.pageFits, "移动端市场弹窗导致页面横向滚动");
  assert(mobileMarketDialog.indexStripScrolls, "移动端指数栏无法横向查看完整行情");
  assert(mobileMarketDialog.actionsInside, "移动端理财快捷入口超出弹窗");
  await evaluate(cdp, `document.querySelector("#market-dialog-close").click()`);
  await waitFor(cdp, `document.querySelector("#market-dialog")?.open === false`);
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
      canScroll: content ? content.scrollWidth >= content.clientWidth : false,
      hasOfficialMacro: content?.textContent.includes("官方数据参照") || false
    };
  })()`);
  assert(mobileBill.formatted, "移动端账单分析没有使用表格排版");
  assert(mobileBill.insideViewport, "移动端账单分析气泡超出视口");
  assert(mobileBill.hasOfficialMacro, "账单分析回答没有引用官方宏观数据");
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
  for (let question = 2; question <= 7; question += 1) {
    await sendPrompt(cdp, "b", `第 ${question}/7 题`);
  }
  await sendPrompt(cdp, "b", "评估完成");
  await sendPrompt(cdp, "我有5万块，稳健一点怎么配置", "约束优化组合");
  const optimizerUi = await evaluate(cdp, `(() => {
    const bubble = [...document.querySelectorAll(".message.agent .bubble")].at(-1);
    const panel = bubble?.querySelector(".model-audit-panel");
    const quality = bubble?.querySelector(".data-quality-line");
    const rows = panel?.querySelectorAll(".analytics-table tbody tr") || [];
    const rect = panel?.getBoundingClientRect();
    return {
      panel: Boolean(panel),
      rows: rows.length,
      badge: panel?.querySelector(".model-audit-badge")?.textContent || "",
      quality: quality?.textContent || "",
      insideViewport: rect ? rect.left >= 0 && rect.right <= innerWidth + 1 : false,
      pageFits: document.documentElement.scrollWidth <= innerWidth
    };
  })()`);
  assert(optimizerUi.panel, "约束优化组合没有生成可视化模块");
  assert(optimizerUi.rows >= 1, "约束优化组合没有显示资产权重");
  assert(
    optimizerUi.badge.includes("硬约束") || optimizerUi.badge.includes("现金"),
    "约束优化模块没有说明硬约束或现金安全方案",
  );
  assert(
    optimizerUi.quality.includes("独立于账单 OCR"),
    "理财数据质量与 OCR 质量没有明确分开",
  );
  assert(optimizerUi.insideViewport && optimizerUi.pageFits, "移动端约束优化模块溢出");
  await screenshot(cdp, "mobile-optimizer.png");

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

  const mobileCreditApplication = await renderCreditApplicationFixtures(cdp);
  assert(mobileCreditApplication.count === 4, "信用卡申请卡片数量异常或 SSE 重放产生重复卡片");
  assert(mobileCreditApplication.approved.tone === "approved", "通过结果没有使用通过态样式");
  assert(mobileCreditApplication.approved.text.includes("预审通过"), "通过结果缺少预审通过文案");
  assert(mobileCreditApplication.approved.text.includes("30,000"), "通过结果没有展示正式参考额度");
  assert(mobileCreditApplication.approved.text.includes("2026-10-30"), "可靠提交时间没有展示");
  assert(mobileCreditApplication.review.tone === "review", "人工复核结果样式异常");
  assert(mobileCreditApplication.review.text.includes("等待人工复核"), "人工复核文案异常");
  assert(mobileCreditApplication.rejected.tone === "rejected", "未通过结果样式异常");
  assert(mobileCreditApplication.rejected.text.includes("预审未通过"), "未通过文案异常");
  assert(!mobileCreditApplication.rejected.text.includes("¥0.00"), "未通过结果不应展示误导性额度");
  assert(mobileCreditApplication.missing.text.includes("申请处理中"), "缺失结果字段时没有降级状态");
  assert(mobileCreditApplication.missing.text.includes("暂未提供"), "缺失申请编号时没有降级展示");
  assert(mobileCreditApplication.missing.text.includes("待确认"), "缺失预审结果时没有降级展示");
  assert(!mobileCreditApplication.confirmHadCard, "确认阶段不应提前显示申请结果卡片");
  assert(mobileCreditApplication.nonApplyUnchanged, "非 card.apply 消息渲染受到影响");
  assert(
    mobileCreditApplication.progressText.includes("核心 5/5"),
    "申请结果卡片没有展示 Top-5 核心资料进度",
  );
  assert(
    mobileCreditApplication.questionProgressShown,
    "采集过程中的问题气泡没有展示模型资料进度",
  );
  assert(
    mobileCreditApplication.modelResultVisible,
    "模型真实执行后前端没有展示风险结果",
  );
  assert(
    mobileCreditApplication.modelDoesNotReplaceApproval,
    "模型结果没有明确与正式审批隔离",
  );
  assert(
    mobileCreditApplication.valuesVisible,
    "用户无法查看已采集的信用风险资料",
  );
  assert(!mobileCreditApplication.shadowLeaked, "申请结果卡片泄漏 Shadow 内部字段");
  assert(!mobileCreditApplication.scoreLeaked, "申请结果卡片泄漏内部评分");
  assert(mobileCreditApplication.pageFits, "移动端信用卡申请结果卡片导致横向滚动");
  await screenshot(cdp, "mobile-credit-application.png");

  await openChat(cdp, 1440, 1000, false);
  const desktopCreditApplication = await renderCreditApplicationFixtures(cdp);
  assert(desktopCreditApplication.count === 4, "桌面端信用卡申请卡片数量异常");
  assert(desktopCreditApplication.pageFits, "桌面端信用卡申请结果卡片导致横向滚动");
  await screenshot(cdp, "desktop-credit-application.png");

  const requestLifecycle = await exerciseRequestLifecycle(cdp);
  assert(
    requestLifecycle.differentOperationsUseDifferentIds,
    "不同业务操作复用了同一个 request_id",
  );
  assert(requestLifecycle.noDuplicateOnReplay, "SSE 结果重放产生了重复聊天气泡");
  assert(
    requestLifecycle.disconnectQueryReusedId,
    "断线恢复查询没有复用原 request_id",
  );
  assert(requestLifecycle.refreshRecoveredOnce, "页面恢复重复插入原请求结果");
  assert(
    requestLifecycle.unknownBlockedResubmit,
    "未知执行状态仍允许相同操作生成新请求",
  );
  assert(
    requestLifecycle.unknownNotice,
    "未知执行状态没有提示先核对且不要重复提交",
  );
  assert(
    requestLifecycle.recoveryStoresNoMessageBody,
    "页面恢复状态包含原始消息正文",
  );
  assert(
    requestLifecycle.secretFingerprintNotPersisted,
    "交易密码或验证码的指纹被写入页面恢复状态",
  );

  console.log(JSON.stringify({
    ok: true,
    desktop,
    mobile,
    mobileBill,
    mobileMarketDialog,
    profileSettings: profilePanel,
    savedContacts,
    cardStepUp: {
      verified: cardAfterStepUp.online_enabled === requestedOnlineState,
      dialogTitle: stepUpDialog.title,
    },
    billTable: tableLayout,
    anomalyTable: anomalyLayout,
    forecastLayout,
    proactive,
    dailyIntake,
    playbook,
    playbookDirectAction,
    optimizerUi,
    health,
    wealthCompare,
    reconcilePanel,
    mobileAnomaly,
    staleSessionRetry: true,
    fileUpload: true,
    ocrQuality,
    mobileCreditApplication,
    desktopCreditApplication,
    requestLifecycle,
    accessPasswordTested: Boolean(accessPassword),
    artifacts: outDir,
  }, null, 2));
  await cdp.send("Browser.close");
} finally {
  if (browser.exitCode === null) browser.kill();
  await sleep(800);
  try {
    rmSync(profileDir, {
      recursive: true,
      force: true,
      maxRetries: 5,
      retryDelay: 200,
    });
  } catch {
    // Chrome can hold the profile directory briefly on Windows.
  }
}
