import {
  startTransition,
  useDeferredValue,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type Dispatch,
  type ReactNode,
  type SetStateAction
} from "react";

import type {
  ConfigSnapshot,
  DesktopBootstrap,
  DesktopEvent,
  DoctorReport,
  ServicePhase,
  ServiceStatus,
  SessionHistoryResult,
  SessionListResult,
  SessionSummary,
  ValidationResult
} from "@shared/api";

import { advancedFields, basicFields, type FieldDefinition, wizardSteps } from "./schema";

type ViewKey = "overview" | "config" | "sessions" | "logs" | "themes";
type DiagnosticsTab = "logs" | "diagnostics";
type ThemeKey = "graphite-emerald" | "charcoal-brass" | "stone-moss";
type ThemePreviewSample = {
  key: ThemeKey;
  name: string;
  eyebrow: string;
  summary: string;
  recommendation?: string;
  notes: string[];
  palette: {
    bg: string;
    bgSoft: string;
    panel: string;
    panelStrong: string;
    line: string;
    text: string;
    muted: string;
    brand: string;
    brandSoft: string;
    accent: string;
    danger: string;
    warning: string;
  };
};

type Notice = {
  tone: "neutral" | "success" | "warning" | "danger";
  text: string;
};

type ServiceAction = "start" | "stop" | "restart";
type ServiceActionResult = {
  status: ServiceStatus;
  output: string;
  started?: boolean;
  stopped?: boolean;
  restarted?: boolean;
};

const navItems: Array<{ key: ViewKey; label: string; eyebrow: string }> = [
  { key: "overview", label: "Overview", eyebrow: "01" },
  { key: "config", label: "Config", eyebrow: "02" },
  { key: "sessions", label: "Sessions", eyebrow: "03" },
  { key: "logs", label: "Logs & Diagnostics", eyebrow: "04" },
  { key: "themes", label: "Themes", eyebrow: "05" }
];

const themeStorageKey = "tiya.desktop.theme";

const themeSamples: ThemePreviewSample[] = [
  {
    key: "graphite-emerald",
    name: "Graphite + Emerald",
    eyebrow: "Control-first",
    summary: "Cold neutral surfaces with a restrained green brand layer. Built for trust, scanning speed, and operator confidence.",
    notes: [
      "Most aligned with a local control console",
      "Lets yellow step back into warning-only territory"
    ],
    palette: {
      bg: "#0b1012",
      bgSoft: "rgba(17, 24, 28, 0.94)",
      panel: "rgba(22, 29, 33, 0.86)",
      panelStrong: "rgba(27, 35, 40, 0.97)",
      line: "rgba(159, 183, 177, 0.18)",
      text: "#e8f0ee",
      muted: "#9caeaa",
      brand: "#49c09b",
      brandSoft: "rgba(73, 192, 155, 0.16)",
      accent: "#85ddd0",
      danger: "#f08878",
      warning: "#e1b15f"
    }
  },
  {
    key: "charcoal-brass",
    name: "Charcoal + Brass",
    eyebrow: "Closest to current",
    summary: "Keeps the warm, cinematic control-room mood, but trims the amber spread so the UI feels less alarm-colored.",
    notes: [
      "Lowest migration cost from the current app",
      "Still distinctive, but less semantically clean than green-first"
    ],
    palette: {
      bg: "#120f0c",
      bgSoft: "rgba(24, 18, 14, 0.95)",
      panel: "rgba(34, 27, 21, 0.86)",
      panelStrong: "rgba(40, 31, 24, 0.97)",
      line: "rgba(215, 177, 120, 0.18)",
      text: "#f3eadf",
      muted: "#bba892",
      brand: "#d7a14a",
      brandSoft: "rgba(215, 161, 74, 0.16)",
      accent: "#c9d7c2",
      danger: "#ef8f74",
      warning: "#f1c36c"
    }
  },
  {
    key: "stone-moss",
    name: "Stone + Moss",
    eyebrow: "Selected",
    summary: "A lighter editorial shell with moss-green accents. More productized and memorable, but less obviously an ops console.",
    recommendation: "Active theme",
    notes: [
      "Strongest personality and best icon/brand alignment",
      "Needs extra discipline on log-heavy views to stay serious"
    ],
    palette: {
      bg: "#dfddd5",
      bgSoft: "rgba(236, 232, 224, 0.96)",
      panel: "rgba(248, 244, 236, 0.9)",
      panelStrong: "rgba(250, 247, 241, 0.98)",
      line: "rgba(73, 98, 79, 0.18)",
      text: "#172119",
      muted: "#6b776e",
      brand: "#4f7f63",
      brandSoft: "rgba(79, 127, 99, 0.14)",
      accent: "#b48f55",
      danger: "#be6f61",
      warning: "#b38a43"
    }
  }
];

const fieldLookup = [...basicFields, ...advancedFields].reduce<Record<string, FieldDefinition>>((acc, field) => {
  acc[field.key] = field;
  return acc;
}, {});

function formatDate(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") {
    return "Not available";
  }
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}

function humanizePhase(phase: ServicePhase | undefined): string {
  if (!phase) {
    return "Unknown";
  }
  return phase
    .split("_")
    .map((item) => item[0].toUpperCase() + item.slice(1))
    .join(" ");
}

function mergeLines(previous: string[], incoming: string[]): string[] {
  const next = [...previous, ...incoming];
  return next.slice(-480);
}

function parseOptionalUserId(raw: string): number | undefined {
  const trimmed = raw.trim();
  if (!trimmed) {
    return undefined;
  }
  const parsed = Number(trimmed);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    return undefined;
  }
  return parsed;
}

function inferOpenPathLabel(targetPath: string): string {
  const parts = targetPath.split("/");
  return parts[parts.length - 1] || targetPath;
}

function isRedactedPath(targetPath: string): boolean {
  return targetPath.startsWith("[REDACTED_");
}

function buildPayload(env: Record<string, string>, config: ConfigSnapshot | null): ConfigSnapshot {
  return {
    env,
    secrets:
      config?.secrets ?? {
        telegramToken: {
          present: false,
          updatedAt: null,
          backend: "unknown",
          available: false
        }
      }
  };
}

function resolveSecretBackendGuidance(secret: ConfigSnapshot["secrets"]["telegramToken"] | null | undefined): string | null {
  if (!secret || secret.available) {
    return null;
  }
  if (secret.backend === "secret-service") {
    return "Linux secret backend is unavailable. Install secret-tool (Debian/Ubuntu: apt install libsecret-tools) and relaunch the desktop.";
  }
  if (secret.backend === "keychain") {
    return "macOS Keychain CLI is unavailable. Ensure the system security tool is present and relaunch the desktop.";
  }
  return "Secret backend is unavailable on this host.";
}

function didServiceActionSucceed(action: ServiceAction, result: ServiceActionResult): boolean {
  if (action === "start") {
    return Boolean(result.started);
  }
  if (action === "stop") {
    return Boolean(result.stopped);
  }
  return Boolean(result.restarted);
}

function describeServiceActionFailure(action: ServiceAction, result: ServiceActionResult): string {
  const blockingIssue = result.status.blockingIssues[0]?.message?.trim();
  if (blockingIssue) {
    return `Service ${action} failed: ${blockingIssue}`;
  }
  const phase = result.status.phase;
  if (phase === "starting" || phase === "stopping") {
    return `Service ${action} is still transitioning: phase=${phase}.`;
  }
  if (phase) {
    return `Service ${action} failed: phase=${phase}.`;
  }
  return `Service ${action} did not complete successfully.`;
}

