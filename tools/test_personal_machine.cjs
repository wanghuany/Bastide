/* Windows device authorization + real MCP auto-start. Fictional vaults only. */
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const output = process.env.PERSONAL_TEST_OUTPUT;
if (!output) throw Error("PERSONAL_TEST_OUTPUT must point to a fresh temporary folder");
fs.mkdirSync(output, { recursive: true });
const repo = path.resolve(__dirname, "..");
const python = path.join(repo, "backend/.venv/Scripts/python.exe");
const vault = path.join(output, "fictional-\u8d26\u672c with spaces.bfin");
if (fs.existsSync(vault)) throw Error("Refusing to use an existing vault");
const base = "http://127.0.0.1:19012";
const env = { ...process.env, LOCALAPPDATA: path.join(output, "local"),
  BASTIDE_DB_PATH: ":memory:", BASTIDE_PERSONAL_VAULT: vault,
  PYTHONPATH: path.join(repo, "backend"), PYTHONIOENCODING: "utf-8" };
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function wait(check, message) {
  for (let i = 0; i < 450; i++) { if (await check()) return; await pause(100); }
  throw Error(message);
}
async function main() {
  let child = spawn(python, ["-m", "app.personal_cli", "--no-browser", "--port", "19012", "--vault", vault],
    { cwd: path.join(repo, "backend"), env, windowsHide: true, stdio: "ignore" });
  const browser = await chromium.launch({
    executablePath: "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    headless: true, args: ["--disable-background-networking", "--no-first-run"],
  });
  let context;
  try {
    await wait(async () => { try { return (await fetch(base + "/api/status")).ok; } catch { return false; } }, "Server not ready");
    context = await browser.newContext();
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    await page.goto(base);
    await page.fill("#credential", "fictional-machine-password");
    await page.fill("#password-confirm", "fictional-machine-password");
    await page.click("#auth-submit");
    await page.locator("#saved-recovery").click();
    await page.click('.nav-item[data-page="settings"]');
    await page.click("#enable-machine-access");
    await page.fill('#generic-content [name="password"]', "fictional-machine-password");
    await page.click('#generic-content button[type="submit"]');
    await page.locator("#generic-dialog").waitFor({ state: "hidden" });
    await page.locator("#machine-access-status").filter({ hasText: "已授权" }).waitFor();
    const issued = await (await context.request.post(base + "/api/agent-token", { data: { allow_writes: true } })).json();
    const token = issued.token;
    const credentials = fs.readdirSync(path.join(env.LOCALAPPDATA, "BastidePersonal/device"));
    assert.equal(credentials.length, 1);
    const encrypted = fs.readFileSync(path.join(env.LOCALAPPDATA, "BastidePersonal/device", credentials[0]));
    assert(!encrypted.includes(Buffer.from("fictional-machine-password")));
    await page.close();
    assert.equal((await fetch(base + "/api/agent/catalog", { headers: { Authorization: "Bearer " + token } })).status, 200);
    child.kill();
    await wait(() => child.exitCode !== null || child.signalCode !== null, "Initial process not stopped");
    child = null;

    const code = `import asyncio,json,os,sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
async def main():
 async with stdio_client(StdioServerParameters(command=sys.executable,args=['-m','app.personal_mcp'],env=dict(os.environ))) as (r,w):
  async with ClientSession(r,w) as s:
   await s.initialize()
   result=await s.call_tool('list_accounts_and_categories',{})
   assert not result.isError
   data=json.loads(next(x.text for x in result.content if x.type=='text'))
   assert 'accounts' in data,data
   result=await s.call_tool('finance_since_start',{})
   assert not result.isError
   assert 'error' not in json.loads(next(x.text for x in result.content if x.type=='text'))
asyncio.run(main())`;
    const mcp = spawn(python, ["-c", code], { cwd: path.join(repo, "backend"), windowsHide: true,
      env: { ...env, BASTIDE_PERSONAL_URL: base, BASTIDE_PERSONAL_TOKEN: token }, stdio: ["ignore", "pipe", "pipe"] });
    let mcpErrors = "";
    mcp.stderr.on("data", chunk => { mcpErrors += chunk.toString(); });
    await wait(() => mcp.exitCode !== null, "MCP auto-start timed out");
    assert.equal(mcp.exitCode, 0, "MCP could not auto-start and read the authorized ledger: " + mcpErrors);
    // No cookies: the auto-unlocked background vault must never unlock browser access.
    const status = await (await fetch(base + "/api/status")).json();
    assert.equal(status.unlocked, true);
    assert.equal(status.authenticated, false);
    assert.equal((await fetch(base + "/api/accounts")).status, 401);
    const launcher = spawn("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
      path.join(repo, "tools/start_personal.ps1"), "-NoBrowser", "-Port", "19012", "-Vault", vault],
      { cwd: repo, env, windowsHide: true, stdio: "ignore" });
    await wait(() => launcher.exitCode !== null, "Hidden launcher did not return");
    assert.equal(launcher.exitCode, 0, "Authorized launcher failed for a Chinese vault path");
    await context.close();
    context = await browser.newContext();
    const reopened = await context.newPage();
    await reopened.goto(base);
    await reopened.fill("#credential", "fictional-machine-password");
    await reopened.click("#auth-submit");
    await reopened.locator("#workspace:not([hidden])").waitFor();
    await reopened.click('.nav-item[data-page="settings"]');
    await reopened.click("#disable-machine-access");
    await reopened.locator("#auth:not([hidden])").waitFor();
    assert.equal((await fetch(base + "/api/agent/catalog", { headers: { Authorization: "Bearer " + token } })).status, 401);
    assert.equal(fs.readdirSync(path.join(env.LOCALAPPDATA, "BastidePersonal/device")).length, 0);
    await reopened.fill("#credential", "fictional-machine-password");
    await reopened.click("#auth-submit");
    await reopened.locator("#workspace:not([hidden])").waitFor();
    assert.equal((await context.request.post(base + "/api/shutdown", { data: {} })).status(), 200);
    await wait(async () => { try { await fetch(base + "/api/status"); return false; } catch { return true; } }, "Hidden service did not stop");
    assert.deepEqual(errors, []);
    console.log("PASS: device authorization UI; native DPAPI ciphertext; access after page close; real MCP stdio hidden auto-start; browser remains unauthenticated; hidden PowerShell launcher with Chinese/space path; revoke locks agent out; hidden service clean shutdown.");
  } finally {
    if (context) {
      try {
        await context.request.post(base + "/api/unlock", { data: { credential: "fictional-machine-password" } });
        await context.request.post(base + "/api/shutdown", { data: {} });
      } catch {}
    }
    await browser.close();
    if (child && child.exitCode === null) child.kill();
  }
}
main().catch(e => { console.error(e); process.exitCode = 1; });
