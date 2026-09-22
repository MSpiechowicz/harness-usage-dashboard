import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";

const dashboard = fileURLToPath(new URL("./dashboard.py", import.meta.url));
const updater = fileURLToPath(new URL("./updater.py", import.meta.url));
const sessionStore = fileURLToPath(new URL("./session_usage.py", import.meta.url));
const thinkingLevels = new Set(["off", "minimal", "low", "medium", "high", "xhigh", "max"]);

// Only normalized counters cross this pipe: never prompts, tool output, or credentials.
function saveSession(payload, profile, cwd, environment) {
  return new Promise((resolve, reject) => {
    const args = [sessionStore];
    if (profile !== undefined) args.push("--profile", profile);
    const child = spawn("python3", args, { cwd, env: { ...process.env, ...environment },
      stdio: ["pipe", "ignore", "ignore"] });
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
  commands: ["hide", "show"],
  previous: ["hide", "show"],
  "history-other": ["hide", "show"],
  "history-total": ["hide", "show"],
  position: ["left", "right"],
  providers: ["add", "remove", "hide", "show"],
  theme: ["green", "blue", "brown", "yellow", "cyan", "magenta", "orange", "red", "custom", "reset"],
  window: ["on", "off", "focus", "refresh", "interval", "hide", "show"],
  update: ["check", "install"],
};
const themeOptions = [
  { value: "green", label: "green   (classic green accent)" },
  { value: "blue", label: "blue    (cool blue accent)" },
  { value: "brown", label: "brown   (warm brown accent)" },
  { value: "yellow", label: "yellow  (bright yellow accent)" },
  { value: "cyan", label: "cyan    (clear cyan accent)" },
  { value: "magenta", label: "magenta (bold magenta accent)" },
  { value: "orange", label: "orange  (warm orange accent)" },
  { value: "red", label: "red     (strong red accent)" },
  { value: "custom", label: "custom  (override individual colors)" },
  { value: "reset", label: "reset   (restore green and remove custom colors)" },
];
const help = "Sections: view (compact, details; list remains available as a command for showing settings); commands (hide, show); previous (hide, show); history-other (hide, show); history-total (hide, show); position (left, right); providers (add, remove, hide, show PROVIDER); theme (green, blue, brown, yellow, cyan, magenta, orange, red, custom TOKEN COLOR, reset); window (on, off, focus, refresh, interval, hide/show PROVIDER FILTER); update (check, install).";
const menuSections = {...sections, view: ["compact", "details"]};
const visibilitySections = ["commands", "previous", "history-other", "history-total"];
const visibilityLabels = {
  "history-other": "History Other Sessions",
  "history-total": "History Total",
};
const rootMenuSections = [
  ...Object.keys(menuSections).filter(section => !visibilitySections.includes(section)),
  { value: "visibility", label: "Section Visibility" },
];
const MENU_BACK = Symbol("menu-back");
const MENU_HELP = "up/down navigate  enter select  ← back  esc cancel";

function capitalizeLabel(label) {
  return String(label).replace(/^(\s*)([a-z])/, (_match, whitespace, letter) =>
    `${whitespace}${letter.toUpperCase()}`);
}

function notify(ctx, message, level = "info") {
  const theme = ctx.ui.theme;
  const color = level === "info" ? "accent" : level;
  const label = level === "info" ? "Usage Dashboard" : `Usage Dashboard · ${capitalizeLabel(level)}`;
  const heading = theme.bold(theme.fg(color, label));
  const body = message.split("\n").map(line => theme.fg("text", line)).join("\n");
  ctx.ui.notify(`${heading}\n${body}`, level);
}

async function menuSelect(ctx, title, options, { nested = false } = {}) {
  const choices = options.map(option => typeof option === "string"
    ? { value: option, label: option }
    : option).sort((a, b) => a.label.localeCompare(b.label));
  const labels = choices.map(choice => capitalizeLabel(choice.label));
  let wentBack = false;
  const dialogOptions = nested
    ? {
        helpText: MENU_HELP,
        onLeft() {
          wentBack = true;
        },
      }
    : undefined;
  const selected = await ctx.ui.select(title, labels, dialogOptions);
  if (wentBack) return MENU_BACK;
  return choices.find((choice, index) => labels[index] === selected)?.value ?? selected;
}

async function visibilityMenu(ctx) {
  while (true) {
    const section = await menuSelect(ctx, "Usage Dashboard / Section Visibility",
      visibilitySections.map(value => ({ value, label: visibilityLabels[value] ?? value })), { nested: true });
    if (!section || section === MENU_BACK) return section;
    const action = await menuSelect(ctx,
      `Usage Dashboard / Section Visibility / ${visibilityLabels[section] ?? capitalizeLabel(section)}`,
      sections[section], { nested: true });
    if (!action) return;
    if (action === MENU_BACK) continue;
    return [section, action];
  }
}

export default function usageDashboard(pi) {
  pi.setLabel("Usage Dashboard");
  let recording;
  let pending = Promise.resolve();
  const writes = new Set();
  const children = new Map();
  let writing = false;
  let timer;
  let saveFailed = false;
  let origin;

  function taskGroup(session, toolCallId) {
    return typeof toolCallId === "string" && toolCallId.trim()
      ? createHash("sha256").update(JSON.stringify([session, toolCallId])).digest("hex")
      : undefined;
  }

  function flushWrites() {
    if (writing || !writes.size) return pending;
    writing = true;
    pending = pending.then(async () => {
      try {
        for (const write of writes) {
          const { state, payload } = write;
          // A failed start may be retried after its owner has switched or stopped.
          if (!state.active && payload.action === "start") payload.action = "sync";
          try {
            await saveSession(payload, state.profile, state.cwd, state.environment);
          } catch {
            if (!saveFailed) notify(state.ctx, "Dashboard session history could not be saved; recording will retry.", "warning");
            saveFailed = true;
            break;
          }
          writes.delete(write);
          write.saved?.();
          saveFailed = false;
        }
      } finally {
        writing = false;
      }
    });
    return pending;
  }

  function enqueue(state, entries, action = "sync", saved) {
    writes.add({ state, payload: { session: state.session, activation: state.activation,
      owner: state.owner, action, entries }, saved });
    return flushWrites();
  }

  function sealChild(child) {
    if (!child?.pending) return;
    const entry = child.pending;
    child.pending = undefined;
    if (child.taskGroup) entry.taskGroup = child.taskGroup;
    enqueue(child.origin, [entry]);
  }

  function childThinking(child, entry) {
    return child.modelIdentity === `${entry.provider}/${entry.model}` ? child.thinkingLevel : "unknown";
  }

  pi.events?.on?.("task:subagent:lifecycle", payload => {
    if (typeof payload?.id !== "string" || !payload.id) return;
    let child = children.get(payload.id);
    sealChild(child);
    if (payload.status === "started") {
      if (!child) {
        child = {};
        children.set(payload.id, child);
      }
      const group = origin ? taskGroup(origin.session, payload.parentToolCallId) : undefined;
      // A name may be reused by a new task in a later session. A different
      // spawning call is new ownership, not a late event from the old child.
      if (group && child.taskGroup && payload.parentToolCallId !== child.toolCallId) {
        child.origin = origin;
        child.taskGroup = group;
      }
      if (group) child.toolCallId = payload.parentToolCallId;
      if (!child.origin && origin) {
        child.origin = origin;
        child.started = true;
      }
      if (child.started && !child.taskGroup) {
        child.taskGroup = taskGroup(child.origin.session, payload.parentToolCallId);
      }
    } else if (child) {
      child.modelIdentity = undefined;
      child.thinkingLevel = "unknown";
    }
  });

  pi.events?.on?.("task:subagent:progress", payload => {
    const progress = payload?.progress;
    if (typeof progress?.id !== "string" || !progress.id) return;
    let child = children.get(progress.id);
    if (!child) {
      child = {};
      children.set(progress.id, child);
    }
    child.modelIdentity = typeof progress.resolvedModelIdentity === "string" ? progress.resolvedModelIdentity : undefined;
    child.thinkingLevel = thinkingLevels.has(progress.resolvedThinkingLevel) ? progress.resolvedThinkingLevel : "unknown";
    if (child.started && !child.taskGroup) {
      child.taskGroup = taskGroup(child.origin.session, payload.parentToolCallId);
    }
    if (child.pending) child.pending.thinkingLevel = childThinking(child, child.pending);
  });

  pi.events?.on?.("task:subagent:event", payload => {
    const id = payload?.id;
    if (typeof id !== "string" || !id) return;
    let child = children.get(id);
    // Seal before another raw event can change this request's serving metadata.
    sealChild(child);
    const event = payload.event;
    if (event?.type !== "message_end" || event.message?.role !== "assistant") return;
    const message = event.message;
    const usage = message.usage;
    if (!usage || (!child?.origin && !origin)) return;
    const counts = [usage.input, usage.output, usage.cacheRead, usage.cacheWrite];
    const total = usage.totalTokens ?? counts.reduce((sum, value) => sum + value, 0);
    const at = typeof message.timestamp === "number" ? message.timestamp : Date.parse(message.timestamp);
    if (!Number.isFinite(at) || !counts.every(value => Number.isSafeInteger(value) && value >= 0)
      || !Number.isSafeInteger(total) || total < 0) return;
    if (!child) {
      child = {};
      children.set(id, child);
    }
    // Older hosts may omit lifecycle association. Capture without guessing a group.
    if (!child.origin) child.origin = origin;
    const provider = typeof message.provider === "string" && message.provider.trim() ? message.provider : "unknown";
    const model = typeof message.model === "string" && message.model.trim() ? message.model : "unknown";
    if (child.modelIdentity !== `${provider}/${model}`) {
      child.modelIdentity = undefined;
      child.thinkingLevel = "unknown";
    }
    const entry = {
      id: createHash("sha256").update(JSON.stringify([child.origin.session, id, message.timestamp, provider, model])).digest("hex"),
      // Native message timestamps mark request start, not when usage becomes known.
      // Keep that timestamp in the stable ID, but chart the completed report now.
      at: Date.now() / 1000, provider, model, taskAggregate: false,
      input: counts[0], output: counts[1], cacheRead: counts[2], cacheWrite: counts[3], total,
    };
    entry.thinkingLevel = childThinking(child, entry);
    child.pending = entry;
    // Native raw events precede their synchronous serving-model progress update.
    queueMicrotask(() => { if (child.pending === entry) sealChild(child); });
  });

  function record(ctx, action = "sync") {
    if (!ctx.hasUI) return pending;
    for (const child of children.values()) sealChild(child);
    if (action === "sync" && writes.size) {
      return flushWrites().then(() => writes.size ? undefined : record(ctx));
    }
    const manager = ctx.sessionManager;
    const session = manager.getSessionId();
    if (action === "start") {
      if (recording) recording.active = false;
      recording = { session, activation: randomUUID(), count: 0, signature: undefined,
        thinkingLevel: "unknown", saved: false, active: true, cwd: ctx.cwd,
        profile: process.env.OMP_PROFILE ?? process.env.PI_PROFILE ?? "default",
        owner: process.env.TMUX_PANE || "standalone",
        environment: { HOME: process.env.HOME, PI_CODING_AGENT_DIR: process.env.PI_CODING_AGENT_DIR,
          TMUX: process.env.TMUX, TMUX_PANE: process.env.TMUX_PANE },
        ctx: { hasUI: true, sessionManager: manager, cwd: ctx.cwd, ui: ctx.ui } };
      origin = recording;
    }
    const state = recording;
    if (!state || state.session !== session) return flushWrites();
    const entries = manager.getEntries();
    const count = entries.length;
    const usage = manager.getUsageStatistics();
    const signature = JSON.stringify([manager.getLeafId(), usage.input, usage.output,
      usage.cacheRead, usage.cacheWrite, usage.totalTokens, count]);
    if (action === "sync" && signature === state.signature) return pending;
    if (!state.saved && state.active && action === "sync") action = "start";
    const header = manager.getHeader();
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
      const thinkingLevel = entry.type === "model_usage"
        ? (thinkingLevels.has(entry.thinkingLevel) ? entry.thinkingLevel : "unknown")
        : message?.role === "assistant" ? state.thinkingLevel : "unknown";
      const group = task ? taskGroup(session, message.toolCallId) : undefined;
      batch.push({ id, at: at / 1000, provider, model, thinkingLevel,
        input: counts[0], output: counts[1], cacheRead: counts[2], cacheWrite: counts[3], total,
        ...(group ? { taskGroup: group, taskAggregate: true } : {}) });
    }
    return enqueue(state, batch, action, () => {
      state.count = Math.max(state.count, count);
      state.signature = signature;
      state.saved = true;
    });
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
        notify(ctx, result.code === 0 ? result.stdout.trim() : (result.stderr.trim() || "Could not update usage dashboard"),
          result.code === 0 ? "info" : "error");
      }
    } catch (error) {
      notify(ctx, `Usage Dashboard: ${error.message}`, "error");
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
        ctx.ui.notify(`Usage Dashboard update available: ${report.currentVersion} → ${report.latestVersion}. Run \`/usage-dashboard update install\` to update it.`, "warning");
      } else if (!quiet) {
        ctx.ui.notify(`Usage Dashboard ${report.currentVersion}: ${report.message || "No newer release available."}`, "info");
      }
    } catch (error) {
      if (!quiet) ctx.ui.notify(`Usage Dashboard update: ${error.message}`, "error");
    }
  }

  pi.on("session_start", async (_event, ctx) => {
    await record(ctx, "start");
    if (ctx.hasUI && !timer) timer = ctx.setInterval(() => origin ? record(origin.ctx) : flushWrites(), 1000);
    if (ctx.hasUI && process.env.TMUX && process.env.TMUX_PANE && process.env.OMP_USAGE_LAUNCHER !== "1") {
      await control(["init"], ctx, true);
    }
    if (ctx.hasUI) ctx.setTimeout(() => update("check", ctx, true), 0);
  });
  pi.on("session_shutdown", async (_event, ctx) => {
    if (!ctx.hasUI) return;
    if (recording) recording.active = false;
    origin = undefined;
    if (timer) ctx.clearTimer(timer);
    timer = undefined;
    // Detach before the final usage write so a slow/forced shutdown cannot
    // leave the curses pane repainting into the terminal.
    if (ctx.hasUI && process.env.TMUX && process.env.TMUX_PANE) {
      await control(["detach"], ctx, true);
    }
    await record(ctx, "stop");
    // Retry a transient failure once more before the interval is gone.
    await flushWrites();
  });
  for (const event of ["message_end", "agent_end", "session_compact", "session_tree"]) {
    pi.on(event, async (_event, ctx) => { await record(ctx); });
  }
  for (const event of ["session_before_switch", "session_before_branch"]) {
    pi.on(event, async (_event, ctx) => {
      if (!ctx.hasUI) return;
      // A before-switch hook can be cancelled; keep the active origin until
      // the corresponding switch/branch event actually starts a new recording.
      await record(ctx);
    });
  }
  for (const event of ["session_switch", "session_branch"]) {
    pi.on(event, async (_event, ctx) => { await record(ctx, "start"); });
  }
  pi.registerCommand("usage-dashboard", {
    description: "Manage usage dashboard: view, commands, previous, history-other, history-total, position, providers, theme, window, and updates",
    handler: async (args, ctx) => {
      if (!ctx.hasUI) return;
      const words = args.trim().split(/\s+/).filter(Boolean);
      while (true) {
        while (words.length < 2) {
          if (words.length === 0) {
            const section = await menuSelect(ctx, "Usage Dashboard", rootMenuSections);
            if (!section || section === MENU_BACK) return;
            if (section === "visibility") {
              const selected = await visibilityMenu(ctx);
              if (!selected) return;
              if (selected === MENU_BACK) continue;
              words.push(...selected);
              break;
            }
            words.push(section);
          }
          if (!Object.hasOwn(sections, words[0])) {
            notify(ctx, help);
            return;
          }
          const options = words[0] === "theme" ? themeOptions : menuSections[words[0]];
          const action = await menuSelect(ctx, `Usage Dashboard / ${capitalizeLabel(words[0])}`, options, { nested: true });
          if (!action) return;
          if (action === MENU_BACK) {
            words.length = 0;
            continue;
          }
          words.push(action);
        }
        const [section, action] = words;
        if (!Object.hasOwn(sections, section) || !sections[section].includes(action)) {
          notify(ctx, help);
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
        if (section === "theme" && action === "custom" && words.length === 2) {
          notify(ctx, "Custom changes one token on the selected palette. Use one of: text, muted, secondary, accent, chart, good, warn, error. Colors can be terminal names, gray, brown, orange, or #RRGGBB. Example: accent #58a66a. Repeat for more tokens; reset restores the green palette.");
          const value = await ctx.ui.input("Custom theme: TOKEN COLOR (for example: accent #58a66a)", "accent #58a66a");
          if (!value?.trim()) return;
          const custom = value.trim().split(/\s+/);
          if (custom.length !== 2) {
            notify(ctx, "Usage: /usage-dashboard theme custom TOKEN COLOR");
            return;
          }
          words.push(...custom);
        }
        const windowFilter = section === "window" && ["hide", "show"].includes(action);
        if ((section === "providers" || windowFilter) && words.length === 2) {
          const providers = [...new Set([...popular, ...ctx.models.list().map(model => model.provider)])];
          const provider = await menuSelect(ctx, "Provider (availability depends on OMP login and usage support)", providers, { nested: true });
          if (!provider) return;
          if (provider === MENU_BACK) {
            words.pop();
            continue;
          }
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
        return;
      }
    },
  });
}
