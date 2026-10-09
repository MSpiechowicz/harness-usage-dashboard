export const PANE = "useful-sidebar";

export const HELP = "Useful Sidebar: /useful-sidebar opens the settings pane (arrows move, Enter picks, Esc closes). Direct commands: view compact|details|list; chart bars|dots|trace; theme green|blue|brown|yellow|cyan|magenta|orange|red|claude|custom TOKEN COLOR|reset; token-rate|current|previous|history-other|history-total|commands hide|show; providers add|remove|hide|show anthropic; window on|off|focus|refresh; window hide|show anthropic FILTER. Native Claude supports only Anthropic. Pane placement is managed by Claude; position and polling interval controls are not supported. Updates use Claude's plugin manager.";

export function snapshotText(snapshot) {
  if (!snapshot) return "Useful Sidebar: capture unknown; no local snapshot is available.";
  const lines = snapshot.rows?.map(row => row.text) ?? [];
  if (snapshot.failure) lines.unshift(`Useful Sidebar: ${snapshot.failure}`);
  return lines.join("\n") || "Useful Sidebar: capture unknown; no reported usage.";
}

export function renderDashboard(elements, snapshot) {
  const { Box, Text } = elements;
  const tokens = snapshot?.tokens ?? {};
  const rows = (snapshot?.rows ?? []).map(row => {
    if (!row.text) return Text({ children: [" "] });
    const mark = row.emphasis;
    if (!mark) return Text({ color: tokens[row.token], wrap: "wrap", children: [row.text] });
    const text = Array.from(row.text);
    return Text({ color: tokens[row.token], wrap: "wrap", children: [
      text.slice(0, mark.start).join(""),
      Text({ color: tokens[mark.token], children: [text.slice(mark.start, mark.end).join("")] }),
      text.slice(mark.end).join(""),
    ] });
  });
  return Box({ flexDirection: "column", gap: 0, paddingX: 1, children: [
    snapshot?.failure ? Text({ color: tokens.error, children: [snapshot.failure] }) : null,
    ...rows,
    rows.length ? null : Text({ children: ["Capture unknown; waiting for a local snapshot."] }),
  ] });
}

const SHOWN = [{ value: "show", label: "Shown" }, { value: "hide", label: "Hidden" }];
const THEMES = ["claude", "blue", "brown", "cyan", "green", "magenta", "orange", "red", "yellow"];
const named = values => values.map(value => ({ value, label: value[0].toUpperCase() + value.slice(1) }));

// One row per dashboard section, in the order the dashboard draws them.
export function settingsRows(config) {
  const shown = field => config[field] === false ? "hide" : "show";
  const toggle = (key, label, field, section = key) =>
    ({ key, label, options: SHOWN, value: shown(field), words: value => [section, value] });
  const claude = config.providers?.includes("anthropic") && !config.hidden?.includes("anthropic");
  const filters = config.windows?.anthropic ?? [];
  const limit = (key, label) => ({ key, label, options: SHOWN, value: filters.includes(key) ? "hide" : "show",
    words: value => ["window", value, "anthropic", key] });
  return [
    { key: "dashboard", label: "Dashboard", options: named(["on", "off"]),
      value: config.enabled === false ? "off" : "on", words: value => ["window", value] },
    { key: "view", label: "View", options: named(["compact", "details"]),
      value: config.compact === false ? "details" : "compact", words: value => ["view", value] },
    { key: "theme", label: "Theme", options: named(THEMES),
      value: config.theme ?? "claude", words: value => ["theme", value] },
    { heading: "SECTIONS" },
    toggle("token-rate", "Token rate", "rate_visible"),
    { key: "chart", label: "  Chart style", options: named(["bars", "dots", "trace"]),
      value: config.chart_type ?? "bars", words: value => ["chart", value] },
    toggle("current", "Current session", "current_visible"),
    toggle("previous", "Previous session", "previous_visible"),
    toggle("history-other", "History other sessions", "history_other_visible"),
    toggle("history-total", "History total", "history_total_visible"),
    { key: "claude", label: "Claude", options: SHOWN, value: claude ? "show" : "hide",
      words: value => ["providers", value, "anthropic"] },
    limit("five_hour", "  5h limit"),
    limit("seven_day", "  7d limit"),
    toggle("commands", "Commands", "commands_visible"),
  ];
}

export function renderSettings(elements, snapshot, choose) {
  const { Box, Text, Select } = elements;
  const tokens = snapshot?.tokens ?? {};
  const rows = settingsRows(snapshot?.preferences ?? {});
  const width = Math.max(...rows.map(row => row.label?.length ?? 0)) + 2;
  return Box({ flexDirection: "column", gap: 0, paddingX: 1, children: [
    snapshot?.failure ? Text({ color: tokens.error, children: [snapshot.failure] }) : null,
    ...rows.map((row, index) => row.heading
      ? Text({ color: tokens.accent, children: [index ? `\n${row.heading}` : row.heading] })
      : Select({ key: row.key, label: row.label.padEnd(width), options: row.options, value: row.value,
        ...(row.key === "dashboard" ? { autoFocus: true } : {}),
        onSelect: value => { if (value !== row.value) choose(row.words(value)); } })),
    Text({ color: tokens.muted, children: ["\nArrows move, Enter picks, Esc closes. Custom colors: /useful-sidebar theme custom TOKEN COLOR"] }),
  ] });
}
