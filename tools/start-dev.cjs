const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const devUrl = "http://127.0.0.1:5173";
const isWindows = process.platform === "win32";

function bin(name) {
  return path.join(root, "node_modules", ".bin", isWindows ? `${name}.cmd` : name);
}

function start(command, args, options = {}) {
  return spawn(command, args, {
    cwd: root,
    stdio: "inherit",
    shell: isWindows,
    windowsHide: true,
    ...options,
  });
}

function waitForUrl(url, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;

  return new Promise((resolve, reject) => {
    const tryOnce = () => {
      const request = http.get(url, (response) => {
        response.resume();
        resolve();
      });

      request.on("error", () => {
        if (Date.now() > deadline) {
          reject(new Error(`Timed out waiting for ${url}`));
          return;
        }
        setTimeout(tryOnce, 350);
      });

      request.setTimeout(1000, () => {
        request.destroy();
      });
    };

    tryOnce();
  });
}

const vite = start(bin("vite"), ["--host", "127.0.0.1"]);
let electron;

function stopAll() {
  if (electron && !electron.killed) electron.kill();
  if (!vite.killed) vite.kill();
}

process.on("SIGINT", () => {
  stopAll();
  process.exit(130);
});

process.on("SIGTERM", () => {
  stopAll();
  process.exit(143);
});

vite.on("exit", (code) => {
  if (electron) stopAll();
  process.exit(code || 0);
});

waitForUrl(devUrl)
  .then(() => {
    electron = start(bin("electron"), ["."], {
      env: {
        ...process.env,
        VITE_DEV_SERVER_URL: devUrl,
      },
    });
    electron.on("exit", (code) => {
      stopAll();
      process.exit(code || 0);
    });
  })
  .catch((error) => {
    console.error(error.message);
    stopAll();
    process.exit(1);
  });
