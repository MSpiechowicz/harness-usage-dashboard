import type { On, TurnStepChunk, TurnStepResult, UsefulSidebarSnapshot } from "claude-code";
import type { Engine } from "claude-code/testing";
import { expect, mock, test } from "claude-code/testing";
import { normalizedAllowance, normalizedUsage } from "./register.js";

type CaptureEntry = {
  id: string; at: number; provider: "anthropic"; model: string;
  input: number; output: number; cacheRead: number; cacheWrite: number; total: number;
};

type HelperPayload = {
  op: "capture" | "snapshot";
  action?: "start" | "record" | "stop";
  session: string; activation: string; owner: string; cwd: string;
  entries?: CaptureEntry[];
  incomplete?: boolean;
};

function helperPayload(text: string | undefined): HelperPayload {
  if (typeof text !== "string") throw new Error("Missing helper stdin");
  const payload: unknown = JSON.parse(text);
  if (!payload || typeof payload !== "object"
    || !("op" in payload) || !["capture", "snapshot"].includes(String(payload.op))
    || !("session" in payload) || typeof payload.session !== "string"
    || !("activation" in payload) || typeof payload.activation !== "string"
    || !("owner" in payload) || typeof payload.owner !== "string"
    || !("cwd" in payload) || typeof payload.cwd !== "string") throw new Error("Invalid helper context");
  return payload as HelperPayload;
}

function helperSuccess(snapshot: UsefulSidebarSnapshot | undefined = undefined) {
  return { value: { exitCode: 0, stderr: "", isStdoutTruncated: false, isStderrTruncated: false,
    stdout: JSON.stringify({ version: 1, ok: true, ...snapshot }) } };
}

function observeSnapshots(on: On): UsefulSidebarSnapshot[] {
  const snapshots: UsefulSidebarSnapshot[] = [];
  on("state.set", { plugin: "harness-useful-sidebar", key: "snapshot" }, async (_$, event, next) => {
    const result = await next(event);
    if (result.value !== undefined && result.value.isSet) snapshots.push(event.value);
    return result;
  });
  return snapshots;
}

function latestSnapshot(snapshots: UsefulSidebarSnapshot[]): UsefulSidebarSnapshot {
  const snapshot = snapshots.at(-1);
  if (!snapshot) throw new Error("No accepted snapshot write");
  return snapshot;
}

function mountDashboard($: Engine) {
  return $.ui.mount({
    plugin: "harness-useful-sidebar", surface: "terminal", component: "Pane",
    requestId: "useful-sidebar",
    props: {
      title: "Useful Sidebar", isFocused: false, bodyColumns: 64, placement: "inline",
      scroll: { offset: 0, bodyRows: 20 }, view: {},
    },
  });
}

function errorSemantics(error: unknown) {
  if (!(error instanceof Error)) throw new Error("Request did not expose an error");
  const code = "code" in error ? error.code : undefined;
  return { name: error.name, message: error.message, code };
}

async function drain(stream: AsyncGenerator<TurnStepChunk, TurnStepResult>): Promise<TurnStepResult> {
  let step = await stream.next();
  while (step.done !== true) step = await stream.next();
  return step.value;
}

const zero = { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 };

