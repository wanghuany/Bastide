"use strict";

const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const state = {
  page: "dashboard",
  accounts: [],
  categories: [],
  entries: [],
  pageNumber: 1,
  initialized: false,
};
const titles = {
  dashboard: "财务总览",
  transactions: "交易记录",
  accounts: "我的账户",
  categories: "分类与规则",
  reports: "数据分析",
  imports: "账单导入",
  history: "操作历史",
  settings: "安全与智能体",
};
const paths = {
  dashboard:
    '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  transactions: '<path d="M4 7h16m-4-4 4 4-4 4M20 17H4m4-4-4 4 4 4"/>',
  wallet:
    '<path d="M20 8V5H5a2 2 0 0 0 0 4h16v11H5a2 2 0 0 1-2-2V7m13 7h5"/><circle cx="16" cy="14" r=".5"/>',
  chart: '<path d="M3 3v18h18M7 14l4-5 4 3 6-8"/>',
  tags: '<path d="m3 3 8 0 10 10-8 8L3 11Z"/><circle cx="7.5" cy="7.5" r="1"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  history: '<path d="M3 11a9 9 0 1 1 2 7M3 4v7h7m2-4v5l3 2"/>',
  settings:
    '<path d="m9 3-1 3-3 1-2 4 2 3 1 4 4 3 4-1 4-2 3-4-1-4-3-4-4-1Z"/><circle cx="12" cy="12" r="3"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/>',
  shield:
    '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
  plus: '<path d="M12 4v16M4 12h16"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1"/>',
};
function icons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((el) => {
    el.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[el.dataset.icon] || paths.tags}</svg>`;
  });
}
function today() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function monthRange(month) {
  const [y, m] = month.split("-").map(Number);
  return {
    start: `${month}-01`,
    end: `${month}-${new Date(y, m, 0).getDate()}`,
  };
}
function money(minor, signed = false) {
  const n = Number(minor);
  const v = (Math.abs(n) / 100).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return `${n < 0 ? "−" : signed && n > 0 ? "+" : ""}¥${v}`;
}
function toMinor(value, signed = false) {
  const text = String(value).trim();
  const pattern = signed ? /^-?\d+(\.\d{1,2})?$/ : /^\d+(\.\d{1,2})?$/;
  if (!pattern.test(text))
    throw Error("金额最多两位小数，请不要输入货币符号。");
  const negative = text.startsWith("-");
  const [whole, fraction = ""] = text.replace("-", "").split(".");
  const result = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  if (
    !Number.isSafeInteger(result) ||
    result > 9000000000000 ||
    (!signed && result === 0)
  )
    throw Error("请输入有效金额。");
  return negative ? -result : result;
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    $("toast").hidden = true;
  }, 5000);
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  let data;
  try {
    data = await response.json();
  } catch {
    throw Error("本地服务返回异常，请重试。");
  }
  if (!response.ok) {
    if (
      response.status === 401 &&
      ![/^\/api\/unlock/, /^\/api\/setup/, /^\/api\/restore/].some((r) =>
        r.test(path),
      )
    )
      await showAuth();
    throw Error(data.error?.message || "操作未完成，请重试。");
  }
  return data;
}
const post = (path, data = {}) =>
  api(path, { method: "POST", body: JSON.stringify(data) });
async function busy(form, action, errorId) {
  const buttons = [...form.querySelectorAll("button")];
  buttons.forEach((b) => (b.disabled = true));
  if (errorId) $(errorId).textContent = "";
  try {
    await action();
  } catch (error) {
    if (errorId) $(errorId).textContent = error.message;
    else toast(error.message);
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}
function option(id, label, selected) {
  return `<option value="${esc(id)}"${id === selected ? " selected" : ""}>${esc(label)}</option>`;
}
function categoryName(id) {
  const c = state.categories.find((x) => x.id === id);
  if (!c) return "未分类";
  const p = state.categories.find((x) => x.id === c.parent_id);
  return p ? `${p.name} / ${c.name}` : c.name;
}
function categoryOptions(
  kind = null,
  blank = "未分类 / 使用本地规则",
  selected = null,
) {
  const values = state.categories.filter((c) => !kind || c.kind === kind);
  return (
    option("", blank, selected) +
    values.map((c) => option(c.id, categoryName(c.id), selected)).join("")
  );
}
function accountOptions(blank = null, selected = null) {
  return (
    (blank !== null ? option("", blank, selected) : "") +
    state.accounts
      .filter((a) => !a.archived || a.id === selected)
      .map((a) => option(a.id, a.name, selected))
      .join("")
  );
}
function empty(
  title = "这里还没有记录",
  description = "记下第一笔收支，开始了解自己的财务。",
) {
  return `<div class="empty"><strong>${esc(title)}</strong><p>${esc(description)}</p></div>`;
}
function metric(label, value, caption, klass = "") {
  return `<div class="metric"><div class="metric-label">${esc(label)}</div><div class="metric-value ${klass}">${value}</div><div class="metric-caption">${esc(caption)}</div></div>`;
}
function renderMetrics(id, summary) {
  const total = (summary.accounts || state.accounts).reduce(
    (s, a) => s + (a.balance_minor || 0),
    0,
  );
  $(id).innerHTML =
    metric("账户总资产", money(total), "全部账户 · 包含期初余额") +
    metric(
      "区间收入",
      money(summary.income_minor),
      "不包含账户间转账",
      "positive",
    ) +
    metric(
      "区间支出",
      money(summary.expense_minor),
      `${summary.transaction_count} 笔非转账交易`,
    ) +
    metric(
      "区间结余",
      money(summary.net_minor),
      "收入 − 支出",
      summary.net_minor >= 0 ? "positive" : "negative",
    );
}
function chart(id, items) {
  if (!items.length || !items.some((x) => x.income_minor || x.expense_minor)) {
    $(id).innerHTML = empty(
      "暂无收支趋势",
      "录入真实交易后，这里会展示你的月度变化。",
    );
    return;
  }
  const width = 760,
    height = 230,
    left = 70,
    right = 12,
    top = 18,
    bottom = 35,
    plotH = height - top - bottom,
    plotW = width - left - right;
  const max = Math.max(
    100,
    ...items.flatMap((x) => [x.income_minor, x.expense_minor]),
  );
  const step = plotW / items.length;
  const bar = Math.min(18, step * 0.28);
  let svg = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="收入与支出月度柱状图">`;
  for (let i = 0; i <= 4; i++) {
    const y = top + (plotH * i) / 4;
    const value = Math.round(max * (1 - i / 4));
    svg += `<line class="gridline" x1="${left}" y1="${y}" x2="${width - right}" y2="${y}"/><text text-anchor="end" x="${left - 10}" y="${y + 4}">${esc(money(value))}</text>`;
  }
  items.forEach((item, i) => {
    const center = left + step * (i + 0.5);
    const ih = (plotH * item.income_minor) / max,
      eh = (plotH * item.expense_minor) / max;
    svg += `<rect class="income-bar" x="${center - bar - 1}" y="${top + plotH - ih}" width="${bar}" height="${ih}" rx="2"><title>${esc(item.period)} 收入 ${esc(money(item.income_minor))}</title></rect><rect class="expense-bar" x="${center + 1}" y="${top + plotH - eh}" width="${bar}" height="${eh}" rx="2"><title>${esc(item.period)} 支出 ${esc(money(item.expense_minor))}</title></rect>`;
    if (items.length <= 12 || i % Math.ceil(items.length / 12) === 0)
      svg += `<text text-anchor="middle" x="${center}" y="${height - 10}">${esc(item.period.slice(2))}</text>`;
  });
  $(id).innerHTML = svg + "</svg>";
}
function ranks(id, items) {
  if (!items.length) {
    $(id).innerHTML = empty("暂无支出数据", "录入支出后即可查看分布。");
    return;
  }
  const max = items[0].amount_minor || 1;
  const colors = [
    "var(--cat2)",
    "var(--cat3)",
    "var(--cat1)",
    "var(--cat4)",
    "var(--cat5)",
    "var(--cat6)",
  ];
  $(id).innerHTML = items
    .slice(0, 8)
    .map(
      (x, i) =>
        `<div class="rank-item"><div class="rank-heading"><span class="name">${esc(x.name)}</span><span class="amount">${esc(money(x.amount_minor))}</span></div><div class="rank-track"><div class="rank-fill" style="width:${Math.max(1, (x.amount_minor / max) * 100)}%;background:${colors[i % colors.length]}"></div></div></div>`,
    )
    .join("");
}
function transactionTable(rows, actions = false) {
  if (!rows.length)
    return empty(
      "暂无交易记录",
      "可以点击右上角“记一笔”，或者在账单导入中迁移已有数据。",
    );
  return `<div class="table-scroll"><table><thead><tr><th>日期</th><th>交易 / 商家</th><th>分类</th><th>账户</th><th class="num">金额</th>${actions ? '<th class="num">操作</th>' : ""}</tr></thead><tbody>${rows
    .map((r) => {
      const transfer = r.categorization_source === "transfer";
      const category = transfer ? "账户间转账" : categoryName(r.category_id);
      const account = state.accounts.find((a) => a.id === r.account_id);
      return `<tr><td class="muted">${esc(r.booked_date)}</td><td><div class="transaction-label">${esc(r.merchant || r.description_clean || "未填写商家")}</div>${r.memo ? `<div class="transaction-note" title="${esc(r.memo)}">${esc(r.memo)}</div>` : ""}</td><td><span class="badge ${!r.category_id && !transfer ? "unclassified" : ""}">${esc(category)}</span></td><td class="muted">${esc(account?.name || "未知账户")}</td><td class="num ${transfer ? "" : r.amount_minor > 0 ? "positive" : "negative"}">${esc(money(r.amount_minor, true))}</td>${actions ? `<td class="num"><button class="ghost" data-edit-entry="${esc(r.id)}">编辑</button><button class="ghost danger" data-delete-entry="${esc(r.id)}">删除</button></td>` : ""}</tr>`;
    })
    .join("")}</tbody></table></div>`;
}
async function refreshCatalog() {
  [state.accounts, state.categories] = await Promise.all([
    api("/api/accounts"),
    api("/api/categories"),
  ]);
}
async function showAuth() {
  $("workspace").hidden = true;
  $("auth").hidden = false;
  for (const id of ["entry-dialog", "generic-dialog"])
    if ($(id).open) $(id).close();
  state.accounts = [];
  state.categories = [];
  state.entries = [];
  state.importPreview = null;
  state.bulkPreview = null;
  document
    .querySelectorAll(
      ".metrics,.chart,#recent-transactions,#transactions-table,#accounts-list,#category-list,#rules-list,#history-list,#category-chart,#report-categories,#report-merchants,#import-result",
    )
    .forEach((x) => (x.innerHTML = ""));
  $("agent-config-text").value = "";
  $("agent-config").hidden = true;
  $("generic-content").innerHTML = "";
  $("entry-form").reset();
  $("credential").value = "";
  $("password-confirm").value = "";
  const status = await api("/api/status");
  state.initialized = status.initialized;
  $("auth-title").textContent = status.initialized
    ? "欢迎回来"
    : "创建你的本地账本";
  $("auth-subtitle").textContent = status.initialized
    ? "输入主密码，解锁只属于你的财务记录。"
    : "设置主密码，开始一份真正属于自己的账本。";
  $("confirm-wrap").hidden = status.initialized;
  $("password-confirm").required = !status.initialized;
  $("recovery-wrap").hidden = !status.initialized;
  $("auth-submit").textContent = status.initialized
    ? "解锁账本"
    : "创建加密账本";
  $("auth-submit").disabled = false;
  $("credential").autocomplete = status.initialized
    ? "current-password"
    : "new-password";
  $("auth-error").textContent = "";
  $("credential").focus();
}
async function enterWorkspace() {
  $("credential").value = "";
  $("password-confirm").value = "";
  $("auth").hidden = true;
  $("workspace").hidden = false;
  await refreshCatalog();
  await navigate(state.page);
}
async function navigate(page) {
  if (!titles[page]) return;
  state.page = page;
  document
    .querySelectorAll(".page")
    .forEach((x) => (x.hidden = x.id !== `page-${page}`));
  document
    .querySelectorAll(".nav-item")
    .forEach((x) => x.classList.toggle("active", x.dataset.page === page));
  $("page-title").textContent = titles[page];
  document.title = `${titles[page]} · 本地账本`;
  $("main").classList.add("loading");
  try {
    await loaders[page]();
  } finally {
    $("main").classList.remove("loading");
  }
}
const loaders = {
  async dashboard() {
    await refreshCatalog();
    const range = monthRange($("dashboard-month").value);
    const selected = new Date(`${range.start}T12:00:00`);
    selected.setMonth(selected.getMonth() - 5);
    const start = `${selected.getFullYear()}-${String(selected.getMonth() + 1).padStart(2, "0")}-01`;
    const [overview, trend] = await Promise.all([
      api(`/api/overview?${new URLSearchParams(range)}`),
      api(
        `/api/analysis?${new URLSearchParams({ start, end: range.end, report: "monthly" })}`,
      ),
    ]);
    renderMetrics("summary-metrics", overview.summary);
    chart("dashboard-chart", trend.items);
    ranks("category-chart", overview.summary.categories);
    $("recent-transactions").innerHTML = transactionTable(overview.recent);
  },
  async transactions() {
    await refreshCatalog();
    const account = $("filter-account").value,
      category = $("filter-category").value;
    $("filter-account").innerHTML = accountOptions("全部账户", account);
    $("filter-category").innerHTML = categoryOptions(
      null,
      "全部分类",
      category,
    );
    const params = new URLSearchParams({ page: state.pageNumber });
    for (const [key, id] of [
      ["q", "filter-q"],
      ["start", "filter-start"],
      ["end", "filter-end"],
      ["account_id", "filter-account"],
      ["category_id", "filter-category"],
    ])
      if ($(id).value) params.set(key, $(id).value);
    const result = await api(`/api/transactions?${params}`);
    state.entries = result.items;
    $("transactions-count").textContent = `共 ${result.total} 笔`;
    $("transactions-table").innerHTML = transactionTable(result.items, true);
    const pages = Math.max(1, Math.ceil(result.total / result.page_size));
    $("pagination").innerHTML =
      `<button data-pagination="-1"${state.pageNumber <= 1 ? " disabled" : ""}>上一页</button><span>${state.pageNumber} / ${pages}</span><button data-pagination="1"${state.pageNumber >= pages ? " disabled" : ""}>下一页</button>`;
  },
  async accounts() {
    await refreshCatalog();
    $("accounts-list").innerHTML =
      state.accounts
        .map(
          (a) =>
            `<div class="account-row"><div class="account-main"><div class="account-monogram">${esc(a.name.slice(0, 1))}</div><div><div class="account-name">${esc(a.name)} ${a.archived ? '<span class="badge">已归档</span>' : ""}</div><div class="account-subtitle">期初余额 ${esc(money(a.opening_balance_minor))} · ${esc(a.currency)}</div></div></div><div class="toolbar"><strong class="account-balance ${a.balance_minor < 0 ? "negative" : ""}">${esc(money(a.balance_minor))}</strong><button class="ghost" data-edit-account="${esc(a.id)}">管理</button></div></div>`,
        )
        .join("") || empty();
  },
  async categories() {
    await refreshCatalog();
    let html = "";
    for (const [kind, title] of [
      ["expense", "支出分类"],
      ["income", "收入分类"],
    ]) {
      html += `<h2 class="section">${title}</h2>`;
      for (const c of state.categories.filter(
        (x) => x.kind === kind && !x.parent_id,
      )) {
        html += categoryRow(c);
        for (const child of state.categories.filter(
          (x) => x.parent_id === c.id,
        ))
          html += categoryRow(child);
      }
    }
    $("category-list").innerHTML = html;
    $("bulk-source").innerHTML = categoryOptions(null, "选择原分类");
    $("bulk-target").innerHTML = categoryOptions(null, "选择目标分类");
    $("bulk-preview").hidden = true;
    const rules = await api("/api/rules");
    $("rules-list").innerHTML =
      rules
        .map(
          (r) =>
            `<div class="rule-row"><div>包含 <strong>${esc(r.keyword)}</strong><span class="muted"> → </span>${esc(categoryName(r.category_id))}${r.merchant ? `<div class="small muted">商家归一为：${esc(r.merchant)}</div>` : ""}</div><button class="ghost danger" data-delete-rule="${esc(r.id)}">删除</button></div>`,
        )
        .join("") ||
      empty(
        "还没有自定义规则",
        "先为常见商家添加规则，后续记账和导入时即可自动分类。",
      );
  },
  async reports() {
    await refreshCatalog();
    const category = $("report-category").value;
    $("report-category").innerHTML = categoryOptions(
      null,
      "全部分类",
      category,
    );
    const params = {
      start: $("report-start").value,
      end: $("report-end").value,
    };
    if ($("report-category").value)
      params.category_id = $("report-category").value;
    const [summary, yoy] = await Promise.all([
      api(
        `/api/analysis?${new URLSearchParams({ ...params, report: "summary" })}`,
      ),
      api(
        `/api/analysis?${new URLSearchParams({ ...params, report: "year_over_year" })}`,
      ),
    ]);
    renderMetrics("report-metrics", summary);
    chart("report-chart", summary.monthly);
    ranks("report-categories", summary.categories);
    ranks("report-merchants", summary.merchants);
    $("report-yoy").textContent =
      `本期支出 ${money(yoy.expense_minor)}；去年同期（${yoy.previous_start} 至 ${yoy.previous_end}）支出 ${money(yoy.previous_expense_minor)}。` +
      (yoy.change_basis_points === null
        ? " 去年同期无支出，暂不计算增长比例。"
        : ` 同比${yoy.change_basis_points >= 0 ? "增加" : "减少"} ${(Math.abs(yoy.change_basis_points) / 100).toFixed(2)}%。`);
  },
  async imports() {
    await refreshCatalog();
    $("import-account").innerHTML = accountOptions();
  },
  async history() {
    const items = await api("/api/history");
    const names = {
      create: "新增账目",
      update: "修改账目",
      delete: "删除账目",
      category: "批量调整分类",
      import: "导入账单",
    };
    $("history-list").innerHTML =
      items
        .map(
          (x) =>
            `<div class="history-row"><div><strong>${esc(names[x.kind] || x.kind)}</strong> <span class="muted">· ${x.count} 笔记录</span><div class="small muted">${esc(new Date(x.created_at).toLocaleString("zh-CN", { hour12: false }))}</div></div>${x.undone ? '<span class="badge">已撤销</span>' : `<button data-undo="${esc(x.id)}">撤销</button>`}</div>`,
        )
        .join("") ||
      empty(
        "还没有操作历史",
        "新增、修改、删除和分类整理都会留下可撤销的操作记录。",
      );
  },
  async settings() {},
};
function categoryRow(c) {
  return `<div class="category-row ${c.parent_id ? "child" : ""}"><div class="category-name"><span class="category-dot" style="background:var(--${/^cat[0-6]$/.test(c.color) ? c.color : "positive"})"></span>${esc(c.name)}</div><div><button class="ghost" data-edit-category="${esc(c.id)}">编辑</button><button class="ghost danger" data-delete-category="${esc(c.id)}">删除</button></div></div>`;
}

