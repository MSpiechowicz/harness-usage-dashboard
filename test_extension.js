import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, rename, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import test from "node:test";
import usageDashboard from "./extension.js";

const execute = promisify(execFile);

test("startup reports a new release despite a fresh no-update cache", async () => {
  const home = await mkdtemp(join(tmpdir(), "dashboard-startup-"));
  const keys = ["HOME", "PI_CODING_AGENT_DIR", "OMP_PROFILE", "PI_PROFILE", "OMP_USAGE_LAUNCHER"];
  const saved = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  Object.assign(process.env, { HOME: home, PI_CODING_AGENT_DIR: join(home, "agent"),
    OMP_PROFILE: "default", PI_PROFILE: "default", OMP_USAGE_LAUNCHER: "1" });
  try {
    const handlers = new Map();
    const scheduled = [];
    const notices = [];
    usageDashboard({
      setLabel() {}, registerCommand() {},
      on(event, handler) { handlers.set(event, handler); },
      async exec(binary, args, options) {
        // Keep the real updater and cache; replace only the remote release response.
        const script = `
import json, sys
import updater
current = updater.current_version(updater.ROOT)
updater.write_cache(None, {'version': current, 'tag': 'v' + current, 'url': updater.RELEASE_BASE + 'v' + current})
version = str(updater.version_tuple(current)[0] + 1) + '.0.0'
updater.latest_release = lambda: {'version': version, 'tag': 'v' + version, 'url': updater.RELEASE_BASE + 'v' + version}
print(json.dumps(updater.check(cached='--cached' in sys.argv)))
`;
        const result = await execute(binary, ["-c", script, ...args.slice(1)], options);
        return { code: 0, ...result };
      },
    });
    const ctx = {
      hasUI: true, cwd: process.cwd(),
      sessionManager: {
        getSessionId: () => "startup-regression", getUsageStatistics: () => ({}),
        getLeafId: () => null, getHeader: () => ({}), getEntries: () => [],
      },
      setInterval: () => 1,
      setTimeout(callback) { scheduled.push(callback); },
      ui: { notify(message, level) { notices.push({ message, level }); } },
    };
    await handlers.get("session_start")({}, ctx);
    assert.deepEqual(notices, [], "startup must return before the background check");
    for (const callback of scheduled) await callback();
    assert.equal(notices.length, 1);
    assert.equal(notices[0].message, "Usage Dashboard update available. Run `/usage-dashboard update install` to update it.");
    assert.equal(notices[0].level, "warning");
  } finally {
    for (const key of keys) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
    await rm(home, { recursive: true, force: true });
  }
});

test("records the effective thinking level for model usage", async () => {
  const home = await mkdtemp(join(tmpdir(), "dashboard-thinking-"));
  const keys = ["HOME", "PI_CODING_AGENT_DIR", "OMP_PROFILE", "PI_PROFILE", "TMUX", "TMUX_PANE"];
  const saved = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  Object.assign(process.env, {
    HOME: home,
    PI_CODING_AGENT_DIR: join(home, "agent"),
    OMP_PROFILE: "default",
    PI_PROFILE: "default",
  });
  delete process.env.TMUX;
  delete process.env.TMUX_PANE;
  try {
    const handlers = new Map();
    usageDashboard({
      setLabel() {},
      registerCommand() {},
      on(event, handler) { handlers.set(event, handler); },
      async exec() { return { code: 0, stdout: "", stderr: "" }; },
    });
    const timestamp = new Date().toISOString();
    const timestamp2 = new Date(Date.parse(timestamp) + 1000).toISOString();
    const usage = { input: 100, output: 20, cacheRead: 30, cacheWrite: 10, totalTokens: 160 };
    const ctx = {
      hasUI: true,
      cwd: process.cwd(),
      sessionManager: {
        getSessionId: () => "thinking-session",
        getUsageStatistics: () => usage,
        getLeafId: () => "request-xhigh",
        getHeader: () => ({}),
        getEntries: () => [
          { type: "thinking_level_change", thinkingLevel: "high", configured: null },
          { type: "message", id: "request-high", timestamp,
            message: { role: "assistant", provider: "openai-codex", model: "Luna", usage } },
          { type: "thinking_level_change", thinkingLevel: "xhigh", configured: null },
          { type: "message", id: "request-xhigh", timestamp: timestamp2,
            message: { role: "assistant", provider: "openai-codex", model: "Luna", usage } },
        ],
      },
      setInterval: () => 1,
      setTimeout() {},
      ui: { notify() {} },
    };
    await handlers.get("session_start")({}, ctx);

    const script = `
import json
from session_usage import summary
print(json.dumps(summary(owner=None)))
`;
    const result = await execute("python3", ["-c", script], {
      cwd: process.cwd(),
      env: process.env,
    });
    const report = JSON.parse(result.stdout);
    assert.deepEqual(report.current.models.map(item => ({
      model: item.model,
      thinking: item.thinking_level,
      total: item.total,
    })), [
      { model: "Luna", thinking: "high", total: 160 },
      { model: "Luna", thinking: "xhigh", total: 160 },
    ]);
    assert.equal(report.current.model_summaries[0].total, 320);
  } finally {
    for (const key of keys) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
    await rm(home, { recursive: true, force: true });
  }
});

