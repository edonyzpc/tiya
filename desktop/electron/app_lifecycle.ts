export interface DesktopWindowLike {
  isMinimized(): boolean;
  restore(): void;
  show(): void;
  focus(): void;
}

export interface DesktopAppLike {
  quit(): void;
  on(event: string, listener: (...args: unknown[]) => void): void;
}

export interface DesktopShutdownControllerLike {
  shutdown(): Promise<void>;
}

export interface QuitEventLike {
  preventDefault(): void;
}

export interface DesktopLifecycleState {
  isShuttingDown: boolean;
}

export function focusPrimaryWindow(windows: readonly DesktopWindowLike[]): void {
  const window = windows[0];
  if (!window) {
    return;
  }
  if (window.isMinimized()) {
    window.restore();
  }
  window.show();
  window.focus();
}

export function wireSingleInstanceLifecycle(params: {
  app: DesktopAppLike;
  hasSingleInstanceLock: boolean;
  focusPrimaryWindow: () => void;
}): void {
  const { app, hasSingleInstanceLock, focusPrimaryWindow: focus } = params;
  if (!hasSingleInstanceLock) {
    app.quit();
    return;
  }
  app.on("second-instance", () => {
    focus();
  });
}

export function handleWindowAllClosed(params: {
  app: Pick<DesktopAppLike, "quit">;
  platform: string;
}): void {
  if (params.platform !== "darwin") {
    params.app.quit();
  }
}

export async function handleBeforeQuit(params: {
  event: QuitEventLike;
  state: DesktopLifecycleState;
  controller: DesktopShutdownControllerLike;
  quit: () => void;
}): Promise<void> {
  const { event, state, controller, quit } = params;
  if (state.isShuttingDown) {
    return;
  }
  event.preventDefault();
  state.isShuttingDown = true;
  try {
    await controller.shutdown();
  } finally {
    quit();
  }
}