function generic(title, html, onSubmit) {
  $("generic-title").textContent = title;
  $("generic-content").innerHTML = html;
  $("generic-dialog").showModal();
  const form = $("generic-content").querySelector("form");
  if (form && onSubmit)
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      busy(form, async () => {
        await onSubmit(new FormData(form));
        $("generic-dialog").close();
        await navigate(state.page);
      });
    });
}
function accountDialog(id) {
  const a = state.accounts.find((x) => x.id === id);
  generic(
    a ? "管理账户" : "新增账户",
    `<form><label>账户名称<input name="name" value="${esc(a?.name || "")}" required maxlength="100"></label><label>期初余额（元，可为负数）<input name="opening" value="${a ? (a.opening_balance_minor / 100).toFixed(2) : "0.00"}" required inputmode="decimal"></label><label class="check"><input type="checkbox" name="archived"${a?.archived ? " checked" : ""}>归档账户（保留历史数据，停止新增账目）</label><div class="dialog-actions"><button class="primary" type="submit">保存账户</button></div></form>`,
    async (data) => {
      await api(a ? `/api/accounts/${a.id}` : "/api/accounts", {
        method: a ? "PUT" : "POST",
        body: JSON.stringify({
          name: data.get("name"),
          opening_balance_minor: toMinor(data.get("opening"), true),
          archived: data.get("archived") === "on",
        }),
      });
      toast("账户已保存");
    },
  );
}
function categoryDialog(id) {
  const c = state.categories.find((x) => x.id === id);
  generic(
    c ? "编辑分类" : "新增分类",
    `<form><label>分类名称<input name="name" value="${esc(c?.name || "")}" required maxlength="80"></label><label>收支类型<select name="kind" id="category-kind">${option("expense", "支出", c?.kind || "expense")}${option("income", "收入", c?.kind)}</select></label><label>父分类<select name="parent" id="category-parent"></select></label><p class="small muted">最多两级分类。已有分类的收支类型不能变更。</p><div class="dialog-actions"><button class="primary" type="submit">保存分类</button></div></form>`,
    async (data) => {
      await api(c ? `/api/categories/${c.id}` : "/api/categories", {
        method: c ? "PUT" : "POST",
        body: JSON.stringify({
          name: data.get("name"),
          kind: data.get("kind"),
          parent_id: data.get("parent") || null,
        }),
      });
      toast("分类已保存");
    },
  );
  const fill = () => {
    $("category-parent").innerHTML =
      option("", "一级分类", c?.parent_id) +
      state.categories
        .filter(
          (x) =>
            !x.parent_id && x.id !== id && x.kind === $("category-kind").value,
        )
        .map((x) => option(x.id, x.name, c?.parent_id))
        .join("");
  };
  $("category-kind").addEventListener("change", fill);
  fill();
}
function ruleDialog() {
  generic(
    "新增本地分类规则",
    `<form><label>匹配关键词<input name="keyword" required maxlength="100" placeholder="例如：瑞幸"></label><label>归入分类<select name="category" required>${categoryOptions(null, "选择分类")}</select></label><label>统一商家名称（可选）<input name="merchant" maxlength="200" placeholder="例如：瑞幸咖啡"></label><div class="dialog-actions"><button type="submit" class="primary">保存规则</button></div></form>`,
    async (data) => {
      await post("/api/rules", {
        keyword: data.get("keyword"),
        category_id: data.get("category"),
        merchant: data.get("merchant"),
      });
      toast("规则已保存");
    },
  );
}
function fillEntryKind(selected = null) {
  const kind = $("entry-kind").value;
  $("entry-category-wrap").hidden = kind === "transfer";
  $("entry-destination-wrap").hidden = kind !== "transfer";
  $("entry-destination").required = kind === "transfer";
  $("entry-category").innerHTML = categoryOptions(
    kind,
    null === selected ? "未分类 / 使用本地规则" : "未分类",
    selected,
  );
}
async function entryDialog(id) {
  await refreshCatalog();
  $("entry-form").reset();
  $("entry-id").value = id || "";
  $("entry-request").value = crypto.randomUUID();
  $("entry-error").textContent = "";
  $("entry-title").textContent = id ? "修改账目" : "记一笔";
  let row = null,
    other = null;
  if (id) {
    const detail = await api(`/api/transactions/${encodeURIComponent(id)}`);
    row = detail.items.find((x) => x.amount_minor < 0) || detail.items[0];
    other = detail.items.find((x) => x.id !== row.id);
    $("entry-kind").value =
      row.categorization_source === "transfer"
        ? "transfer"
        : row.amount_minor < 0
          ? "expense"
          : "income";
    $("entry-amount").value = (Math.abs(row.amount_minor) / 100).toFixed(2);
    $("entry-merchant").value = row.merchant || "";
    $("entry-note").value = row.memo || "";
  }
  $("entry-date").value = row?.booked_date || today();
  $("entry-account").innerHTML = accountOptions(null, row?.account_id);
  $("entry-destination").innerHTML = accountOptions(null, other?.account_id);
  fillEntryKind(row?.category_id || null);
  $("entry-dialog").showModal();
  $("entry-amount").focus();
}
function fileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.onerror = () => reject(Error("无法读取所选文件。"));
    reader.readAsDataURL(file);
  });
}
async function downloadBlob(url, filename) {
  const response = await fetch(url, { credentials: "same-origin" });
  if (!response.ok) throw Error("导出失败，请先解锁账本。");
  const blob = await response.blob();
  saveBlob(blob, filename);
}
function saveBlob(blob, name) {
  const link = document.createElement("a");
  const url = URL.createObjectURL(blob);
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
function recoveryDialog(key) {
  generic(
    "保存你的离线恢复密钥",
    `<p>这是忘记主密码时的恢复方式。仅本次显示，请离线保存，勿与备份放在同一云盘目录。</p><div class="recovery-key">${esc(key)}</div><p class="small warning">不要把它发给 Workbuddy 或任何其他人。恢复密钥不会再次显示。</p><button id="copy-recovery">复制恢复密钥</button><div class="dialog-actions"><button id="saved-recovery" class="primary">我已离线保存，进入账本</button></div>`,
  );
  const dialog = $("generic-dialog");
  const closeButton = dialog.querySelector("[data-close]");
  const preventClose = (event) => event.preventDefault();
  closeButton.hidden = true;
  dialog.addEventListener("cancel", preventClose);
  dialog.addEventListener("close", () => {
    dialog.removeEventListener("cancel", preventClose);
    closeButton.hidden = false;
  }, {once:true});
  $("copy-recovery").onclick = () =>
    navigator.clipboard
      .writeText(key)
      .then(() => toast("已复制，请离线保存"))
      .catch(() => toast("请手动选择并复制密钥"));
  $("saved-recovery").onclick = () => {
    $("generic-content").innerHTML = "";
    $("generic-dialog").close();
    enterWorkspace().catch((e) => toast(e.message));
  };
}

$("auth-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(
    event.currentTarget,
    async () => {
      const credential = $("credential").value;
      if (!state.initialized && credential !== $("password-confirm").value)
        throw Error("两次输入的密码不一致。");
      if (!state.initialized && credential.length < 10)
        throw Error("请设置至少 10 个字符的主密码。");
      const result = await post(
        state.initialized ? "/api/unlock" : "/api/setup",
        state.initialized
          ? { credential, recovery: $("recovery-mode").checked }
          : { password: credential },
      );
      $("credential").value = "";
      $("password-confirm").value = "";
      if (result.recovery_key) recoveryDialog(result.recovery_key);
      else await enterWorkspace();
    },
    "auth-error",
  );
});
$("restore-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.currentTarget, async () => {
    const file = $("restore-file").files[0];
    if (!file) throw Error("请选择加密备份文件。");
    if (file.size > 128 * 1024 * 1024) throw Error("备份文件超过 128 MiB。");
    if (
      !confirm("将恢复该备份。当前账本如存在，会先保留一份加密副本。是否继续？")
    )
      return;
    await post("/api/restore", {
      credential: $("restore-credential").value,
      recovery: $("restore-recovery").checked,
      content_base64: await fileBase64(file),
    });
    $("restore-form").reset();
    toast("恢复成功，请解锁账本");
    await showAuth();
  });
});
$("entry-kind").addEventListener("change", () => fillEntryKind());
$("entry-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(
    event.currentTarget,
    async () => {
      const kind = $("entry-kind").value;
      const payload = {
        account_id: $("entry-account").value,
        booked_date: $("entry-date").value,
        kind,
        amount_minor: toMinor($("entry-amount").value),
        category_id:
          kind === "transfer" ? null : $("entry-category").value || null,
        merchant: $("entry-merchant").value,
        note: $("entry-note").value,
        destination_account_id:
          kind === "transfer" ? $("entry-destination").value : null,
        request_id: $("entry-request").value,
      };
      const id = $("entry-id").value;
      await api(
        id
          ? `/api/transactions/${encodeURIComponent(id)}`
          : "/api/transactions",
        { method: id ? "PUT" : "POST", body: JSON.stringify(payload) },
      );
      $("entry-dialog").close();
      toast("账目已加密保存");
      await navigate(state.page);
    },
    "entry-error",
  );
});
$("transaction-filter").addEventListener("submit", (e) => {
  e.preventDefault();
  state.pageNumber = 1;
  navigate("transactions").catch((e) => toast(e.message));
});
$("clear-filters").addEventListener("click", () => {
  $("transaction-filter").reset();
  state.pageNumber = 1;
  navigate("transactions").catch((e) => toast(e.message));
});
$("dashboard-month").addEventListener("change", () =>
  navigate("dashboard").catch((e) => toast(e.message)),
);
$("report-filter").addEventListener("submit", (e) => {
  e.preventDefault();
  navigate("reports").catch((e) => toast(e.message));
});
$("bulk-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.currentTarget, async () => {
    const result = await post("/api/categories/preview", {
      source_category_id: $("bulk-source").value,
      target_category_id: $("bulk-target").value,
    });
    state.bulkPreview = result.preview_id;
    $("bulk-preview").hidden = false;
    $("bulk-preview").innerHTML =
      `将把「${esc(result.source)}」中的 <strong>${result.count} 笔</strong>账目迁移至「${esc(result.target)}」。仅包含直接归入原分类的账目，不包含子分类。<br><button id="confirm-bulk"${result.count === 0 ? " disabled" : ""}>确认执行分类调整</button>`;
  });
});
$("import-form").addEventListener("submit", (event) => {
  event.preventDefault();
  busy(event.currentTarget, async () => {
    const file = $("import-file").files[0];
    if (!file) throw Error("请选择 CSV 文件。");
    if (file.size > 2 * 1024 * 1024) throw Error("CSV 文件最大 2 MiB。");
    const result = await post("/api/imports/preview", {
      account_id: $("import-account").value,
      filename: file.name,
      format: $("import-format").value,
      content_base64: await fileBase64(file),
    });
    state.importPreview = result.preview_id;
    $("import-result").hidden = false;
    $("import-result").innerHTML =
      `<section class="section"><h2>导入预览</h2><div class="import-summary"><div><strong>${result.count}</strong><span class="muted">待导入</span></div><div><strong>${result.duplicates}</strong><span class="muted">重复已排除</span></div><div><strong>${result.unclassified}</strong><span class="muted">尚未分类</span></div><div><strong>${result.skipped_count}</strong><span class="muted">需人工核对 / 已跳过</span></div></div><p class="muted">以下最多显示前 100 笔。退款、失败、转账和不计收支记录不会自动入账；未分类记录可在导入后整理。</p><div class="table-scroll"><table><thead><tr><th>日期</th><th>商家</th><th>类型</th><th>建议分类</th><th class="num">金额</th></tr></thead><tbody>${result.items.map((x) => `<tr><td>${esc(x.booked_date)}</td><td>${esc(x.merchant)}</td><td>${x.kind === "expense" ? "支出" : "收入"}</td><td>${esc(categoryName(x.category_id))}</td><td class="num">${esc(money(x.amount_minor))}</td></tr>`).join("")}</tbody></table></div>${result.skipped.length ? `<details class="notice"><summary>查看跳过原因（前 100 条）</summary>${result.skipped.map((x) => `<p class="small">第 ${x.line} 行：${esc(x.reason)}</p>`).join("")}</details>` : ""}<div class="dialog-actions"><button id="confirm-import" class="primary"${result.count === 0 ? " disabled" : ""}>确认导入 ${result.count} 笔</button></div></section>`;
  });
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button,[data-page]");
  if (!button || button.disabled) return;
  try {
    if (button.dataset.page) {
      await navigate(button.dataset.page);
      return;
    }
    if (button.hasAttribute("data-close")) {
      button.closest("dialog").close();
      return;
    }
    if (button.dataset.pagination) {
      state.pageNumber += Number(button.dataset.pagination);
      await navigate("transactions");
      return;
    }
    if (button.dataset.editEntry) {
      await entryDialog(button.dataset.editEntry);
      return;
    }
    if (button.dataset.deleteEntry) {
      if (
        confirm(
          "删除这笔账目？如为转账，两端会一起删除；之后可在操作历史中撤销。",
        )
      ) {
        await api(
          `/api/transactions/${encodeURIComponent(button.dataset.deleteEntry)}`,
          { method: "DELETE" },
        );
        toast("账目已删除，可在操作历史中撤销");
        await navigate("transactions");
      }
      return;
    }
    if (button.dataset.editAccount) {
      accountDialog(button.dataset.editAccount);
      return;
    }
    if (button.dataset.editCategory) {
      categoryDialog(button.dataset.editCategory);
      return;
    }
    if (button.dataset.deleteCategory) {
      if (
        confirm(
          "确认删除这个空分类？仍有账目、子分类或规则引用的分类将不会删除。",
        )
      ) {
        await api(`/api/categories/${button.dataset.deleteCategory}`, {
          method: "DELETE",
        });
        await navigate("categories");
      }
      return;
    }
    if (button.dataset.deleteRule) {
      if (confirm("删除这条自动分类规则？")) {
        await api(`/api/rules/${button.dataset.deleteRule}`, {
          method: "DELETE",
        });
        await navigate("categories");
      }
      return;
    }
    if (button.dataset.undo) {
      if (confirm("撤销这次操作？账目和账户余额将一起回退。")) {
        await post(`/api/history/${button.dataset.undo}/undo`);
        toast("已撤销");
        await navigate("history");
      }
      return;
    }
    switch (button.id) {
      case "add-entry":
        await entryDialog();
        break;
      case "lock-button":
        await post("/api/lock");
        await showAuth();
        break;
      case "theme-toggle": {
        const theme =
          document.documentElement.dataset.theme === "dark" ? "light" : "dark";
        document.documentElement.dataset.theme = theme;
        localStorage.setItem("bastide-personal-theme", theme);
        break;
      }
      case "new-account":
        accountDialog();
        break;
      case "new-category":
        categoryDialog();
        break;
      case "new-rule":
        ruleDialog();
        break;
      case "confirm-bulk":
        button.disabled = true;
        try {
          const result = await post("/api/categories/commit", {
            preview_id: state.bulkPreview,
          });
          toast(`已调整 ${result.count} 笔，可在操作历史中撤销`);
          await navigate("categories");
        } finally {
          button.disabled = false;
        }
        break;
      case "confirm-import":
        button.disabled = true;
        try {
          const result = await post("/api/imports/commit", {
            preview_id: state.importPreview,
          });
          $("import-result").innerHTML =
            `<div class="notice">已加密保存 ${result.count} 笔账目，提交时另排除 ${result.duplicates} 笔重复记录。可在操作历史中整批撤销。<br><button data-page="transactions">查看交易记录</button></div>`;
          toast("导入完成");
        } finally {
          button.disabled = false;
        }
        break;
      case "download-template":
        saveBlob(
          new Blob(
            ["\ufeffdate,kind,amount,merchant,note,category,external_id\r\n"],
            { type: "text/csv;charset=utf-8" },
          ),
          "finance-template.csv",
        );
        toast(
          "填写模板时：kind 使用 expense / income，金额使用元，日期使用 YYYY-MM-DD",
        );
        break;
      case "change-password":
        generic(
          "修改主密码",
          '<form><label>新主密码<input name="password" type="password" minlength="10" maxlength="1024" autocomplete="new-password" required></label><label>再次输入<input name="confirm" type="password" minlength="10" maxlength="1024" autocomplete="new-password" required></label><div class="dialog-actions"><button class="primary" type="submit">更新密码</button></div></form>',
          async (data) => {
            if (data.get("password") !== data.get("confirm"))
              throw Error("两次输入的密码不一致。");
            await post("/api/password", { password: data.get("password") });
            toast("密码已更新，请记住新密码；旧备份仍使用备份时的密码。");
          },
        );
        break;
      case "generate-agent": {
        button.disabled = true;
        try {
          const result = await post("/api/agent-token", {
            allow_writes: $("agent-writes").checked,
            allow_details: $("agent-details").checked,
          });
          $("agent-config-text").value = JSON.stringify(result.config, null, 2);
          $("agent-config").hidden = false;
          toast("已生成新令牌，旧令牌立即失效");
        } finally {
          button.disabled = false;
        }
        break;
      }
      case "revoke-agent":
        if (confirm("撤销后，原 Workbuddy 配置将无法访问账本。是否继续？")) {
          await api("/api/agent-token", { method: "DELETE" });
          $("agent-config-text").value = "";
          $("agent-config").hidden = true;
          toast("Agent 访问已撤销");
        }
        break;
      case "copy-config":
        await navigator.clipboard.writeText($("agent-config-text").value);
        toast("MCP 配置已复制");
        break;
      case "export-plaintext":
        if (
          confirm(
            "即将导出未加密的完整账目。请勿将明文文件放入公开仓库或交给云端模型。仍然导出？",
          )
        )
          await downloadBlob(
            "/api/export?confirm_plaintext=true",
            "finance-plaintext.json",
          );
        break;
    }
  } catch (error) {
    toast(error.message);
  }
});