test("auxiliary usage keeps its own thinking variants while the parent stays Low", async () => {
  const home = await mkdtemp(join(tmpdir(), "dashboard-aux-thinking-"));
  const keys = ["HOME", "PI_CODING_AGENT_DIR", "OMP_PROFILE", "PI_PROFILE", "TMUX", "TMUX_PANE"];
  const saved = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  Object.assign(process.env, {
    HOME: home, PI_CODING_AGENT_DIR: join(home, "agent"),
    OMP_PROFILE: "default", PI_PROFILE: "default",
  });
  delete process.env.TMUX;
  delete process.env.TMUX_PANE;
  try {
    const handlers = new Map();
    usageDashboard({
      setLabel() {}, registerCommand() {},
      on(event, handler) { handlers.set(event, handler); },
      async exec() { return { code: 0, stdout: "", stderr: "" }; },
    });
    const timestamp = new Date().toISOString();
    const usage = { input: 100, output: 20, cacheRead: 30, cacheWrite: 10, totalTokens: 160 };
    const levels = ["high", "xhigh", undefined, null, "", " ", 42, "auto", "invalid", "high\0"];
    const entries = [
      { type: "thinking_level_change", thinkingLevel: "low" },
      ...levels.map((thinkingLevel, index) => ({
        type: "model_usage", id: `aux-${index}`, timestamp,
        provider: "openai-codex", model: "Luna", thinkingLevel,
        role: index === 1 ? "scout" : "implementation", usage,
      })),
      { type: "message", id: "parent-low", timestamp,
        message: { role: "assistant", provider: "openai-codex", model: "Luna", usage } },
      { type: "message", id: "task-mixed", timestamp,
        message: { role: "toolResult", toolName: "task", details: { usage } } },
    ];
    // Re-emitting an identical request must not double-count it.
    entries.push(entries[1]);
    const ctx = {
      hasUI: true, cwd: process.cwd(),
      sessionManager: {
        getSessionId: () => "aux-thinking-session", getUsageStatistics: () => usage,
        getLeafId: () => "parent-low", getHeader: () => ({}), getEntries: () => entries.slice(),
      },
      setInterval: () => 1, setTimeout() {},
      ui: { notify(message) { assert.fail(message); } },
    };
    await handlers.get("session_start")({}, ctx);
    await handlers.get("agent_end")({}, ctx);
    // A new activation replays the same entries against the real ledger.
    await handlers.get("session_start")({}, ctx);
    const result = await execute("python3", ["-c", `
import json
from session_usage import summary, database
with database() as db:
    requests = db.execute('SELECT COUNT(*) FROM tokens').fetchone()[0]
print(json.dumps({'report': summary(owner=None), 'requests': requests}))
`], { cwd: process.cwd(), env: process.env });
    const { report, requests } = JSON.parse(result.stdout);
    assert.equal(requests, levels.length + 2);
    assert.deepEqual(report.current.models, [
      { provider: "openai-codex", model: "Luna", thinking_level: "high",
        input: 100, output: 20, cache_read: 30, cache_write: 10, total: 160 },
      { provider: "openai-codex", model: "Luna", thinking_level: "low",
        input: 100, output: 20, cache_read: 30, cache_write: 10, total: 160 },
      { provider: "openai-codex", model: "Luna", thinking_level: "unknown",
        input: 800, output: 160, cache_read: 240, cache_write: 80, total: 1280 },
      { provider: "openai-codex", model: "Luna", thinking_level: "xhigh",
        input: 100, output: 20, cache_read: 30, cache_write: 10, total: 160 },
      { provider: "task (mixed)", model: "mixed / unattributed", thinking_level: "unknown",
        input: 100, output: 20, cache_read: 30, cache_write: 10, total: 160 },
    ]);
  } finally {
    for (const key of keys) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
    await rm(home, { recursive: true, force: true });
  }
});