function themePreviewStyle(sample: ThemePreviewSample): CSSProperties {
  return {
    "--theme-bg": sample.palette.bg,
    "--theme-bg-soft": sample.palette.bgSoft,
    "--theme-panel": sample.palette.panel,
    "--theme-panel-strong": sample.palette.panelStrong,
    "--theme-line": sample.palette.line,
    "--theme-text": sample.palette.text,
    "--theme-muted": sample.palette.muted,
    "--theme-brand": sample.palette.brand,
    "--theme-brand-soft": sample.palette.brandSoft,
    "--theme-accent": sample.palette.accent,
    "--theme-danger": sample.palette.danger,
    "--theme-warning": sample.palette.warning
  } as CSSProperties;
}

function resolveStoredTheme(): ThemeKey {
  if (typeof window === "undefined") {
    return "stone-moss";
  }
  const stored = window.localStorage.getItem(themeStorageKey);
  return themeSamples.some((sample) => sample.key === stored) ? (stored as ThemeKey) : "stone-moss";
}

function waitForNextPaint(): Promise<void> {
  return new Promise((resolve) => {
    window.requestAnimationFrame(() => {
      resolve();
    });
  });
}

function useDesktopRuntime(setNotice: Dispatch<SetStateAction<Notice | null>>) {
  const [bootstrap, setBootstrap] = useState<DesktopBootstrap | null>(null);
  const [initState, setInitState] = useState<DesktopBootstrap["initState"]>({
    supervisorReady: false,
    subscriptionReady: false,
    diagnosticsReady: false,
    initError: null
  });
  const [status, setStatus] = useState<ServiceStatus | null>(null);
  const [config, setConfig] = useState<ConfigSnapshot | null>(null);
  const [draftEnv, setDraftEnv] = useState<Record<string, string>>({});
  const [tokenDraft, setTokenDraft] = useState("");
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [fatalError, setFatalError] = useState<string | null>(null);
  const lastInitErrorRef = useRef<string | null>(null);

  function applyBootstrap(nextBootstrap: DesktopBootstrap): void {
    startTransition(() => {
      setBootstrap(nextBootstrap);
      setInitState(nextBootstrap.initState);
      if (nextBootstrap.status) {
        setStatus(nextBootstrap.status);
      }
      if (nextBootstrap.config) {
        setConfig(nextBootstrap.config);
        setDraftEnv({ ...nextBootstrap.config.env });
      }
      setFatalError(null);
    });
  }

  async function loadRuntimeData(params: { suppressNotice?: boolean; resetDraft?: boolean } = {}): Promise<void> {
    const { suppressNotice = false, resetDraft = false } = params;
    try {
      const [nextStatus, nextConfig] = await Promise.all([
        window.tiyaDesktop.service.status(),
        window.tiyaDesktop.config.get()
      ]);
      startTransition(() => {
        setStatus(nextStatus);
        setConfig(nextConfig);
        setDraftEnv((current) => (resetDraft || Object.keys(current).length === 0 ? { ...nextConfig.env } : current));
        setInitState((current) => ({
          ...current,
          supervisorReady: true,
          initError: null
        }));
        setFatalError(null);
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setFatalError(message);
      if (!suppressNotice) {
        setNotice({
          tone: "danger",
          text: message
        });
      }
    }
  }

  async function hydrate(): Promise<void> {
    setBusyAction("hydrate");
    try {
      const nextBootstrap = await window.tiyaDesktop.desktop.bootstrap();
      applyBootstrap(nextBootstrap);
      await loadRuntimeData({ resetDraft: true });
    } finally {
      setBusyAction((current) => (current === "hydrate" ? null : current));
    }
  }

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const nextBootstrap = await window.tiyaDesktop.desktop.bootstrap();
        if (cancelled) {
          return;
        }
        applyBootstrap(nextBootstrap);
      } catch (error) {
        if (cancelled) {
          return;
        }
        const message = error instanceof Error ? error.message : String(error);
        setFatalError(message);
        setNotice({
          tone: "danger",
          text: `Desktop bootstrap failed: ${message}`
        });
        return;
      }
      if (!cancelled) {
        void loadRuntimeData({ suppressNotice: true, resetDraft: true });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!initState.initError || initState.initError === lastInitErrorRef.current) {
      return;
    }
    lastInitErrorRef.current = initState.initError;
    setNotice({
      tone: "danger",
      text: `Desktop initialization failed: ${initState.initError}`
    });
  }, [initState.initError, setNotice]);

  function handleEvent(event: DesktopEvent): void {
    if (
      ["service_phase_changed", "health_updated", "worker_started", "worker_stopped", "worker_crashed"].includes(event.name) &&
      event.payload &&
      typeof event.payload === "object" &&
      "phase" in event.payload
    ) {
      startTransition(() => {
        setStatus(event.payload as ServiceStatus);
        setInitState((current) => ({
          ...current,
          supervisorReady: true,
          initError: null
        }));
      });
      return;
    }

    if (event.name === "desktop_init_changed" && event.payload && typeof event.payload === "object") {
      const nextInitState = event.payload as DesktopBootstrap["initState"];
      setInitState(nextInitState);
      setBootstrap((current) => (current ? { ...current, initState: nextInitState } : current));
      return;
    }

    if (event.name === "supervisor_connected") {
      setInitState((current) => ({
        ...current,
        supervisorReady: true,
        subscriptionReady: true,
        initError: null
      }));
      return;
    }

    if (event.name === "config_changed") {
      void loadRuntimeData({ suppressNotice: true, resetDraft: true });
    }
  }

  async function runServiceAction(action: ServiceAction): Promise<ServiceActionResult | null> {
    setBusyAction(action);
    await waitForNextPaint();
    try {
      const result: ServiceActionResult =
        action === "start"
          ? await window.tiyaDesktop.service.start()
          : action === "stop"
            ? await window.tiyaDesktop.service.stop()
            : await window.tiyaDesktop.service.restart();
      setStatus(result.status);
      setNotice({
        tone: didServiceActionSucceed(action, result) ? "success" : "danger",
        text: didServiceActionSucceed(action, result) ? `Service ${action} completed.` : describeServiceActionFailure(action, result)
      });
      return result;
    } catch (error) {
      setNotice({
        tone: "danger",
        text: error instanceof Error ? error.message : String(error)
      });
      return null;
    } finally {
      setBusyAction((current) => (current === action ? null : current));
    }
  }

  async function validateDraft(): Promise<ValidationResult | null> {
    setBusyAction("validate");
    await waitForNextPaint();
    try {
      const payload = buildPayload(draftEnv, config);
      const result = await window.tiyaDesktop.config.validate(payload);
      setValidation(result);
      return result;
    } catch (error) {
      setNotice({
        tone: "danger",
        text: error instanceof Error ? error.message : String(error)
      });
      return null;
    } finally {
      setBusyAction((current) => (current === "validate" ? null : current));
    }
  }

  async function saveDraft(origin: "wizard" | "config"): Promise<boolean> {
    setBusyAction(`save-${origin}`);
    await waitForNextPaint();
    try {
      if (!config) {
        setNotice({
          tone: "danger",
          text: "Configuration snapshot is not loaded yet."
        });
        return false;
      }

      const secretBackendGuidance = resolveSecretBackendGuidance(config.secrets.telegramToken);
      if (tokenDraft.trim() && secretBackendGuidance) {
        setNotice({
          tone: "danger",
          text: secretBackendGuidance
        });
        return false;
      }

      const result = await window.tiyaDesktop.config.validate(buildPayload(draftEnv, config));
      setValidation(result);
      if (!result.ok) {
        setNotice({
          tone: "warning",
          text: "Validation failed. Resolve blocking items before saving."
        });
        return false;
      }

      if (tokenDraft.trim()) {
        await window.tiyaDesktop.config.setSecret(tokenDraft.trim());
        setTokenDraft("");
      }

      const [savedConfig, nextStatus] = await Promise.all([
        window.tiyaDesktop.config.set(buildPayload(result.normalized.env, config)),
        window.tiyaDesktop.service.status()
      ]);
      startTransition(() => {
        setConfig(savedConfig);
        setDraftEnv({ ...savedConfig.env });
        setStatus(nextStatus);
      });
      setNotice({
        tone: "success",
        text: origin === "wizard" ? "Initial setup saved." : "Configuration saved."
      });
      return true;
    } catch (error) {
      setNotice({
        tone: "danger",
        text: error instanceof Error ? error.message : String(error)
      });
      return false;
    } finally {
      setBusyAction((current) => (current === `save-${origin}` ? null : current));
    }
  }

  async function clearSecret(): Promise<void> {
    setBusyAction("clear-secret");
    await waitForNextPaint();
    try {
      await window.tiyaDesktop.config.clearSecret();
      const [nextConfig, nextStatus] = await Promise.all([
        window.tiyaDesktop.config.get(),
        window.tiyaDesktop.service.status()
      ]);
      startTransition(() => {
        setConfig(nextConfig);
        setDraftEnv({ ...nextConfig.env });
        setStatus(nextStatus);
      });
      setNotice({
        tone: "warning",
        text: "Telegram token secret cleared."
      });
    } catch (error) {
      setNotice({
        tone: "danger",
        text: error instanceof Error ? error.message : String(error)
      });
    } finally {
      setBusyAction((current) => (current === "clear-secret" ? null : current));
    }
  }

  return {
    bootstrap,
    initState,
    status,
    config,
    draftEnv,
    tokenDraft,
    validation,
    busyAction,
    fatalError,
    setDraftEnv,
    setTokenDraft,
    hydrate,
    handleEvent,
    runServiceAction,
    validateDraft,
    saveDraft,
    clearSecret
  };
}

