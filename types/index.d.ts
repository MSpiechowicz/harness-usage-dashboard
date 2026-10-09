declare module "claude-code" {
  export type UsefulSidebarContext = {
    owner: string;
    session: string;
    activation: string;
    cwd: string;
  };

  export type UsefulSidebarInstance = {
    owner: string;
    context: UsefulSidebarContext | null;
    failure?: string;
  };

  export type UsefulSidebarToken = "text" | "muted" | "secondary" | "accent" | "chart" | "good" | "warn" | "error";
  export type UsefulSidebarPalette = Partial<Record<UsefulSidebarToken, string>>;

  export type UsefulSidebarPreferences = {
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
    tokens: UsefulSidebarPalette;
  };

  export type UsefulSidebarRow = {
    text: string;
    token: UsefulSidebarToken;
    emphasis: { start: number; end: number; token: UsefulSidebarToken } | null;
  };

  export type UsefulSidebarCapture = {
    state: "available" | "unknown" | "incomplete";
    reason: string;
    lastEventAt: number | null;
  };

  export type UsefulSidebarProviderTotals = {
    provider: string;
    input: number;
    output: number;
    cache_read: number;
    cache_write: number;
    total: number;
  };

  export type UsefulSidebarModelTotals = UsefulSidebarProviderTotals & { model: string };
  export type UsefulSidebarEffortTotals = UsefulSidebarModelTotals & { thinking_level: string };

  export type UsefulSidebarSessionHistory = {
    project: string;
    id: string;
    started: number;
    updated: number;
    providers: UsefulSidebarProviderTotals[];
    models: UsefulSidebarEffortTotals[];
    model_summaries: UsefulSidebarModelTotals[];
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

  export type UsefulSidebarHistory = {
    current: UsefulSidebarSessionHistory | null;
    previous: UsefulSidebarSessionHistory | null;
    history: UsefulSidebarEffortTotals[];
    history_summaries: UsefulSidebarModelTotals[];
    total_history: UsefulSidebarEffortTotals[];
    total_history_summaries: UsefulSidebarModelTotals[];
    chart: number[];
  };

  export type UsefulSidebarAllowanceReport = {
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
  export type UsefulSidebarSnapshot = {
    rows: UsefulSidebarRow[];
    tokens: UsefulSidebarPalette;
    preferences: Partial<UsefulSidebarPreferences>;
    session?: string;
    activation?: string;
    failure?: string;
    capture?: UsefulSidebarCapture;
    history?: UsefulSidebarHistory;
    reports?: UsefulSidebarAllowanceReport[];
    version?: 1;
    ok?: true;
  };

  interface PluginState {
    "harness-useful-sidebar": {
      instance: UsefulSidebarInstance;
      snapshot: UsefulSidebarSnapshot;
    };
  }
}
