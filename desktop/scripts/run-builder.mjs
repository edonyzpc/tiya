import { mkdir } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { spawn } from "node:child_process";

const desktopRoot = path.resolve(process.cwd());
const cacheRoot = path.join(desktopRoot, ".cache");
const builderCache = path.join(cacheRoot, "electron-builder");
const electronCache = path.join(cacheRoot, "electron");

await mkdir(builderCache, { recursive: true });
await mkdir(electronCache, { recursive: true });

const child = spawn(
  path.join(desktopRoot, "node_modules", ".bin", "electron-builder"),
  ["--config", "electron-builder.yml", ...process.argv.slice(2)],
  {
    cwd: desktopRoot,
    stdio: "inherit",
    env: (() => {
      const env = {
        ...process.env,
        ELECTRON_BUILDER_CACHE: builderCache,
        ELECTRON_CACHE: electronCache
      };
      delete env.TG_PROXY_URL;
      delete env.HTTP_PROXY;
      delete env.HTTPS_PROXY;
      delete env.ALL_PROXY;
      delete env.http_proxy;
      delete env.https_proxy;
      delete env.all_proxy;
      return env;
    })()
  }
);

child.on("exit", (code) => {
  process.exit(code ?? 1);
});
