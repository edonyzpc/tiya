export const IPC_CHANNELS = {
  invoke: "tiya:invoke",
  event: "tiya:event"
} as const;

export type ServicePhase =
  | "unconfigured"
  | "stopped"
  | "starting"
  | "stopping"
  | "running"
  | "schema_mismatch"
  | "misconfigured"
  | "crashed";

export interface BlockingIssue {
  code: string;
  message: string;
}

export interface RunnerHealthItem {
  bin: string;
  available: boolean;
}

export interface RuntimePathsPayload {
  runtimeRoot: string;
  supervisorDir: string;
  socketPath: string;
  envPath: string;
  supervisorLogPath: string;
  storagePath?: string;
  logPath?: string;
  instanceDir?: string;
  workerStatePath?: string;
  pidPath?: string;
}

export interface RecentActivityItem {
  telegramUserId: number;
  provider: string;
  activeSessionId: string | null;
  activeCwd: string | null;
  activeRunId: string | null;
  pendingInteraction: boolean;
  lastSessionIds: string[];
  updatedAt: number;
}

export interface ServiceStatus {
  phase: ServicePhase;
  desktopPid: number | null;
  supervisorPid: number;
  workerPid: number | null;
  launchId: string | null;
  workerStartedAt: number | null;
  readyAt: number | null;
  runtimePaths: RuntimePathsPayload;
  schemaStatus: {
    status: string;
    message: string;
  };
  runnerHealth: {
    codex: RunnerHealthItem;
    claude: RunnerHealthItem;
  };
  blockingIssues: BlockingIssue[];
  warnings: string[];
  recentActivity: RecentActivityItem[];
  logPath: string;
  secrets: ConfigSnapshot["secrets"];
}

export interface SecretDescriptor {
  present: boolean;
  updatedAt: number | null;
  backend: string;
  available: boolean;
}

export interface ConfigSnapshot {
  env: Record<string, string>;
  secrets: {
    telegramToken: SecretDescriptor;
  };
}

export interface ValidationResult {
  ok: boolean;
  errors: string[];
  warnings: string[];
  normalized: {
    env: Record<string, string>;
  };
}

export interface SessionSummary {
  provider: string;
  sessionId: string;
  title: string;
  cwd: string;
  timestamp: string;
  sourcePath: string;
  isActiveForUser: boolean;
}

export interface SessionListResult {
  provider: string;
  items: SessionSummary[];
}

export interface SessionHistoryMessage {
  role: string;
  content: string;
}

export interface SessionHistoryMeta {
  sessionId: string;
  title: string;
  cwd: string;
  timestamp: string;
  sourcePath: string;
}

export interface SessionHistoryResult {
  provider: string;
  meta: SessionHistoryMeta | null;
  messages: SessionHistoryMessage[];
}

export interface DoctorReport {
  envPath: string;
  runtimeRoot: string;
  socketPath: string;
  storagePath?: string;
  logPath: string;
  launchId?: string | null;
  workerStartedAt?: number | null;
  lockStatus: {
    conflict: boolean;
    message?: string;
    path?: string;
  };
  schemaStatus: {
    status: string;
    message: string;
  };
  runnerHealth: ServiceStatus["runnerHealth"];
  sessionRoots: Array<{
    key: string;
    path: string;
    exists: boolean;
    readable: boolean;
  }>;
  secretStoreStatus: {
    backend: string;
    available: boolean;
    telegramToken: {
      present: boolean;
      updatedAt: number | null;
    };
  };
  recentErrors: string[];
  recommendedActions: string[];
}

export interface DesktopPaths {
  projectRoot: string;
  backendRoot: string;
  userData: string;
  envFile: string;
  runtimeRoot: string;
  socketPath: string;
}

export interface DesktopBootstrap {
  paths: DesktopPaths;
  status: ServiceStatus;
  config: ConfigSnapshot;
  diagnostics: DoctorReport;
}

export type DesktopEventName =
  | "service_phase_changed"
  | "supervisor_connected"
  | "worker_started"
  | "worker_stopped"
  | "worker_crashed"
  | "health_updated"
  | "log_appended"
  | "config_changed";

export interface DesktopEvent {
  name: DesktopEventName;
  payload: unknown;
}

export interface DesktopBridge {
  desktop: {
    bootstrap(): Promise<DesktopBootstrap>;
  };
  service: {
    status(): Promise<ServiceStatus>;
    start(): Promise<{ status: ServiceStatus; output: string; started: boolean }>;
    stop(): Promise<{ status: ServiceStatus; output: string; stopped: boolean }>;
    restart(): Promise<{ status: ServiceStatus; output: string; restarted: boolean }>;
  };
  config: {
    get(): Promise<ConfigSnapshot>;
    validate(payload: ConfigSnapshot): Promise<ValidationResult>;
    set(payload: ConfigSnapshot): Promise<ConfigSnapshot>;
    setSecret(value: string): Promise<SecretDescriptor>;
    clearSecret(): Promise<SecretDescriptor>;
  };
  sessions: {
    list(params: { provider: string; limit: number; telegramUserId?: number }): Promise<SessionListResult>;
    history(params: { provider: string; sessionId: string; limit: number }): Promise<SessionHistoryResult>;
  };
  diagnostics: {
    report(): Promise<DoctorReport>;
    export(destinationPath?: string): Promise<{ path: string }>;
  };
  dialog: {
    pickDirectory(): Promise<string | null>;
  };
  shell: {
    openPath(targetPath: string): Promise<string>;
  };
  events: {
    subscribe(listener: (event: DesktopEvent) => void): () => void;
  };
}