document.addEventListener("keydown", (event) => {
  if (
    (event.ctrlKey || event.metaKey) &&
    event.key.toLowerCase() === "n" &&
    !$("workspace").hidden
  ) {
    event.preventDefault();
    entryDialog().catch((e) => toast(e.message));
  }
});
async function init() {
  const stored = localStorage.getItem("bastide-personal-theme");
  document.documentElement.dataset.theme =
    stored === "light" ? "light" : "dark";
  icons();
  const now = today();
  $("dashboard-month").value = now.slice(0, 7);
  $("report-start").value = `${now.slice(0, 4)}-01-01`;
  $("report-end").value = now;
  $("footer-date").textContent = new Date().toLocaleDateString("zh-CN", {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "long",
  });
  const status = await api("/api/status");
  if (status.authenticated) await enterWorkspace();
  else await showAuth();
  setInterval(async () => {
    if (!$("workspace").hidden) {
      try {
        const status = await api("/api/status");
        if (!status.authenticated) {
          await showAuth();
          toast("账本已自动锁定，请重新解锁");
        }
      } catch {
        toast("本地服务连接中断，请重新启动账本。");
      }
    }
  }, 30000);
}
init().catch((error) => {
  $("auth-title").textContent = "无法连接本地服务";
  $("auth-error").textContent = error.message;
});
