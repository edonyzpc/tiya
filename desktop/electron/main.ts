import { app, BrowserWindow, dialog, ipcMain, shell } from "electron";
import path from "node:path";
import type { ChildProcess } from "node:child_process";

import {
  IPC_CHANNELS,
  type ConfigSnapshot,
  type DesktopBootstrap,
  type DesktopEvent,
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
  private supervisorReadyPromise: Promise<void> | null = null;
  private reconnectTimer: NodeJS.Timeout | null = null;
  private supervisorProcess: ChildProcess | null = null;
  readonly paths = resolveDesktopPaths();

  registerWindow(window: BrowserWindow): void {
    this.windows.add(window);
    window.on("closed", () => {
      this.windows.delete(window);
    });
  }

  async initialize(): Promise<void> {
    await this.ensureSupervisorReady();
    await this.ensureSubscription();
  }

  private async ensureSupervisorReady(): Promise<void> {
    if (!this.supervisorReadyPromise) {
      this.supervisorReadyPromise = ensureSupervisor(this.paths, this.supervisorProcess)
        .then((child) => {
          this.supervisorProcess = child;
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
          this.scheduleReconnect();
        });
        subscription.on("error", () => {
          this.subscription = null;
          this.scheduleReconnect();
        });
        await subscription.connect("service.subscribe", {}, this.paths.socketPath);
        this.subscription = subscription;
        this.broadcast({
          name: "supervisor_connected",
          payload: {
            socketPath: this.paths.socketPath
          }
        });
      })().finally(() => {
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
    if (method !== "service.subscribe") {
      await this.ensureSubscription();
    }
    return (await callRpc(method, params, this.paths.socketPath)) as T;
  }

  async bootstrap(): Promise<DesktopBootstrap> {
    const [status, config, diagnostics] = await Promise.all([
      this.invoke<ServiceStatus>("service.status"),
      this.invoke<ConfigSnapshot>("config.get"),
      this.invoke<DoctorReport>("diagnostics.report")
    ]);

    return {
      paths: this.paths,
      status,
      config,
      diagnostics
    };
  }

  async dispose(): Promise<void> {
    this.subscription?.close();
    this.subscription = null;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
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
  try {
    await controller.initialize();
  } catch (error) {
    console.error("Failed to initialize desktop controller:", error);
  }
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
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
    case "service.start":
    case "service.stop":
    case "service.restart":
      return controller.invoke<ServiceStatus | { status: ServiceStatus; output: string; started?: boolean; stopped?: boolean; restarted?: boolean }>(request.method, params);
    case "config.get":
      return controller.invoke<ConfigSnapshot>("config.get");
    case "config.validate":
      return controller.invoke<ValidationResult>("config.validate", params);
    case "config.set":
      return controller.invoke<ConfigSnapshot>("config.set", params);
    case "config.setSecret":
      return controller.invoke("config.setSecret", params);
    case "config.clearSecret":
      return controller.invoke("config.clearSecret", params);
    case "sessions.list":
      return controller.invoke<SessionListResult>("sessions.list", params);
    case "sessions.history":
      return controller.invoke<SessionHistoryResult>("sessions.history", params);
    case "diagnostics.report":
      return controller.invoke<DoctorReport>("diagnostics.report");
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
