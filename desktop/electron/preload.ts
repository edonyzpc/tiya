import { contextBridge, ipcRenderer } from "electron";

import type { ConfigSnapshot, DesktopBridge, DesktopEvent } from "../shared/api";

const IPC_CHANNELS = {
  invoke: "tiya:invoke",
  event: "tiya:event"
} as const;

function invoke<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  return ipcRenderer.invoke(IPC_CHANNELS.invoke, { method, params }) as Promise<T>;
}

const bridge: DesktopBridge = {
  desktop: {
    bootstrap: () => invoke("desktop.bootstrap")
  },
  service: {
    status: () => invoke("service.status"),
    start: () => invoke("service.start"),
    stop: () => invoke("service.stop"),
    restart: () => invoke("service.restart")
  },
  config: {
    get: () => invoke("config.get"),
    validate: (payload: ConfigSnapshot) => invoke("config.validate", { payload }),
    set: (payload: ConfigSnapshot) => invoke("config.set", { payload }),
    setSecret: (value: string) => invoke("config.setSecret", { value }),
    clearSecret: () => invoke("config.clearSecret")
  },
  sessions: {
    list: (params) => invoke("sessions.list", params),
    history: (params) => invoke("sessions.history", params)
  },
  diagnostics: {
    report: () => invoke("diagnostics.report"),
    export: (destinationPath?: string) => invoke("diagnostics.export", destinationPath ? { destinationPath } : {})
  },
  dialog: {
    pickDirectory: () => invoke("dialog.pickDirectory")
  },
  shell: {
    openPath: (targetPath: string) => invoke("shell.openPath", { targetPath })
  },
  events: {
    subscribe(listener: (event: DesktopEvent) => void) {
      const handler = (_event: Electron.IpcRendererEvent, payload: DesktopEvent) => {
        listener(payload);
      };
      ipcRenderer.on(IPC_CHANNELS.event, handler);
      return () => {
        ipcRenderer.off(IPC_CHANNELS.event, handler);
      };
    }
  }
};

contextBridge.exposeInMainWorld("tiyaDesktop", bridge);
