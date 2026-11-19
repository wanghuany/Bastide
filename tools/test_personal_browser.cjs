/* UI integration test. Run against an EMPTY throwaway vault, never a real ledger.
 * npm install --prefix <temporary-folder> playwright@1.58.2
 * PLAYWRIGHT_MODULE=<temporary-folder>/node_modules/playwright node tools/test_personal_browser.cjs
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

async function main() {
  const base = process.env.PERSONAL_TEST_URL || "http://127.0.0.1:18999";
  const output = process.env.PERSONAL_TEST_OUTPUT || path.resolve(__dirname, "../../.cache/browser-qa");
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    executablePath: process.env.PERSONAL_BROWSER || "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    headless: true,
    args: ["--disable-background-networking", "--no-first-run", "--disable-extensions"],
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
  await context.route("**/*", (route) => route.request().url().startsWith(base + "/") ? route.continue() : route.abort());
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("dialog", (dialog) => dialog.accept());
  const password = "browser-test-only-password";
  const checks = [];
  try {
    await page.goto(base, { waitUntil: "networkidle" });
    await page.locator("#auth-title").filter({ hasText: "创建你的本地账本" }).waitFor();
    await page.screenshot({ path: path.join(output, "01-setup.png"), fullPage: true });
    await page.fill("#credential", password);
    await page.fill("#password-confirm", password);
    await page.click("#auth-submit");
    await page.locator("#saved-recovery").waitFor();
    await page.click("#saved-recovery");
    await page.locator("#workspace:not([hidden])").waitFor();
    await page.locator("#summary-metrics .metric").first().waitFor();
    checks.push("first-run encrypted-vault creation and recovery acknowledgement");

    async function add(kind, amount, merchant, transfer = false) {
      await page.click("#add-entry");
      await page.locator("#entry-dialog[open]").waitFor();
      await page.selectOption("#entry-kind", kind);
      await page.fill("#entry-amount", amount);
      await page.fill("#entry-date", "2026-11-17");
      await page.fill("#entry-merchant", merchant);
      if (transfer) {
        const choices = await page.locator("#entry-destination option").evaluateAll((options) => options.map(x => x.value));
        await page.selectOption("#entry-destination", choices[1]);
      } else if (kind === "expense") {
        await page.selectOption("#entry-category", { label: "餐饮 / 早餐" });
      }
      await page.click("#entry-form button[type=submit]");
      await page.locator("#entry-dialog").waitFor({ state: "hidden" });
      await page.locator("#main:not(.loading)").waitFor();
    }
    await add("income", "8800.00", "虚构公司测试工资");
    await add("expense", "25.60", "虚构早餐商家");
    await add("transfer", "100.00", "测试转账", true);
    await page.fill("#dashboard-month", "2026-11");
    await page.dispatchEvent("#dashboard-month", "change");
    await page.locator("#summary-metrics").filter({ hasText: "8,774.40" }).waitFor();
    assert.match(await page.locator("#summary-metrics").innerText(), /8,800\.00/);
    assert.match(await page.locator("#summary-metrics").innerText(), /25\.60/);
    await page.screenshot({ path: path.join(output, "02-dashboard-dark.png"), fullPage: true });
    checks.push("income/expense/paired transfer and local dashboard figures");

    await page.click('.nav-item[data-page="transactions"]');
    const expenseRow = page.locator("#transactions-table tr").filter({ hasText: "虚构早餐商家" });
    await expenseRow.getByRole("button", { name: "编辑" }).click();
    await page.locator("#entry-dialog[open]").waitFor();
    await page.fill("#entry-amount", "30.80");
    await page.click("#entry-form button[type=submit]");
    await page.locator("#entry-dialog").waitFor({ state: "hidden" });
    await page.locator("#transactions-table").filter({ hasText: "30.80" }).waitFor();
    checks.push("edit transaction and account-balance refresh");

    await page.click('.nav-item[data-page="categories"]');
    await page.selectOption("#bulk-source", { label: "餐饮 / 早餐" });
    await page.selectOption("#bulk-target", { label: "餐饮 / 午餐" });
    await page.click("#bulk-form button[type=submit]");
    await page.locator("#bulk-preview").filter({ hasText: "1 笔" }).waitFor();
    await page.click("#confirm-bulk");
    await page.locator("#bulk-preview").waitFor({ state: "hidden" });
    await page.click('.nav-item[data-page="history"]');
    await page.locator(".history-row").filter({ hasText: "批量调整分类" }).getByRole("button", { name: "撤销" }).click();
    await page.locator(".history-row").filter({ hasText: "批量调整分类" }).filter({ hasText: "已撤销" }).waitFor();
    checks.push("category preview/commit and conflict-safe history undo");

    await page.click('.nav-item[data-page="imports"]');
    await page.setInputFiles("#import-file", {
      name: "invented.csv", mimeType: "text/csv",
      buffer: Buffer.from("date,kind,amount,merchant,note,external_id\n2026-11-18,expense,16.20,虚构导入商家,测试导入,browser-test-001\n", "utf8"),
    });
    await page.click("#import-form button[type=submit]");
    await page.locator("#confirm-import").filter({ hasText: "1 笔" }).waitFor();
    await page.click("#confirm-import");
    await page.locator("#import-result").filter({ hasText: "已加密保存 1 笔" }).waitFor();
    checks.push("CSV upload/preview/confirmation without remote traffic");

    await page.click('.nav-item[data-page="reports"]');
    await page.fill("#report-start", "2026-01-01");
    await page.fill("#report-end", "2026-12-31");
    await page.click("#report-filter button[type=submit]");
    await page.locator("#report-metrics").filter({ hasText: "47.00" }).waitFor();
    assert.match(await page.locator("#report-yoy").innerText(), /去年同期/);
    await page.screenshot({ path: path.join(output, "03-reports-dark.png"), fullPage: true });
    checks.push("local Python reports and year comparison");

    await page.click('.nav-item[data-page="settings"]');
    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("link", { name: "下载加密备份" }).click();
    const download = await downloadPromise;
    const destination = path.join(output, "test-only-backup.bfin");
    await download.saveAs(destination);
    const encrypted = fs.readFileSync(destination);
    assert(encrypted.subarray(0, 16).toString().startsWith("BASTIDE-PERSONAL"));
    assert(!encrypted.includes(Buffer.from("虚构早餐商家")));
    await page.click("#generate-agent");
    await page.locator("#agent-config:not([hidden])").waitFor();
    const config = JSON.parse(await page.inputValue("#agent-config-text"));
    assert(config.mcpServers["bastide-personal"].env.BASTIDE_PERSONAL_TOKEN);
    assert(!(await page.isChecked("#agent-details")));
    checks.push("encrypted backup download and least-privilege MCP configuration");

    await page.click('.nav-item[data-page="dashboard"]');
    await page.locator("#main:not(.loading)").waitFor();
    await page.click("#theme-toggle");
    assert.equal(await page.getAttribute("html", "data-theme"), "light");
    await page.screenshot({ path: path.join(output, "04-dashboard-light.png"), fullPage: true });
    await page.setViewportSize({ width: 600, height: 900 });
    await page.screenshot({ path: path.join(output, "05-compact.png"), fullPage: true });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    checks.push("light theme and compact layout without page overflow");

    await page.click("#lock-button");
    await page.locator("#auth:not([hidden])").waitFor();
    assert.equal(await page.inputValue("#agent-config-text"), "");
    await page.fill("#credential", password);
    await page.click("#auth-submit");
    await page.locator("#workspace:not([hidden])").waitFor();
    await page.locator("#summary-metrics").filter({ hasText: "8,753.00" }).waitFor();
    checks.push("lock clears sensitive UI and password unlock restores durable data");
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, "results.json"), JSON.stringify({ passed: checks, browserErrors: errors }, null, 2));
    console.log(JSON.stringify({ passed: checks.length, checks, browserErrors: errors }));
  } catch (error) {
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    throw error;
  } finally {
    await browser.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