test("reported zero is valid, partial and unsafe counts are unknown", () => {
  expect(normalizedUsage(zero, "claude-model").total).toBe(0);
  expect(normalizedUsage({ ...zero, input_tokens: 7, output_tokens: 2,
    cache_read_input_tokens: 11, cache_creation_input_tokens: 3 }, "claude-model").total).toBe(23);
  for (const value of [undefined, null, true, -1, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
    expect(normalizedUsage({ ...zero, output_tokens: value }, "claude-model")).toBe(null);
  }
  expect(normalizedUsage({ ...zero, input_tokens: Number.MAX_SAFE_INTEGER, output_tokens: 1 }, "claude-model")).toBe(null);
  expect(normalizedUsage(zero, undefined)).toBe(null);
  expect(normalizedUsage(null, "claude-model")).toBe(null);
});

test("allowance rejects missing, invalid and expired reports without inventing zero", () => {
  const context = { session: "session", activation: "activation" };
  const now = 1_700_000_000;
  expect(normalizedAllowance(undefined, context, now)).toBe(null);
  expect(normalizedAllowance([{ kind: "five_hour" }], context, now)).toBe(null);
  expect(normalizedAllowance([{ kind: "five_hour", percentUsed: 42, resetsAt: new Date(now * 1000).toISOString() }], context, now)).toBe(null);
  expect(normalizedAllowance([{ kind: "five_hour", percentUsed: NaN }], context, now)).toBe(null);
  expect(normalizedAllowance([{ kind: "five_hour", percentUsed: 0 }, { kind: "spend_limit", percentUsed: 125 }], context, now)?.windows.map(window => window.usedFraction)).toEqual([0, 1.25]);
  expect(normalizedAllowance(["five_hour", "seven_day", "seven_day_opus", "spend_limit"].map(kind => ({ kind, percentUsed: 0 })), context, now)
    ?.windows.map(window => window.label)).toEqual(["5h limit", "7d limit", "7d opus limit", "spend limit"]);
});

test("late request stays historical across clear and private content never reaches helper", async ($, on) => {
  mock.clock(on, { now: 1_700_000_000_000 });
  const snapshots = observeSnapshots(on);
  let session = "old-session";
  const captured: HelperPayload[] = [];
  const privatePayloads: string[] = [];
  on("session.id", () => ({ value: session }));
  on("session.cwd", () => ({ value: "/native-fixture" }));
  on("classic.SessionStart", () => ({}));
  on("process.run", (_$, event) => {
    const payload = helperPayload(event.init?.stdin);
    privatePayloads.push(event.init!.stdin!);
    captured.push(payload);
    if (payload.op === "capture") return helperSuccess();
    return helperSuccess({ session: payload.session, activation: payload.activation,
      rows: [
        { text: payload.session, token: "text", emphasis: null },
        { text: "Capture unknown; no report for this session.", token: "text", emphasis: null },
      ], preferences: {}, tokens: {},
      capture: { state: "unknown", reason: "No report for this session", lastEventAt: null } });
  });
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const result = { turnId: "turn-one", index: 0, answer: "PRIVATE-ANSWER", toolUses: [],
    stopReason: "end_turn" as const, usage: { ...zero, input_tokens: 3, model: "responding-model", private: "PRIVATE-USAGE" } };
  on("turn.step", async function* () {
    yield { kind: "text" as const, index: 0, text: "PRIVATE-CHUNK" };
    await gate;
    return result;
  });
  const stream = $.turn.step({ turnId: "turn-one", index: 0, model: "requested-model", messageCount: 1 });
  let clearSnapshotIndex = 0;
  try {
    const first = await stream.next();
    expect(first.done).toBe(false);
    expect(first.value).toMatchObject({ kind: "text", text: "PRIVATE-CHUNK" });
    session = "new-session";
    clearSnapshotIndex = snapshots.length;
    await $.classic.SessionStart({ source: "clear", session_id: session, cwd: "/native-fixture", transcript_path: "PRIVATE-TRANSCRIPT" });
    expect(latestSnapshot(snapshots).session).toBe("new-session");
  } finally {
    release();
    await drain(stream);
  }
  const oldStart = captured.find(row => row.action === "start" && row.session === "old-session");
  const oldStop = captured.find(row => row.action === "stop" && row.session === "old-session");
  const newStart = captured.find(row => row.action === "start" && row.session === "new-session");
  const oldReport = captured.find(row => row.action === "record");
  if (!oldStart || !oldStop || !newStart || !oldReport) throw new Error("Lifecycle failed to produce complete request attribution");
  expect(oldStop.activation).toBe(oldStart.activation);
  expect(oldReport.session).toBe("old-session");
  expect(oldReport.activation).toBe(oldStart.activation);
  expect(oldReport.incomplete).toBe(false);
  expect(newStart.activation).not.toBe(oldStart.activation);
  expect(newStart.owner).toBe(oldStart.owner);
  expect(privatePayloads.join("\n")).not.toContain("PRIVATE-");
  const snapshot = latestSnapshot(snapshots);
  expect(snapshot.session).toBe("new-session");
  expect(snapshot.activation).toBe(newStart.activation);
  expect(snapshots.slice(clearSnapshotIndex).every(published =>
    published.session === "new-session" && published.activation === newStart.activation)).toBe(true);
  expect(snapshot.capture?.state).toBe("unknown");
  expect(JSON.stringify(snapshots)).not.toContain("PRIVATE-");
  const ui = await mountDashboard($);
  try {
    expect((await ui.find({ type: "Text", text: "new-session" }))?.text).toBe("new-session");
    expect(await ui.find({ type: "Text", text: /capture unknown/i })).toBeDefined();
    expect(await ui.find({ type: "Text", text: "old-session" })).toBeUndefined();
    expect(JSON.stringify(await ui.drawn())).not.toContain("PRIVATE-");
  } finally {
    await ui.unmount();
  }
});

test("helper failure cannot replace the original request error or imply zero capture", async ($, on) => {
  mock.clock(on, { now: 1_700_000_000_000 });
  const snapshots = observeSnapshots(on);
  on("session.id", () => ({ value: "error-session" }));
  on("session.cwd", () => ({ value: "/native-fixture" }));
  on("classic.SessionStart", () => ({}));
  const knownSnapshot: UsefulSidebarSnapshot = {
    rows: [{ text: "Observed tokens: 17", token: "text", emphasis: null }],
    preferences: {}, tokens: {},
    capture: { state: "available", reason: "Observed report", lastEventAt: 1_699_999_999 },
  };
  let attemptedReport: HelperPayload | undefined;
  const privateHelperError = `PRIVATE-HELPER-ERROR ${"PRIVATE-DETAIL ".repeat(1000)}`;
  on("process.run", (_$, event) => {
    const payload = helperPayload(event.init?.stdin);
    if (payload.action === "record") {
      attemptedReport = payload;
      return { deny: privateHelperError };
    }
    return helperSuccess(payload.op === "snapshot" ? knownSnapshot : undefined);
  });
  let requestError: unknown;
  on("turn.step", async function* (_$, event, next) {
    yield { kind: "text" as const, index: 0, text: "PRIVATE-PARTIAL-ANSWER" };
    try {
      // The test kit skips stubs that throw before calling next. Let the real
      // engine bottom reject, so this exercises the mod's request-error path.
      return yield* next(event);
    } catch (error) {
      requestError = error;
      throw error;
    }
  });
  await $.classic.SessionStart({ source: "startup", session_id: "error-session", cwd: "/native-fixture" });
  const ui = await mountDashboard($);
  try {
    expect(await ui.find({ type: "Text", text: "Observed tokens: 17" })).toBeDefined();
    expect(await ui.find({ type: "Text", text: /capture or history may be incomplete/i })).toBeUndefined();
    const stream = $.turn.step({ turnId: "failed-turn", index: 0, model: "claude-model", messageCount: 1 });
    let received: unknown;
    try { await drain(stream); } catch (error) { received = error; }
    expect(errorSemantics(received)).toEqual(errorSemantics(requestError));
    if (!attemptedReport) throw new Error("Failed request did not report its accounting gap");
    expect(attemptedReport.entries).toEqual([]);
    expect(attemptedReport.incomplete).toBe(true);
    expect(JSON.stringify(snapshots)).not.toContain("PRIVATE-");
    const failure = await ui.find({ type: "Text", text: /capture or history may be incomplete/i });
    expect(failure).toBeDefined();
    expect(failure!.text.length).toBeLessThan(1000);
    expect(await ui.find({ type: "Text", text: "Observed tokens: 17" })).toBeDefined();
    expect(JSON.stringify(await ui.drawn())).not.toContain("PRIVATE-");
  } finally {
    await ui.unmount();
  }
});

function menuFixture(on: On) {
  mock.clock(on, { now: 1_700_000_000_000 });
  const snapshots = observeSnapshots(on);
  const fixture = {
    session: "menu-session", surfaces: ["terminal"], opens: 0, closes: 0,
    placed: true, shown: false, paneOpen: false, enabled: false,
    snapshotFailure: false, preferenceFailure: "", surfacesUnavailable: false,
    captureState: "unknown" as "unknown" | "incomplete", captureReason: "No native report",
    writes: [] as string[][], captured: [] as HelperPayload[], snapshots, openedIds: [] as string[],
  };
  on("session.id", () => ({ value: fixture.session }));
  on("session.cwd", () => ({ value: "/native-fixture" }));
  on("session.surfaces", () => fixture.surfacesUnavailable
    ? { deny: "Session surfaces unavailable" } : { value: fixture.surfaces });
  on("classic.SessionStart", () => ({}));
  on("ui.panes", () => ({ value: fixture.paneOpen ? [{
    id: "useful-sidebar", title: "Useful Sidebar", isPlaced: fixture.placed,
    isShown: fixture.shown, isFocused: false,
  }] : [] }));
  on("ui.open", (_$, event) => {
    fixture.openedIds.push(event.id);
    if (event.id === "useful-sidebar-settings") return { value: { isPlaced: true } };
    fixture.opens += 1;
    fixture.paneOpen = true;
    return { value: { isPlaced: fixture.placed } };
  });
  on("ui.close", () => {
    fixture.closes += 1;
    fixture.paneOpen = false;
    return { value: undefined };
  });
  on("process.run", (_$, event) => {
    const request = JSON.parse(event.init!.stdin!) as { op: string; words?: string[] };
    if (request.op === "preferences") {
      if (fixture.preferenceFailure) {
        return { value: { exitCode: 1, stderr: "", isStdoutTruncated: false, isStderrTruncated: false,
          stdout: JSON.stringify({ version: 1, ok: false, error: { code: fixture.preferenceFailure } }) } };
      }
      fixture.writes.push(request.words!);
      return { value: { exitCode: 0, stderr: "", isStdoutTruncated: false, isStderrTruncated: false,
        stdout: JSON.stringify({ version: 1, ok: true, text: "Useful Sidebar settings saved." }) } };
    }
    const payload = helperPayload(event.init?.stdin);
    fixture.captured.push(payload);
    if (payload.op === "capture") return helperSuccess();
    if (fixture.snapshotFailure) {
      return { value: { exitCode: 1, stderr: "", isStdoutTruncated: false, isStderrTruncated: false,
        stdout: JSON.stringify({ version: 1, ok: false, error: { code: "storage_unavailable" } }) } };
    }
    return helperSuccess({
      session: payload.session, activation: payload.activation,
      rows: [
        { text: "TOKEN RATE --------------------------------", token: "secondary", emphasis: null },
        { text: "0    └─────────────────────────────────────", token: "muted", emphasis: null },
        { text: `Capture ${fixture.captureState}; ${fixture.captureReason}.`, token: "warn", emphasis: null },
      ],
      preferences: { enabled: fixture.enabled, theme: "claude", tokens: { accent: "#d97757" }, commands_visible: false },
      tokens: { muted: "gray", warn: "orange" },
      capture: { state: fixture.captureState, reason: fixture.captureReason, lastEventAt: null },
    });
  });
  return fixture;
}

function mountSettings($: Engine) {
  return $.ui.mount({
    plugin: "harness-useful-sidebar", surface: "terminal", component: "Pane",
    requestId: "useful-sidebar-settings",
    props: {
      title: "Dashboard Settings", isFocused: true, bodyColumns: 64, placement: "inline",
      scroll: { offset: 0, bodyRows: 30 }, view: {},
    },
  });
}

test("bare command opens the settings pane without enabling or changing the dashboard", async ($, on) => {
  const fixture = menuFixture(on);
  await $.classic.SessionStart({ source: "startup", session_id: fixture.session, cwd: "/native-fixture", transcript_path: "/unused" });
  const reply = await $.command.run({ command: "useful-sidebar", args: "" });
  expect(reply.text).toMatch(/settings open/i);
  expect(reply.text).toMatch(/capture unknown/i);
  expect(reply.text).not.toMatch(/^TOKEN RATE/m);
  expect(fixture.openedIds).toEqual(["useful-sidebar-settings"]);
  expect(fixture.writes).toEqual([]);
  expect(latestSnapshot(fixture.snapshots).preferences.enabled).toBe(false);
  const ui = await mountDashboard($);
  try {
    expect(await ui.find({ type: "Select" })).toBeUndefined();
    expect(await ui.find({ type: "Button" })).toBeUndefined();
  } finally {
    await ui.unmount();
  }
});

test("settings pane lists every dashboard section in order and each pick saves its command", async ($, on) => {
  const fixture = menuFixture(on);
  await $.classic.SessionStart({ source: "startup", session_id: fixture.session, cwd: "/native-fixture", transcript_path: "/unused" });
  await $.command.run({ command: "useful-sidebar", args: "" });
  const ui = await mountSettings($);
  try {
    const selects = await ui.findAll({ type: "Select" });
    expect(selects.map(select => select.key)).toEqual([
      "dashboard", "view", "theme", "token-rate", "chart", "current", "previous",
      "history-other", "history-total", "claude", "five_hour", "seven_day", "commands",
    ]);
    for (const [key, value, words] of [
      ["history-total", "hide", ["history-total", "hide"]],
      ["current", "hide", ["current", "hide"]],
      ["token-rate", "hide", ["token-rate", "hide"]],
      ["chart", "dots", ["chart", "dots"]],
      ["five_hour", "hide", ["window", "hide", "anthropic", "five_hour"]],
      ["claude", "show", ["providers", "show", "anthropic"]],
      ["theme", "blue", ["theme", "blue"]],
    ] as const) {
      await ui.select({ key, value });
      expect(fixture.writes.at(-1)).toEqual([...words]);
    }
    const before = fixture.writes.length;
    await ui.select({ key: "view", value: "compact" });
    expect(fixture.writes.length).toBe(before);
    await ui.select({ key: "dashboard", value: "on" });
    expect(fixture.writes.at(-1)).toEqual(["window", "on"]);
    expect(fixture.openedIds.at(-1)).toBe("useful-sidebar");
  } finally {
    await ui.unmount();
  }
});

test("settings stay unavailable without surfaces and direct help keeps diagnostics", async ($, on) => {
  const fixture = menuFixture(on);
  for (const captureState of ["unknown", "incomplete"] as const) {
    fixture.captureState = captureState;
    fixture.captureReason = captureState === "unknown" ? "No native report" : "One request has no usage report";
    fixture.snapshotFailure = false;
    await $.command.run({ command: "useful-sidebar", args: "window refresh" });
    fixture.snapshotFailure = true;
    fixture.surfacesUnavailable = true;
    const noSurfaces = await $.command.run({ command: "useful-sidebar", args: "" });
    expect(noSurfaces.text).toMatch(/presentation unavailable/i);
    expect(noSurfaces.text).toMatch(new RegExp(`capture ${captureState}`, "i"));
    expect(noSurfaces.text).toContain(fixture.captureReason);
    expect(noSurfaces.text).not.toMatch(/^TOKEN RATE/m);
    fixture.surfacesUnavailable = false;
    fixture.preferenceFailure = "unsupported_command";
    for (const args of ["help", "unknown-command", "providers add unsupported-provider", "theme custom accent window"]) {
      const reply = await $.command.run({ command: "useful-sidebar", args });
      expect(reply.text).toMatch(/Direct commands:/);
      expect(reply.text).toMatch(new RegExp(`capture ${captureState}`, "i"));
      expect(reply.text).toContain(fixture.captureReason);
      expect(reply.text).not.toMatch(/^TOKEN RATE/m);
    }
    fixture.preferenceFailure = "";
  }
  expect(fixture.writes).toEqual([]);
  expect(fixture.openedIds).toEqual([]);
});

test("headless bare command returns usage and direct help without opening or mutating", async ($, on) => {
  const fixture = menuFixture(on);
  fixture.surfaces = [];
  let questions = 0;
  on("tool.call", () => {
    questions += 1;
    return { deny: "No question should be asked" };
  });
  const answer = await $.command.run({ command: "useful-sidebar", args: "" });
  expect(answer.text).toMatch(/^TOKEN RATE/m);
  expect(questions).toBe(0);
  expect(fixture.writes).toEqual([]);
  expect(fixture.opens).toBe(0);
  expect(latestSnapshot(fixture.snapshots).failure).toBe("");
});

test("refresh seeds the allowance from the session's last reported rate limits", async ($, on) => {
  const fixture = menuFixture(on);
  const resetsAt = new Date(1_700_000_000_000 + 3_600_000).toISOString();
  on("session.usage", () => ({ value: { startedAt: 0, context: { windowSize: 200_000 },
    rateLimits: [{ kind: "five_hour", percentUsed: 3, resetsAt }, { kind: "seven_day", percentUsed: 0 }] } }));
  await $.command.run({ command: "useful-sidebar", args: "window refresh" });
  const snapshot = fixture.captured.filter(payload => payload.op === "snapshot").at(-1) as
    HelperPayload & { allowance: { windows: { label: string; usedFraction: number }[] } | null };
  expect(snapshot.allowance?.windows.map(window => [window.label, window.usedFraction]))
    .toEqual([["5h limit", 0.03], ["7d limit", 0]]);
});

test("routine rendering-surface replies omit the chart even for hidden, closed, disabled or unplaced Panes", async ($, on) => {
  const fixture = menuFixture(on);
  for (const pane of [
    { paneOpen: true, shown: true, placed: true, enabled: true },
    { paneOpen: true, shown: false, placed: true, enabled: true },
    { paneOpen: false, shown: false, placed: true, enabled: true },
    { paneOpen: false, shown: false, placed: true, enabled: false },
    { paneOpen: true, shown: false, placed: false, enabled: true },
  ]) {
    Object.assign(fixture, pane);
    for (const args of ["chart dots", "view list", "window refresh"]) {
      const reply = await $.command.run({ command: "useful-sidebar", args });
      expect(reply.text).not.toMatch(/^TOKEN RATE/m);
      expect(reply.text).toMatch(/capture unknown/i);
      expect(latestSnapshot(fixture.snapshots).preferences.enabled).toBe(pane.enabled);
    }
  }
  expect(fixture.opens).toBe(0);
});

test("only headless or explicitly unplaced opens return chart fallbacks and off stays concise", async ($, on) => {
  const fixture = menuFixture(on);
  fixture.surfaces = [];
  const headless = await $.command.run({ command: "useful-sidebar", args: "window refresh" });
  expect(headless.text).toMatch(/^TOKEN RATE/m);
  expect(fixture.opens).toBe(0);
  fixture.surfaces = ["terminal"];
  fixture.placed = false;
  for (const args of ["window on", "window focus"]) {
    const reply = await $.command.run({ command: "useful-sidebar", args });
    expect(reply.text).toMatch(/^TOKEN RATE/m);
    expect(reply.text).toMatch(/cannot place/i);
  }
  fixture.placed = true;
  const placed = await $.command.run({ command: "useful-sidebar", args: "window focus" });
  expect(placed.text).not.toMatch(/^TOKEN RATE/m);
  for (const surfaces of [["terminal"], []]) {
    fixture.surfaces = surfaces;
    const off = await $.command.run({ command: "useful-sidebar", args: "window off" });
    expect(off.text).not.toMatch(/^TOKEN RATE/m);
    expect(off.text).toMatch(/window off/i);
    expect(fixture.paneOpen).toBe(false);
  }
  expect(fixture.closes).toBe(2);
});

test("concise replies preserve read and storage diagnostics without transcript charts", async ($, on) => {
  const fixture = menuFixture(on);
  await $.command.run({ command: "useful-sidebar", args: "window refresh" });
  fixture.snapshotFailure = true;
  const readFailure = await $.command.run({ command: "useful-sidebar", args: "window refresh" });
  expect(readFailure.text).toMatch(/capture or history may be incomplete/i);
  expect(readFailure.text).not.toMatch(/^TOKEN RATE/m);
  fixture.snapshotFailure = false;
  const recovered = await $.command.run({ command: "useful-sidebar", args: "window refresh" });
  expect(latestSnapshot(fixture.snapshots).failure).toBe("");
  expect(recovered.text).not.toMatch(/capture or history may be incomplete/i);
  fixture.preferenceFailure = "storage_unavailable";
  const storageFailure = await $.command.run({ command: "useful-sidebar", args: "theme blue" });
  expect(storageFailure.text).toMatch(/capture or history may be incomplete/i);
  expect(storageFailure.text).not.toMatch(/^TOKEN RATE/m);
  expect(fixture.writes).toEqual([]);
  expect(fixture.opens).toBe(0);
});

test("unknown session surfaces report presentation failure without changing capture or storage health", async ($, on) => {
  const fixture = menuFixture(on);
  await $.command.run({ command: "useful-sidebar", args: "window refresh" });
  const before = latestSnapshot(fixture.snapshots);
  fixture.surfacesUnavailable = true;
  for (const snapshotFailure of [false, true]) {
    fixture.snapshotFailure = snapshotFailure;
    const reply = await $.command.run({ command: "useful-sidebar", args: "window refresh" });
    expect(reply.text).toMatch(/presentation unavailable/i);
    expect(reply.text).not.toMatch(/^TOKEN RATE/m);
    expect(reply.text).toMatch(/capture unknown/i);
    expect(latestSnapshot(fixture.snapshots).capture).toEqual(before.capture);
    expect(latestSnapshot(fixture.snapshots).preferences).toEqual(before.preferences);
    if (snapshotFailure) {
      expect(reply.text).toMatch(/capture or history may be incomplete/i);
    } else {
      expect(latestSnapshot(fixture.snapshots).failure).toBe("");
      expect(reply.text).not.toMatch(/capture or history may be incomplete/i);
    }
  }
  fixture.surfacesUnavailable = false;
  fixture.snapshotFailure = false;
  const recovered = await $.command.run({ command: "useful-sidebar", args: "window refresh" });
  expect(recovered.text).not.toMatch(/presentation unavailable/i);
  expect(latestSnapshot(fixture.snapshots).failure).toBe("");
  expect(fixture.writes).toEqual([]);
  expect(fixture.opens).toBe(0);
});
