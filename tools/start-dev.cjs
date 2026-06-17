const { spawn } = require("node:child_process");
const http = require("node:http");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const devUrl = "http://127.0.0.1:5173";
const isWindows = process.platform === "win32";
const children = new Set();
let stopping = false;

function bin(name) {
  return path.join(root, "node_modules", ".bin", isWindows ? `${name}.cmd` : name);
}

function start(command, args, options = {}) {
  const child = spawn(command, args, {
    cwd: root,
    stdio: "inherit",
    shell: isWindows,
    windowsHide: true,
    ...options,
  });
  children.add(child);
  child.on("exit", () => children.delete(child));
  return child;
}

function killTree(child) {
  if (!child || child.killed) return;
  if (isWindows) {
    spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
      windowsHide: true,
      stdio: "ignore",
    });
    return;
  }
  child.kill("SIGTERM");
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

const vite = start(bin("vite"), ["--host", "127.0.0.1", "--port", "5173", "--strictPort"]);
let electron;

function stopAll() {
  if (stopping) return;
  stopping = true;
  for (const child of Array.from(children)) {
    killTree(child);
  }
}

function exitCleanly(code) {
  stopAll();
  setTimeout(() => process.exit(code), 250);
}

process.on("SIGINT", () => {
  exitCleanly(130);
});

process.on("SIGTERM", () => {
  exitCleanly(143);
});

vite.on("exit", (code) => {
  exitCleanly(code || 0);
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
      exitCleanly(code || 0);
    });
  })
  .catch((error) => {
    console.error(error.message);
    exitCleanly(1);
  });
