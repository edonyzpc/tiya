import { app, BrowserWindow, dialog, ipcMain, shell } from "electron";
import path from "node:path";
import type { ChildProcess } from "node:child_process";

import {
  IPC_CHANNELS,
  type ConfigSnapshot,
  type DesktopBootstrap,
  type DesktopEvent,
  type DesktopInitState,
  type DoctorReport,
  type ServiceStatus,
  type SessionHistoryResult,
  type SessionListResult,
  type ValidationResult
} from "../shared/api";
import { focusPrimaryWindow, handleBeforeQuit, handleWindowAllClosed, wireSingleInstanceLifecycle } from "./app_lifecycle";
import { applyDesktopGpuMode, resolveDesktopGpuMode } from "./gpu_mode";
import { RpcSubscription, callRpc } from "./rpc";
import { ensureSupervisor, resolveDesktopPaths, shutdownSupervisor } from "./runtime";

// Default to software rendering because some Linux sessions cannot start
// Electron's GPU process. This can be overridden in tiya.env.
applyDesktopGpuMode(
  app,
  resolveDesktopGpuMode({
    envFilePath: path.join(app.getPath("userData"), "config", "tiya.env"),
  })
);

class DesktopController {
  private windows = new Set<BrowserWindow>();
  private subscription: RpcSubscription | null = null;
  private subscriptionPromise: Promise<void> | null = null;
  private backgroundInitializationPromise: Promise<void> | null = null;
  private supervisorReadyPromise: Promise<void> | null = null;
  private reconnectTimer: NodeJS.Timeout | null = null;
  private supervisorProcess: ChildProcess | null = null;
  private status: ServiceStatus | null = null;
  private config: ConfigSnapshot | null = null;
  private diagnostics: DoctorReport | null = null;
  private initState: DesktopInitState = {
    supervisorReady: false,
    subscriptionReady: false,
    diagnosticsReady: false,
    initError: null
  };
  readonly paths = resolveDesktopPaths();

  registerWindow(window: BrowserWindow): void {
    this.windows.add(window);
    window.on("closed", () => {
      this.windows.delete(window);
    });
  }

  startBackgroundInitialization(): void {
    if (this.backgroundInitializationPromise) {
      return;
    }
    this.backgroundInitializationPromise = (async () => {
      try {
        await this.ensureSupervisorReady();
        await Promise.allSettled([this.refreshStatus(), this.refreshConfig()]);
        await this.ensureSubscription();
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        this.updateInitState({
          initError: message
        });
        this.scheduleReconnect();
      }
    })().finally(() => {
      this.backgroundInitializationPromise = null;
    });
  }

  private async ensureSupervisorReady(): Promise<void> {
    if (!this.supervisorReadyPromise) {
      this.supervisorReadyPromise = ensureSupervisor(this.paths, this.supervisorProcess)
        .then((child) => {
          this.supervisorProcess = child;
          this.updateInitState({
            supervisorReady: true,
            initError: null
          });
        })
        .catch((error) => {
          const message = error instanceof Error ? error.message : String(error);
          this.updateInitState({
            supervisorReady: false,
            initError: message
          });
          throw error;
        })
        .finally(() => {
          this.supervisorReadyPromise = null;
        });
    }
    await this.supervisorReadyPromise;
  }

  private broadcast(event: DesktopEvent): void {
    for (const window of this.windows) {
      if (!window.isDestroyed()) {
        window.webContents.send(IPC_CHANNELS.event, event);
      }
    }
  }

  private updateInitState(next: Partial<DesktopInitState>): void {
    const updated: DesktopInitState = {
      ...this.initState,
      ...next
    };
    if (JSON.stringify(updated) === JSON.stringify(this.initState)) {
      return;
    }
    this.initState = updated;
    this.broadcast({
      name: "desktop_init_changed",
      payload: updated
    });
  }

