"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const tokenKey = "lynkco-helper.local-token";
  const captureAccessKey = "lynkco-helper.capture-access-token";
  const captureExpireKey = "lynkco-helper.capture-expire-at";
  const captureIdKey = "lynkco-helper.capture-id";
  const hashToken = location.hash.slice(1); if (hashToken) sessionStorage.setItem(tokenKey, hashToken);
  const token = hashToken || sessionStorage.getItem(tokenKey) || "";
  history.replaceState(null, "", location.pathname + location.search);
  let state = null,
    view = "overview",
    platform = "IOS",
    activeOperations = 0;
  let proxyConfirmed = false, selectedBindingStep = null, activePair = null;
  let proxyStartPromise = null, networksLoaded = false, proxyStarting = false;
  let verifiedCandidateKey = null, localDisconnected = !token, quitting = false, terminated = false;
  let qrUrl = null,
    qrPair = null,
    qrFailedPair = null,
    qrRequestId = 0,
    settingsVersion = "",
    settingsBindingId = null,
    settingsDirty = false,
    historyItems = [],
    historyCursor = null;
  let captureAccessRequested = false;
  let captureAccessCaptureId = null;
  let captureWasReady = false;
  let confirmResolve = null;
  const labels = {
    active: "运行中",
    paused: "已暂停",
    needs_rebind: "需要重新绑定",
    service_error: "服务异常",
    queued: "等待执行",
    pending: "等待执行",
    running: "执行中",
    success: "已完成",
    succeeded: "已完成",
    completed: "已完成",
    failed: "未完成",
    partial: "部分完成",
    skipped: "已跳过",
    signed: "已签到",
    already_signed: "已签到",
    retry_wait: "等待重试",
    inflight: "执行中",
    done: "已完成",
    unknown: "结果待确认",
    disabled: "未开启",
    not_started: "待执行",
    error: "异常",
  };
  const text = (id, value) => {
    $(id).textContent = value == null ? "--" : String(value);
  };
  const statusDot = (id, tone) => {
    const node = $(id);
    if (node) node.className = `status-dot ${tone}`;
  };
  const badge = (id, value, tone = "") => {
    const node = $(id);
    node.className = `badge ${tone}`.trim();
    node.replaceChildren(Object.assign(document.createElement("i"), { ariaHidden: "true" }), document.createTextNode(value == null ? "--" : String(value)));
  };
  function taskIcon(id, status) {
    const node = $(id);
    const success = ["success", "succeeded", "completed", "signed", "already_signed", "done"];
    const failed = ["failed", "error"];
    const running = ["running", "queued", "retry_wait", "inflight"];
    const disabled = ["disabled", "skipped"];
    const tone = success.includes(status) ? "success"
      : failed.includes(status) ? "error"
      : running.includes(status) ? "running"
      : disabled.includes(status) ? "disabled"
      : status === "unknown" ? "warning" : "pending";
    const icon = tone === "success" ? "check"
      : tone === "error" ? "x"
      : tone === "running" ? "loader-circle"
      : tone === "disabled" ? "minus"
      : tone === "warning" ? "circle-help" : "clock-3";
    node.setAttribute("class", `task-state-icon ${tone}`);
    node.setAttribute("data-lucide", icon);
  }
  const show = (id, visible) => {
    $(id).hidden = !visible;
  };
  const icons = () => window.lucide?.createIcons();
  function safeImageUrl(value) {
    if (typeof value !== "string" || value.length > 2048) return null;
    try {
      const url = new URL(value);
      return url.protocol === "https:" ? url.href : null;
    } catch {
      return null;
    }
  }
  function memberVisual(url, fallbackIcon) {
    const visual = document.createElement("span");
    visual.className = "member-visual";
    const fallback = document.createElement("i");
    fallback.setAttribute("data-lucide", fallbackIcon);
    visual.append(fallback);
    const source = safeImageUrl(url);
    if (source) {
      const image = document.createElement("img");
      image.src = source;
      image.alt = "";
      image.loading = "lazy";
      image.referrerPolicy = "no-referrer";
      image.addEventListener("load", () => fallback.remove(), { once: true });
      image.addEventListener("error", () => image.remove(), { once: true });
      visual.prepend(image);
    }
    return visual;
  }
  function renderMemberAssets(inventory = {}) {
    const detailHost = $("member-details");
    const medalHost = $("medal-list");
    detailHost.replaceChildren();
    medalHost.replaceChildren();
    const details = Array.isArray(inventory.details)
      ? inventory.details.filter(item => item && typeof item.label === "string" && (typeof item.value === "string" || typeof item.value === "number"))
      : [];
    const medals = Array.isArray(inventory.medals)
      ? inventory.medals.filter(item => item && typeof item.name === "string").slice(0, 48)
      : [];
    const expiring = details.find(item => item.label === "即将过期积分");
    text("asset-expiring-points", `即将过期 ${expiring?.value ?? 0}`);
    text("medal-count", `共 ${medals.length} 枚`);
    details.forEach((item) => {
      if (item.label === "即将过期积分") return;
      const row = document.createElement("div");
      row.className = "member-detail";
      row.append(memberVisual(item.iconUrl, "sparkles"));
      const copy = document.createElement("div");
      const label = document.createElement("small");
      const value = document.createElement("strong");
      label.textContent = item.label;
      value.textContent = String(item.value);
      copy.append(label, value);
      row.append(copy);
      detailHost.append(row);
    });
    medals.forEach((item) => {
      const row = document.createElement("div");
      row.className = "medal-item";
      row.append(memberVisual(item.iconUrl, "medal"));
      const copy = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = item.name;
      copy.append(name);
      if (typeof item.description === "string" && item.description) {
        const description = document.createElement("small");
        description.textContent = item.description;
        copy.append(description);
      }
      row.append(copy);
      medalHost.append(row);
    });
    show("member-details", details.some(item => item.label !== "即将过期积分"));
    show("member-medals", true);
  }
  const date = (value, withTime = true) =>
    value
      ? new Intl.DateTimeFormat("zh-CN", {
          timeZone: "Asia/Shanghai",
          month: "2-digit",
          day: "2-digit",
          ...(withTime
            ? { hour: "2-digit", minute: "2-digit", hour12: false }
            : {}),
        }).format(new Date(value))
      : "--";
  const shanghaiParts = (value = new Date()) =>
    Object.fromEntries(
      new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      })
        .formatToParts(value)
        .filter((part) => part.type !== "literal")
        .map((part) => [part.type, part.value]),
    );
  const businessDate = (value = new Date()) => {
    const parts = shanghaiParts(value);
    return `${parts.year}-${parts.month}-${parts.day}`;
  };
  const businessDateLabel = (value = new Date()) => {
    const parts = shanghaiParts(value);
    return `${parts.year} 年 ${parts.month} 月 ${parts.day} 日`;
  };
  function notice(message, good = false) {
    text("notice", message);
    $("notice").classList.toggle("good", good);
    show("notice", !!message);
  }
  function renderDisconnectedState() {
    const disconnected = localDisconnected || !token;
    show("disconnected-state", disconnected);
    document.querySelector("main").classList.toggle("is-disconnected", disconnected);
  }
  async function api(path, body) {
    if (!token) throw new Error("页面已失去本机连接，请重新双击打开领+。");
    let response;
    try {
      response = await fetch(path, {
        method: body === undefined ? "GET" : "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(path === "/api/binding/run" ? 130000 : 45000),
      });
    } catch (error) {
      localDisconnected = true;
      renderDisconnectedState();
      throw error;
    }
    let result;
    try {
      result = await response.json();
    } catch (error) {
      localDisconnected = true;
      renderDisconnectedState();
      throw error;
    }
    if (!isTerminating()) {
      localDisconnected = response.status === 401;
      renderDisconnectedState();
    }
    if (response.status === 401) {
      sessionStorage.removeItem(tokenKey);
      throw new Error("客户端已锁定，请返回领+登录。");
    }
    if (!result.ok)
      throw new Error(result.error || "无法连接本机助手，请重新打开程序。");
    return result.data;
  }
  let lastActivitySentAt = 0;
  function reportActivity() {
    const now = Date.now();
    if (!token || localDisconnected || now - lastActivitySentAt < 30000) return;
    lastActivitySentAt = now;
    api('/api/activity', {}).catch(() => {});
  }
  if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    ['pointerdown', 'keydown'].forEach((event) => window.addEventListener(event, reportActivity, { passive: true }));
  }
  function setButtonLoading(button, loading, label = "处理中") {
    if (!button) return;
    if (loading) {
      button.classList.add("is-loading");
      button.dataset.loadingLabel = `${label}…`;
      button.setAttribute("aria-busy", "true");
    } else {
      button.classList.remove("is-loading");
      delete button.dataset.loadingLabel;
      button.removeAttribute("aria-busy");
    }
  }
  async function perform(operation, message, trigger, loadingLabel) {
    const activeButton = trigger?.closest?.("button") || document.activeElement?.closest?.("button");
    if (activeButton?.dataset.operationBusy === "true") return;
    const wasDisabled = activeButton?.disabled;
    activeOperations += 1;
    if (activeButton) activeButton.dataset.operationBusy = "true";
    setButtonLoading(activeButton, true, loadingLabel);
    if (activeButton) activeButton.disabled = true;
    try {
      await operation();
      if (message) notice(message, true);
      state = await api("/api/status");
      render();
      routeRequiredRebind();
    } catch (error) {
      try {
        state = await api("/api/status");
        render();
      } catch {
        // Preserve the original operation error when the local helper is unavailable.
      }
      notice(
        error.name === "TimeoutError"
          ? "等待响应超时，请刷新状态后重试。"
          : error.message || "操作未完成，请重试。",
      );
    } finally {
      setButtonLoading(activeButton, false);
      if (activeButton) {
        activeButton.disabled = wasDisabled;
        delete activeButton.dataset.operationBusy;
      }
      activeOperations -= 1;
      if (state) render();
    }
  }
  function navigate(next, { startProxy = true } = {}) {
    if (view === "bind" && next !== "bind") {
      qrRequestId += 1;
      if (!qrUrl) qrPair = null;
    }
    view = next;
    text(
      "page-title",
      {
        overview: "概览",
        bind: "绑定账号",
        history: "运行记录",
      }[view],
    );
    render();
    if (next === "bind" && startProxy && !state?.proxy?.running) void autoStartProxy();
  }
  function routeRequiredRebind() {
    if (state?.binding?.status !== "needs_rebind" || !state.hasIdentity || view === "bind") return;
    navigate("bind", { startProxy: false });
    notice("云端登录态已失效，请重新抓包并保存。启动抓包后再用手机完成连接。");
  }
  function resetBindingFlow() {
    proxyConfirmed = false;
    selectedBindingStep = null;
    activePair = null;
    verifiedCandidateKey = null;
    qrPair = null;
    qrFailedPair = null;
    qrRequestId += 1;
    if (qrUrl) {
      URL.revokeObjectURL(qrUrl);
      qrUrl = null;
    }
    $("pair-qr").removeAttribute("src");
    $("proxy-removed").checked = false;
  }
  function runTable(target, items) {
    const container = $(target);
    container.replaceChildren();
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "table-empty";
      empty.textContent = "暂无运行记录";
      container.append(empty);
      return;
    }
    const wrapper = document.createElement("div");
    wrapper.className = "table-wrap";
    const table = document.createElement("table");
    table.className = "run-table";
    const head = table.createTHead().insertRow();
    ["执行时间", "任务", "结果", "签到 / 分享", "奖励"].forEach((label) => {
      const cell = document.createElement("th");
      cell.textContent = label;
      head.append(cell);
    });
    const body = table.createTBody();
    items.forEach((run) => {
      const row = body.insertRow();
      row.insertCell().textContent = run.startedAt
        ? date(run.startedAt)
        : run.businessDate;
      row.insertCell().textContent = "每日任务";
      const result = row.insertCell();
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = labels[run.status] || run.status;
      if (["failed", "error"].includes(run.status))
        badge.classList.add("error");
      if (
        ["queued", "pending", "running", "partial", "unknown"].includes(
          run.status,
        )
      )
        badge.classList.add("warning");
      result.append(badge);
      if (run.message) {
        const detail = document.createElement("span");
        detail.className = "detail";
        detail.textContent = run.message;
        result.append(detail);
      }
      Object.entries(run.notifications || {}).forEach(([channel, status]) => {
        const detail = document.createElement("span");
        detail.className = "detail";
        detail.textContent = `${channel === "bark" ? "Bark" : "Server 酱"}：${{sent:"已推送",failed:"推送失败",sending:"推送待确认"}[status] || status}`;
        result.append(detail);
      });
      row.insertCell().textContent = `${labels[run.signStatus] || run.signStatus || "--"} / ${labels[run.shareStatus] || run.shareStatus || "--"}`;
      const rewardCell = row.insertCell();
      rewardCell.className = "reward-list";
      const rewards = run.rewards || {};
      const lines = [`积分：${run.pointsBefore ?? "暂无"} → ${run.pointsAfter ?? "暂无"}`];
      if (rewards.signEnergy != null) lines.push(`签到能量体：+${rewards.signEnergy}`);
      if (run.shareStatus === "success") lines.push(`分享后能量体：${rewards.sharePointsBefore ?? "暂无"} → ${rewards.sharePointsAfter ?? "暂无"}`);
      else lines.push(`分享：${labels[run.shareStatus] || "暂无"}`);
      if (rewards.cardsBefore != null && rewards.cardsAfter != null) {
        const change = rewards.cardsAfter - rewards.cardsBefore;
        lines.push(`签到卡：${rewards.cardsBefore} → ${rewards.cardsAfter}${change > 0 ? `（净增 ${change} 张）` : ""}`);
      } else lines.push("签到卡奖励：暂无数据");
      lines.forEach((value) => {
        const line = document.createElement("span");
        line.className = value.includes("能量") ? "reward energy" : value.includes("签到卡") ? "reward card" : value.includes("分享") ? "reward error" : "reward";
        line.textContent = value;
        rewardCell.append(line);
      });
    });
    wrapper.append(table);
    container.append(wrapper);
  }
  function applyCapabilities() {
    const binding = state?.binding,
      candidate = state?.candidate,
      capture = state?.capture || {},
      proxy = state?.proxy || {};
    const readiness = capture.readiness || {};
    text("bind-share-hint", readiness.share ? "" : "请在领克 App 首页打开一篇文章，分享一次。");
    $("settings-form")
      .querySelectorAll("input,select,button")
      .forEach((element) => {
        element.disabled = !binding;
      });
    $("binding-unbind").disabled = !binding;
    $("run-share-now").disabled = !binding || binding.doShare !== true;
    ["bind", "settings"].forEach(prefix => {
      const available = selectedSlotAvailable(prefix);
      const form = $(prefix === "bind" ? "activate-form" : "settings-form");
      form.querySelector('[type="submit"]').disabled = !available || (prefix === "bind" ? !(candidate || (readiness.login && readiness.device && readiness.share && readiness.vehicle)) : !binding);
    });
    $("bind-save").disabled = (!(state.candidate || (readiness.login && readiness.device && readiness.share && readiness.vehicle))) || !selectedSlotAvailable("bind");
    $("proxy-next").disabled = !proxy.running || !proxy.paired || proxy.needsReconfigure;
    $("proxy-reconfigure").disabled = !proxy.address || (!proxy.running && !proxy.needsReconfigure);
    $("stop-proxy").disabled = !$("proxy-removed").checked;
  }
  function selectedSlotAvailable(prefix) {
    const value = $(`${prefix}-window`).value;
    return !!state?.scheduleWindows?.items?.some(item =>
      item.value === value && item.mode === "execution" && item.selectable !== false && (item.remaining > 0 || item.current));
  }
  function updatePushChoice() {
    const selected = document.querySelector('input[name="push-channel"]:checked')?.value || "none";
    show("push-fields", selected !== "none");
    ["bark", "serverchan"].forEach(channel => {
      const active = selected === channel;
      show(`${channel}-field`, active);
      $(`${channel}-key`).disabled = !active || !state?.binding;
      $(`${channel}-clear`).disabled = !active || !state?.binding;
    });
    $("push-save").disabled = !state?.binding;
    text("push-save-label", selected === "none" ? "保存设置" : "保存并测试");
  }
  function updateBindPushChoice() {
    const selected = document.querySelector('input[name="bind-push-channel"]:checked')?.value || "none";
    show("bind-push-fields", selected !== "none");
    ["bark", "serverchan"].forEach(channel => {
      const active = selected === channel;
      show(`bind-${channel}-field`, active);
      $(`bind-${channel}-key`).disabled = !active;
    });
  }
  function renderScheduleWindows() {
    const items = state.scheduleWindows?.items;
    ["bind", "settings"].forEach(prefix => {
      const select = $(`${prefix}-window`);
      const previous = select.value;
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = items ? "请选择执行区间" : "名额暂不可用";
      placeholder.disabled = true;
      const options = Array.from({length: 12}, (_, index) => {
        const value = hourWindow(index * 2);
        const slot = items?.find(item => item.value === value);
        const option = document.createElement("option");
        option.value = value;
        const execution = slot?.mode === "execution" && slot.selectable !== false;
        option.textContent = `${hourLabel(value)} · ${!slot ? "名额未知" : slot.mode === "maintenance" ? `维护期${slot.maintenanceLogic ? ` · ${slot.maintenanceLogic}` : ""}` : slot.mode === "unassigned" ? "未配置" : `剩余 ${slot.remaining}/${slot.limit}${slot.current ? " · 当前区间" : ""}`}`;
        option.disabled = !execution || (slot.remaining <= 0 && !slot.current);
        return option;
      });
      select.replaceChildren(placeholder, ...options);
      const preferred = options.find(option => option.value === previous && !option.disabled)
        || options.find(option => !option.disabled);
      select.value = preferred?.value || "";
      text(`${prefix}-quota-hint`, items ? "暂停任务仍保留名额，保存时以云端剩余名额为准。" : "暂时无法读取剩余名额，请刷新云端状态后再保存。");
      if (prefix === "settings") {
        const selected = items?.find(item => item.value === select.value);
        const available = selected && (selected.remaining > 0 || selected.current);
        badge("quota-badge", available ? "名额充足" : items ? "名额已满" : "名额查询中", available ? "blue" : "warning");
      }
    });
  }
  function render() {
    if (!state) return;
    const clientMeta = $("client-meta");
    if (clientMeta) {
      clientMeta.replaceChildren(document.createTextNode(`${state.clientVersion || "开发版"} · GitHub `));
      const link = document.createElement("a");
      link.href = "https://github.com/shovelshit/lynkco-plus-desktop";
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = "@shovelshit";
      clientMeta.append(link, document.createTextNode(" · 北京时间 UTC+8"));
    }
    const binding = state.binding,
      capture = state.capture,
      proxy = state.proxy;
    renderDisconnectedState();
    ["overview", "bind", "history"].forEach((name) =>
      show(
        "view-" + name,
        name === view && state.hasIdentity,
      ),
    );
    const online = state.connected && state.configured;
    $("cloud-status").replaceChildren();
    const dot = document.createElement("span");
    dot.className = "dot " + (online ? "online" : "offline");
    $("cloud-status").append(
      dot,
      document.createTextNode(
        online ? "云端已连接" : state.connected ? "云端待配置" : "云端未连接",
      ),
    );
    text(
      "last-sync",
      state.lastRefreshAt ? `${date(state.lastRefreshAt)} 更新` : "尚未同步",
    );
    show("empty-account", !binding);
    show("account-overview", !!binding);
    renderScheduleWindows();
    if (settingsBindingId !== binding?.id) {
      settingsBindingId = binding?.id;
      settingsVersion = "";
      settingsDirty = false;
      $("bark-key").value = $("serverchan-key").value = "";
    }
    if (binding) {
      text("account-label", binding.label);
      const avatarUrl = binding.avatarUrl || binding.avatarurl || binding.profile?.avatarUrl || binding.profile?.avatarurl;
      const avatar = $("account-avatar");
      avatar.replaceChildren();
      if (avatarUrl) {
        const image = document.createElement("img");
        image.src = avatarUrl;
        image.alt = "账号头像";
        image.onerror = () => { avatar.textContent = (binding.label || "账").slice(0, 1); };
        avatar.append(image);
      } else {
        avatar.textContent = (binding.label || "账").slice(0, 1);
      }
      badge("account-status", labels[binding.status] || binding.status, binding.status === "active" ? "success" : "warning");
      text("account-window", `每日 ${hourLabel(binding.scheduleTime)}`);
      text("account-streak", `连续签到 ${binding.inventory?.days ?? "--"} 天`);
      text(
        "next-run",
        binding.status === "active" ? date(binding.nextRunAt) : "--",
      );
      const latest = state.runs.items[0];
      const latestIsToday = latest?.businessDate === businessDate();
      const todayStatus = latestIsToday ? labels[latest.status] || latest.status : "待执行";
      const statusTone = latestIsToday && ["completed", "success", "succeeded", "already_signed", "signed"].includes(latest.status)
        ? "success"
        : latestIsToday && ["failed", "error"].includes(latest.status)
          ? "error"
          : latestIsToday
            ? "warning"
            : "";
      text("today-date", businessDateLabel());
      badge("today-status", todayStatus, statusTone);
      const pointsDelta = latest?.pointsBefore != null && latest?.pointsAfter != null
        ? Number(latest.pointsAfter) - Number(latest.pointsBefore)
        : null;
      text("sign-task-detail", latestIsToday && latest.signStatus === "already_signed" ? "今日已完成，重复触发会自动跳过" : "每天执行一次");
      text("sign-task-reward", pointsDelta == null ? "待执行" : `${pointsDelta >= 0 ? "+" : ""}${pointsDelta} 积分`);
      taskIcon("sign-task-icon", latestIsToday ? latest.signStatus : "pending");
      text("share-task-detail", binding.doShare ? "签到时同时完成分享" : "当前未开启分享");
      const shareEnergy = latest?.rewards?.signEnergy ?? latest?.shareEnergy;
      text("share-task-reward", !binding.doShare ? "已关闭" : shareEnergy == null ? "待执行" : `+${shareEnergy} 能量体`);
      taskIcon("share-task-icon", !binding.doShare ? "disabled" : latestIsToday ? latest.shareStatus : "pending");
      text("next-run-label", binding.status === "active" && binding.nextRunAt ? `下一次执行：${date(binding.nextRunAt)}` : "下一次执行：已暂停");
      const inventory = binding.inventory;
      text("points", inventory?.points ?? latest?.pointsAfter ?? latest?.pointsBefore ?? "--");
      text("sign-cards", inventory?.cards != null ? inventory.cards : binding.inventoryError ? "查询失败" : "暂无");
      text("energy", inventory?.energy != null ? `${inventory.energy}` : latest?.energyAfter != null ? `${latest.energyAfter}` : binding.inventoryError ? "查询失败" : "暂无");
      renderMemberAssets(inventory);
      text("asset-updated", state.lastRefreshAt ? date(state.lastRefreshAt) : "暂无");
      text(
        "last-result",
        latest ? labels[latest.status] || latest.status : "暂无记录",
      );
      text(
        "last-run-date",
        latest ? date(latest.startedAt || latest.finishedAt) : "等待首次执行",
      );
      $("pause").querySelector("span").textContent =
        binding.status === "paused" ? "恢复任务" : "暂停任务";
      const version = JSON.stringify([
        binding.id,
        binding.label,
        binding.scheduleTime,
        binding.doShare,
        binding.notifications,
      ]);
      if (version !== settingsVersion && !settingsDirty) {
        $("settings-window").value = hourWindow(Number(binding.scheduleTime.slice(0, 2)));
        let selectedPushChannel = null;
        ["bark", "serverchan"].forEach(channel => {
          const config = binding.notifications?.[channel];
          const enabled = !!config?.enabled && !selectedPushChannel;
          if (enabled) selectedPushChannel = channel;
          $(`${channel}-enabled`).checked = enabled;
          $(`${channel}-key`).placeholder = config?.configured ? "已保存，留空保持不变" : channel === "bark" ? "未配置" : "SCT 或 sctp 开头";
        });
        $("push-none").checked = !selectedPushChannel;
        settingsVersion = version;
      }
    }
    runTable("recent-runs", state.runs.items.slice(0, 5));
    if (!historyItems.length) {
      runTable("all-runs", state.runs.items);
      historyCursor = state.runs.nextCursor;
    }
    show("more-runs", !!historyCursor);
    if (activePair !== proxy.pairUrl) {
      activePair = proxy.pairUrl;
      qrRequestId += 1;
      proxyConfirmed = false;
      selectedBindingStep = null;
      verifiedCandidateKey = null;
      qrFailedPair = null;
      if (qrUrl) {
        URL.revokeObjectURL(qrUrl);
        qrUrl = null;
        $("pair-qr").removeAttribute("src");
      }
    }
    const stage = capture.stage;
    const events = capture.events || [];
    const hasTunnel = events.some((event) => event.outcome === "tunnel");
    const hasHttpsResponse = events.some((event) => typeof event.outcome === "string"
      && !["pending", "tunnel", "network_error"].includes(event.outcome));
    if (["idle", "waiting", "captured"].includes(stage)) verifiedCandidateKey = null;
    const candidateKey = state.candidate?.expiresAt || state.candidate?.preview?.displayName || null;
    if (stage === "verified" && state.candidate && verifiedCandidateKey !== candidateKey) {
      verifiedCandidateKey = candidateKey;
      selectedBindingStep = 2;
    }
    if (proxy.needsReconfigure || proxy.networkChanged) {
      selectedBindingStep = 0;
      proxyConfirmed = false;
      if (qrUrl) {
        URL.revokeObjectURL(qrUrl);
        qrUrl = null;
      }
      qrPair = null;
      qrFailedPair = null;
      qrRequestId += 1;
      $("pair-qr").removeAttribute("src");
    }
    const captureReady = !!(capture.readiness?.login && capture.readiness?.device && capture.readiness?.share && capture.readiness?.vehicle);
    if (captureReady && !captureWasReady) selectedBindingStep = 2;
    captureWasReady = captureReady;
    const flowStep = stage === "cleanup" ? 3
      : (stage === "verified" && state.candidate) || captureReady ? 2
      : proxyConfirmed || ["captured", "verified"].includes(stage) ? 1 : 0;
    const accessibleSteps = new Set([0]);
    if (proxy.running) [1, 3].forEach(item => accessibleSteps.add(item));
    if (state.candidate || captureReady) accessibleSteps.add(2);
    if (selectedBindingStep != null && !accessibleSteps.has(selectedBindingStep)) selectedBindingStep = null;
    const step = selectedBindingStep ?? flowStep;
    $("view-bind").classList.toggle("binding-start-layout", step === 0);
    document.querySelectorAll("[data-binding-step]").forEach((button) => {
      const item = button.closest("li");
      const itemStep = Number(button.dataset.bindingStep);
      button.disabled = !accessibleSteps.has(itemStep);
      item.classList.toggle("active", itemStep === step);
      item.classList.toggle("done", itemStep < flowStep);
    });
    show("bind-start", step === 0 && stage !== "cleanup");
    show("bind-back", !!binding && !proxy.running && stage !== "cleanup");
    show("proxy-start", !proxy.running && !proxyStarting && stage !== "cleanup");
    show("bind-connect", [0, 1].includes(step) && stage !== "cleanup");
    show("pairing-step", step === 0);
    show("capture-step", step === 1);
    show("capture-details", step === 1);
    show("capture-wait", stage === "waiting" || stage === "captured");
    show("capture-ready", stage === "verified");
    show("capture-verifying", stage === "verifying");
    show("capture-verification-error", stage === "verification_failed");
    show("bind-confirm", step === 2 && (captureReady || !!state.candidate));
    show("bind-cleanup", step === 3);
    show("save-complete", stage === "cleanup");
    show("unsaved-exit", step === 3 && stage !== "cleanup");
    show("disconnect-panel", proxy.running && step === 3);
    text("traffic-status", events.length ? `代理已收到请求 · 当前 ${events.length} 条记录` : "尚未收到代理请求");
    text("proxy-address", proxy.address);
    text("proxy-port", proxy.port);
    $("network").disabled = proxy.running && !proxy.networkChanged;
    show("proxy-runtime-state", proxy.running || proxy.needsReconfigure || proxyStarting);
    text("proxy-runtime-status", proxyStarting ? "启动中" : proxy.needsReconfigure ? "需要重新配置" : proxy.running ? "运行中" : "未启动");
    text("proxy-runtime-help", proxy.needsReconfigure
      ? "电脑网络已变化，请选择当前网络后重新启动代理。"
      : proxyStarting
        ? "正在启动电脑代理，请稍候。"
      : proxy.running
        ? "请将手机 Wi-Fi 代理设为手动，完成后开始抓包。"
        : "启动后这里会显示手机要填写的服务器和端口。");
    $("pair-qr-caption").textContent = proxy.pairUrl
      ? proxy.paired ? "已配对，无需重复扫码" : "使用手机相机扫码"
      : "启动电脑代理后显示二维码；使用手机相机扫码";
    $("pair-qr-placeholder").hidden = !!qrUrl;
    if (!proxy.pairUrl) {
      $("pair-qr-placeholder").hidden = false;
      $("pair-qr-placeholder").textContent = proxy.needsReconfigure
        ? "网络已变化，请重置网络"
        : "启动电脑代理后显示二维码";
      $("pair-qr").hidden = true;
      if (qrUrl) {
        URL.revokeObjectURL(qrUrl);
        qrUrl = null;
        $("pair-qr").removeAttribute("src");
      }
    } else if (!qrUrl) {
      $("pair-qr-placeholder").hidden = false;
      $("pair-qr-placeholder").textContent = qrFailedPair === proxy.pairUrl
        ? "二维码加载失败，请重试" : "二维码加载中…";
      $("pair-qr").hidden = true;
    }
    show("pair-qr-retry", !!proxy.pairUrl && qrFailedPair === proxy.pairUrl);
    show("pairing-network-warning", !!proxy.networkChanged || !!proxy.needsReconfigure);
    text("pairing-network-warning", "二维码失效或电脑网络已变化时，请点击“重置网络”。");
    text("phone-paired", proxy.paired
      ? events.length
        ? "手机请求已检测，证书状态将通过真实 HTTPS 请求推断"
        : "手机已配对；代理会话未重启且网络未变化时无需重复扫码"
      : proxy.needsReconfigure
        ? "网络已变化，请重新配置后扫码"
        : "等待手机扫码/请求；未配对不代表证书失败");
    text("proxy-running-state", proxy.needsReconfigure ? "需重新配置" : proxy.running ? "运行中" : "未启动");
    text("phone-request-state", proxy.paired ? (events.length ? "已检测" : "等待请求") : "等待扫码/请求");
    text("tls-state", hasHttpsResponse ? "HTTPS 请求已通过" : hasTunnel ? "通道已建立" : "未开始");
    text("certificate-state", hasHttpsResponse ? "已通过请求推断" : hasTunnel ? "等待 HTTPS 验证" : "未确认");
    statusDot("proxy-running-dot", proxy.needsReconfigure || proxyStarting ? "yellow" : proxy.running ? "green" : "gray");
    statusDot("phone-request-dot", events.length || proxy.paired ? "green" : proxy.running ? "yellow" : "gray");
    statusDot("tls-dot", hasHttpsResponse ? "green" : hasTunnel ? "yellow" : "gray");
    statusDot("certificate-dot", hasHttpsResponse ? "green" : hasTunnel ? "yellow" : "gray");
    const readiness = capture.readiness || {};
    const loginReady = !!readiness.login;
    const deviceReady = !!readiness.device;
    const shareReady = !!readiness.share;
    const vehicleReady = !!readiness.vehicle;
    $("capture-login-state").className = loginReady ? "capture-state ready" : "capture-state";
    $("capture-login-state").innerHTML = `<i class="state-dot"></i>${loginReady ? "已抓到" : "等待识别"}`;
    [["capture-device-state", deviceReady], ["capture-share-state", shareReady], ["capture-vehicle-state", vehicleReady]].forEach(([id, ready]) => {
      $(id).className = ready ? "capture-state ready" : "capture-state";
      $(id).innerHTML = `<i class="state-dot"></i>${ready ? "已抓到" : "等待识别"}`;
    });
    if (stage === "verifying") {
      text("auto-verify-status", "正在静默验证个人信息，请保持手机代理开启。");
    } else if (stage === "verification_failed") {
      text("capture-verification-message", capture.verificationError || "个人信息验证暂时失败，请稍后重试。");
      text("auto-verify-status", "验证未完成，不会上传或保存本次登录状态。");
    } else if (stage === "verified") {
      text("auto-verify-status", "个人信息验证完成，可以进入下一步。");
    } else if (stage === "captured") {
      text("auto-verify-status", !readiness.login
        ? "请打开领克 App"
        : !readiness.device
          ? "请退出账号后，使用手机验证码重新登录"
          : !readiness.share
            ? "请在领克 App 首页打开一篇文章，分享一次"
            : !readiness.vehicle
              ? "请在领克 App 中打开车辆页面，识别车架号"
            : "信息已准备好，请确认保存。");
    }
    if (step === 0 && proxy.pairUrl && qrPair !== proxy.pairUrl) {
      const requestedPair = proxy.pairUrl;
      const requestId = ++qrRequestId;
      qrPair = requestedPair;
      fetch("/api/capture/qr", {
        headers: { Authorization: `Bearer ${token}` },
      })
        .then((response) => {
          if (!response.ok) throw new Error("二维码加载失败，请重试");
          return response.blob();
        })
        .then((blob) => {
          if (requestId !== qrRequestId || state?.proxy?.pairUrl !== requestedPair) return;
          if (view !== "bind" || (selectedBindingStep ?? flowStep) !== 0) {
            qrPair = null;
            return;
          }
          if (qrUrl) URL.revokeObjectURL(qrUrl);
          qrUrl = URL.createObjectURL(blob);
          qrFailedPair = null;
          $("pair-qr").src = qrUrl;
          $("pair-qr").hidden = false;
          $("pair-qr-placeholder").hidden = true;
          show("pair-qr-retry", false);
        })
        .catch((error) => {
          if (requestId !== qrRequestId || state?.proxy?.pairUrl !== requestedPair) return;
          if (view === "bind" && (selectedBindingStep ?? flowStep) === 0 && state?.proxy?.pairUrl === requestedPair) {
            qrFailedPair = requestedPair;
            text("pair-qr-placeholder", "二维码加载失败，请重试");
            show("pair-qr-retry", true);
            notice(error.message);
          } else qrPair = null;
        });
    }
    text(
      "phone-instructions-title",
      platform === "IOS" ? "iPhone 证书设置" : "安卓证书设置",
    );
    text(
      "phone-instructions",
      platform === "IOS"
        ? "1. 设置 → 通用 → VPN 与设备管理，安装证书。\n2. 设置 → 通用 → 关于本机 → 证书信任设置，开启信任。"
        : "在设置 → 安全 → 加密与凭据中安装 CA 证书；不同机型的入口名称可能不同。",
    );
    if (state.candidate) {
      text("preview-points", state.candidate.preview.verified ? "已通过" : "待重新验证");
      text(
        "preview-sign",
        "云端",
      );
      text(
        "candidate-expiry",
        `请在 ${date(state.candidate.expiresAt)} 前确认保存。超时可重新验证；这不是账号登录态的有效期。`,
      );
    } else if (captureReady) {
      const deadline = Number(capture.capturedAt) + 30 * 60 * 1000;
      text("preview-points", "资料已齐，待保存");
      text("candidate-expiry", `资料仅在本机暂存，请在 ${date(deadline)} 前确认保存。确认窗口最长 30 分钟，补充资料不会延长期限；确认前不会上传到云端。`);
    }
    const accessKey = `${capture.captureId || ""}:${capture.accessRevision || ""}`;
    const cachedAccessKey = sessionStorage.getItem(captureIdKey);
    const cachedAccessExpiry = Number(sessionStorage.getItem(captureExpireKey) || 0);
    const hasCachedAccess = cachedAccessKey === accessKey && cachedAccessExpiry > Date.now()
      && !!sessionStorage.getItem(captureAccessKey);
    if (stage !== "captured") {
      sessionStorage.removeItem?.(captureAccessKey);
      sessionStorage.removeItem?.(captureExpireKey);
      sessionStorage.removeItem?.(captureIdKey);
      captureAccessCaptureId = null;
      captureAccessRequested = false;
    } else if (hasCachedAccess) {
      captureAccessCaptureId = accessKey;
      captureAccessRequested = true;
    } else if (accessKey !== captureAccessCaptureId || (cachedAccessKey && !hasCachedAccess)) {
      sessionStorage.removeItem?.(captureAccessKey);
      sessionStorage.removeItem?.(captureExpireKey);
      sessionStorage.removeItem?.(captureIdKey);
      captureAccessCaptureId = accessKey || capture.capturedAt || null;
      captureAccessRequested = false;
    }
    if (stage === "captured" && !captureAccessRequested) {
      captureAccessRequested = true;
      const requestedCaptureId = capture.captureId;
      const requestedAccessRevision = capture.accessRevision;
      api("/api/capture/access-token-once", {}).then((access) => {
        if (state?.capture?.stage !== "captured" || state.capture.captureId !== requestedCaptureId || state.capture.accessRevision !== requestedAccessRevision || access?.captureId !== requestedCaptureId || access?.accessRevision !== requestedAccessRevision) return;
        if (access?.accessToken) sessionStorage.setItem(captureAccessKey, access.accessToken);
        if (access?.expireAt) sessionStorage.setItem(captureExpireKey, String(access.expireAt));
        if (access?.captureId) sessionStorage.setItem(captureIdKey, accessKey);
      }).catch(() => {
        if (state?.capture?.stage === "captured" && state.capture.captureId === requestedCaptureId && state.capture.accessRevision === requestedAccessRevision) captureAccessRequested = false;
      });
    }
    applyCapabilities();
    updatePushChoice();
    updateBindPushChoice();
    icons();
  }
  document
    .querySelectorAll("[data-go-bind]")
    .forEach((button) =>
      button.addEventListener("click", () => navigate("bind")),
    );
  document
    .querySelectorAll("[data-go-history]")
    .forEach((button) =>
      button.addEventListener("click", () => navigate("history")),
    );
  $("history-back").addEventListener("click", () => navigate("overview"));
  $("bind-back").addEventListener("click", () => navigate("overview"));
  $("proxy-start").addEventListener("click", (event) =>
    perform(() => ensureProxyStarted(), null, event.currentTarget, "启动中"),
  );
  document.querySelectorAll("[data-platform]").forEach((button) =>
    button.addEventListener("click", () => {
      platform = button.dataset.platform;
      document
        .querySelectorAll("[data-platform]")
        .forEach((item) => item.classList.toggle("selected", item === button));
      render();
    }),
  );
  async function copy(value) {
    try {
      await navigator.clipboard.writeText(value);
      notice("已复制", true);
    } catch {
      notice("无法访问剪贴板，请手动选择并复制。");
    }
  }
  document
    .querySelectorAll("[data-copy]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        copy($(button.dataset.copy).textContent),
      ),
    );
  $("proxy-next").addEventListener("click", () => {
    proxyConfirmed = true;
    selectedBindingStep = 1;
    render();
  });
  async function loadNetworks() {
    const previous = $("network").value || state?.proxy?.address || "";
    const networks = await api("/api/networks");
    $("network").replaceChildren();
    networks.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.address;
      option.textContent = `${item.address} · ${item.name}`;
      $("network").append(option);
    });
    if (!networks.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "未找到局域网，请连接 Wi-Fi";
      $("network").append(option);
    }
    if (networks.some((item) => item.address === previous)) {
      $("network").value = previous;
    }
    networksLoaded = true;
    return networks;
  }
  async function ensureProxyStarted(options = {}) {
    if (state?.proxy?.running) return state.proxy;
    if (proxyStartPromise) return proxyStartPromise;
    proxyStartPromise = (async () => {
      const networks = networksLoaded ? Array.from($("network").options || []).filter((option) => option.value) : await loadNetworks();
      if (!networks.length) return null;
      const address = options.address || $("network").value;
      if (!address) return null;
      proxyStarting = true;
      render();
      try {
        await api("/api/capture/start", { address, platform });
        state = await api("/api/status");
        render();
        return state.proxy;
      } finally {
        proxyStarting = false;
        if (state) render();
      }
    })().finally(() => { proxyStartPromise = null; });
    return proxyStartPromise;
  }
  async function autoStartProxy() {
    try {
      return await ensureProxyStarted();
    } catch (error) {
      if (!isTerminating()) {
        notice(error.message || "电脑代理启动失败，请重试。");
        if (state) render();
      }
      return null;
    }
  }
  $("refresh-network").addEventListener("click", (event) =>
    perform(loadNetworks, "电脑网络列表已更新", event.currentTarget, "读取中"),
  );
  async function rebindProxy() {
      const networks = await loadNetworks();
      const address = $("network").value;
      if (!networks.some((item) => item.address === address))
        throw new Error("请选择当前电脑正在使用的 Wi-Fi 或有线网络。");
      await api("/api/capture/rebind", { address, platform });
      proxyConfirmed = false;
      selectedBindingStep = 0;
      qrPair = null;
      qrFailedPair = null;
      qrRequestId += 1;
      if (qrUrl) {
        URL.revokeObjectURL(qrUrl);
        qrUrl = null;
      }
      $("pair-qr").removeAttribute("src");
  }
  $("proxy-reconfigure").addEventListener("click", (event) =>
    perform(rebindProxy, "网络已重置，已生成新的二维码", event.currentTarget, "重置中"),
  );
  $("pair-qr-retry").addEventListener("click", () => {
    qrPair = null;
    qrFailedPair = null;
    render();
  });
  document.querySelectorAll("[data-binding-step]").forEach((button) =>
    button.addEventListener("click", () => {
      selectedBindingStep = Number(button.dataset.bindingStep);
      render();
    }),
  );
  function chosenWindow(prefix) {
    return $(`${prefix}-window`).value;
  }
  function hourWindow(hour) {
    hour = Math.floor(hour / 2) * 2;
    return `${String(hour).padStart(2, "0")}:00-${String((hour + 2) % 24).padStart(2, "0")}:00`;
  }
  function hourLabel(value) {
    const hour = Number(value.slice(0, 2));
    return `${hour}-${hour + 2} 点`;
  }
  $("activate-form").addEventListener("submit", (event) => {
    event.preventDefault();
    perform(async () => {
      if (!selectedSlotAvailable("bind")) throw new Error("请选择仍有名额的执行区间，或刷新云端状态。");
      const selectedPush = document.querySelector('input[name="bind-push-channel"]:checked')?.value || "none";
      const barkKey = $("bind-bark-key").value.trim();
      const serverchanKey = $("bind-serverchan-key").value.trim();
      if (selectedPush === "bark" && !barkKey) throw new Error("请选择 Bark 后填写 Bark Device Key。");
      if (selectedPush === "serverchan" && !serverchanKey) throw new Error("请选择 Server 酱后填写 SendKey。");
      state.candidate = await api("/api/candidates/prepare", { consent: true });
      render();
      await api("/api/candidates/activate", {
        label: state.candidate.preview.displayName || "账号用户",
        scheduleTime: chosenWindow("bind"),
      });
      await api("/api/binding/settings", {
        notifications: {
          bark: {enabled: selectedPush === "bark", ...(barkKey ? {key: barkKey} : {})},
          serverchan: {enabled: selectedPush === "serverchan", ...(serverchanKey ? {key: serverchanKey} : {})},
        },
      });
      navigate("overview");
      notice("");
    }, null, event.submitter, "保存到云端");
  });
  $("stop-proxy").addEventListener("click", (event) =>
    perform(async () => {
      if (!$("proxy-removed").checked)
        throw new Error("请先在手机上关闭 Wi-Fi 代理。");
      await api("/api/capture/stop", { proxyRemoved: true });
      qrPair = null;
      if (state.binding) navigate("overview");
    }, "手机连接已断开，现在可以退出助手。", event.currentTarget, "断开中"),
  );
  $("proxy-removed").addEventListener("change", applyCapabilities);
  $("refresh").addEventListener("click", (event) =>
    perform(async () => {
      historyItems = [];
      await api("/api/refresh", {});
    }, "云端状态已更新", event.currentTarget, "刷新中"),
  );
  const runTaskNow = (button, kind, path, loadingLabel, runningMessage, completedMessage) =>
    button.addEventListener("click", (event) => perform(async () => {
      if (kind === "share" && state?.binding?.doShare !== true) throw new Error("当前账号尚未配置分享，请先完成绑定并开启分享。");
      notice(runningMessage);
      const result = await api(path, {mode: kind});
      await api("/api/refresh", {});
      const completed = result.status === "completed" || result.status === "success";
      notice(completed ? completedMessage : result.message || `${kind === "share" ? "分享" : "签到"}任务：${labels[result.status] || result.status}`, completed);
    }, null, event.currentTarget, loadingLabel));
  runTaskNow($("run-now"), "sign", "/api/binding/run", "签到中", "正在执行签到，请稍候。", "今日签到已完成。");
  runTaskNow($("run-share-now"), "share", "/api/binding/run", "分享中", "正在执行分享，请稍候。", "今日分享已完成。");
  runTaskNow($("cleanup-run-now"), "sign", "/api/binding/run", "签到中", "正在执行签到，请稍候。", "今日签到已完成。");
  $("pause").addEventListener("click", (event) =>
    perform(
      () =>
        api("/api/binding/settings", {
          status: state.binding.status === "paused" ? "active" : "paused",
        }),
      "任务状态已更新",
      event.currentTarget,
      state.binding.status === "paused" ? "恢复中" : "暂停中",
    ),
  );
  $("settings-form").addEventListener("input", () => { settingsDirty = true; });
  $("push-form").addEventListener("input", () => { settingsDirty = true; });
  document.querySelectorAll('input[name="push-channel"]').forEach((input) => input.addEventListener("change", () => {
    settingsDirty = true;
    updatePushChoice();
  }));
  document.querySelectorAll('input[name="bind-push-channel"]').forEach((input) => input.addEventListener("change", updateBindPushChoice));
  ["bind", "settings"].forEach(prefix => $(`${prefix}-window`).addEventListener("change", applyCapabilities));
  $("settings-form").addEventListener("submit", (event) => {
    event.preventDefault();
    perform(
      async () => {
        if (!selectedSlotAvailable("settings")) throw new Error("请选择仍有名额的执行区间，或刷新云端状态。");
        await api("/api/binding/settings", {
          scheduleTime: chosenWindow("settings"),
        });
        settingsDirty = false;
        settingsVersion = "";
      },
      "设置已保存",
      event.submitter,
      "保存中",
    );
  });
  $("push-form").addEventListener("submit", (event) => {
    event.preventDefault();
    perform(async () => {
      const selected = document.querySelector('input[name="push-channel"]:checked')?.value || "none";
      const notifications = {};
      ["bark", "serverchan"].forEach(channel => {
        const enabled = selected === channel;
        const key = $(`${channel}-key`).value.trim();
        if (enabled && !key && !state.binding.notifications?.[channel]?.configured)
          throw new Error(`请填写 ${channel === "bark" ? "Bark Device Key" : "Server 酱 SendKey"}。`);
        notifications[channel] = {enabled, ...(key ? {key} : {})};
      });
      await api("/api/binding/settings", {notifications});
      if (selected !== "none") await api("/api/binding/notification-test", {});
      $("bark-key").value = $("serverchan-key").value = "";
      settingsDirty = false;
      settingsVersion = "";
    }, document.querySelector('input[name="push-channel"]:checked')?.value === "none" ? "推送设置已保存" : "推送设置已保存，测试消息已发送", event.submitter, "保存并测试");
  });
  ["bark", "serverchan"].forEach(channel => {
    $(`${channel}-clear`).addEventListener("click", (event) => perform(async () => {
      await api("/api/binding/settings", {notifications:{[channel]:{clear:true}}});
      $(`${channel}-key`).value = "";
      $(`${channel}-enabled`).checked = false;
      $("push-none").checked = true;
      updatePushChoice();
      $(`${channel}-key`).placeholder = channel === "bark" ? "未配置" : "SCT 或 sctp 开头";
    }, "推送配置已清除", event.currentTarget, "清除中"));
  });
  function confirmDelete() {
    return new Promise((resolve) => {
      confirmResolve = resolve;
      text("confirm-title", "解除账号绑定？");
      text("confirm-message", "云端登录状态将被删除，每日任务会停止。");
      $("confirm-dialog").showModal();
    });
  }
  $("binding-settings").addEventListener("click", () => $("binding-settings-dialog").showModal());
  $("binding-settings-cancel").addEventListener("click", () => $("binding-settings-dialog").close());
  $("binding-replace").addEventListener("click", (event) => {
    perform(async () => {
      await api("/api/capture/reset", {});
      resetBindingFlow();
      $("binding-settings-dialog").close();
      view = "bind";
      notice("");
    }, null, event.currentTarget, "准备中");
  });
  $("binding-unbind").addEventListener("click", async () => {
    $("binding-settings-dialog").close();
    if (await confirmDelete()) {
      perform(async () => {
        await api("/api/binding/delete", { confirmed: true });
        historyItems = [];
        navigate("overview");
      }, "已解除绑定", $("binding-unbind"), "解绑中");
    }
  });
  $("confirm-cancel").addEventListener("click", () => {
    $("confirm-dialog").close();
    confirmResolve?.(false);
  });
  $("confirm-accept").addEventListener("click", () => {
    $("confirm-dialog").close();
    confirmResolve?.(true);
  });
  $("confirm-dialog").addEventListener("cancel", () => confirmResolve?.(false));
  $("more-runs").addEventListener("click", (event) =>
    perform(async () => {
      const result = await api("/api/history", { cursor: historyCursor });
      if (!historyItems.length) historyItems = [...state.runs.items];
      historyItems.push(...result.items);
      historyCursor = result.nextCursor;
      runTable("all-runs", historyItems);
    }, null, event.currentTarget, "加载中"),
  );
  $("quit").addEventListener("click", async (event) => {
    if (quitting) return;
    const button = event.currentTarget;
    quitting = true;
    button.disabled = true;
    setButtonLoading(button, true, "退出中");
    try {
      const quitResult = await api("/api/quit", {});
      terminated = true;
      stopPolling();
      localDisconnected = true;
      renderDisconnectedState();
      notice(quitResult?.phoneMustDisable ? "电脑代理已关闭，请手动关闭手机 Wi-Fi 代理。" : "助手已退出，可以关闭此页面。", true);
      document
        .querySelectorAll("button")
        .forEach((button) => (button.disabled = true));
    } catch (error) {
      notice(
        localDisconnected
          ? "本机连接已断开，请重新双击打开助手。"
          : error.message || "退出未完成，请重试。",
      );
      if (localDisconnected) {
        terminated = true;
        stopPolling();
        document
          .querySelectorAll("button")
          .forEach((item) => (item.disabled = true));
      } else {
        button.disabled = false;
        quitting = false;
      }
    } finally {
      setButtonLoading(button, false);
    }
  });
  let pollTimer = null;
  const isTerminating = () => quitting || terminated;
  function stopPolling() {
    if (pollTimer != null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }
  async function poll() {
    if (activeOperations > 0 || document.hidden || isTerminating()) return;
    try {
      const next = await api("/api/status");
      if (isTerminating()) return;
      if (JSON.stringify(next) !== JSON.stringify(state)) {
        state = next;
        render();
        routeRequiredRebind();
      }
    } catch (error) {
      if (isTerminating()) return;
      notice(error.message);
      render();
    }
  }
  async function start() {
    ["bind-window", "settings-window"].forEach((id) => {
      $(id).replaceChildren(...Array.from({ length: 12 }, (_, index) => {
        const hour = index * 2;
        const option = document.createElement("option");
        option.value = hourWindow(hour);
        option.textContent = hourLabel(option.value);
        return option;
      }));
      $(id).value = hourWindow(8);
    });
    icons();
    renderDisconnectedState();
    if (!token) {
      notice("请重新双击打开领+，以恢复本机连接。");
      return;
    }
    await poll();
    if (isTerminating()) return;
    try {
      const networks = await api("/api/networks");
      if (isTerminating()) return;
      $("network").replaceChildren(...networks.map((item) => {
        const option = document.createElement("option");
        option.value = item.address;
        option.textContent = `${item.address} · ${item.name}`;
        return option;
      }));
      networksLoaded = true;
      if (!networks.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "未找到局域网，请连接 Wi-Fi";
        $("network").append(option);
      }
    } catch (error) {
      if (isTerminating()) return;
      notice(error.message);
      if (state) render();
    }
    if (isTerminating()) return;
    if (view === "bind" && !state?.proxy.running) await autoStartProxy();
    if (isTerminating()) return;
    try {
      await api("/api/refresh", {});
      if (isTerminating()) return;
      state = await api("/api/status");
      if (isTerminating()) return;
      render();
    } catch (error) {
      if (isTerminating()) return;
      notice(error.message);
      if (state) render();
    }
    if (isTerminating()) return;
    routeRequiredRebind();
    if (isTerminating()) return;
    pollTimer = setInterval(() => {
      if (!isTerminating() && state?.proxy.running) poll();
    }, 2000);
  }
  start();
})();