test("native child requests retain serving models while idle and across parent switches", async () => {
  const home = await mkdtemp(join(tmpdir(), "dashboard-native-"));
  const keys = ["HOME", "PI_CODING_AGENT_DIR", "OMP_PROFILE", "PI_PROFILE", "TMUX", "TMUX_PANE"];
  const saved = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  Object.assign(process.env, {
    HOME: home, PI_CODING_AGENT_DIR: join(home, "agent"),
    OMP_PROFILE: "default", PI_PROFILE: "default",
  });
  delete process.env.TMUX;
  delete process.env.TMUX_PANE;
  try {
    const handlers = new Map();
    const events = new Map();
    let tick;
    let session = "native-parent";
    let entries = [];
    const usage = { input: 100, output: 20, cacheRead: 30, cacheWrite: 10, totalTokens: 160 };
    usageDashboard({
      setLabel() {}, registerCommand() {},
      on(event, handler) { handlers.set(event, handler); },
      events: { on(event, handler) { events.set(event, handler); return () => events.delete(event); } },
      async exec() { return { code: 0, stdout: "", stderr: "" }; },
    });
    const warnings = [];
    const ctx = {
      hasUI: true, cwd: process.cwd(),
      sessionManager: {
        getSessionId: () => session, getUsageStatistics: () => usage,
        getLeafId: () => "unchanged-parent-leaf", getHeader: () => ({}),
        getEntries: () => entries.slice(),
      },
      setInterval(callback) { tick = callback; return 1; },
      setTimeout() {}, clearTimer() {},
      ui: { notify(message, type) { warnings.push(type); } },
    };
    const emit = (channel, payload) => events.get(`task:subagent:${channel}`)?.(payload);
    const request = (id, provider, model, timestamp, level) => {
      const payload = { id, event: { type: "message_end",
        message: { role: "assistant", provider, model, timestamp, usage } } };
      emit("event", payload);
      // The executor publishes resolved serving metadata after the raw event.
      emit("progress", { progress: { id, resolvedModelIdentity: `${provider}/${model}`,
        resolvedThinkingLevel: level } });
      return payload;
    };
    const report = async () => {
      await Promise.resolve();
      await tick();
      const result = await execute("python3", ["-c", `
import json
from session_usage import summary
print(json.dumps(summary()))
`], { cwd: process.cwd(), env: process.env });
      return JSON.parse(result.stdout);
    };
    await handlers.get("session_start")({}, ctx);
    // A cancelled switch has no matching session_switch event.
    await handlers.get("session_before_switch")({}, ctx);
    emit("lifecycle", { id: "First", status: "started", parentToolCallId: "task-one" });
    emit("lifecycle", { id: "Second", status: "started", parentToolCallId: "task-two" });
    const now = Date.now();
    const first = request("First", "openai-codex", "Luna", now, "high");
    await Promise.resolve();
    request("Second", "anthropic", "Claude", now + 1, "max");
    let history = await report();
    assert.deepEqual(history.current.models.map(row => [row.provider, row.model, row.thinking_level, row.total]), [
      ["anthropic", "Claude", "max", 160], ["openai-codex", "Luna", "high", 160],
    ]);
    // Repeated delivery is not another request; sync task totals aren't extra usage.
    emit("event", first);
    entries.push({ type: "message", id: "task-result", timestamp: new Date(now + 2).toISOString(),
      message: { role: "toolResult", toolName: "task", toolCallId: "task-one", details: { usage } } });
    history = await report();
    assert.equal(history.current.models.reduce((sum, row) => sum + row.total, 0), 320);
    assert.ok(history.current.models.every(row => row.provider !== "task (mixed)"));
    await handlers.get("session_before_switch")({}, ctx);
    session = "next-parent";
    entries = [];
    await handlers.get("session_switch")({}, ctx);
    request("Second", "openai-codex", "Astra", now + 3, "xhigh");
    emit("lifecycle", { id: "Second", status: "completed" });
    history = await report();
    assert.equal(history.current.id, "next-parent");
    assert.deepEqual(history.current.models, []);
    assert.equal(history.previous.id, "native-parent");
    assert.deepEqual(history.previous.models.map(row => [row.model, row.thinking_level, row.total]), [
      ["Claude", "max", 160], ["Astra", "xhigh", 160], ["Luna", "high", 160],
    ]);
    emit("lifecycle", { id: "Second", status: "started", parentToolCallId: "new-task" });
    request("Second", "anthropic", "NewModel", now + 4, "low");
    history = await report();
    assert.deepEqual(history.current.models.map(row => [row.model, row.thinking_level, row.total]),
      [["NewModel", "low", 160]]);
    assert.equal(history.previous.models.reduce((sum, row) => sum + row.total, 0), 480);
    assert.deepEqual(warnings, []);
    // Force a real SQLite-open failure, then restore storage before the retry.
    await rename(join(home, "agent"), join(home, "saved-agent"));
    await writeFile(join(home, "agent"), "temporarily unavailable");
    request("Second", "anthropic", "NewModel", now + 5, "low");
    await Promise.resolve();
    await tick();
    assert.deepEqual(warnings, ["warning"]);
    await rm(join(home, "agent"));
    await rename(join(home, "saved-agent"), join(home, "agent"));
    history = await report();
    assert.equal(history.current.models[0].total, 320);
    assert.equal(history.previous.models.reduce((sum, row) => sum + row.total, 0), 480);
    await handlers.get("session_shutdown")({}, ctx);
  } finally {
    for (const key of keys) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
    await rm(home, { recursive: true, force: true });
  }
});

