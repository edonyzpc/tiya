import { readFileSync } from "node:fs";

export type DesktopGpuMode = "disabled" | "enabled";

export const DESKTOP_GPU_MODE_ENV_KEY = "TIYA_DESKTOP_GPU_MODE";
export const DEFAULT_DESKTOP_GPU_MODE: DesktopGpuMode = "disabled";

type ElectronAppLike = {
  disableHardwareAcceleration(): void;
  commandLine: {
    appendSwitch(name: string): void;
  };
};

export function normalizeDesktopGpuMode(raw: string | null | undefined): DesktopGpuMode {
  const value = (raw ?? "").trim().toLowerCase();
  if (value === "enabled") {
    return "enabled";
  }
  return DEFAULT_DESKTOP_GPU_MODE;
}

export function parseEnvLine(line: string): [string, string] | null {
  let stripped = line.trim();
  if (!stripped || stripped.startsWith("#")) {
    return null;
  }
  if (stripped.startsWith("export ")) {
    stripped = stripped.slice("export ".length).trimStart();
  }
  const separatorIndex = stripped.indexOf("=");
  if (separatorIndex < 0) {
    return null;
  }

  const key = stripped.slice(0, separatorIndex).trim();
  if (!key) {
    return null;
  }

  let value = stripped.slice(separatorIndex + 1).trim();
  if (value.length >= 2 && (value.startsWith('"') || value.startsWith("'")) && value.endsWith(value[0])) {
    value = value.slice(1, -1);
  } else {
    const commentIndex = value.indexOf(" #");
    if (commentIndex >= 0) {
      value = value.slice(0, commentIndex).trimEnd();
    }
  }
  return [key, value];
}

export function readDesktopGpuModeFromEnvFile(
  envFilePath: string,
  reader: (path: string, encoding: BufferEncoding) => string = readFileSync
): DesktopGpuMode {
  try {
    const content = reader(envFilePath, "utf8");
    for (const line of content.split(/\r?\n/)) {
      const parsed = parseEnvLine(line);
      if (!parsed) {
        continue;
      }
      const [key, value] = parsed;
      if (key === DESKTOP_GPU_MODE_ENV_KEY) {
        return normalizeDesktopGpuMode(value);
      }
    }
  } catch {
    return DEFAULT_DESKTOP_GPU_MODE;
  }
  return DEFAULT_DESKTOP_GPU_MODE;
}

export function resolveDesktopGpuMode(params: {
  processEnv?: NodeJS.ProcessEnv;
  envFilePath: string;
  reader?: (path: string, encoding: BufferEncoding) => string;
}): DesktopGpuMode {
  const { processEnv = process.env, envFilePath, reader } = params;
  const explicit = processEnv[DESKTOP_GPU_MODE_ENV_KEY];
  if (explicit && explicit.trim()) {
    return normalizeDesktopGpuMode(explicit);
  }
  return readDesktopGpuModeFromEnvFile(envFilePath, reader);
}

export function applyDesktopGpuMode(app: ElectronAppLike, mode: DesktopGpuMode): void {
  if (mode !== "disabled") {
    return;
  }
  app.disableHardwareAcceleration();
  app.commandLine.appendSwitch("disable-gpu");
}