function useSessionsView(setNotice: Dispatch<SetStateAction<Notice | null>>) {
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [sessionProvider, setSessionProvider] = useState("codex");
  const [sessionUserIdInput, setSessionUserIdInput] = useState("");
  const [sessionList, setSessionList] = useState<SessionListResult | null>(null);
  const [sessionHistory, setSessionHistory] = useState<SessionHistoryResult | null>(null);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);

  async function loadSessions(): Promise<void> {
    setBusyAction("sessions");
    await waitForNextPaint();
    try {
      const telegramUserId = parseOptionalUserId(sessionUserIdInput);
      const listing = await window.tiyaDesktop.sessions.list({
        provider: sessionProvider,
        limit: 24,
        telegramUserId
      });
      startTransition(() => {
        setSessionList(listing);
        setSelectedSessionId((current) => (current && listing.items.some((item) => item.sessionId === current) ? current : null));
        setSessionHistory((current) => {
          if (!current?.meta) {
            return null;
          }
          return listing.items.some((item) => item.sessionId === current.meta?.sessionId) ? current : null;
        });
      });
    } catch (error) {
      setNotice({
        tone: "danger",
        text: error instanceof Error ? error.message : String(error)
      });
    } finally {
      setBusyAction((current) => (current === "sessions" ? null : current));
    }
  }

  async function selectSession(session: SessionSummary, params: { silent?: boolean } = {}): Promise<void> {
    const { silent = false } = params;
    if (!silent) {
      setBusyAction("history");
      await waitForNextPaint();
    }
    try {
      const history = await window.tiyaDesktop.sessions.history({
        provider: session.provider,
        sessionId: session.sessionId,
        limit: 80
      });
      startTransition(() => {
        setSelectedSessionId(session.sessionId);
        setSessionHistory(history);
      });
    } catch (error) {
      setNotice({
        tone: "danger",
        text: error instanceof Error ? error.message : String(error)
      });
    } finally {
      if (!silent) {
        setBusyAction((current) => (current === "history" ? null : current));
      }
    }
  }

  useEffect(() => {
    if (!sessionList?.items.length) {
      return;
    }
    const targetSession =
      sessionList.items.find((item) => item.sessionId === selectedSessionId) ??
      (selectedSessionId ? null : sessionList.items[0] ?? null);
    if (!targetSession) {
      return;
    }
    if (sessionHistory?.meta?.sessionId === targetSession.sessionId) {
      return;
    }
    const timer = window.setTimeout(() => {
      void selectSession(targetSession, { silent: true });
    }, 250);
    return () => {
      window.clearTimeout(timer);
    };
  }, [sessionHistory?.meta?.sessionId, sessionList, selectedSessionId]);

  return {
    busyAction,
    sessionProvider,
    sessionUserIdInput,
    sessionList,
    sessionHistory,
    selectedSessionId,
    setSessionProvider,
    setSessionUserIdInput,
    loadSessions,
    selectSession
  };
}

function useDiagnosticsView(
  setNotice: Dispatch<SetStateAction<Notice | null>>,
  params: {
    bootstrapDiagnostics: DoctorReport | null;
    diagnosticsActive: boolean;
    logsActive: boolean;
  }
) {
  const { bootstrapDiagnostics, diagnosticsActive, logsActive } = params;
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [diagnostics, setDiagnostics] = useState<DoctorReport | null>(bootstrapDiagnostics);
  const [lastExportPath, setLastExportPath] = useState<string | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [hasLoaded, setHasLoaded] = useState(Boolean(bootstrapDiagnostics));
  const bufferedLogsRef = useRef<string[]>([]);

  useEffect(() => {
    if (!bootstrapDiagnostics) {
      return;
    }
    setDiagnostics(bootstrapDiagnostics);
    setHasLoaded(true);
  }, [bootstrapDiagnostics]);

  useEffect(() => {
    if (!logsActive) {
      return;
    }
    setLogLines(bufferedLogsRef.current);
  }, [logsActive]);

  async function loadDiagnostics(params: { force?: boolean; busyKey?: string | null } = {}): Promise<DoctorReport | null> {
    const { force = false, busyKey = null } = params;
    if (!force && hasLoaded && diagnostics) {
      return diagnostics;
    }
    if (busyKey) {
      setBusyAction(busyKey);
      await waitForNextPaint();
    }
    try {
      const nextDiagnostics = await window.tiyaDesktop.diagnostics.report();
      startTransition(() => {
        setDiagnostics(nextDiagnostics);
        setHasLoaded(true);
      });
      return nextDiagnostics;
    } catch (error) {
      setNotice({
        tone: "danger",
        text: error instanceof Error ? error.message : String(error)
      });
      return null;
    } finally {
      if (busyKey) {
        setBusyAction((current) => (current === busyKey ? null : current));
      }
    }
  }

  useEffect(() => {
    if (diagnosticsActive) {
      void loadDiagnostics();
      return;
    }
    const timer = window.setTimeout(() => {
      void loadDiagnostics();
    }, 2000);
    return () => {
      window.clearTimeout(timer);
    };
  }, [diagnosticsActive]);

  function handleEvent(event: DesktopEvent): void {
    if (!(event.name === "log_appended" && event.payload && typeof event.payload === "object" && "lines" in event.payload)) {
      return;
    }
    const lines = (event.payload as { lines?: unknown }).lines;
    if (!Array.isArray(lines)) {
      return;
    }
    const merged = mergeLines(bufferedLogsRef.current, lines.map((item) => String(item)));
    bufferedLogsRef.current = merged;
    if (logsActive) {
      setLogLines(merged);
    }
  }

  function appendOutput(lines: string[]): void {
    if (!lines.length) {
      return;
    }
    const merged = mergeLines(bufferedLogsRef.current, lines);
    bufferedLogsRef.current = merged;
    if (logsActive) {
      setLogLines(merged);
    }
  }

  function invalidate(): void {
    setHasLoaded(false);
  }

  function scheduleRefresh(): void {
    window.setTimeout(() => {
      void loadDiagnostics({ force: true });
    }, 0);
  }

  async function exportDiagnostics(): Promise<void> {
    setBusyAction("export");
    await waitForNextPaint();
    try {
      const result = await window.tiyaDesktop.diagnostics.export();
      setLastExportPath(result.path);
      setNotice({
        tone: "success",
        text: `Diagnostics exported to ${result.path}`
      });
    } catch (error) {
      setNotice({
        tone: "danger",
        text: error instanceof Error ? error.message : String(error)
      });
    } finally {
      setBusyAction((current) => (current === "export" ? null : current));
    }
  }

  return {
    busyAction,
    diagnostics,
    logLines,
    lastExportPath,
    hasLoaded,
    loadDiagnostics,
    handleEvent,
    appendOutput,
    invalidate,
    scheduleRefresh,
    exportDiagnostics
  };
}