test("periodic sync records idle-parent bursts and entries appended during a write exactly once", async () => {
  const home = await mkdtemp(join(tmpdir(), "dashboard-live-sync-"));
  const keys = ["HOME", "PI_CODING_AGENT_DIR", "OMP_PROFILE", "PI_PROFILE", "TMUX", "TMUX_PANE"];
  const saved = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  Object.assign(process.env, {
    HOME: home, PI_CODING_AGENT_DIR: join(home, "agent"),
    OMP_PROFILE: "default", PI_PROFILE: "default",
  });
  delete process.env.TMUX;
  delete process.env.TMUX_PANE;
  try {
    const handlers = new Map();
    let tick;
    usageDashboard({
      setLabel() {}, registerCommand() {},
      on(event, handler) { handlers.set(event, handler); },
      async exec() { return { code: 0, stdout: "", stderr: "" }; },
    });
    const session = "idle-parent-session";
    const entries = [{ type: "thinking_level_change", thinkingLevel: "low" }];
    const ctx = {
      hasUI: true, cwd: process.cwd(),
      sessionManager: {
        getSessionId: () => session,
        // Native statistics include auxiliary model_usage even without a parent turn.
        getUsageStatistics: () => entries.reduce((total, entry) => {
          if (entry.usage) {
            for (const field of Object.keys(total)) total[field] += entry.usage[field];
          }
          return total;
        }, { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0 }),
        getLeafId: () => entries.at(-1).id ?? null,
        getHeader: () => ({}),
        // Native getEntries returns a shallow snapshot, not the live array.
        getEntries: () => entries.slice(),
      },
      setInterval(callback) {
        tick = callback;
        return 1;
      },
      setTimeout() {},
      ui: { notify(message) { assert.fail(message); } },
    };
    await handlers.get("session_start")({}, ctx);
    const timestamp = new Date().toISOString();
    const requests = Array.from({ length: 12 }, (_, index) => ({
      type: "model_usage", id: `${session}-${index}`, timestamp,
      provider: "openai-codex", model: "Luna", thinkingLevel: "xhigh", role: "scout",
      usage: { input: 100 + index, output: 20 + index, cacheRead: 30 + index,
        cacheWrite: 10 + index, totalTokens: 160 + 4 * index },
    }));
    entries.push(...requests.slice(0, 3));
    const firstWrite = tick();
    // Start the actual Python save, then emit requests before its close event.
    await Promise.resolve();
    entries.push(...requests.slice(3, 8));
    const busyTick = tick();
    entries.push(...requests.slice(8));
    await Promise.all([firstWrite, busyTick]);
    await tick();
    // Repeated intervals and a duplicate emitted entry retain one ledger row.
    entries.push(requests[0]);
    await tick();
    await tick();
    const result = await execute("python3", ["-c", `
import json, sys
from session_usage import database
with database() as db:
    rows = db.execute('''SELECT provider, model, thinking_level, input, output,
                         cache_read, cache_write, total FROM tokens
                         WHERE session=? ORDER BY total''', (sys.argv[1],))
    print(json.dumps([dict(row) for row in rows]))
`, session], { cwd: process.cwd(), env: process.env });
    assert.deepEqual(JSON.parse(result.stdout), requests.map(({ provider, model, thinkingLevel, usage }) => ({
      provider, model, thinking_level: thinkingLevel, input: usage.input, output: usage.output,
      cache_read: usage.cacheRead, cache_write: usage.cacheWrite, total: usage.totalTokens,
    })));
  } finally {
    for (const key of keys) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
    await rm(home, { recursive: true, force: true });
  }
});

