export const PANE = "usage-dashboard";

export const HELP = "Usage Dashboard: view compact|details|list; chart bars|dots|trace; theme green|blue|brown|yellow|cyan|magenta|orange|red|claude|custom TOKEN COLOR|reset; commands|previous|history-other|history-total hide|show; providers add|remove|hide|show anthropic|claude; window on|off|focus|refresh; window hide|show anthropic|claude FILTER. Native Claude supports only Anthropic. Pane placement is managed by Claude; position and polling interval controls are not supported. Updates use Claude's plugin manager.";

export function snapshotText(snapshot) {
  if (!snapshot) return "Usage Dashboard: capture unknown; no local snapshot is available.";
  const lines = snapshot.rows?.map(row => row.text) ?? [];
  if (snapshot.failure) lines.unshift(`Usage Dashboard: ${snapshot.failure}`);
  return lines.join("\n") || "Usage Dashboard: capture unknown; no reported usage.";
}

export function renderDashboard(elements, e, snapshot, control) {
  const { Box, Text, Button, Select, Input } = elements;
  const tokens = snapshot?.tokens ?? {};
  const config = snapshot?.preferences ?? {};
  function select(key, title, values, value, section) {
    const selected = values.includes(value) ? value : values[0];
    if (e.surface === "mobile") {
      const next = values[(values.indexOf(selected) + 1) % values.length];
      return Button({ key, label: `${title}: ${selected} → ${next}`, onPress: () => control([section, next]) });
    }
    return Select({ key, label: title, options: values.map(item => ({ value: item, label: item })),
      value: selected, onSelect: item => control([section, item]) });
  }
  const rows = (snapshot?.rows ?? []).map(row => {
    const text = Array.from(row.text);
    const mark = row.emphasis;
    if (!mark) return Text({ color: tokens[row.token], wrap: "wrap", children: row.text });
    return Text({ color: tokens[row.token], wrap: "wrap", children: [
      text.slice(0, mark.start).join(""),
      Text({ color: tokens[mark.token], children: text.slice(mark.start, mark.end).join("") }),
      text.slice(mark.end).join(""),
    ] });
  });
  const actions = [
    Button({ key: "refresh", label: "Refresh", hotkey: "r", onPress: () => control(["window", "refresh"]) }),
    Button({ key: "close", label: "Close", hotkey: "x", role: "dismiss", onPress: () => control(["window", "off"]) }),
  ];
  const settings = [
    select("view", "View", ["compact", "details"], config.compact === false ? "details" : "compact", "view"),
    select("chart", "Chart", ["bars", "dots", "trace"], config.chart_type, "chart"),
    select("theme", "Theme", ["green", "blue", "brown", "yellow", "cyan", "magenta", "orange", "red", "claude"], config.theme, "theme"),
    ...["commands", "previous", "history-other", "history-total"].map(section => {
      const field = { commands: "commands_visible", previous: "previous_visible", "history-other": "history_other_visible", "history-total": "history_total_visible" }[section];
      return Button({ key: section, label: `${section}: ${config[field] === false ? "show" : "hide"}`,
        onPress: () => control([section, config[field] === false ? "show" : "hide"]) });
    }),
  ];
  if (e.surface !== "mobile") settings.push(Input({ key: "setting", label: "Setting",
    placeholder: "theme custom accent #58a66a · window hide anthropic five_hour", submitLabel: "Apply",
    onSubmit: value => control(value.trim().split(/\s+/).filter(Boolean)) }));
  return Box({ flexDirection: "column", gap: 1, children: [
    Text({ color: tokens.accent, bold: true, children: "Usage Dashboard" }),
    snapshot?.failure ? Text({ color: tokens.error, children: snapshot.failure }) : null,
    rows.length ? Box({ flexDirection: "column", children: rows })
      : Text({ children: "Capture unknown; waiting for a local snapshot." }),
    Box({ flexDirection: "row", flexWrap: "wrap", gap: 1, children: actions }),
    Box({ flexDirection: "column", gap: 1, children: settings }),
  ] });
}
