const { spawn, spawnSync } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const isWindows = process.platform === "win32";
const children = new Set();
let stopping = false;
let devUrl;
let vite;
let electron;

function bin(name) {
  return path.join(root, "node_modules", ".bin", isWindows ? `${name}.cmd` : name);
}

function command(name) {
  return isWindows ? `${name}.cmd` : name;
}

function prepareFirstRunFolders() {
  for (const folder of [
    "settings",
    "applications",
    "older_applications",
    "Application templates",
    "Resumes",
    "scraper_plugins",
    "Backups",
  ]) {
    fs.mkdirSync(path.join(root, folder), { recursive: true });
  }
}

function ensureNodeDependencies() {
  if (fs.existsSync(bin("vite")) && fs.existsSync(bin("electron"))) return;

  console.log("First run setup: installing npm dependencies...");
  const result = spawnSync(command("npm"), ["install"], {
    cwd: root,
    stdio: "inherit",
    shell: false,
    windowsHide: true,
  });

  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`npm install failed with exit code ${result.status}`);
  }
}

function start(command, args, options = {}) {
  if (!fs.existsSync(command) && path.isAbsolute(command)) {
    throw new Error(`Required command was not found: ${command}`);
  }
  const child = spawn(command, args, {
    cwd: root,
    // Vite and Electron do not need terminal input. Giving child processes the
    // parent console's stdin can leave Windows console mode altered after they
    // exit, which breaks PowerShell/PSReadLine history and line editing.
    stdio: ["ignore", "inherit", "inherit"],
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
    // Wait for the complete cmd -> Vite/Electron process tree to disappear.
    // The previous fire-and-forget taskkill allowed this launcher to exit first,
    // intermittently leaving Vite behind on port 5173.
    spawnSync("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
      windowsHide: true,
      stdio: "ignore",
      timeout: 10000,
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

function canListen(port) {
  return new Promise((resolve) => {
    const probe = net.createServer();
    probe.unref();
    probe.once("error", () => resolve(false));
    probe.listen({ host: "127.0.0.1", port, exclusive: true }, () => {
      probe.close(() => resolve(true));
    });
  });
}

async function findAvailablePort(firstPort = 5173, attempts = 50) {
  for (let port = firstPort; port < firstPort + attempts; port += 1) {
    if (await canListen(port)) return port;
  }
  throw new Error(`No free development port found from ${firstPort} to ${firstPort + attempts - 1}.`);
}

try {
  prepareFirstRunFolders();
  ensureNodeDependencies();
} catch (error) {
  console.error(`Startup setup failed: ${error.message}`);
  process.exit(1);
}

function stopAll() {
  if (stopping) return;
  stopping = true;
  for (const child of Array.from(children)) {
    killTree(child);
  }
}

function exitCleanly(code) {
  stopAll();
  process.exit(code);
}

process.on("SIGINT", () => {
  exitCleanly(130);
});

process.on("SIGTERM", () => {
  exitCleanly(143);
});

// Final safety net for non-signal exits after children have been started.
process.on("exit", stopAll);

async function launch() {
  const port = await findAvailablePort();
  devUrl = `http://127.0.0.1:${port}`;
  if (port !== 5173) {
    console.log(`Port 5173 is still being released; starting JSE on ${port} instead.`);
  }

  vite = start(bin("vite"), ["--host", "127.0.0.1", "--port", String(port), "--strictPort"]);
  vite.on("exit", (code) => {
    exitCleanly(code || 0);
  });

  await waitForUrl(devUrl);
  electron = start(bin("electron"), ["."], {
      env: {
        ...process.env,
        VITE_DEV_SERVER_URL: devUrl,
      },
  });
  electron.on("exit", (code) => {
    exitCleanly(code || 0);
  });
}

launch().catch((error) => {
    console.error(error.message);
    exitCleanly(1);
});