test("command menu uses native bordered selectors and left arrow navigation", async () => {
  let command;
  let execArgs;
  let selectIndex = 0;
  const calls = [];
  usageDashboard({
    setLabel() {},
    on() {},
    registerCommand(_name, definition) { command = definition; },
    async exec(_binary, args) {
      execArgs = args;
      return { code: 0, stdout: "", stderr: "" };
    },
  });
  const ctx = {
    hasUI: true,
    cwd: process.cwd(),
    models: { list: () => [] },
    ui: {
      async select(title, options, dialogOptions) {
        calls.push({ title, options, dialogOptions });
        if (selectIndex === 0) {
          selectIndex += 1;
          return "View";
        }
        if (selectIndex === 1) {
          selectIndex += 1;
          dialogOptions.onLeft();
          return undefined;
        }
        if (selectIndex === 2) {
          selectIndex += 1;
          return "View";
        }
        selectIndex += 1;
        return "Details";
      },
      notify() {},
    },
  };

  await command.handler("", ctx);
  assert.deepEqual(execArgs.slice(-3), ["--", "view", "details"]);
  assert.deepEqual(calls.slice(0, 2).map(({ title, options }) => ({ title, options })), [
    { title: "Usage Dashboard", options: ["Position", "Providers", "Theme", "Update", "View", "Window"] },
    { title: "Usage Dashboard / View", options: ["Compact", "Details"] },
  ]);
  assert.equal(calls[0].dialogOptions, undefined);
  assert.equal(typeof calls[1].dialogOptions.onLeft, "function");
  assert.match(calls[1].dialogOptions.helpText, /back/);
  assert.ok(calls.flatMap(({ options }) => options).every(option => !/^\d+\./.test(option)));
});