  private async ensureSubscription(): Promise<void> {
    if (this.subscription) {
      return;
    }
    if (!this.subscriptionPromise) {
      this.subscriptionPromise = (async () => {
        await this.ensureSupervisorReady();
        const subscription = new RpcSubscription();
        subscription.on("event", (event: DesktopEvent) => {
          this.broadcast(event);
        });
        subscription.on("close", () => {
          this.subscription = null;
          this.updateInitState({
            subscriptionReady: false
          });
          this.scheduleReconnect();
        });
        subscription.on("error", (error) => {
          this.subscription = null;
          this.updateInitState({
            subscriptionReady: false,
            initError: error instanceof Error ? error.message : String(error)
          });
          this.scheduleReconnect();
        });
        await subscription.connect("service.subscribe", {}, this.paths.socketPath);
        this.subscription = subscription;
        this.updateInitState({
          supervisorReady: true,
          subscriptionReady: true,
          initError: null
        });
        this.broadcast({
          name: "supervisor_connected",
          payload: {
            socketPath: this.paths.socketPath
          }
        });
      })().catch((error) => {
        const message = error instanceof Error ? error.message : String(error);
        this.updateInitState({
          subscriptionReady: false,
          initError: message
        });
        throw error;
      }).finally(() => {
        this.subscriptionPromise = null;
      });
    }
    await this.subscriptionPromise;
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) {
      return;
    }
    this.reconnectTimer = setTimeout(async () => {
      this.reconnectTimer = null;
      try {
        await this.ensureSubscription();
      } catch {
        this.scheduleReconnect();
      }
    }, 1000);
  }

  async invoke<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    await this.ensureSupervisorReady();
    return (await callRpc(method, params, this.paths.socketPath)) as T;
  }

  async refreshStatus(): Promise<ServiceStatus> {
    const status = await this.invoke<ServiceStatus>("service.status");
    this.status = status;
    this.updateInitState({
      supervisorReady: true,
      initError: null
    });
    return status;
  }

  async refreshConfig(): Promise<ConfigSnapshot> {
    const config = await this.invoke<ConfigSnapshot>("config.get");
    this.config = config;
    return config;
  }

  async refreshDiagnostics(): Promise<DoctorReport> {
    const diagnostics = await this.invoke<DoctorReport>("diagnostics.report");
    this.diagnostics = diagnostics;
    this.updateInitState({
      diagnosticsReady: true
    });
    return diagnostics;
  }

  updateStatus(status: ServiceStatus): void {
    this.status = status;
    this.updateInitState({
      supervisorReady: true,
      initError: null
    });
  }

  updateConfig(config: ConfigSnapshot): void {
    this.config = config;
  }

  clearDiagnostics(): void {
    this.diagnostics = null;
    this.updateInitState({
      diagnosticsReady: false
    });
  }

  async bootstrap(): Promise<DesktopBootstrap> {
    return {
      paths: this.paths,
      status: this.status,
      config: this.config,
      diagnostics: this.diagnostics,
      initState: this.initState
    };
  }

  async dispose(): Promise<void> {
    this.subscription?.close();
    this.subscription = null;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.updateInitState({
      subscriptionReady: false
    });
  }

  async shutdown(): Promise<void> {
    await this.dispose();
    await shutdownSupervisor(this.paths, this.supervisorProcess);
    this.supervisorProcess = null;
  }
}

const controller = new DesktopController();
const hasSingleInstanceLock = app.requestSingleInstanceLock();
const lifecycleState = { isShuttingDown: false };

function focusPrimaryDesktopWindow(): void {
  focusPrimaryWindow(BrowserWindow.getAllWindows());
}

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1560,
    height: 980,
    minWidth: 1200,
    minHeight: 780,
    backgroundColor: "#0b0b0b",
    titleBarStyle: "hiddenInset",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  controller.registerWindow(window);

  window.once("ready-to-show", () => {
    window.show();
  });

  const devServerUrl = process.env.TIYA_DESKTOP_DEV_SERVER_URL;
  if (devServerUrl) {
    window.loadURL(devServerUrl).catch((error) => {
      console.error("Failed to load dev server:", error);
    });
    window.webContents.openDevTools({ mode: "detach" });
  } else {
    window.loadFile(path.join(__dirname, "..", "..", "dist-renderer", "index.html")).catch((error) => {
      console.error("Failed to load renderer build:", error);
    });
  }

  return window;
}

wireSingleInstanceLifecycle({
  app,
  hasSingleInstanceLock,
  focusPrimaryWindow: focusPrimaryDesktopWindow,
});

app.whenReady().then(async () => {
  createWindow();
  controller.startBackgroundInitialization();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
      controller.startBackgroundInitialization();
    }
  });
});

app.on("window-all-closed", () => {
  handleWindowAllClosed({ app, platform: process.platform });
});

app.on("before-quit", (event) => {
  void handleBeforeQuit({
    event,
    state: lifecycleState,
    controller: {
      shutdown: () => controller.shutdown().catch(() => undefined),
    },
    quit: () => {
      app.quit();
    },
  });
});

ipcMain.handle(IPC_CHANNELS.invoke, async (_event, request: { method: string; params?: Record<string, unknown> }) => {
  const params = request.params ?? {};

  switch (request.method) {
    case "desktop.bootstrap":
      return controller.bootstrap();
    case "service.status":
      return controller.refreshStatus();
    case "service.start":
    case "service.stop":
    case "service.restart": {
      const result = await controller.invoke<ServiceStatus | { status: ServiceStatus; output: string; started?: boolean; stopped?: boolean; restarted?: boolean }>(request.method, params);
      if (result && typeof result === "object" && "status" in result) {
        controller.updateStatus(result.status as ServiceStatus);
      }
      return result;
    }
    case "config.get":
      return controller.refreshConfig();
    case "config.validate":
      return controller.invoke<ValidationResult>("config.validate", params);
    case "config.set": {
      const config = await controller.invoke<ConfigSnapshot>("config.set", params);
      controller.updateConfig(config);
      controller.clearDiagnostics();
      return config;
    }
    case "config.setSecret":
      controller.clearDiagnostics();
      return controller.invoke("config.setSecret", params);
    case "config.clearSecret":
      controller.clearDiagnostics();
      return controller.invoke("config.clearSecret", params);
    case "sessions.list":
      return controller.invoke<SessionListResult>("sessions.list", params);
    case "sessions.history":
      return controller.invoke<SessionHistoryResult>("sessions.history", params);
    case "diagnostics.report":
      return controller.refreshDiagnostics();
    case "diagnostics.export":
      return controller.invoke("diagnostics.export", params);
    case "dialog.pickDirectory": {
      const result = await dialog.showOpenDialog({
        properties: ["openDirectory", "createDirectory"]
      });
      return result.canceled ? null : result.filePaths[0] ?? null;
    }
    case "shell.openPath":
      return shell.openPath(String(params.targetPath ?? ""));
    default:
      throw new Error(`Unsupported desktop invoke method: ${request.method}`);
  }
});
