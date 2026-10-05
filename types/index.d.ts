declare module "claude-code" {
  export type UsageDashboardContext = {
    owner: string;
    session: string;
    activation: string;
    cwd: string;
  };

  export type UsageDashboardInstance = {
    owner: string;
    context: UsageDashboardContext | null;
    failure?: string;
    blocked?: boolean;
  };

  export type UsageDashboardToken = "text" | "muted" | "secondary" | "accent" | "chart" | "good" | "warn" | "error";
  export type UsageDashboardPalette = Partial<Record<UsageDashboardToken, string>>;

  export type UsageDashboardPreferences = {
    providers: string[];
    hidden: string[];
    windows: Record<string, string[]>;
    side: "left" | "right";
    compact: boolean;
    commands_visible: boolean;
    rate_visible: boolean;
    current_visible: boolean;
    previous_visible: boolean;
    history_other_visible: boolean;
    history_total_visible: boolean;
    interval: number;
    enabled: boolean;
    theme: "green" | "blue" | "brown" | "yellow" | "cyan" | "magenta" | "orange" | "red" | "claude";
    chart_type: "bars" | "dots" | "trace";
    tokens: UsageDashboardPalette;
  };

  export type UsageDashboardRow = {
    text: string;
    token: UsageDashboardToken;
    emphasis: { start: number; end: number; token: UsageDashboardToken } | null;
  };

  export type UsageDashboardCapture = {
    state: "available" | "unknown" | "incomplete";
    reason: string;
    lastEventAt: number | null;
  };

  export type UsageDashboardProviderTotals = {
    provider: string;
    input: number;
    output: number;
    cache_read: number;
    cache_write: number;
    total: number;
  };

  export type UsageDashboardModelTotals = UsageDashboardProviderTotals & { model: string };
  export type UsageDashboardEffortTotals = UsageDashboardModelTotals & { thinking_level: string };

  export type UsageDashboardSessionHistory = {
    project: string;
    id: string;
    started: number;
    updated: number;
    providers: UsageDashboardProviderTotals[];
    models: UsageDashboardEffortTotals[];
    model_summaries: UsageDashboardModelTotals[];
    quota: {
      provider: string;
      label: string;
      key: string;
      points: number;
      intervals: number;
      segments: number;
      last: number;
    }[];
  };

  export type UsageDashboardHistory = {
    current: UsageDashboardSessionHistory | null;
    previous: UsageDashboardSessionHistory | null;
    history: UsageDashboardEffortTotals[];
    history_summaries: UsageDashboardModelTotals[];
    total_history: UsageDashboardEffortTotals[];
    total_history_summaries: UsageDashboardModelTotals[];
    chart: number[];
  };

  export type UsageDashboardAllowanceReport = {
    provider: "anthropic";
    fetchedAt: number;
    limits: {
      id: string;
      label: string;
      amount: { usedFraction: number };
      window: { resetsAt: number | null };
    }[];
    metadata: { source: "native-mod" };
  };

  // Initial and helper-failure snapshots have rows, palette and preferences but
  // no accounting report. Missing capture/history never means measured zero.
  export type UsageDashboardSnapshot = {
    rows: UsageDashboardRow[];
    tokens: UsageDashboardPalette;
    preferences: Partial<UsageDashboardPreferences>;
    session?: string;
    activation?: string;
    failure?: string;
    capture?: UsageDashboardCapture;
    history?: UsageDashboardHistory;
    reports?: UsageDashboardAllowanceReport[];
    version?: 1;
    ok?: true;
  };

  interface PluginState {
    "harness-usage-dashboard": {
      instance: UsageDashboardInstance;
      snapshot: UsageDashboardSnapshot;
    };
  }
}
