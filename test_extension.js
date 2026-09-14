import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
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
    assert.equal(notices[0].message, "Usage dashboard update available.");
    assert.equal(notices[0].level, "warning");
  } finally {
    for (const key of keys) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
    await rm(home, { recursive: true, force: true });
  }
});