export default function App() {
  const [view, setView] = useState<ViewKey>("overview");
  const [diagnosticsTab, setDiagnosticsTab] = useState<DiagnosticsTab>("logs");
  const [activeTheme, setActiveTheme] = useState<ThemeKey>(() => resolveStoredTheme());
  const [notice, setNotice] = useState<Notice | null>(null);
  const [wizardStepIndex, setWizardStepIndex] = useState(0);
  const logsActive = view === "logs" && diagnosticsTab === "logs";
  const diagnosticsActive = view === "logs" && diagnosticsTab === "diagnostics";
  const runtime = useDesktopRuntime(setNotice);
  const sessions = useSessionsView(setNotice);
  const diagnosticsView = useDiagnosticsView(setNotice, {
    bootstrapDiagnostics: runtime.bootstrap?.diagnostics ?? null,
    diagnosticsActive,
    logsActive
  });
  const eventHandlerRef = useRef<(event: DesktopEvent) => void>(() => undefined);

  const {
    bootstrap,
    initState,
    status,
    config,
    draftEnv,
    tokenDraft,
    validation,
    busyAction: runtimeBusyAction,
    fatalError,
    setDraftEnv,
    setTokenDraft,
    hydrate,
    handleEvent: handleRuntimeEvent,
    runServiceAction: runRuntimeServiceAction,
    validateDraft,
    saveDraft: saveRuntimeDraft,
    clearSecret
  } = runtime;
  const {
    busyAction: sessionsBusyAction,
    sessionProvider,
    sessionUserIdInput,
    sessionList,
    sessionHistory,
    selectedSessionId,
    setSessionProvider,
    setSessionUserIdInput,
    loadSessions,
    selectSession
  } = sessions;
  const {
    busyAction: diagnosticsBusyAction,
    diagnostics,
    logLines,
    lastExportPath,
    hasLoaded: diagnosticsLoaded,
    loadDiagnostics,
    handleEvent: handleDiagnosticsEvent,
    appendOutput,
    invalidate: invalidateDiagnostics,
    scheduleRefresh: scheduleDiagnosticsRefresh,
    exportDiagnostics
  } = diagnosticsView;

  const busyAction = runtimeBusyAction ?? sessionsBusyAction ?? diagnosticsBusyAction;
  const deferredLogs = useDeferredValue(logLines);
  const requiresSetup = Boolean(config?.secrets && (!config.secrets.telegramToken.present || status?.phase === "unconfigured"));
  const showWizard = requiresSetup;
  const currentView = navItems.find((item) => item.key === view) ?? navItems[0];
  const activeThemeSample = themeSamples.find((sample) => sample.key === activeTheme) ?? themeSamples[2];
  const reducedEffects = (config?.env.TIYA_DESKTOP_GPU_MODE ?? bootstrap?.config?.env.TIYA_DESKTOP_GPU_MODE ?? "disabled") !== "enabled";

  useEffect(() => {
    document.documentElement.dataset.theme = activeTheme;
    window.localStorage.setItem(themeStorageKey, activeTheme);
  }, [activeTheme]);

  useEffect(() => {
    eventHandlerRef.current = (event: DesktopEvent) => {
      handleRuntimeEvent(event);
      if (event.name === "config_changed") {
        invalidateDiagnostics();
      }
      handleDiagnosticsEvent(event);
    };
  }, [handleDiagnosticsEvent, handleRuntimeEvent, invalidateDiagnostics]);

  useEffect(() => {
    const unsubscribe = window.tiyaDesktop.events.subscribe((event) => {
      eventHandlerRef.current(event);
    });
    return () => {
      unsubscribe();
    };
  }, []);

  useEffect(() => {
    if (view !== "sessions") {
      return;
    }
    void loadSessions();
  }, [sessionProvider, view]);

  useEffect(() => {
    if (!showWizard) {
      return;
    }
    setView("overview");
  }, [showWizard]);

  async function runServiceAction(action: ServiceAction): Promise<void> {
    const result = await runRuntimeServiceAction(action);
    if (!result) {
      return;
    }
    if (result.output.trim()) {
      appendOutput(result.output.trim().split("\n"));
    }
    if (diagnosticsLoaded) {
      scheduleDiagnosticsRefresh();
    }
    setView("overview");
  }

  async function saveDraft(origin: "wizard" | "config"): Promise<void> {
    const saved = await saveRuntimeDraft(origin);
    if (!saved) {
      return;
    }
    invalidateDiagnostics();
    if (diagnosticsLoaded) {
      scheduleDiagnosticsRefresh();
    }
    if (origin === "wizard") {
      setView("overview");
    }
  }

  async function handleClearSecret(): Promise<void> {
    await clearSecret();
    invalidateDiagnostics();
    if (diagnosticsLoaded) {
      scheduleDiagnosticsRefresh();
    }
  }

  async function refreshDiagnostics(): Promise<void> {
    await loadDiagnostics({ force: true, busyKey: "diagnostics" });
  }

  async function pickDirectory(fieldKey: string): Promise<void> {
    const selected = await window.tiyaDesktop.dialog.pickDirectory();
    if (!selected) {
      return;
    }
    setDraftEnv((current) => ({
      ...current,
      [fieldKey]: selected
    }));
  }

  async function openPath(targetPath: string): Promise<void> {
    const error = await window.tiyaDesktop.shell.openPath(targetPath);
    if (error) {
      setNotice({
        tone: "danger",
        text: error
      });
    }
  }

  function renderOverview(): JSX.Element {
    if (!status) {
      return (
        <EmptyState
          title="Awaiting supervisor status"
          copy={
            initState.initError
              ? `Desktop is still initializing: ${initState.initError}`
              : "The desktop-owned control plane has not returned a service snapshot yet."
          }
        />
      );
    }

    const runtime = status.runtimePaths;

    return (
      <section className="view-grid">
        <Card title="Service Health" eyebrow="Current phase" accent={status.phase}>
          <div className="metric-strip">
            <Metric label="Phase" value={humanizePhase(status.phase)} tone={status.phase} />
            <Metric label="Desktop PID" value={status.desktopPid ? String(status.desktopPid) : "Unknown"} />
            <Metric label="Supervisor PID" value={String(status.supervisorPid)} />
            <Metric label="Worker PID" value={status.workerPid ? String(status.workerPid) : "Not running"} />
            <Metric label="Launch ID" value={status.launchId ?? "Not started"} />
            <Metric label="Started At" value={formatDate(status.workerStartedAt)} />
            <Metric label="Ready At" value={formatDate(status.readyAt)} />
          </div>
          <div className="button-row">
            <ActionButton label="Start" onClick={() => void runServiceAction("start")} busy={busyAction === "start"} />
            <ActionButton label="Stop" onClick={() => void runServiceAction("stop")} busy={busyAction === "stop"} subtle />
            <ActionButton label="Restart" onClick={() => void runServiceAction("restart")} busy={busyAction === "restart"} subtle />
          </div>
        </Card>

        <Card title="Runner Availability" eyebrow="Local CLIs">
          <div className="runner-grid">
            <RunnerBadge name="Codex" bin={status.runnerHealth.codex.bin} available={status.runnerHealth.codex.available} />
            <RunnerBadge name="Claude" bin={status.runnerHealth.claude.bin} available={status.runnerHealth.claude.available} />
          </div>
        </Card>

        <Card title="Blocking Issues" eyebrow="Operator focus">
          {status.blockingIssues.length ? (
            <div className="issue-list">
              {status.blockingIssues.map((issue) => (
                <div key={`${issue.code}:${issue.message}`} className="issue-item issue-item-danger">
                  <strong>{issue.code}</strong>
                  <span>{issue.message}</span>
                </div>
              ))}
            </div>
          ) : (
            <EmptyInline copy="No blocking issues reported by the supervisor." />
          )}
          {status.warnings.length ? (
            <div className="issue-list">
              {status.warnings.map((warning) => (
                <div key={warning} className="issue-item issue-item-warning">
                  <strong>warning</strong>
                  <span>{warning}</span>
                </div>
              ))}
            </div>
          ) : null}
        </Card>

        <Card title="Runtime Map" eyebrow="Desktop-managed paths">
          <PathList
            items={[
              runtime.envPath,
              runtime.socketPath,
              runtime.runtimeRoot,
              runtime.storagePath ?? "",
              runtime.logPath ?? runtime.supervisorLogPath
            ].filter(Boolean)}
            onOpen={openPath}
          />
        </Card>

        <Card title="Recent Activity" eyebrow="Per Telegram user">
          {status.recentActivity.length ? (
            <div className="activity-table">
              <div className="activity-row activity-row-head">
                <span>User</span>
                <span>Provider</span>
                <span>Active Session</span>
                <span>CWD</span>
                <span>Updated</span>
              </div>
              {status.recentActivity.map((item) => (
                <div key={`${item.telegramUserId}:${item.provider}`} className="activity-row">
                  <span>{item.telegramUserId}</span>
                  <span>{item.provider}</span>
                  <span>{item.activeSessionId ?? "None"}</span>
                  <span>{item.activeCwd ?? "None"}</span>
                  <span>{formatDate(item.updatedAt)}</span>
                </div>
              ))}
            </div>
          ) : (
            <EmptyInline copy="No stored activity yet. Start the worker and let Telegram traffic arrive." />
          )}
        </Card>
      </section>
    );
  }

  function renderConfig(): JSX.Element {
    if (!config) {
      return <EmptyState title="No config snapshot" copy="The supervisor has not returned a configuration snapshot yet." />;
    }

    const secretBackendGuidance = resolveSecretBackendGuidance(config.secrets.telegramToken);

    return (
      <section className="view-grid">
        <Card title="Basic Configuration" eyebrow="Operator defaults">
          <FieldGrid fields={basicFields} values={draftEnv} onChange={setDraftEnv} onPickDirectory={pickDirectory} />
        </Card>

        <Card title="Advanced Configuration" eyebrow="Runtime behavior">
          <FieldGrid fields={advancedFields} values={draftEnv} onChange={setDraftEnv} onPickDirectory={pickDirectory} />
        </Card>

        <Card title="Secrets" eyebrow="Stored outside tiya.env">
          <div className="secret-state">
            <div>
              <span className={`secret-pill ${config.secrets.telegramToken.present ? "secret-pill-ready" : "secret-pill-empty"}`}>
                {config.secrets.telegramToken.present ? "Secret present" : "Secret missing"}
              </span>
              <p className="field-hint">
                Backend: {config.secrets.telegramToken.backend} •
                {" "}
                {config.secrets.telegramToken.available ? "ready" : "unavailable"} • Updated {formatDate(config.secrets.telegramToken.updatedAt)}
              </p>
            </div>
            <label className="field">
              <span className="field-label">Telegram Bot Token</span>
              <input
                className="field-input"
                type="password"
                value={tokenDraft}
                onChange={(event) => setTokenDraft(event.target.value)}
                placeholder="123456789:abc..."
              />
            </label>
            {secretBackendGuidance ? (
              <div className="issue-item issue-item-danger">
                <strong>blocked</strong>
                <span>{secretBackendGuidance}</span>
              </div>
            ) : null}
            <div className="button-row">
              <ActionButton
                label="Store Secret"
                onClick={() => void saveDraft("config")}
                busy={busyAction === "save-config"}
                disabled={Boolean(secretBackendGuidance)}
              />
              <ActionButton
                label="Clear Secret"
                onClick={() => void handleClearSecret()}
                busy={busyAction === "clear-secret"}
                subtle
                disabled={Boolean(secretBackendGuidance)}
              />
            </div>
          </div>
        </Card>

        <Card title="Validation" eyebrow="Before write-back">
          <div className="button-row">
            <ActionButton label="Validate" onClick={() => void validateDraft()} busy={busyAction === "validate"} subtle />
            <ActionButton label="Save Configuration" onClick={() => void saveDraft("config")} busy={busyAction === "save-config"} />
          </div>
          <ValidationPanel validation={validation} />
        </Card>
      </section>
    );
  }

  function renderSessions(): JSX.Element {
    return (
      <section className="sessions-layout">
        <Card title="Session Filters" eyebrow="Provider + Telegram user">
          <div className="filter-row">
            <label className="field">
              <span className="field-label">Provider</span>
              <select className="field-input" value={sessionProvider} onChange={(event) => setSessionProvider(event.target.value)}>
                <option value="codex">Codex</option>
                <option value="claude">Claude</option>
              </select>
            </label>
            <label className="field">
              <span className="field-label">Telegram User ID</span>
              <input
                className="field-input"
                value={sessionUserIdInput}
                onChange={(event) => setSessionUserIdInput(event.target.value)}
                placeholder="Optional filter"
              />
            </label>
            <div className="button-row">
              <ActionButton label="Refresh" onClick={() => void loadSessions()} busy={busyAction === "sessions"} />
            </div>
          </div>
        </Card>

        <div className="sessions-columns">
          <Card title="Recent Sessions" eyebrow="Local archives">
            {sessionList?.items.length ? (
              <div className="session-list">
                {sessionList.items.map((item) => (
                  <button
                    key={item.sessionId}
                    className={`session-item ${selectedSessionId === item.sessionId ? "session-item-active" : ""}`}
                    onClick={() => void selectSession(item)}
                  >
                    <div className="session-item-head">
                      <strong>{item.title}</strong>
                      {item.isActiveForUser ? <span className="session-active-pill">Active</span> : null}
                    </div>
                    <span>{item.sessionId}</span>
                    <span>{item.cwd}</span>
                    <span>{formatDate(item.timestamp)}</span>
                  </button>
                ))}
              </div>
            ) : (
              <EmptyInline copy="No sessions matched the current filter." />
            )}
          </Card>

          <Card title="History Preview" eyebrow="Selected conversation">
            {sessionHistory?.meta ? (
              <div className="history-panel">
                <div className="history-meta">
                  <strong>{sessionHistory.meta.title}</strong>
                  <span>{sessionHistory.meta.cwd}</span>
                  <span>{sessionHistory.meta.sessionId}</span>
                  <div className="button-row">
                    <ActionButton
                      label={`Open ${inferOpenPathLabel(sessionHistory.meta.sourcePath)}`}
                      onClick={() => void openPath(sessionHistory.meta?.sourcePath ?? "")}
                      subtle
                    />
                  </div>
                </div>
                <div className="history-messages">
                  {sessionHistory.messages.map((message, index) => (
                    <article key={`${message.role}:${index}`} className={`history-message history-message-${message.role}`}>
                      <strong>{message.role}</strong>
                      <p>{message.content}</p>
                    </article>
                  ))}
                </div>
              </div>
            ) : (
              <EmptyInline copy="Pick a recent session to inspect its history preview." />
            )}
          </Card>
        </div>
      </section>
    );
  }

  function renderLogsAndDiagnostics(): JSX.Element {
    return (
      <section className="view-grid">
        <Card title="Control Surface" eyebrow="Logs + diagnostics">
          <div className="tab-row">
            <button
              className={`tab-button ${diagnosticsTab === "logs" ? "tab-button-active" : ""}`}
              onClick={() => setDiagnosticsTab("logs")}
            >
              Logs
            </button>
            <button
              className={`tab-button ${diagnosticsTab === "diagnostics" ? "tab-button-active" : ""}`}
              onClick={() => setDiagnosticsTab("diagnostics")}
            >
              Diagnostics
            </button>
          </div>

          {diagnosticsTab === "logs" ? (
            <div className="log-console">
              {deferredLogs.length ? deferredLogs.map((line, index) => <pre key={`${index}:${line}`}>{line}</pre>) : <EmptyInline copy="Waiting for supervisor log events." />}
            </div>
          ) : diagnostics ? (
            <div className="diagnostics-grid">
              <div className="issue-list">
                {diagnostics.recommendedActions.map((action) => (
                  <div key={action} className="issue-item">
                    <strong>action</strong>
                    <span>{action}</span>
                  </div>
                ))}
              </div>
              <PathList
                items={[
                  diagnostics.envPath,
                  diagnostics.runtimeRoot,
                  diagnostics.socketPath,
                  diagnostics.storagePath ?? "",
                  diagnostics.logPath
                ].filter(Boolean)}
                onOpen={openPath}
              />
              <div className="button-row">
                <ActionButton label="Refresh Diagnostics" onClick={() => void refreshDiagnostics()} busy={busyAction === "diagnostics"} subtle />
                <ActionButton label="Export Bundle" onClick={() => void exportDiagnostics()} busy={busyAction === "export"} />
              </div>
              {lastExportPath ? <p className="field-hint">Last export: {lastExportPath}</p> : null}
              <div className="log-console">
                {diagnostics.recentErrors.length ? diagnostics.recentErrors.map((line, index) => <pre key={`${index}:${line}`}>{line}</pre>) : <EmptyInline copy="No recent error excerpts captured." />}
              </div>
            </div>
          ) : (
            <EmptyInline copy="Diagnostics report is not available yet." />
          )}
        </Card>
      </section>
    );
  }

  function renderWorkspaceContent(): JSX.Element {
    if (view === "themes") {
      return renderThemeLab();
    }

    if (fatalError && !bootstrap) {
      return (
        <Card title="Supervisor Unavailable" eyebrow="Bootstrap error">
          <p className="fatal-copy">{fatalError}</p>
          <div className="button-row">
            <ActionButton label="Retry Bootstrap" onClick={() => void hydrate()} busy={busyAction === "hydrate"} />
          </div>
        </Card>
      );
    }

    if (view === "overview") {
      return renderOverview();
    }
    if (view === "config") {
      return renderConfig();
    }
    if (view === "sessions") {
      return renderSessions();
    }
    return renderLogsAndDiagnostics();
  }

  function renderThemeLab(): JSX.Element {
    return (
      <section className="theme-lab">
        <Card title="Theme Directions" eyebrow="Visual compare" accent={activeThemeSample.name}>
          <p className="fatal-copy">
            Compare the three directions on the same shell, then activate the one you want. Theme changes apply instantly and persist
            locally on this desktop.
          </p>
        </Card>

        <div className="theme-lab-grid">
          {themeSamples.map((sample) => (
            <article key={sample.key} className="theme-preview-card" style={themePreviewStyle(sample)}>
              <header className="theme-preview-meta">
                <div>
                  <span className="theme-preview-kicker">{sample.eyebrow}</span>
                  <h3>{sample.name}</h3>
                  <p>{sample.summary}</p>
                </div>
                {sample.recommendation ? <span className="theme-preview-badge">{sample.recommendation}</span> : null}
              </header>

              <div className="theme-preview-shell">
                <aside className="theme-preview-sidebar">
                  <span className="theme-preview-brand-kicker">tiya desktop</span>
                  <strong className="theme-preview-brand-mark">Signal Desk</strong>
                  <div className="theme-preview-status">Running · supervisor healthy</div>
                  <div className="theme-preview-nav">
                    <span className="theme-preview-nav-item theme-preview-nav-item-active">Overview</span>
                    <span className="theme-preview-nav-item">Config</span>
                    <span className="theme-preview-nav-item">Sessions</span>
                    <span className="theme-preview-nav-item">Logs</span>
                  </div>
                </aside>

                <div className="theme-preview-workspace">
                  <div className="theme-preview-toolbar">
                    <div>
                      <span>Overview</span>
                      <strong>Runtime control</strong>
                    </div>
                    <div className="theme-preview-toolbar-actions">
                      <span className="theme-preview-chip theme-preview-chip-subtle">Refresh</span>
                      <span className="theme-preview-chip">Start</span>
                    </div>
                  </div>

                  <div className="theme-preview-stats">
                    <div className="theme-preview-stat">
                      <span>Phase</span>
                      <strong>Running</strong>
                    </div>
                    <div className="theme-preview-stat">
                      <span>Provider</span>
                      <strong>Codex</strong>
                    </div>
                    <div className="theme-preview-stat">
                      <span>Diagnostics</span>
                      <strong>No blockers</strong>
                    </div>
                  </div>

                  <div className="theme-preview-console">
                    <span>$ tiya status</span>
                    <span>phase=running</span>
                    <span>supervisor=connected</span>
                    <span>recentActivity=12</span>
                  </div>
                </div>

                <div className="theme-preview-modal">
                  <span className="theme-preview-modal-kicker">Setup wizard</span>
                  <strong>Telegram token</strong>
                  <p>Store the token securely, then continue to provider defaults.</p>
                  <div className="theme-preview-input" />
                  <div className="theme-preview-input theme-preview-input-short" />
                  <div className="theme-preview-modal-actions">
                    <span className="theme-preview-chip theme-preview-chip-subtle">Back</span>
                    <span className="theme-preview-chip">Next</span>
                  </div>
                </div>
              </div>

              <div className="theme-preview-swatches">
                <span>
                  <i className="theme-swatch" style={{ background: sample.palette.brand }} />
                  Brand
                </span>
                <span>
                  <i className="theme-swatch" style={{ background: sample.palette.panelStrong }} />
                  Surface
                </span>
                <span>
                  <i className="theme-swatch" style={{ background: sample.palette.warning }} />
                  Warning
                </span>
              </div>

              <div className="theme-preview-notes">
                {sample.notes.map((note) => (
                  <div key={note} className="theme-preview-note">
                    {note}
                  </div>
                ))}
              </div>

              <div className="button-row theme-preview-actions">
                <ActionButton
                  label={sample.key === activeTheme ? "Active Theme" : "Use Theme"}
                  onClick={() => setActiveTheme(sample.key)}
                  subtle={sample.key === activeTheme}
                  disabled={sample.key === activeTheme}
                />
              </div>
            </article>
          ))}
        </div>
      </section>
    );
  }

  function renderSetupModal(): JSX.Element | null {
    if (!showWizard || !config) {
      return null;
    }

    const currentStep = wizardSteps[wizardStepIndex];
    const isReviewStep = wizardStepIndex === wizardSteps.length - 1;
    const secretBackendGuidance = resolveSecretBackendGuidance(config.secrets.telegramToken);
    const canCommit = Boolean(config.secrets.telegramToken.present || (tokenDraft.trim() && !secretBackendGuidance));

    return (
      <div className="setup-backdrop" role="presentation">
        <section className="setup-modal" aria-modal="true" role="dialog" aria-labelledby="setup-modal-title">
          <header className="setup-modal-head">
            <div className="setup-modal-topline">
              <div className="setup-modal-topcopy">
                <span className="setup-kicker">First launch</span>
                <span className="setup-inline-note">Desktop shell stays locked until the minimum setup is committed.</span>
              </div>
              <ThemeSwitcher activeTheme={activeTheme} onSelect={setActiveTheme} />
            </div>

            <div className="setup-head-grid">
              <div>
                <span className="section-kicker">
                  Step {String(wizardStepIndex + 1).padStart(2, "0")} of {String(wizardSteps.length).padStart(2, "0")}
                </span>
                <h2 id="setup-modal-title">{currentStep.title}</h2>
                <p className="setup-copy">{currentStep.description}</p>
              </div>
              <div className="setup-stage-progress" aria-hidden="true">
                {wizardSteps.map((step, index) => (
                  <span
                    key={step.title}
                    className={`setup-progress-dot ${index <= wizardStepIndex ? "setup-progress-dot-active" : ""}`}
                  />
                ))}
              </div>
            </div>

            <div className="setup-step-tabs">
              {wizardSteps.map((step, index) => (
                <button
                  key={step.title}
                  className={`setup-step-tab ${index === wizardStepIndex ? "setup-step-tab-active" : ""}`}
                  onClick={() => setWizardStepIndex(index)}
                >
                  <span>{step.eyebrow}</span>
                  <strong>{step.title}</strong>
                </button>
              ))}
            </div>
          </header>

          <div className="setup-stage-card">
            <div className="setup-stage-body">
              {wizardStepIndex === 0 ? (
                <label className="field">
                  <span className="field-label">Telegram Bot Token</span>
                  <input
                    className="field-input"
                    type="password"
                    placeholder="Stored in system keychain / secret service"
                    value={tokenDraft}
                    onChange={(event) => setTokenDraft(event.target.value)}
                  />
                  <span className="field-hint">
                    Current secret: {config.secrets.telegramToken.present ? "configured" : "not configured"} • backend{" "}
                    {config.secrets.telegramToken.backend} • {config.secrets.telegramToken.available ? "ready" : "unavailable"}
                  </span>
                  {secretBackendGuidance ? (
                    <div className="issue-item issue-item-danger">
                      <strong>blocked</strong>
                      <span>{secretBackendGuidance}</span>
                    </div>
                  ) : null}
                </label>
              ) : null}

              {currentStep.fields.length ? (
                <FieldGrid
                  fields={currentStep.fields
                    .map((fieldKey) => fieldLookup[fieldKey])
                    .filter((field): field is FieldDefinition => Boolean(field))}
                  values={draftEnv}
                  onChange={setDraftEnv}
                  onPickDirectory={pickDirectory}
                />
              ) : (
                <div className="setup-review-grid">
                  <Metric label="Token Secret" value={canCommit ? "Ready" : "Missing"} />
                  <Metric label="Default Provider" value={draftEnv.DEFAULT_PROVIDER || "codex"} />
                  <Metric label="Default CWD" value={draftEnv.DEFAULT_CWD || "Unset"} />
                  <Metric
                    label="Allowed Users"
                    value={draftEnv.ALLOWED_TELEGRAM_USER_IDS?.trim() || "Unset"}
                  />
                </div>
              )}

              {isReviewStep ? <ValidationPanel validation={validation} /> : null}
            </div>

            <footer className="setup-stage-footer">
              <p className="setup-footer-copy">
                {isReviewStep
                  ? "Run validation before commit, then save the initial snapshot."
                  : "Complete the highlighted fields, then continue to the next setup step."}
              </p>
              <div className="button-row wizard-actions">
                <ActionButton
                  label="Back"
                  onClick={() => setWizardStepIndex((current) => Math.max(0, current - 1))}
                  disabled={wizardStepIndex === 0}
                  subtle
                />
                {isReviewStep ? (
                  <>
                    <ActionButton label="Validate" onClick={() => void validateDraft()} subtle />
                    <ActionButton
                      label="Commit Setup"
                      onClick={() => void saveDraft("wizard")}
                      busy={busyAction === "save-wizard"}
                      disabled={!canCommit}
                    />
                  </>
                ) : (
                  <ActionButton
                    label="Next"
                    onClick={() => setWizardStepIndex((current) => Math.min(wizardSteps.length - 1, current + 1))}
                  />
                )}
              </div>
            </footer>
          </div>
        </section>
      </div>
    );
  }

  return (
    <div className={`app-shell ${showWizard ? "app-shell-setup-lock" : ""} ${reducedEffects ? "app-shell-reduced-effects" : ""}`}>
      <div className="ambient ambient-a" />
      <div className="ambient ambient-b" />

      <aside className="sidebar">
        <div className="brand-lockup">
          <span className="brand-kicker">tiya desktop</span>
          <h1>Signal Desk</h1>
          <p>Local operations console for the Telegram-facing runtime. Keep desktop open to keep the worker running.</p>
        </div>

        <div className="status-card">
          <span className={`phase-pill phase-pill-${showWizard ? "unconfigured" : status?.phase ?? "stopped"}`}>
            {showWizard ? "Setup Required" : humanizePhase(status?.phase)}
          </span>
          <p>
            {showWizard
              ? "Finish the guided setup before using the rest of the desktop console."
              : status?.blockingIssues[0]?.message ?? initState.initError ?? "Desktop-managed runtime, supervisor, and diagnostics."}
          </p>
        </div>

        <nav className="nav-stack">
          {navItems.map((item) => (
            <button
              key={item.key}
              className={`nav-item ${item.key === view ? "nav-item-active" : ""}`}
              onClick={() => setView(item.key)}
              disabled={showWizard}
            >
              <span>{item.eyebrow}</span>
              <strong>{item.label}</strong>
            </button>
          ))}
        </nav>

        <Card title="Desktop Runtime" eyebrow="Pinned paths" compact>
          {bootstrap?.paths ? (
            <PathList items={[bootstrap.paths.envFile, bootstrap.paths.runtimeRoot, bootstrap.paths.socketPath]} onOpen={openPath} compact />
          ) : (
            <EmptyInline copy="Bootstrap not loaded yet." />
          )}
        </Card>
      </aside>

      <main className="workspace">
        <header className="workspace-header">
          <div>
            <span className="section-kicker">{view === "themes" ? "Theme study" : showWizard ? "Setup required" : humanizePhase(status?.phase)}</span>
            <h2>{currentView.label}</h2>
            <p>
              {view === "themes"
                ? "Compare three visual directions on the same desktop skeleton before committing to a full theme refactor."
                : showWizard
                ? "The shell is visible for context, but configuration is guided through a dedicated setup modal."
                : "Observe the supervisor, adjust runtime policy, inspect recent sessions, and export diagnostics without leaving the desktop shell."}
            </p>
          </div>
          <div className="button-row">
            {view === "themes" ? (
              <ThemeSwitcher activeTheme={activeTheme} onSelect={setActiveTheme} />
            ) : (
              <>
                <ThemeSwitcher activeTheme={activeTheme} onSelect={setActiveTheme} onOpenThemes={() => setView("themes")} />
                <ActionButton label="Refresh" onClick={() => void hydrate()} busy={busyAction === "hydrate"} subtle />
                <ActionButton label="Start" onClick={() => void runServiceAction("start")} busy={busyAction === "start"} disabled={showWizard} />
                <ActionButton label="Restart" onClick={() => void runServiceAction("restart")} busy={busyAction === "restart"} subtle disabled={showWizard} />
              </>
            )}
          </div>
        </header>

        {notice ? <div className={`notice notice-${notice.tone}`}>{notice.text}</div> : null}

        {renderWorkspaceContent()}
      </main>

      {renderSetupModal()}
    </div>
  );
}

