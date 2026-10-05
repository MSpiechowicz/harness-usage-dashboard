import type { Args, On, TurnStepChunk, TurnStepResult, UsageDashboardSnapshot } from "claude-code";
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

function helperSuccess(snapshot: UsageDashboardSnapshot | undefined = undefined) {
  return { value: { exitCode: 0, stderr: "", isStdoutTruncated: false, isStderrTruncated: false,
    stdout: JSON.stringify({ version: 1, ok: true, ...snapshot }) } };
}

function observeSnapshots(on: On): UsageDashboardSnapshot[] {
  const snapshots: UsageDashboardSnapshot[] = [];
  on("state.set", { plugin: "harness-usage-dashboard", key: "snapshot" }, async (_$, event, next) => {
    const result = await next(event);
    if (result.value !== undefined && result.value.isSet) snapshots.push(event.value);
    return result;
  });
  return snapshots;
}

function latestSnapshot(snapshots: UsageDashboardSnapshot[]): UsageDashboardSnapshot {
  const snapshot = snapshots.at(-1);
  if (!snapshot) throw new Error("No accepted snapshot write");
  return snapshot;
}

function mountDashboard($: Engine) {
  return $.ui.mount({
    plugin: "harness-usage-dashboard", surface: "terminal", component: "Pane",
    requestId: "usage-dashboard",
    props: {
      title: "Usage Dashboard", isFocused: false, bodyColumns: 64, placement: "inline",
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
  const knownSnapshot: UsageDashboardSnapshot = {
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
    writes: [] as string[][], captured: [] as HelperPayload[], snapshots,
  };
  on("session.id", () => ({ value: fixture.session }));
  on("session.cwd", () => ({ value: "/native-fixture" }));
  on("session.surfaces", () => fixture.surfacesUnavailable
    ? { deny: "Session surfaces unavailable" } : { value: fixture.surfaces });
  on("classic.SessionStart", () => ({}));
  on("ui.panes", () => ({ value: fixture.paneOpen ? [{
    id: "usage-dashboard", title: "Usage Dashboard", isPlaced: fixture.placed,
    isShown: fixture.shown, isFocused: false,
  }] : [] }));
  on("ui.open", () => {
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
        stdout: JSON.stringify({ version: 1, ok: true, text: "Usage Dashboard settings saved." }) } };
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

function questionAnswer(event: Args<"tool.call">, answer: string) {
  const questions = event.questions as readonly { question: string }[];
  return { result: { answers: { [questions[0].question]: answer } } };
}

test("native menu cancellation, unknown selections and unavailability leave a disabled Pane untouched", async ($, on) => {
  const fixture = menuFixture(on);
  const answers = ["Cancel", "window on", "Chart", "Back", "Cancel"];
  on("tool.call", (_$, event) => {
    if (event.tool !== "AskUserQuestion") return { deny: "Unexpected tool" };
    if (answers.length) return questionAnswer(event, answers.shift()!);
    return { deny: "Question dismissed or unavailable" };
  });
  await $.classic.SessionStart({ source: "startup", session_id: fixture.session, cwd: "/native-fixture", transcript_path: "/unused" });
  for (let index = 0; index < 4; index += 1) {
    await $.command.run({ command: "usage-dashboard", args: "" });
  }
  expect(fixture.writes).toEqual([]);
  expect(fixture.opens).toBe(0);
  expect(latestSnapshot(fixture.snapshots).preferences.enabled).toBe(false);
  expect(latestSnapshot(fixture.snapshots).preferences.tokens).toEqual({ accent: "#d97757" });
  expect(latestSnapshot(fixture.snapshots).failure).toBe("");
  const ui = await mountDashboard($);
  try {
    expect(await ui.find({ type: "Text", text: /capture unknown/i })).toBeDefined();
    expect(await ui.find({ type: "Text", text: /capture or history may be incomplete/i })).toBeUndefined();
    expect(await ui.find({ type: "Button" })).toBeUndefined();
    expect(await ui.find({ type: "Select" })).toBeUndefined();
    expect(await ui.find({ type: "Input" })).toBeUndefined();
  } finally {
    await ui.unmount();
  }
});

test("closed-Pane help, cancellation and menu errors retain capture and storage diagnostics without side effects", async ($, on) => {
  const fixture = menuFixture(on);
  let answers: string[] = [];
  let unavailable = false;
  on("tool.call", (_$, event) => {
    if (event.tool !== "AskUserQuestion") return { deny: "Unexpected tool" };
    if (unavailable) return { deny: "Question dismissed or unavailable" };
    return questionAnswer(event, answers.shift()!);
  });
  for (const captureState of ["unknown", "incomplete"] as const) {
    fixture.captureState = captureState;
    fixture.captureReason = captureState === "unknown" ? "No native report" : "One request has no usage report";
    fixture.snapshotFailure = false;
    fixture.preferenceFailure = "";
    await $.command.run({ command: "usage-dashboard", args: "window refresh" });
    fixture.preferenceFailure = "storage_unavailable";
    await $.command.run({ command: "usage-dashboard", args: "theme blue" });
    fixture.snapshotFailure = true;

    for (const path of [
      ["Cancel"],
      ["window on"],
      ["Help", "Commands"],
      ["Help", "Current settings"],
      ["Next", "Next", "Theme", "Next", "Custom", "Accent", "window on"],
      ["Next", "Next", "Next", "Window", "Hide", "bad\nfilter"],
    ]) {
      answers = [...path];
      const reply = await $.command.run({ command: "usage-dashboard", args: "" });
      expect(answers).toEqual([]);
      expect(reply.text).toMatch(new RegExp(`capture ${captureState}`, "i"));
      expect(reply.text).toContain(fixture.captureReason);
      expect(reply.text).toMatch(/capture or history may be incomplete/i);
      expect(reply.text).not.toMatch(/^TOKEN RATE/m);
      if (path.at(-1) === "Current settings") expect(reply.text).toMatch(/Window: off/);
    }

    unavailable = true;
    const dismissed = await $.command.run({ command: "usage-dashboard", args: "" });
    unavailable = false;
    expect(dismissed.text).toMatch(/dismissed|unavailable/i);
    expect(dismissed.text).toMatch(new RegExp(`capture ${captureState}`, "i"));
    expect(dismissed.text).toContain(fixture.captureReason);
    expect(dismissed.text).toMatch(/capture or history may be incomplete/i);
    expect(dismissed.text).not.toMatch(/^TOKEN RATE/m);

    fixture.surfacesUnavailable = true;
    const noSurfaces = await $.command.run({ command: "usage-dashboard", args: "" });
    expect(noSurfaces.text).toMatch(/dismissed|unavailable/i);
    expect(noSurfaces.text).toMatch(/presentation unavailable/i);
    expect(noSurfaces.text).toMatch(new RegExp(`capture ${captureState}`, "i"));
    expect(noSurfaces.text).toContain(fixture.captureReason);
    expect(noSurfaces.text).toMatch(/capture or history may be incomplete/i);
    expect(noSurfaces.text).not.toMatch(/^TOKEN RATE/m);
    fixture.surfacesUnavailable = false;

    fixture.preferenceFailure = "unsupported_command";
    for (const surfaces of [["terminal"], []]) {
      fixture.surfaces = surfaces;
      for (const args of ["help", "unknown-command", "providers add unsupported-provider"]) {
        const reply = await $.command.run({ command: "usage-dashboard", args });
        expect(reply.text).toMatch(/Direct commands:/);
        expect(reply.text).toMatch(new RegExp(`capture ${captureState}`, "i"));
        expect(reply.text).toContain(fixture.captureReason);
        expect(reply.text).toMatch(/capture or history may be incomplete/i);
        expect(reply.text).not.toMatch(/^TOKEN RATE/m);
      }
    }
    fixture.surfaces = ["terminal"];
    expect(fixture.writes).toEqual([]);
    expect(fixture.opens).toBe(0);
    expect(fixture.closes).toBe(0);
    expect(fixture.paneOpen).toBe(false);
    expect(latestSnapshot(fixture.snapshots).preferences.enabled).toBe(false);
  }
});

test("invalid custom color and filter inputs cannot reach preference storage", async ($, on) => {
  const fixture = menuFixture(on);
  const answers = [
    "Next", "Next", "Theme", "Next", "Custom", "Accent", "window on",
    "Next", "Next", "Next", "Window", "Hide", "bad\nfilter",
  ];
  on("tool.call", (_$, event) => {
    if (event.tool !== "AskUserQuestion") return { deny: "Unexpected tool" };
    return questionAnswer(event, answers.shift() ?? "Cancel");
  });
  await $.classic.SessionStart({ source: "startup", session_id: fixture.session, cwd: "/native-fixture", transcript_path: "/unused" });
  await $.command.run({ command: "usage-dashboard", args: "" });
  await $.command.run({ command: "usage-dashboard", args: "" });
  expect(answers).toEqual([]);
  expect(fixture.writes).toEqual([]);
  expect(fixture.opens).toBe(0);
  expect(latestSnapshot(fixture.snapshots).failure).toBe("");
});

test("headless bare command returns usage and direct help without opening or mutating", async ($, on) => {
  const fixture = menuFixture(on);
  fixture.surfaces = [];
  let questions = 0;
  on("tool.call", () => {
    questions += 1;
    return { deny: "No question should be asked" };
  });
  const answer = await $.command.run({ command: "usage-dashboard", args: "" });
  expect(answer.text).toMatch(/^TOKEN RATE/m);
  expect(questions).toBe(0);
  expect(fixture.writes).toEqual([]);
  expect(fixture.opens).toBe(0);
  expect(latestSnapshot(fixture.snapshots).failure).toBe("");
});

test("a waiting settings question permits capture and refuses an answer from an old activation", async ($, on) => {
  const fixture = menuFixture(on);
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  let asked!: () => void;
  const waiting = new Promise<void>(resolve => { asked = resolve; });
  on("tool.call", async (_$, event) => {
    if (event.tool !== "AskUserQuestion") return { deny: "Unexpected tool" };
    asked();
    await gate;
    return questionAnswer(event, "Chart");
  });
  on("turn.step", async function* (_$, event) {
    return { turnId: event.turnId, index: event.index, answer: "", toolUses: [],
      stopReason: "end_turn" as const, usage: { ...zero, input_tokens: 7, model: "claude-model" } };
  });
  await $.classic.SessionStart({ source: "startup", session_id: fixture.session, cwd: "/native-fixture", transcript_path: "/unused" });
  fixture.captureReason = "PRIVATE-old-session-report";
  const command = $.command.run({ command: "usage-dashboard", args: "" });
  await waiting;
  try {
    await drain($.turn.step({ turnId: "during-menu", index: 0, model: "claude-model", messageCount: 1 }));
    expect(fixture.captured.some(payload =>
      payload.action === "record" && payload.session === "menu-session" && payload.entries?.[0]?.total === 7)).toBe(true);
    fixture.session = "replacement-session";
    fixture.captureReason = "PRIVATE-replacement-session-report";
    await $.classic.SessionStart({ source: "clear", session_id: fixture.session, cwd: "/native-fixture", transcript_path: "/unused" });
  } finally {
    release();
    const reply = await command;
    expect(reply.text).toMatch(/session changed|command ended/i);
    expect(reply.text).not.toContain("PRIVATE-");
    expect(reply.text).not.toMatch(/^TOKEN RATE/m);
  }
  expect(fixture.writes).toEqual([]);
  expect(fixture.opens).toBe(0);
  expect(latestSnapshot(fixture.snapshots).session).toBe("replacement-session");
  expect(latestSnapshot(fixture.snapshots).preferences.enabled).toBe(false);
  expect(latestSnapshot(fixture.snapshots).failure).toBe("");
});

test("paging and Back restore parent pages before cancellation without side effects", async ($, on) => {
  const fixture = menuFixture(on);
  const answers = [
    "Next", "Next", "Theme", "Next", "Custom", "Next", "Back", "Back",
    "Back", "Back", "Next", "Back", "Theme",
    "Next", "Next", "Next", "Next", "Next", "Back", "Next",
    "Back", "Back", "Back", "Back", "Back", "Back", "Back", "Back", "Cancel",
  ];
  let question = 0;
  on("tool.call", (_$, event) => {
    if (event.tool !== "AskUserQuestion") return { deny: "Unexpected tool" };
    const questions = event.questions as readonly { options: readonly { label: string }[] }[];
    const labels = questions[0].options.map(option => option.label);
    expect(labels.length).toBeGreaterThanOrEqual(2);
    expect(labels.length).toBeLessThanOrEqual(4);
    expect(fixture.writes).toEqual([]);
    expect(fixture.opens).toBe(0);
    if (question === 8) expect(labels).toContain("Custom");
    if (question === 10 || question === 12) expect(labels).toContain("Theme");
    if (question === 18 || question === 20) {
      expect(labels).toContain("Yellow");
      expect(labels.length).toBe(2);
    }
    const answer = answers[question++];
    expect(labels).toContain(answer);
    return questionAnswer(event, answer);
  });
  const reply = await $.command.run({ command: "usage-dashboard", args: "" });
  expect(question).toBe(answers.length);
  expect(fixture.writes).toEqual([]);
  expect(fixture.opens).toBe(0);
  expect(reply.text).not.toMatch(/^TOKEN RATE/m);
});

test("section visibility menu writes the chosen hide or show", async ($, on) => {
  const fixture = menuFixture(on);
  let answers: string[] = [];
  on("tool.call", (_$, event) => {
    if (event.tool !== "AskUserQuestion") return { deny: "Unexpected tool" };
    return questionAnswer(event, answers.shift()!);
  });
  for (const [path, words] of [
    [["Next", "Section Visibility", "Next", "History Total", "Hide"], ["history-total", "hide"]],
    [["Next", "Section Visibility", "History Other Sessions", "Show"], ["history-other", "show"]],
  ] as const) {
    answers = [...path];
    await $.command.run({ command: "usage-dashboard", args: "" });
    expect(answers).toEqual([]);
    expect(fixture.writes.at(-1)).toEqual([...words]);
  }
});

test("fixed pages reject off-page, obsolete, prototype and Other answers without side effects", async ($, on) => {
  const fixture = menuFixture(on);
  const paths = [
    ["Theme"],
    ["Next", "Next", "Theme", "Yellow"],
    ["Settings"],
    ["Appearance"],
    ["Other"],
    ["constructor"],
    ["__proto__"],
    ["Next", "Next", "Next", "Window", "On"],
    ["Back"],
  ];
  let answers: string[] = [];
  on("tool.call", (_$, event) => {
    if (event.tool !== "AskUserQuestion") return { deny: "Unexpected tool" };
    return questionAnswer(event, answers.shift()!);
  });
  for (const path of paths) {
    answers = [...path];
    await $.command.run({ command: "usage-dashboard", args: "" });
    expect(answers).toEqual([]);
    expect(fixture.writes).toEqual([]);
    expect(fixture.opens).toBe(0);
  }
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
      const reply = await $.command.run({ command: "usage-dashboard", args });
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
  const headless = await $.command.run({ command: "usage-dashboard", args: "window refresh" });
  expect(headless.text).toMatch(/^TOKEN RATE/m);
  expect(fixture.opens).toBe(0);
  fixture.surfaces = ["terminal"];
  fixture.placed = false;
  for (const args of ["window on", "window focus"]) {
    const reply = await $.command.run({ command: "usage-dashboard", args });
    expect(reply.text).toMatch(/^TOKEN RATE/m);
    expect(reply.text).toMatch(/cannot place/i);
  }
  fixture.placed = true;
  const placed = await $.command.run({ command: "usage-dashboard", args: "window focus" });
  expect(placed.text).not.toMatch(/^TOKEN RATE/m);
  for (const surfaces of [["terminal"], []]) {
    fixture.surfaces = surfaces;
    const off = await $.command.run({ command: "usage-dashboard", args: "window off" });
    expect(off.text).not.toMatch(/^TOKEN RATE/m);
    expect(off.text).toMatch(/window off/i);
    expect(fixture.paneOpen).toBe(false);
  }
  expect(fixture.closes).toBe(2);
});

test("concise replies preserve read, storage and migration diagnostics without transcript charts", async ($, on) => {
  const fixture = menuFixture(on);
  await $.command.run({ command: "usage-dashboard", args: "window refresh" });
  fixture.snapshotFailure = true;
  const readFailure = await $.command.run({ command: "usage-dashboard", args: "window refresh" });
  expect(readFailure.text).toMatch(/capture or history may be incomplete/i);
  expect(readFailure.text).not.toMatch(/^TOKEN RATE/m);
  fixture.snapshotFailure = false;
  const recovered = await $.command.run({ command: "usage-dashboard", args: "window refresh" });
  expect(latestSnapshot(fixture.snapshots).failure).toBe("");
  expect(recovered.text).not.toMatch(/capture or history may be incomplete/i);
  fixture.preferenceFailure = "storage_unavailable";
  const storageFailure = await $.command.run({ command: "usage-dashboard", args: "theme blue" });
  expect(storageFailure.text).toMatch(/capture or history may be incomplete/i);
  expect(storageFailure.text).not.toMatch(/^TOKEN RATE/m);
  fixture.preferenceFailure = "migration_required";
  const migrationFailure = await $.command.run({ command: "usage-dashboard", args: "theme blue" });
  expect(migrationFailure.text).toMatch(/explicitly migrate/i);
  expect(migrationFailure.text).not.toMatch(/^TOKEN RATE/m);
  expect(fixture.writes).toEqual([]);
  expect(fixture.opens).toBe(0);
});

test("unknown session surfaces report presentation failure without changing capture or storage health", async ($, on) => {
  const fixture = menuFixture(on);
  await $.command.run({ command: "usage-dashboard", args: "window refresh" });
  const before = latestSnapshot(fixture.snapshots);
  fixture.surfacesUnavailable = true;
  for (const snapshotFailure of [false, true]) {
    fixture.snapshotFailure = snapshotFailure;
    const reply = await $.command.run({ command: "usage-dashboard", args: "window refresh" });
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
  const recovered = await $.command.run({ command: "usage-dashboard", args: "window refresh" });
  expect(recovered.text).not.toMatch(/presentation unavailable/i);
  expect(latestSnapshot(fixture.snapshots).failure).toBe("");
  expect(fixture.writes).toEqual([]);
  expect(fixture.opens).toBe(0);
});
