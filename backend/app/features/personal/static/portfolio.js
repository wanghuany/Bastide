"use strict";

// Loaded before app.js. Functions use the shared local UI helpers only on invocation.
const accountPurposes = {
  generic: "普通账户", bond: "债券投资", stock_allowance: "股票型基金 / 零花钱",
  savings: "结余储蓄 / 理财通", allowance: "日常零花钱",
};
let currentPortfolio = null;

async function loadPortfolio() {
  const selected = $("finance-account").value;
  $("finance-account").innerHTML = state.accounts.map((a) =>
    option(a.id, `${a.name}${a.archived ? "（已归档）" : ""}`, selected)).join("");
  if (!selected) $("finance-account").value = (state.accounts.find((a) => !a.archived && a.purpose === "bond") || state.accounts.find((a) => !a.archived) || state.accounts[0])?.id || "";
  const id = $("finance-account").value;
  if (!id) return;
  const p = await api(`/api/accounts/${encodeURIComponent(id)}/portfolio`);
  currentPortfolio = p;
  $("portfolio-metrics").innerHTML =
    metric("账户总资产", money(p.balance_minor), accountPurposes[p.purpose] || "普通账户") +
    metric("现金", money(p.cash_balance_minor), "可用于买入或日常支付") +
    metric("基金最新金额", money(p.holdings_value_minor), "各基金最后登记金额之和") +
    metric("已记录估值变动", money(p.valuation_gain_minor, true), "排除已登记的买入 / 赎回本金", p.valuation_gain_minor >= 0 ? "positive" : "negative");
  const budget = p.allowance;
  $("portfolio-allowance").textContent = ["stock_allowance", "allowance"].includes(p.purpose)
    ? budget.budget_configured
      ? `${budget.month} 零花钱额度 ${money(budget.budget_minor)}；个人开销 ${money(budget.personal_expense_minor)}；已转存 ${money(budget.saved_minor)}；可结余额度 ${money(budget.remaining_minor)}。按现有现金建议最多转存 ${money(budget.suggested_transfer_minor)}，请核对后确认。`
      : "可在“我的账户 → 管理”中设置每月零花钱额度。未设置时不会推算结余；可自行确认金额后转存。"
    : "债券账户用于入金和基金持仓；理财通用于接收零花钱结余。投资买卖及账户间转存不计入日常收支。";
  $("add-holding").disabled = p.archived;
  $("record-values").disabled = p.archived || !p.holdings.length;
  $("save-allowance").disabled = p.archived || !["stock_allowance", "allowance"].includes(p.purpose);
  $("holdings-list").innerHTML = p.holdings.length ? `<div class="table-scroll"><table><thead><tr><th>基金</th><th>最后记录日期</th><th class="num">最新金额</th><th>操作</th></tr></thead><tbody>${p.holdings.map((h) => `<tr><td>${esc(h.name)}</td><td>${esc(h.as_of)}</td><td class="num">${esc(money(h.value_minor))}</td><td>${p.archived ? "已归档" : `<button class="ghost" data-holding-buy="${esc(h.id)}">买入</button><button class="ghost" data-holding-sell="${esc(h.id)}">赎回</button>`}</td></tr>`).join("")}</tbody></table></div>` : empty("还没有基金持仓", "已有基金请登记期初金额；准备新买基金时，先以 0 元建立该基金，再登记买入。");
  const latest = new Set(p.holdings.map((h) => h.latest_event_id));
  $("holding-history").innerHTML = p.events.length ? `<div class="table-scroll"><table><thead><tr><th>日期 / 基金</th><th>记录</th><th class="num">原金额 → 新金额</th><th class="num">估值变动</th><th>操作</th></tr></thead><tbody>${p.events.map((e) => `<tr><td>${esc(e.booked_date)}<br>${esc(e.name)}</td><td>${{valuation: "周度金额", buy: "买入", sell: "赎回"}[e.kind] || esc(e.kind)}${e.undone ? "（已撤销）" : ""}</td><td class="num">${esc(money(e.previous_value_minor))} → ${esc(money(e.value_minor))}</td><td class="num ${e.gain_minor < 0 ? "negative" : "positive"}">${e.kind === "valuation" ? esc(money(e.gain_minor, true)) : "本金调整"}</td><td>${!p.archived && !e.undone && latest.has(e.id) ? `<button class="ghost danger" data-holding-undo="${esc(e.id)}">撤销</button>` : "—"}</td></tr>`).join("")}</tbody></table></div>` : empty("尚无周度记录", "每周填各个基金的新总金额，系统计算金额变化，无需提供百分比。");
}

