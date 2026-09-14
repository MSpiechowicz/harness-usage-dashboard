import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";

const dashboard = fileURLToPath(new URL("./dashboard.py", import.meta.url));
const updater = fileURLToPath(new URL("./updater.py", import.meta.url));
const sessionStore = fileURLToPath(new URL("./session_usage.py", import.meta.url));

// Only normalized counters cross this pipe: never prompts, tool output, or credentials.
function saveSession(payload, profile, cwd) {
  return new Promise((resolve, reject) => {
    const args = [sessionStore];
    if (profile !== undefined) args.push("--profile", profile);
    const child = spawn("python3", args, { cwd, stdio: ["pipe", "ignore", "ignore"] });
    const timeout = setTimeout(() => child.kill(), 10000);
    child.on("error", reject);
    child.stdin.on("error", reject);
    child.on("close", code => {
      clearTimeout(timeout);
      if (code === 0) resolve();
      else reject(new Error("Could not save dashboard session usage."));
    });
    child.stdin.end(JSON.stringify(payload));
  });
}
const popular = ["codex", "claude", "copilot", "grok", "xai-oauth", "deepseek", "gemini", "google-antigravity", "cursor", "kimi-code", "minimax-code", "openrouter"];
const sections = {
  view: ["list", "compact", "details"],
  position: ["left", "right"],
  providers: ["add", "remove", "hide", "show"],
  theme: ["green", "blue", "brown", "yellow", "color", "reset"],
  window: ["on", "off", "focus", "refresh", "interval", "hide", "show"],
  update: ["check", "install"],
};
const help = "Sections: view (list, compact, details; details separates model thinking levels and adds summaries); position (left, right); providers (add, remove, hide, show PROVIDER); theme (green, blue, brown, yellow, color TOKEN COLOR, reset); window (on, off, focus, refresh, interval SECONDS, hide/show PROVIDER FILTER); update (check, install).";