function Card(props: {
  title: string;
  eyebrow: string;
  children: ReactNode;
  accent?: string;
  compact?: boolean;
}): JSX.Element {
  return (
    <section className={`card ${props.compact ? "card-compact" : ""}`}>
      <header className="card-head">
        <div>
          <span className="section-kicker">{props.eyebrow}</span>
          <h3>{props.title}</h3>
        </div>
        {props.accent ? <span className="accent-label">{props.accent}</span> : null}
      </header>
      {props.children}
    </section>
  );
}

function EmptyState(props: { title: string; copy: string }): JSX.Element {
  return (
    <Card title={props.title} eyebrow="Waiting">
      <p className="fatal-copy">{props.copy}</p>
    </Card>
  );
}

function EmptyInline(props: { copy: string }): JSX.Element {
  return <p className="empty-inline">{props.copy}</p>;
}

function Metric(props: { label: string; value: string; tone?: string }): JSX.Element {
  return (
    <div className={`metric ${props.tone ? `metric-${props.tone}` : ""}`}>
      <span>{props.label}</span>
      <strong>{props.value}</strong>
    </div>
  );
}

function RunnerBadge(props: { name: string; bin: string; available: boolean }): JSX.Element {
  return (
    <div className={`runner-badge ${props.available ? "runner-badge-ready" : "runner-badge-missing"}`}>
      <span>{props.name}</span>
      <strong>{props.available ? "Available" : "Missing"}</strong>
      <code>{props.bin}</code>
    </div>
  );
}