function addHoldingDialog() {
  const p = currentPortfolio;
  if (!p) return;
  const request = crypto.randomUUID();
  generic("登记期初基金", `<form><p class="muted">${esc(p.name)}：已有基金填当前真实金额，不会扣减现金、也不计收入。新增买入应先建 0 元持仓，再点“买入”。</p><label>基金名称<input name="name" required maxlength="100"></label><label>期初日期<input name="date" type="date" value="${today()}" required max="${today()}"></label><label>期初基金金额（元）<input name="amount" value="0.00" required inputmode="decimal"></label><div class="dialog-actions"><button class="primary" type="submit">保存期初持仓</button></div></form>`, async (data) => {
    await post(`/api/accounts/${p.id}/holdings`, {name: data.get("name"), opening_date: data.get("date"), opening_value_minor: toMinor(data.get("amount"), true), request_id: request});
    toast("期初基金已保存，请核对现金中未重复包含它");
  });
}

function weeklyValuesDialog() {
  const p = currentPortfolio;
  if (!p?.holdings.length) return;
  const request = crypto.randomUUID();
  generic("记录本周基金金额", `<form><p class="muted">只填写新的总金额，不填百分比。如期间有买入/赎回，先登记买卖。所有基金请逐项核对；原金额仅为上次记录参考。</p><label>记录日期<input name="date" type="date" value="${today()}" required max="${today()}"></label>${p.holdings.map((h) => `<label>${esc(h.name)}（上次 ${esc(money(h.value_minor))}，${esc(h.as_of)}）<input name="value-${esc(h.id)}" required inputmode="decimal" placeholder="填写新的总金额（元）"></label>`).join("")}<div class="dialog-actions"><button class="primary" type="submit">确认全部金额并保存</button></div></form>`, async (data) => {
    const result = await post(`/api/accounts/${p.id}/valuations`, {booked_date: data.get("date"), holdings: p.holdings.map((h) => ({holding_id: h.id, value_minor: toMinor(data.get(`value-${h.id}`), true)})), request_id: request});
    toast(`本次记录估值变动 ${money(result.gain_minor, true)}，不计入日常收支`);
  });
}

function holdingTradeDialog(id, kind) {
  const p = currentPortfolio;
  const h = p?.holdings.find((x) => x.id === id);
  if (!h) return;
  const request = crypto.randomUUID();
  generic(kind === "buy" ? "基金买入" : "基金赎回", `<form><p class="muted">${esc(h.name)}；现金 ${esc(money(p.cash_balance_minor))}，基金 ${esc(money(h.value_minor))}。买入/赎回只在现金与基金间划转，不计消费或收入。赎回前市值有变化时，先记录新市值再赎回。</p><label>日期<input name="date" type="date" value="${today()}" required max="${today()}"></label><label>实际金额（元）<input name="amount" required inputmode="decimal"></label><div class="dialog-actions"><button class="primary" type="submit">确认${kind === "buy" ? "买入" : "赎回"}</button></div></form>`, async (data) => {
    await post(`/api/holdings/${h.id}/trades`, {kind, booked_date: data.get("date"), amount_minor: toMinor(data.get("amount")), request_id: request});
    toast("现金和基金持仓已同步更新");
  });
}

function allowanceSavingsDialog() {
  const p = currentPortfolio;
  const destinations = state.accounts.filter((a) => !a.archived && a.purpose === "savings" && a.id !== p?.id);
  if (!p || !destinations.length) { toast("请先建立用途为“结余储蓄 / 理财通”的账户"); return; }
  const request = crypto.randomUUID();
  generic("确认零花钱结余转存", `<form><p class="muted">从 ${esc(p.name)} 转出现金。建议仅供核对；不会卖出基金，也不会在后台自动操作真实资金。完成真实转账后，再登记这笔账。</p><label>转入账户<select name="destination">${destinations.map((a) => option(a.id, a.name)).join("")}</select></label><label>实际转存日期<input name="date" type="date" value="${today()}" required></label><label>转存金额（元）<input name="amount" value="${(p.allowance.suggested_transfer_minor / 100).toFixed(2)}" required inputmode="decimal"></label><div class="dialog-actions"><button class="primary" type="submit">确认登记转存</button></div></form>`, async (data) => {
    const amount = toMinor(data.get("amount"));
    if (amount > p.cash_balance_minor) throw Error("转存金额超过当前现金；需要赎回基金时请先登记赎回");
    await post(`/api/accounts/${p.id}/savings-transfers`, {destination_account_id: data.get("destination"), booked_date: data.get("date"), amount_minor: amount, request_id: request});
    toast("转存已记账，不计收入和支出，可在操作历史撤销");
  });
}
