import type { On, TurnStepChunk, TurnStepResult, UsageDashboardSnapshot } from "claude-code";
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