function PathList(props: { items: string[]; onOpen: (targetPath: string) => void; compact?: boolean }): JSX.Element {
  return (
    <div className={`path-list ${props.compact ? "path-list-compact" : ""}`}>
      {props.items.map((item) => (
        <button
          key={item}
          className="path-item"
          onClick={() => void props.onOpen(item)}
          disabled={isRedactedPath(item)}
        >
          <span>{inferOpenPathLabel(item)}</span>
          <code>{item}</code>
        </button>
      ))}
    </div>
  );
}

function ValidationPanel(props: { validation: ValidationResult | null }): JSX.Element {
  if (!props.validation) {
    return <EmptyInline copy="No validation run yet." />;
  }

  return (
    <div className="validation-panel">
      <div className={`issue-item ${props.validation.ok ? "issue-item-ready" : "issue-item-danger"}`}>
        <strong>{props.validation.ok ? "ready" : "blocked"}</strong>
        <span>{props.validation.ok ? "Snapshot is valid for write-back." : "Snapshot has blocking validation errors."}</span>
      </div>
      {props.validation.errors.map((error) => (
        <div key={error} className="issue-item issue-item-danger">
          <strong>error</strong>
          <span>{error}</span>
        </div>
      ))}
      {props.validation.warnings.map((warning) => (
        <div key={warning} className="issue-item issue-item-warning">
          <strong>warning</strong>
          <span>{warning}</span>
        </div>
      ))}
    </div>
  );
}

