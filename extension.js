import { fileURLToPath } from "node:url";

const dashboard = fileURLToPath(new URL("./dashboard.py", import.meta.url));
const updater = fileURLToPath(new URL("./updater.py", import.meta.url));
const popular = ["codex", "claude", "copilot", "grok", "xai-oauth", "deepseek", "gemini", "google-antigravity", "cursor", "kimi-code", "minimax-code", "openrouter"];
const sections = {
  view: ["list", "compact", "details"],
  position: ["left", "right"],
  providers: ["add", "remove", "hide", "show"],
  window: ["on", "off", "focus", "refresh", "interval", "hide", "show"],
  update: ["check", "install"],
};
const help = "Sections: view (list, compact, details); position (left, right); providers (add, remove, hide, show PROVIDER); window (on, off, focus, refresh, interval SECONDS, hide/show PROVIDER FILTER); update (check, install).";

export default function usageDashboard(pi) {
  pi.setLabel("Usage dashboard");
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
    if (quiet) command.push("--cached");
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
        ctx.ui.notify(`Usage dashboard ${report.latestVersion} is available (installed ${report.currentVersion}). Run /usage-dashboard update install.`, "info");
      } else if (!quiet) {
        ctx.ui.notify(`Usage dashboard ${report.currentVersion}: ${report.message || "No newer release available."}`, "info");
      }
    } catch (error) {
      if (!quiet) ctx.ui.notify(`Usage dashboard update: ${error.message}`, "error");
    }
  }

  pi.on("session_start", async (_event, ctx) => {
    if (ctx.hasUI && process.env.TMUX && process.env.TMUX_PANE && process.env.OMP_USAGE_LAUNCHER !== "1") {
      await control(["init"], ctx, true);
    }
    if (ctx.hasUI) ctx.setTimeout(() => update("check", ctx, true), 0);
  });
  pi.on("session_shutdown", async (_event, ctx) => {
    if (ctx.hasUI && process.env.TMUX && process.env.TMUX_PANE) {
      await control(["detach"], ctx, true);
    }
  });
  pi.registerCommand("usage-dashboard", {
    description: "Manage usage dashboard: view, position, providers, window, and updates",
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