test("theme menu preserves palette descriptions with native selectors", async () => {
  let command;
  let execArgs;
  const calls = [];
  const notices = [];
  const inputs = [];
  usageDashboard({
    setLabel() {},
    on() {},
    registerCommand(_name, definition) { command = definition; },
    async exec(_binary, args) {
      execArgs = args;
      return { code: 0, stdout: "", stderr: "" };
    },
  });
  const ctx = {
    hasUI: true,
    cwd: process.cwd(),
    models: { list: () => [] },
    ui: {
      async select(title, options, dialogOptions) {
        calls.push({ title, options, dialogOptions });
        return "Custom  (override individual colors)";
      },
      async input(title, placeholder) {
        inputs.push({ title, placeholder });
        return "accent #58a66a";
      },
      notify(message, level) { notices.push({ message, level }); },
    },
  };

  await command.handler("theme", ctx);
  const themeOptions = calls[0].options;
  assert.deepEqual(themeOptions.map(option => option.split(/\s/)[0]),
    ["Blue", "Brown", "Custom", "Cyan", "Green", "Magenta", "Orange", "Red", "Reset", "Yellow"]);
  assert.match(themeOptions.find(option => option.startsWith("Cyan")), /clear cyan accent/);
  assert.match(themeOptions.find(option => option.startsWith("Magenta")), /bold magenta accent/);
  assert.match(themeOptions.find(option => option.startsWith("Custom")), /override individual colors/);
  assert.equal(calls[0].dialogOptions.helpText.includes("back"), true);
  assert.deepEqual(inputs, [{
    title: "Custom theme: TOKEN COLOR (for example: accent #58a66a)",
    placeholder: "accent #58a66a",
  }]);
  assert.equal(notices[0].level, "info");
  assert.match(notices[0].message, /text, muted, secondary, accent, chart, good, warn, error/);
  assert.match(notices[0].message, /terminal names, gray, brown, orange, or #RRGGBB/);
  assert.deepEqual(execArgs.slice(-5), ["--", "theme", "custom", "accent", "#58a66a"]);
});

test("detaches the dashboard before waiting for shutdown recording", async () => {
  const home = await mkdtemp(join(tmpdir(), "dashboard-shutdown-"));
  const keys = ["HOME", "PI_CODING_AGENT_DIR", "OMP_PROFILE", "PI_PROFILE", "TMUX", "TMUX_PANE"];
  const saved = Object.fromEntries(keys.map(key => [key, process.env[key]]));
  Object.assign(process.env, {
    HOME: home,
    PI_CODING_AGENT_DIR: join(home, "agent"),
    OMP_PROFILE: "default",
    PI_PROFILE: "default",
    TMUX: "/tmp/tmux-test,123,0",
    TMUX_PANE: "%1",
  });
  try {
    const handlers = new Map();
    const calls = [];
    usageDashboard({
      setLabel() {},
      registerCommand() {},
      on(event, handler) { handlers.set(event, handler); },
      async exec(_binary, args) {
        calls.push(args);
        return { code: 0, stdout: "", stderr: "" };
      },
    });
    const ctx = {
      hasUI: true,
      cwd: process.cwd(),
      sessionManager: {
        getSessionId: () => "shutdown-session",
        getUsageStatistics: () => ({}),
        getLeafId: () => null,
        getHeader: () => ({}),
        getEntries: () => [],
      },
      ui: { notify() {} },
    };
    await handlers.get("session_shutdown")({}, ctx);
    assert.deepEqual(calls[0].slice(-2), ["--", "detach"]);
  } finally {
    for (const key of keys) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
    await rm(home, { recursive: true, force: true });
  }
});