function ActionButton(props: {
  label: string;
  onClick: () => void;
  busy?: boolean;
  subtle?: boolean;
  disabled?: boolean;
}): JSX.Element {
  return (
    <button
      className={`action-button ${props.subtle ? "action-button-subtle" : ""}`}
      onClick={props.onClick}
      disabled={props.busy || props.disabled}
    >
      {props.busy ? "Working..." : props.label}
    </button>
  );
}

function ThemeSwitcher(props: {
  activeTheme: ThemeKey;
  onSelect: (theme: ThemeKey) => void;
  onOpenThemes?: () => void;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const activeTheme = themeSamples.find((sample) => sample.key === props.activeTheme) ?? themeSamples[2];

  useEffect(() => {
    if (!open) {
      return;
    }

    function handlePointerDown(event: MouseEvent): void {
      if (containerRef.current && event.target instanceof Node && !containerRef.current.contains(event.target)) {
        setOpen(false);
      }
    }

    document.addEventListener("mousedown", handlePointerDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
    };
  }, [open]);

  return (
    <div className="theme-switcher" ref={containerRef}>
      <button className="action-button action-button-subtle theme-switcher-trigger" onClick={() => setOpen((current) => !current)} type="button">
        Theme · {activeTheme.name}
      </button>
      {open ? (
        <div className="theme-switcher-popover">
          <div className="theme-switcher-head">
            <span className="section-kicker">Theme</span>
            <strong>{activeTheme.name}</strong>
          </div>
          <div className="theme-switcher-list">
            {themeSamples.map((sample) => (
              <button
                key={sample.key}
                type="button"
                className={`theme-switcher-option ${sample.key === props.activeTheme ? "theme-switcher-option-active" : ""}`}
                onClick={() => {
                  props.onSelect(sample.key);
                  setOpen(false);
                }}
              >
                <span className="theme-switcher-swatch" style={{ background: sample.palette.brand }} />
                <div>
                  <strong>{sample.name}</strong>
                  <span>{sample.summary}</span>
                </div>
              </button>
            ))}
          </div>
          {props.onOpenThemes ? (
            <button
              type="button"
              className="theme-switcher-link"
              onClick={() => {
                props.onOpenThemes?.();
                setOpen(false);
              }}
            >
              Open themes page
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function FieldGrid(props: {
  fields: FieldDefinition[];
  values: Record<string, string>;
  onChange: Dispatch<SetStateAction<Record<string, string>>>;
  onPickDirectory: (fieldKey: string) => Promise<void>;
}): JSX.Element {
  return (
    <div className="field-grid">
      {props.fields.map((field) => (
        <label key={field.key} className={`field ${field.kind === "textarea" ? "field-span-two" : ""}`}>
          <span className="field-label">{field.label}</span>
          {field.kind === "select" ? (
            <select
              className="field-input"
              value={props.values[field.key] ?? ""}
              onChange={(event) =>
                props.onChange((current) => ({
                  ...current,
                  [field.key]: event.target.value
                }))
              }
            >
              {field.options?.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          ) : field.kind === "textarea" ? (
            <textarea
              className="field-input field-textarea"
              value={props.values[field.key] ?? ""}
              placeholder={field.placeholder}
              onChange={(event) =>
                props.onChange((current) => ({
                  ...current,
                  [field.key]: event.target.value
                }))
              }
            />
          ) : (
            <div className="field-input-row">
              <input
                className="field-input"
                type={field.kind === "number" ? "number" : "text"}
                value={props.values[field.key] ?? ""}
                placeholder={field.placeholder}
                onChange={(event) =>
                  props.onChange((current) => ({
                    ...current,
                    [field.key]: event.target.value
                  }))
                }
              />
              {field.kind === "directory" ? (
                <button className="field-picker" type="button" onClick={() => void props.onPickDirectory(field.key)}>
                  Browse
                </button>
              ) : null}
            </div>
          )}
          <span className="field-hint">{field.hint}</span>
        </label>
      ))}
    </div>
  );
}
