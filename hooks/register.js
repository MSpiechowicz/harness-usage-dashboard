import { HELP, PANE, renderDashboard, renderSettings, snapshotText } from "./claude-view.js";

const INSTANCE = { plugin: "harness-usage-dashboard", key: "instance" };
const SNAPSHOT = { plugin: "harness-usage-dashboard", key: "snapshot" };
const LEVELS = ["low", "medium", "high", "xhigh", "max"];
const label = value => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:/ +()-]{0,199}$/.test(value);
const count = value => Number.isSafeInteger(value) && value >= 0;

// Only these scalar fields cross the helper boundary. Never spread a host event.
export function normalizedUsage(usage, model, thinkingLevel) {
  if (!usage || !label(model)) return null;
  const values = [usage.input_tokens, usage.output_tokens,
    usage.cache_read_input_tokens, usage.cache_creation_input_tokens];
  if (!values.every(count)) return null;
  const total = values.reduce((sum, value) => sum + value, 0);
  if (!count(total)) return null;
  return { provider: "anthropic", model, input: values[0], output: values[1],
    cacheRead: values[2], cacheWrite: values[3], total,
    ...(LEVELS.includes(thinkingLevel) ? { thinkingLevel } : {}) };
}

// Known windows read as compact limits: five_hour -> "5h limit", seven_day_opus -> "7d opus limit".
export function windowLabel(kind) {
  const short = kind.replace(/^five_hour/, "5h").replace(/^seven_day/, "7d");
  return short === kind ? kind.replace(/_/g, " ") : `${short.replace(/_/g, " ")} limit`;
}

export function normalizedAllowance(limits, context, observedAt) {
  if (!Array.isArray(limits)) return null;
  const windows = [];
  for (const limit of limits) {
    if (!label(limit?.kind) || typeof limit.percentUsed !== "number"
      || !Number.isFinite(limit.percentUsed) || limit.percentUsed < 0) continue;
    let resetsAt = null;
    if (limit.resetsAt !== undefined) {
      if (typeof limit.resetsAt !== "string") continue;
      resetsAt = Date.parse(limit.resetsAt) / 1000;
      if (!Number.isFinite(resetsAt) || resetsAt <= observedAt) continue;
    }
    windows.push({ id: limit.kind, label: windowLabel(limit.kind),
      usedFraction: limit.percentUsed / 100, resetsAt });
  }
  return windows.length ? { session: context.session, activation: context.activation,
    observedAt, windows } : null;
}

async function digest(identity) {
  const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(JSON.stringify(identity)));
  return Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, "0")).join("");
}