export default function usageDashboard(pi) {
  pi.setLabel("Usage dashboard");
  let recording;
  let pending = Promise.resolve();
  let queued = 0;
  let timer;
  let saveFailed = false;

  function record(ctx, action = "sync") {
    if (!ctx.hasUI) return pending;
    if (action === "sync" && queued) return pending;
    const manager = ctx.sessionManager;
    const session = manager.getSessionId();
    if (!recording || recording.session !== session || action === "start") {
      recording = { session, activation: randomUUID(), count: 0, signature: undefined,
        thinkingLevel: "unknown", saved: false };
      action = "start";
    }
    const state = recording;
    const usage = manager.getUsageStatistics();
    const signature = JSON.stringify([manager.getLeafId(), usage.input, usage.output,
      usage.cacheRead, usage.cacheWrite, usage.totalTokens]);
    if (action === "sync" && signature === state.signature) return pending;
    if (!state.saved && action === "sync") action = "start";
    const header = manager.getHeader();
    const entries = manager.getEntries();
    const batch = [];
    for (const entry of entries.slice(state.count)) {
      if (entry.type === "thinking_level_change") {
        const value = entry.thinkingLevel ?? entry.configured;
        state.thinkingLevel = typeof value === "string" && value.trim() ? value.trim() : "unknown";
        continue;
      }
      const message = entry.type === "message" ? entry.message : undefined;
      const task = message?.role === "toolResult" && message.toolName === "task";
      const usage = entry.type === "model_usage" ? entry.usage
        : message?.role === "assistant" ? message.usage : task ? message.details?.usage : undefined;
      if (!usage) continue;
      const at = Date.parse(entry.timestamp);
      // Forks copy old entries; inherited context is not newly consumed usage.
      if (header?.parentSession && at <= Date.parse(header.timestamp)) continue;
      const counts = [usage.input, usage.output, usage.cacheRead, usage.cacheWrite];
      if (!Number.isFinite(at) || !counts.every(value => Number.isSafeInteger(value) && value >= 0)) continue;
      const provider = entry.provider ?? message?.provider ?? (task ? "task (mixed)" : "unknown");
      const model = entry.model ?? message?.model ?? (task ? "mixed / unattributed" : "unknown");
      const total = usage.totalTokens ?? counts.reduce((sum, value) => sum + value, 0);
      if (!Number.isSafeInteger(total) || total < 0) continue;
      const id = createHash("sha256").update(JSON.stringify([entry.id, entry.timestamp, provider, model])).digest("hex");
      batch.push({ id, at: at / 1000, provider, model, thinkingLevel: state.thinkingLevel,
        input: counts[0], output: counts[1], cacheRead: counts[2], cacheWrite: counts[3], total });
    }
    const payload = { session, activation: state.activation, owner: process.env.TMUX_PANE, action, entries: batch };
    const profile = process.env.OMP_PROFILE ?? process.env.PI_PROFILE;
    queued++;
    pending = pending.then(async () => {
      try {
        await saveSession(payload, profile, ctx.cwd);
        state.count = Math.max(state.count, entries.length);
        state.signature = signature;
        state.saved = true;
        saveFailed = false;
      } catch {
        if (!saveFailed) ctx.ui.notify("Dashboard session history could not be saved; recording will retry.", "warning");
        saveFailed = true;
      } finally {
        queued--;
      }
    });
    return pending;
  }
  async function control(words, ctx, quiet = false) {
    const command = [dashboard, "control"];
    if (process.env.TMUX && process.env.TMUX_PANE) command.push("--owner", process.env.TMUX_PANE);
    const profile = process.env.OMP_PROFILE ?? process.env.PI_PROFILE;
    if (profile !== undefined) command.push("--profile", profile);
    command.push("--", ...words);
    try {
      const result = await pi.exec("python3", command, { timeout: 15000, cwd: ctx.cwd });
      if (!quiet || result.code !== 0) {
        ctx.ui.notify(result.code === 0 ? result.stdout.trim() : (result.stderr.trim() || "Could not update usage dashboard"),
          result.code === 0 ? "info" : "error");
      }
    } catch (error) {
      ctx.ui.notify(`Usage dashboard: ${error.message}`, "error");
    }
  }

  async function update(action, ctx, quiet = false) {
    const command = [updater, action];
    const profile = process.env.OMP_PROFILE ?? process.env.PI_PROFILE;
    if (profile !== undefined) command.push("--profile", profile);
    try {
      if (action === "install") ctx.ui.notify("Updating the dashboard through OMP's native plugin manager…", "info");
      const result = await pi.exec("python3", command, {
        timeout: action === "install" ? 180000 : 15000,
        cwd: ctx.cwd,
      });
      if (result.code !== 0) {
        if (!quiet) ctx.ui.notify(result.stderr.trim() || "Dashboard update failed.", "error");
        return;
      }
      const report = JSON.parse(result.stdout);
      if (report.updated) {
        ctx.ui.notify(report.message || `Dashboard updated to ${report.currentVersion}. Restart OMP to load it.`, "info");
      } else if (report.updateAvailable) {
        ctx.ui.notify("Usage dashboard update available. Run `/usage-dashboard update install` to update it.", "warning");
      } else if (!quiet) {
        ctx.ui.notify(`Usage dashboard ${report.currentVersion}: ${report.message || "No newer release available."}`, "info");
      }
    } catch (error) {
      if (!quiet) ctx.ui.notify(`Usage dashboard update: ${error.message}`, "error");
    }
  }

  pi.on("session_start", async (_event, ctx) => {
    await record(ctx, "start");
    if (ctx.hasUI && !timer) timer = ctx.setInterval(() => record(ctx), 1000);
    if (ctx.hasUI && process.env.TMUX && process.env.TMUX_PANE && process.env.OMP_USAGE_LAUNCHER !== "1") {
      await control(["init"], ctx, true);
    }
    if (ctx.hasUI) ctx.setTimeout(() => update("check", ctx, true), 0);
  });
  pi.on("session_shutdown", async (_event, ctx) => {
    if (timer) ctx.clearTimer(timer);
    timer = undefined;
    await record(ctx, "stop");
    if (ctx.hasUI && process.env.TMUX && process.env.TMUX_PANE) {
      await control(["detach"], ctx, true);
    }
  });
  for (const event of ["message_end", "agent_end", "session_compact", "session_tree"]) {
    pi.on(event, async (_event, ctx) => { await record(ctx); });
  }
  for (const event of ["session_before_switch", "session_before_branch"]) {
    pi.on(event, async (_event, ctx) => { await record(ctx); });
  }
  for (const event of ["session_switch", "session_branch"]) {
    pi.on(event, async (_event, ctx) => { await record(ctx, "start"); });
  }
  pi.registerCommand("usage-dashboard", {
    description: "Manage usage dashboard: view, position, providers, theme, window, and updates",
    handler: async (args, ctx) => {
      if (!ctx.hasUI) return;
      const words = args.trim().split(/\s+/).filter(Boolean);
      while (words.length < 2) {
        if (words.length === 0) {
          const section = await ctx.ui.select("Usage dashboard", Object.keys(sections));
          if (!section) return;
          words.push(section);
        }
        if (!Object.hasOwn(sections, words[0])) {
          ctx.ui.notify(help, "info");
          return;
        }
        const action = await ctx.ui.select(`Usage dashboard / ${words[0]}`, [...sections[words[0]], "Back"]);
        if (!action) return;
        if (action === "Back") {
          words.length = 0;
          continue;
        }
        words.push(action);
      }
      const [section, action] = words;
      if (!Object.hasOwn(sections, section) || !sections[section].includes(action)) {
        ctx.ui.notify(help, "info");
        return;
      }
      if (section === "update") {
        if (words.length !== 2) {
          ctx.ui.notify("Usage: /usage-dashboard update check|install", "info");
          return;
        }
        await update(action, ctx);
        return;
      }
      if (section === "theme" && action === "color" && words.length === 2) {
        const value = await ctx.ui.input("Design token and color (for example: accent #58a66a)", "accent #58a66a");
        if (!value?.trim()) return;
        const custom = value.trim().split(/\s+/);
        if (custom.length !== 2) {
          ctx.ui.notify("Usage: /usage-dashboard theme color TOKEN COLOR", "info");
          return;
        }
        words.push(...custom);
      }
      const windowFilter = section === "window" && ["hide", "show"].includes(action);
      if ((section === "providers" || windowFilter) && words.length === 2) {
        const providers = [...new Set([...popular, ...ctx.models.list().map(model => model.provider)])];
        const provider = await ctx.ui.select("Provider (availability depends on OMP login and usage support)", providers);
        if (!provider) return;
        words.push(provider);
      }
      if (windowFilter) {
        if (words.length === 3) {
          const filter = await ctx.ui.input("Usage-window label/ID substring", "spark");
          if (!filter?.trim()) return;
          words.push(filter.trim());
        } else if (words.length > 4) {
          words.splice(3, words.length - 3, words.slice(3).join(" "));
        }
      }
      if (section === "window" && action === "interval" && words.length === 2) {
        const interval = await ctx.ui.input("Polling interval in seconds (minimum 15)", "60");
        if (!interval) return;
        words.push(interval);
      }
      await control(words, ctx);
    },
  });
}
