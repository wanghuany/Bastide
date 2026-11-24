/* Real-process test with fictional vaults only. Uses the same Playwright module as UI tests. */
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const repo = path.resolve(__dirname, "..");
const output = process.env.PERSONAL_TEST_OUTPUT;
if (!output) throw Error("Set PERSONAL_TEST_OUTPUT to an empty temporary test folder.");
fs.mkdirSync(output, { recursive: true });
const python = path.join(repo, "backend/.venv/Scripts/python.exe");
const vault = path.join(output, "fictional-lifetime.bfin");
if (fs.existsSync(vault)) throw Error("Refusing to overwrite an existing vault.");
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const children = [];

async function waitUntil(check, message, timeout = 15000) {
  const until = Date.now() + timeout;
  while (Date.now() < until) {
    if (await check()) return;
    await delay(100);
  }
  throw Error(message);
}

async function launch(port, keepRunning = false) {
  // Disable the OS browser launcher only; exercise default CLI lifetime behavior.
  const child = spawn(python, ["-c", "import webbrowser; webbrowser.open=lambda *a, **k: True; from app.personal_cli import main; main()",
    "--port", String(port), "--vault", vault, ...(keepRunning ? ["--keep-running"] : [])], {
    cwd: path.join(repo, "backend"), windowsHide: true,
    env: { ...process.env, BASTIDE_DB_PATH: ":memory:", PYTHONIOENCODING: "utf-8" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  children.push(child);
  let log = "";
  child.stdout.on("data", chunk => { log += chunk.toString(); });
  child.stderr.on("data", chunk => { log += chunk.toString(); });
  const base = `http://127.0.0.1:${port}`;
  await waitUntil(async () => {
    if (child.exitCode !== null) throw Error(`Server exited before ready: ${log}`);
    try { return (await fetch(base + "/api/window")).ok; } catch { return false; }
  }, "Server did not start");
  return { child, base, log: () => log };
}

async function main() {
  const browser = await chromium.launch({
    executablePath: process.env.PERSONAL_BROWSER || "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    headless: true, args: ["--disable-background-networking", "--no-first-run", "--disable-extensions"],
  });
  const context = await browser.newContext();
  await context.addInitScript(() => {
    const Original = window.WebSocket;
    window.__testSockets = [];
    window.WebSocket = class extends Original {
      constructor(...args) { super(...args); window.__testSockets.push(this); }
    };
  });
  const errors = [];
  context.on("page", page => page.on("pageerror", e => errors.push(e.message)));
  async function connected(page) {
    await page.waitForFunction(() => window.__testSockets.some(s => s.readyState === 1));
  }
  try {
    const running = await launch(19009);
    const page = await context.newPage();
    await page.goto(running.base);
    await connected(page);
    await page.fill("#credential", "fictional-lifetime-password");
    await page.fill("#password-confirm", "fictional-lifetime-password");
    await page.click("#auth-submit");
    await page.locator("#saved-recovery").click();
    await page.locator("#workspace:not([hidden])").waitFor();
    const accounts = await (await context.request.get(running.base + "/api/accounts")).json();
    const entry = await context.request.post(running.base + "/api/transactions", { data: {
      account_id: accounts[0].id, booked_date: "2026-11-01", kind: "income", amount_minor: 777,
      merchant: "Fictional lifecycle test", request_id: "lifetime-fictional-income",
    } });
    assert.equal(entry.status(), 201);
    await page.reload();
    await connected(page);
    await delay(6000);
    assert.equal(running.child.exitCode, null, "Refreshing must not stop the process");
    const second = await context.newPage();
    await second.goto(running.base);
    await connected(second);
    await page.close();
    await delay(6000);
    assert.equal(running.child.exitCode, null, "One remaining tab must keep the process alive");
    const unrelated = await context.newPage();
    await unrelated.goto("about:blank");
    await delay(6000);
    assert.equal(running.child.exitCode, null, "A background ledger tab must stay alive");
    await second.locator("#lock-button").click();
    await second.locator("#auth:not([hidden])").waitFor();
    await delay(6000);
    assert.equal(running.child.exitCode, null, "A locked page is still an open page");
    await second.fill("#credential", "fictional-lifetime-password");
    await second.click("#auth-submit");
    await second.locator("#workspace:not([hidden])").waitFor();
    await second.close(); // Exit with an unlocked vault: lifespan must release its file lock.
    await waitUntil(() => running.child.exitCode !== null, "Last page did not stop the process");
    assert.equal(running.child.exitCode, 0, running.log());
    await assert.rejects(fetch(running.base + "/api/status"));
    assert(fs.readFileSync(vault).subarray(0, 32).toString().indexOf("SQLite format") < 0);

    const reopened = await launch(19009);
    const restored = await context.newPage();
    await restored.goto(reopened.base);
    await connected(restored);
    await restored.fill("#credential", "fictional-lifetime-password");
    await restored.click("#auth-submit");
    await restored.locator("#workspace:not([hidden])").waitFor();
    const totals = await (await context.request.get(reopened.base + "/api/finance/since-start")).json();
    assert.equal(totals.income_minor, 777);
    await restored.close();
    await waitUntil(() => reopened.child.exitCode !== null, "Reopened process did not exit");
    assert.equal(reopened.child.exitCode, 0, reopened.log());

    const background = await launch(19010, true);
    const backgroundPage = await context.newPage();
    await backgroundPage.goto(background.base);
    assert.equal((await (await fetch(background.base + "/api/window")).json()).exit_on_close, false);
    await backgroundPage.close();
    await delay(6000);
    assert.equal(background.child.exitCode, null, "Keep-running mode must remain running");
    assert.deepEqual(errors, []);
    console.log("PASS: default CLI automatic exit; refresh; multiple tabs; background tab; locked page; graceful process exit; port released; encrypted data persisted and vault reopened; keep-running mode; no browser errors.");
  } finally {
    await browser.close();
    for (const child of children) if (child.exitCode === null) child.kill();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
