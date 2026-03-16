/// <reference types="vite/client" />

import type { DesktopBridge } from "@shared/api";

declare global {
  interface Window {
    tiyaDesktop?: DesktopBridge;
  }
}