function validPreference(words) {
  const [section, action, target, filter] = words;
  if (section === "view") return words.length === 2 && ["compact", "details", "list"].includes(action);
  if (section === "chart") return words.length === 2 && ["bars", "dots", "trace"].includes(action);
  if (section === "theme") {
    if (words.length === 2) return ["green", "blue", "brown", "yellow", "cyan", "magenta", "orange", "red", "claude", "reset"].includes(action);
    return words.length === 4 && action === "custom"
      && ["text", "muted", "secondary", "accent", "chart", "good", "warn", "error"].includes(target)
      && /^(?:#[0-9a-fA-F]{6}|default|black|red|green|yellow|blue|magenta|cyan|white|gray|brown|orange)$/.test(filter);
  }
  if (["commands", "token-rate", "current", "previous", "history-other", "history-total"].includes(section)) return words.length === 2 && ["hide", "show"].includes(action);
  if (section === "providers") return words.length === 3 && ["add", "remove", "hide", "show"].includes(action) && label(target);
  if (section === "window") {
    if (words.length === 2) return ["on", "off", "focus", "refresh"].includes(action);
    return words.length === 4 && ["hide", "show"].includes(action) && label(target) && label(filter);
  }
  return false;
}

const MENU_STALE = "Usage Dashboard: the session changed or the command ended; no settings changed. Run /usage-dashboard again.";
const SETTINGS = "usage-dashboard-settings";

async function menuContext($, state, context, signal) {
  if (signal?.aborted || state.active !== context) return false;
  try {
    const [session, cwd] = await Promise.all([$.session.id(), $.session.cwd()]);
    return !signal?.aborted && state.active === context && session === context.session && cwd === context.cwd;
  } catch {
    return false;
  }
}

// A pick goes through the same validation and storage as a typed command.
async function choose($, state, words) {
  const context = state.active;
  if (!context || !validPreference(words)) return;
  try {
    await serial(state, async () => {
      if (state.active !== context) return;
      await helper($, { op: "preferences", words });
      await refresh($, state, context);
    });
    if (words[0] === "window" && words[1] === "off") await $.ui.close({ id: PANE });
    if (words[0] === "window" && words[1] === "on") await $.ui.open({ id: PANE, title: "Usage Dashboard" });
  } catch (error) {
    $.ui.toast(`Usage Dashboard: ${error?.message ?? "setting not saved"}`);
  }
}

async function openSettings($, state, context) {
  let surfaces;
  try { surfaces = await $.session.surfaces(); }
  catch { return commandReply($, state, HELP, false, context); }
  if (!surfaces.length) {
    await serial(state, () => refresh($, state, context));
    return `${snapshotText(state.snapshot)}\nSettings need an interactive surface. ${HELP}`;
  }
  await serial(state, () => refresh($, state, context));
  const opened = await $.ui.open({ id: SETTINGS, title: "Dashboard Settings", focus: true, closeOnEscape: true });
  return commandReply($, state, opened.isPlaced
    ? "Usage Dashboard: settings open. Arrows move, Enter picks, Esc closes."
    : "Usage Dashboard: this surface cannot place the settings pane yet; use a direct command.\n" + HELP, false, context);
}

// Functions receiving $ must be top-level: the runtime inventories literal APIs.
function serial(state, operation) {
  const task = state.pending.then(operation);
  state.pending = task.catch(() => {});
  return task;
}

async function helper($, payload) {
  const ran = await $.process.run(["python3", `${$.plugin.root}/claude_native.py`], {
    stdin: JSON.stringify({ version: 1, ...payload }), timeoutMs: 3000,
  });
  let response;
  try { response = JSON.parse(ran.stdout); } catch { throw new Error("Local dashboard helper returned no valid response."); }
  if (ran.isStdoutTruncated || response?.version !== 1 || response.ok !== true || ran.exitCode !== 0) {
    const error = new Error("Local dashboard storage is unavailable.");
    if (!ran.isStdoutTruncated && response?.version === 1 && response.ok === false
      && response.error?.code === "unsupported_command") {
      error.code = "unsupported_command";
    }
    if (response?.error?.code === "migration_required") {
      error.message = "Stop old Claude sessions and explicitly migrate the owned legacy installation before native capture.";
      error.code = "migration_required";
    }
    throw error;
  }
  return response;
}

async function publish($, state, context, nextSnapshot) {
  if (state.active !== context) return;
  const failure = state.failure || state.readFailure;
  state.snapshot = { ...nextSnapshot, ...(failure ? { failure } : { failure: "" }) };
  await $.state.set(SNAPSHOT, state.snapshot);
}

async function failed($, state, context, error, transient = false) {
  if (error?.code === "migration_required") state.blocked = true;
  const message = error?.code === "migration_required" ? error.message
    : "Local dashboard helper failed; capture or history may be incomplete. Refresh to retry the local snapshot.";
  if (transient) state.readFailure = message;
  else state.failure = message;
  if (!transient && state.instance) {
    state.instance = { ...state.instance, failure: state.failure, blocked: state.blocked };
    await $.state.set(INSTANCE, state.instance).catch(() => {});
  }
  if (state.active === context) await publish($, state, context,
    state.snapshot ?? { rows: [], tokens: {}, preferences: {} }).catch(() => {});
}

async function capture($, state, context, action, entries = [], incomplete = false) {
  if (!context || state.blocked) return;
  try {
    await helper($, { op: "capture", cwd: context.cwd, owner: context.owner,
      session: context.session, activation: context.activation, action, entries, incomplete });
  } catch (error) { await failed($, state, context, error); }
}

function presentationDeadline(state, now) {
  // Chart buckets roll on wall-clock minutes. Short reset labels show seconds.
  let deadline = (Math.floor(now / 60000) + 1) * 60000;
  const allowance = state.allowance;
  if (!allowance) return deadline;
  const expiresAt = allowance.observedAt * 1000 + 300001;
  if (expiresAt <= now) return deadline;
  deadline = Math.min(deadline, expiresAt);
  for (const window of allowance.windows) {
    const resetsAt = window.resetsAt === null ? null : window.resetsAt * 1000;
    if (resetsAt === null || resetsAt <= now) continue;
    deadline = Math.min(deadline, resetsAt);
    if (resetsAt - now < 3600000) deadline = Math.min(deadline, now + 1000);
  }
  return deadline;
}

async function presentationTick($, state) {
  const context = state.active;
  if (!context || state.presentationPending) return;
  // Include visibility reads and queue wait in the guard: a slow helper must
  // never accumulate one queued refresh per timer tick.
  state.presentationPending = true;
  try {
    await serial(state, async () => {
      if (state.active !== context) return;
      const surfaces = await $.session.surfaces();
      const panes = surfaces.length ? await $.ui.panes() : [];
      const shown = panes.some(pane => pane.id === PANE && pane.isShown && pane.isPlaced);
      const wasShown = state.wasShown;
      state.wasShown = shown;
      if (!shown) return;
      const now = await $.clock.now();
      if (wasShown && state.width === state.cachedWidth && now < state.refreshAt) return;
      await refresh($, state, context);
    });
  } catch (error) {
    await failed($, state, context, error, true);
  } finally {
    state.presentationPending = false;
  }
}

function startPresentationTimer($, state) {
  state.presentationTimer?.cancel();
  state.wasShown = false;
  state.presentationTimer = $.clock.every(500, () => {
    void presentationTick($, state).catch(() => {});
  });
}

async function refresh($, state, context) {
  if (!context || state.active !== context) return state.snapshot;
  const width = state.width;
  state.cachedWidth = width;
  state.refreshAt = presentationDeadline(state, await $.clock.now());
  try {
    // A new activation or reload has no measurement yet; seed it from the last response.
    if (!state.allowance) await observeLimits($, state, context, (await $.session.usage()).rateLimits);
  } catch { /* Allowance stays unknown; history still refreshes. */ }
  try {
    const nextSnapshot = await helper($, { op: "snapshot", cwd: context.cwd,
      owner: context.owner, session: context.session, activation: context.activation,
      width, allowance: state.allowance });
    state.readFailure = "";
    await publish($, state, context, nextSnapshot);
  } catch (error) { await failed($, state, context, error, true); }
  return state.snapshot;
}

async function ensure($, state, session, cwd) {
  if (!state.instance) {
    const held = await $.state.get(INSTANCE);
    state.instance = held.value ?? { owner: crypto.randomUUID(), context: null };
    state.failure = state.instance.failure ?? "";
    state.blocked = state.instance.blocked === true;
    if (!held.value) await $.state.set(INSTANCE, state.instance);
  }
  if (!state.active && state.instance.context?.session === session) state.active = state.instance.context;
  if (state.active?.session === session) {
    if (!state.presentationTimer) startPresentationTimer($, state);
    return state.active;
  }
  state.presentationTimer?.cancel();
  state.presentationTimer = null;
  const previous = state.active ?? state.instance.context;
  if (previous) await capture($, state, previous, "stop");
  state.active = { owner: state.instance.owner, session, cwd, activation: crypto.randomUUID() };
  state.instance = { ...state.instance, context: state.active };
  state.allowance = null;
  state.snapshot = null;
  await $.state.set(INSTANCE, state.instance);
  await $.state.set(SNAPSHOT, { rows: [], tokens: {}, preferences: {}, session, activation: state.active.activation });
  await capture($, state, state.active, "start");
  startPresentationTimer($, state);
  return state.active;
}

// The op's dispatch snapshot pins id/cwd before the request starts beneath us.
async function origin($, state) {
  const [session, cwd] = await Promise.all([$.session.id(), $.session.cwd()]);
  return serial(state, () => ensure($, state, session, cwd));
}

async function observeLimits($, state, context, limits) {
  if (state.active !== context) return;
  state.allowance = normalizedAllowance(limits, context, await $.clock.now() / 1000);
}

async function record($, state, context, identity, usage, model, effort) {
  if (!context) return;
  const entry = normalizedUsage(usage, model, effort);
  if (entry) {
    entry.id = await digest(identity);
    entry.at = await $.clock.now() / 1000;
  }
  await serial(state, async () => {
    await capture($, state, context, "record", entry ? [entry] : [], !entry);
    await refresh($, state, context);
  });
}

async function commandReply($, state, text, allowSnapshot = true, context) {
  let headless = false;
  let presentationFailure = "";
  try { headless = !(await $.session.surfaces()).length; }
  catch {
    presentationFailure = "Usage Dashboard: presentation unavailable; could not determine session surfaces.";
  }
  // An aborted command can still report its own diagnostics, but a replaced
  // session must not expose either the obsolete snapshot or its successor.
  const current = !context || await menuContext($, state, context);
  if (!current) return [MENU_STALE, presentationFailure].filter(Boolean).join("\n");
  const snapshot = state.snapshot;
  if (allowSnapshot && headless) {
    return [text, snapshotText(snapshot)].filter(Boolean).join("\n");
  }
  const diagnostics = [
    presentationFailure,
    snapshot?.failure ? `Usage Dashboard: ${snapshot.failure}` : "",
    snapshot?.capture && snapshot.capture.state !== "available"
      ? `Token capture ${snapshot.capture.state}: ${snapshot.capture.reason}` : "",
  ];
  return [text, ...diagnostics].filter(Boolean).join("\n");
}

async function control($, state, words, context, signal) {
  try {
    if (signal?.aborted) return commandReply($, state, MENU_STALE, false, context);
    if (words[0] === "help") return commandReply($, state, HELP, false, context);
    if (!validPreference(words)) {
      return commandReply($, state, HELP, false, context);
    }
    if (!context) context = await origin($, state);
    return await serial(state, async () => {
      if (state.active !== context || signal?.aborted) return commandReply($, state, MENU_STALE, false, context);
      if (signal && !await menuContext($, state, context, signal)) return commandReply($, state, MENU_STALE, false, context);
      const action = words[0] === "window" ? words[1] : null;
      let text = "";
      if (!["refresh", "focus"].includes(action)) {
        const changed = await helper($, { op: "preferences", words });
        text = changed.text ?? "";
      }
      await refresh($, state, context);
      if (action === "off") {
        await $.ui.close({ id: PANE });
        return commandReply($, state, "Usage Dashboard: window off.", false, context);
      }
      if (["on", "focus"].includes(action) && (await $.session.surfaces()).length) {
        const opened = await $.ui.open({ id: PANE, title: "Usage Dashboard", focus: true, closeOnEscape: true });
        if (!opened.isPlaced) return `${snapshotText(state.snapshot)}\nThe dashboard is open but this surface cannot place it yet.`;
      }
      if (!text) {
        text = action === "focus"
          ? "Usage Dashboard: window focus requested."
          : "Usage Dashboard: refreshed.";
      }
      return commandReply($, state, text, true, context);
    });
  } catch (error) {
    if (error?.code === "unsupported_command") {
      return commandReply($, state, HELP, false, context);
    }
    await failed($, state, context, error, true);
    return commandReply($, state, HELP, words[0] !== "window" || words[1] !== "off", context);
  }
}

/** @type {import('claude-code').Register} */
export const register = on => {
  const state = { pending: Promise.resolve(), active: null, instance: null, snapshot: null,
    failure: "", readFailure: "", blocked: false, allowance: null, width: 64, cachedWidth: 64,
    refreshAt: 0, presentationPending: false, wasShown: false, presentationTimer: null };

  on("session.start", async ($, e, next) => {
    try {
      const context = await origin($, state);
      await $.command.register({ name: "usage-dashboard", description: "Local token history, limits, charts and dashboard settings", immediate: true });
      await serial(state, () => refresh($, state, context));
      if (e.surface && state.snapshot?.preferences?.enabled !== false) await $.ui.open({ id: PANE, title: "Usage Dashboard" });
      startPresentationTimer($, state);
    } catch (error) { await failed($, state, state.active, error); }
    return next(e);
  });

  on("classic.SessionStart", async ($, e, next) => {
    try {
      if (e.source !== "compact") {
        const context = await serial(state, () => ensure($, state, e.session_id, e.cwd));
        await serial(state, () => refresh($, state, context));
      }
    } catch (error) { await failed($, state, state.active, error); }
    return next(e);
  });

  on("session.end", async ($, e, next) => {
    const context = state.active?.session === e.sessionId ? state.active
      : state.instance?.context?.session === e.sessionId ? state.instance.context : null;
    try {
      await serial(state, async () => {
        if (!context) return;
        await capture($, state, context, "stop");
        if (state.active === context) {
          state.active = null;
          state.instance = { ...state.instance, context: null };
          state.allowance = null;
          state.presentationTimer?.cancel();
          state.presentationTimer = null;
          state.wasShown = false;
          await $.state.set(INSTANCE, state.instance);
        }
      });
    } catch (error) { await failed($, state, context, error); }
    return next(e);
  });

  on("turn.step", async function* ($, e, next) {
    let context;
    try { context = await origin($, state); } catch (error) { await failed($, state, state.active, error); }
    let result;
    try { result = yield* next(e); }
    catch (error) {
      try { await record($, state, context, [], null, null); } catch { /* Never replace the request's error. */ }
      throw error;
    }
    try {
      const identity = context && label(e.turnId) && count(e.index) && (e.agentId === undefined || label(e.agentId))
        ? [context.session, e.turnId, e.agentId ?? "main", e.index] : null;
      await record($, state, context, identity, identity ? result?.usage : null, result?.usage?.model, e.effort);
    } catch (error) { await failed($, state, context, error); }
    return result;
  });

  on("session.compact", async ($, e, next) => {
    let context;
    try { context = await origin($, state); } catch (error) { await failed($, state, state.active, error); }
    const identity = context ? [context.session, "compaction", crypto.randomUUID()] : null;
    let result;
    try { result = await next(e); }
    catch (error) {
      try { await record($, state, context, [], null, "unknown"); } catch { /* Preserve the compaction error. */ }
      throw error;
    }
    try {
      // No usage also means a reused summary. Coverage is unknown, not a lost bill.
      if (!result.skip) await record($, state, context, identity, result.usage ?? null, "unknown");
    } catch (error) { await failed($, state, context, error); }
    return result;
  });

  on("session.measure", async ($, e, next) => {
    try {
      const context = await origin($, state);
      await serial(state, async () => {
        // Each measurement follows a response, so its windows are current even when unchanged.
        if (e.rateLimits?.length) await observeLimits($, state, context, e.rateLimits);
        await refresh($, state, context);
      });
    } catch (error) { await failed($, state, state.active, error); }
    return next(e);
  });

  on("command.run", { command: "usage-dashboard" }, async ($, e, next) => {
    const words = e.args.trim().split(/\s+/).filter(Boolean);
    let context;
    try { context = await origin($, state); }
    catch (error) {
      await failed($, state, state.active, error);
      return { text: await commandReply($, state, "", words[0] !== "window" || words[1] !== "off") };
    }
    return { text: words.length
      ? await control($, state, words, context, next.signal)
      : await openSettings($, state, context) };
  });

  on("ui.close", { id: PANE }, async ($, e, next) => {
    const result = await next(e);
    if (e.origin.kind === "person") {
      try {
        const context = state.active;
        await serial(state, () => helper($, { op: "preferences", words: ["window", "off"] }));
        await serial(state, () => refresh($, state, context));
      } catch (error) { await failed($, state, state.active, error, true); }
    }
    return result;
  });

  on("ui.render", { component: "Pane", requestId: SETTINGS }, async ($, e) => {
    const held = await $.state.get(SNAPSHOT);
    return renderSettings($.ui.resolve(e), held.value, words => void choose($, state, words));
  });

  on("ui.render", { component: "Pane", requestId: PANE }, async ($, e) => {
    const held = await $.state.get(SNAPSHOT);
    state.width = Math.max(1, Math.min(300, Math.floor(e.props.bodyColumns) - 2));
    return renderDashboard($.ui.resolve(e), held.value);
  });
};
