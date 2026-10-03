export const PANE = "usage-dashboard";

export const HELP = "Usage Dashboard: /usage-dashboard opens the native settings menu without enabling the Pane; window on enables/opens it, focus opens/focuses it, off disables/closes it, refresh reads the local snapshot. Direct commands: view compact|details|list; chart bars|dots|trace; theme green|blue|brown|yellow|cyan|magenta|orange|red|claude|custom TOKEN COLOR|reset; commands|previous|history-other|history-total hide|show; providers add|remove|hide|show anthropic|claude; window on|off|focus|refresh; window hide|show anthropic|claude FILTER. Native Claude supports only Anthropic. Pane placement is managed by Claude; position and polling interval controls are not supported. Updates use Claude's plugin manager.";

export function snapshotText(snapshot) {
  if (!snapshot) return "Usage Dashboard: capture unknown; no local snapshot is available.";
  const lines = snapshot.rows?.map(row => row.text) ?? [];
  if (snapshot.failure) lines.unshift(`Usage Dashboard: ${snapshot.failure}`);
  return lines.join("\n") || "Usage Dashboard: capture unknown; no reported usage.";
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
