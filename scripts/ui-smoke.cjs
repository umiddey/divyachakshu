/* Headless-Chromium driver for the `oi ui` smoke. Screenshot per step. */
"use strict";
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

const [url, smokeDir] = process.argv.slice(2);
const shots = path.join(smokeDir, "ui-shots");
const fail = (msg) => { console.error("SMOKE FAIL: " + msg); process.exit(1); };

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const shot = async (name) => {
    const p = path.join(shots, name);
    await page.screenshot({ path: p, fullPage: true });
    console.log("SHOT " + p);
  };

  await page.goto(url);
  await page.waitForSelector("#view-welcome:not([hidden])", { timeout: 10000 });
  await shot("01-welcome.png");

  await page.click("#btn-start");
  await page.waitForSelector("#view-opencode:not([hidden])");
  await page.waitForFunction(() =>
    !document.getElementById("oc-status").textContent.includes("Checking"));
  await shot("02-opencode.png");

  await page.click("#btn-oc-login-start");
  await page.waitForSelector("#oc-integrations .panel");
  await page.fill("#oc-integrations input[type=password]", "smoke-key-1");
  // Empty-model case first: the fake models endpoint returns no models, so
  // Next must stay disabled even though the provider is connected.
  const emptyFlag = path.join(smokeDir, "models-empty");
  fs.writeFileSync(emptyFlag, "1");
  await Promise.all([
    page.waitForResponse((r) => r.url().endsWith("/api/opencode/models") && r.ok()),
    page.getByRole("button", { name: "Connect with API key" }).click(),
  ]);
  await page.waitForFunction(() =>
    document.getElementById("oc-status").textContent.includes("Provider connected"));
  await page.waitForTimeout(300); // let loadModels apply the empty list
  if (await page.locator("#oc-model option").count() !== 0) fail("model list not empty");
  if (await page.isEnabled("#btn-oc-next")) fail("Next enabled with an empty model list");
  if (!(await page.isVisible("#oc-model-hint"))) fail("no hint shown for an empty model list");
  await shot("02b-opencode-no-models.png");

  // Normal case: the fake models endpoint answers with the real list again.
  fs.rmSync(emptyFlag);
  await Promise.all([
    page.waitForResponse((r) => r.url().endsWith("/api/opencode/models") && r.ok()),
    page.reload(),
  ]);
  await page.waitForSelector("#view-welcome:not([hidden])", { timeout: 10000 });
  await page.click("#btn-start");
  await page.waitForSelector("#view-opencode:not([hidden])");
  await page.waitForFunction(() => document.getElementById("oc-model").options.length > 0);
  await page.waitForFunction(() => !document.getElementById("btn-oc-next").disabled);
  if (await page.isVisible("#oc-model-hint")) fail("hint still shown with a non-empty model list");
  await page.selectOption("#oc-model", "stepfun/gpt-test");
  await shot("03-opencode-connected.png");
  await page.click("#btn-oc-next");

  await page.waitForSelector("#view-discord:not([hidden])");
  await page.fill("#discord-token", "your-fake-bot-token");
  await page.click("#btn-discord-check");
  await page.waitForSelector("#discord-result:not([hidden])");
  const discordText = await page.textContent("#discord-result");
  if (!discordText.includes("fake-bot")) fail("discord validate did not show the application");
  await shot("04-discord.png");
  await page.click("#btn-discord-next");

  await page.waitForSelector("#view-channels:not([hidden])");
  await page.waitForSelector("#guilds .guild");
  await page.check("#guilds .guild:nth-of-type(1) input[type=checkbox]");
  await page.waitForFunction(() => !document.getElementById("btn-channels-next").disabled);
  await shot("05-channels.png");
  await page.click("#btn-channels-next");

  await page.waitForSelector("#view-repos:not([hidden])");
  await page.click("#btn-repos-scan");
  await page.waitForSelector("#watch-repos .watch label input");
  await page.check("#watch-repos .watch label input");
  await shot("06-repos.png");
  await page.click("#btn-repos-next");

  await page.waitForSelector("#view-behavior:not([hidden])");
  await page.fill("#f-personality", "smoke personality");
  await page.selectOption("#f-environment_mode", "workstation");
  await shot("07-behavior.png");
  await page.click("#btn-behavior-next");

  await page.waitForSelector("#view-review:not([hidden])");
  await shot("08-review.png");
  await page.click("#btn-save");
  await page.waitForSelector("#view-service:not([hidden])", { timeout: 10000 });

  await page.click("#btn-install-service");
  await page.waitForFunction(() => !document.getElementById("service-result").hidden);
  const serviceText = await page.textContent("#service-result");
  if (!serviceText.includes("Started")) fail("service install failed: " + serviceText);
  await shot("09-service.png");
  await page.click("#btn-open-dashboard");

  await page.waitForSelector("#view-dashboard:not([hidden])");
  await page.waitForFunction(() =>
    !document.getElementById("dash-status").textContent.includes("Loading"));
  const statusText = await page.textContent("#dash-status");
  if (!statusText.includes("posting")) fail("dashboard status not posting: " + statusText);
  await page.click("#btn-dash-logs");
  await shot("10-dashboard.png");
  await page.waitForSelector("#settings-form [data-field=personality]");
  await page.fill("#settings-form [data-field=personality]", "smoke edited personality");
  await page.click("#btn-save-settings");
  await page.waitForSelector("#settings-ok:not([hidden])", { timeout: 10000 });
  await shot("11-settings-saved.png");

  const home = process.env.HOME;
  const cfg = fs.readFileSync(path.join(home, ".config/oi/config.toml"), "utf8");
  for (const needle of ["smoke edited personality", "guild_id = 100", "stepfun/gpt-test"]) {
    if (!cfg.includes(needle)) fail("config.toml missing " + JSON.stringify(needle));
  }
  const secrets = fs.readFileSync(path.join(home, ".config/oi/secrets.env"), "utf8");
  if (!secrets.includes("OI_DISCORD_TOKEN=your-fake-bot-token")) fail("token not saved");
  const install = JSON.parse(fs.readFileSync(path.join(smokeDir, "install_daemon.json"), "utf8"));
  if (install.user_mode !== true) fail("install_daemon not called with user_mode=true: " + JSON.stringify(install));
  await browser.close();
  console.log("SMOKE OK");
})().catch((err) => fail(err.stack || String(err)));
