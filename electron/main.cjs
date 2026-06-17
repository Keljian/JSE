const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const rootDir = path.resolve(__dirname, "..");
const bridgePath = path.join(rootDir, "python_bridge.py");
const runningTasks = new Map();
const bridgeChildren = new Set();

// Persistent worker for the one-shot bridge:invoke path. One long-lived Python process
// handles all invokes over a single stdin/stdout pipe (one thread per request on the
// Python side), so interpreter startup, imports and the DB warmup are paid once per
// session instead of per call. Long-running cancellable tasks still spawn per task.
let isQuitting = false;
let bridgeWorker = null;
let workerStdoutBuffer = "";
let workerSeq = 0;
let workerRestartTimer = null;
const pendingRequests = new Map();
const userDataDir = path.join(rootDir, "settings");
const cacheDir = path.join(app.getPath("temp"), `JobApplicationAssistantCache-${process.pid}`);

app.setPath("userData", userDataDir);
app.setPath("cache", cacheDir);
app.disableHardwareAcceleration();
app.commandLine.appendSwitch("disable-gpu");
app.commandLine.appendSwitch("disable-gpu-compositing");
app.commandLine.appendSwitch("disable-software-rasterizer");
app.commandLine.appendSwitch("disable-features", "SharedDictionary,UseSkiaRenderer,VizDisplayCompositor");

function getPythonCommand() {
  const bundledPython = path.join(rootDir, "build", "python", "python.exe");
  if (fs.existsSync(bundledPython)) {
    return bundledPython;
  }
  if (process.platform === "darwin") {
    const macArch = process.arch === "arm64" ? "macos-arm64" : "macos-x64";
    const bundledMacPython = path.join(rootDir, "build", "python", macArch, "bin", "python3");
    if (fs.existsSync(bundledMacPython)) {
      return bundledMacPython;
    }
  }
  return process.env.PYTHON || "python";
}

function copySeedItem(source, destination) {
  if (!fs.existsSync(source) || fs.existsSync(destination)) return;
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.cpSync(source, destination, { recursive: true });
}

function prepareWritableWorkspace() {
  fs.mkdirSync(userDataDir, { recursive: true });
  fs.mkdirSync(path.join(rootDir, "applications"), { recursive: true });
  fs.mkdirSync(path.join(rootDir, "older_applications"), { recursive: true });
  for (const item of [
    "job_applications.db",
    "search_terms.json",
    "Application templates",
    "Resumes"
  ]) {
    copySeedItem(path.join(rootDir, item), path.join(userDataDir, item));
  }
}

function killProcessTree(child) {
  if (!child || child.killed) return;
  if (process.platform === "win32") {
    spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
      windowsHide: true,
      stdio: "ignore"
    });
  } else {
    child.kill("SIGKILL");
  }
}

function spawnBridgeProcess(command) {
  const child = spawn(getPythonCommand(), [bridgePath, command], {
    cwd: userDataDir,
    env: {
      ...process.env,
      JSE_APP_ROOT: rootDir,
      JSE_DATA_DIR: userDataDir,
      PYTHONPATH: [rootDir, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      PYTHONNOUSERSITE: "1"
    },
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true
  });
  bridgeChildren.add(child);
  child.on("close", () => bridgeChildren.delete(child));
  child.on("error", () => bridgeChildren.delete(child));
  return child;
}

function sendTaskEvent(sender, taskId, message) {
  if (!sender || sender.isDestroyed()) return;
  sender.send(`task:event:${taskId}`, message);
}

function cancelAllBridgeProcesses() {
  for (const child of bridgeChildren) {
    killProcessTree(child);
  }
  runningTasks.clear();
  bridgeChildren.clear();
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1360,
    height: 900,
    minWidth: 1080,
    minHeight: 720,
    backgroundColor: "#f7f7f2",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  // A file dropped outside a drop zone (or any stray link) must never navigate
  // the window away from the app — that replaced the UI until restart.
  win.webContents.on("will-navigate", (event, url) => {
    const devUrl = process.env.VITE_DEV_SERVER_URL;
    if (!devUrl || !url.startsWith(devUrl)) event.preventDefault();
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//i.test(url)) {
      shell.openExternal(url);
    }
    return { action: "deny" };
  });

  if (process.env.VITE_DEV_SERVER_URL) {
    win.loadURL(process.env.VITE_DEV_SERVER_URL);
  } else {
    win.loadFile(path.join(rootDir, "dist", "index.html"));
  }
}

function startBridgeWorker() {
  const child = spawn(getPythonCommand(), [bridgePath, "--serve"], {
    cwd: userDataDir,
    env: {
      ...process.env,
      JSE_APP_ROOT: rootDir,
      JSE_DATA_DIR: userDataDir,
      PYTHONPATH: [rootDir, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      PYTHONNOUSERSITE: "1"
    },
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true
  });

  workerStdoutBuffer = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", () => {});

  child.stdout.on("data", (chunk) => {
    workerStdoutBuffer += chunk;
    const lines = workerStdoutBuffer.split(/\r?\n/);
    workerStdoutBuffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      let event;
      try {
        event = JSON.parse(line);
      } catch {
        continue;
      }
      if (event.id == null) continue;
      const pending = pendingRequests.get(event.id);
      if (!pending) continue;
      if (event.type === "result") {
        pendingRequests.delete(event.id);
        pending.resolve(event.data);
      } else if (event.type === "error") {
        pendingRequests.delete(event.id);
        pending.reject(new Error(event.message));
      }
      // "log" events on the invoke path have no consumer; ignore them.
    }
  });

  child.on("error", () => handleWorkerExit(child));
  child.on("close", () => handleWorkerExit(child));

  bridgeWorker = child;
  return child;
}

function handleWorkerExit(child) {
  if (bridgeWorker !== child) return; // a newer worker already took over
  bridgeWorker = null;
  for (const pending of pendingRequests.values()) {
    pending.reject(new Error("Python bridge worker exited."));
  }
  pendingRequests.clear();
  if (isQuitting || workerRestartTimer) return;
  workerRestartTimer = setTimeout(() => {
    workerRestartTimer = null;
    startBridgeWorker();
  }, 500);
}

function invokeViaWorker(command, payload = {}) {
  return new Promise((resolve, reject) => {
    if (!bridgeWorker) startBridgeWorker();
    if (!bridgeWorker || !bridgeWorker.stdin.writable) {
      reject(new Error("Python bridge worker is not available."));
      return;
    }
    const id = ++workerSeq;
    pendingRequests.set(id, { resolve, reject });
    try {
      bridgeWorker.stdin.write(`${JSON.stringify({ id, command, payload })}\n`);
    } catch (error) {
      pendingRequests.delete(id);
      reject(error);
    }
  });
}

ipcMain.handle("bridge:invoke", (_event, command, payload) => invokeViaWorker(command, payload));

ipcMain.handle("dialog:resume", async () => {
  const result = await dialog.showOpenDialog({
    title: "Select resume",
    filters: [{ name: "Word Documents", extensions: ["docx"] }],
    properties: ["openFile"]
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle("dialog:template", async () => {
  const result = await dialog.showOpenDialog({
    title: "Select document template",
    filters: [{ name: "Word Documents", extensions: ["docx"] }],
    properties: ["openFile"]
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle("dialog:scraperPlugin", async () => {
  const result = await dialog.showOpenDialog({
    title: "Select scraper plugin folder or manifest",
    filters: [{ name: "Scraper Plugin Manifest", extensions: ["json"] }],
    properties: ["openFile", "openDirectory"]
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle("dialog:folder", async (_event, title = "Select folder") => {
  const result = await dialog.showOpenDialog({
    title,
    properties: ["openDirectory", "createDirectory"]
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle("shell:openExternal", (_event, url) => {
  if (!/^https?:\/\//i.test(url) && !/^mailto:/i.test(url)) return false;
  return shell.openExternal(url);
});
ipcMain.handle("shell:showPath", (_event, filePath) => {
  const abs = path.isAbsolute(filePath) ? filePath : path.join(rootDir, filePath);
  return shell.showItemInFolder(abs);
});
ipcMain.handle("shell:downloadFile", async (_event, filePath) => {
  const abs = path.isAbsolute(filePath) ? filePath : path.join(rootDir, filePath);
  if (!fs.existsSync(abs)) {
    throw new Error(`Document file not found: ${abs}`);
  }
  const result = await dialog.showSaveDialog({
    title: "Download document",
    defaultPath: path.join(app.getPath("downloads"), path.basename(abs)),
    properties: ["createDirectory"]
  });
  if (result.canceled || !result.filePath) {
    return { canceled: true };
  }
  fs.copyFileSync(abs, result.filePath);
  return { canceled: false, path: result.filePath };
});
ipcMain.on("task:start", (event, taskId, command, payload) => {
  const child = spawnBridgeProcess(command);

  runningTasks.set(taskId, child);

  let stdoutBuffer = "";
  let stderr = "";
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");

  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });

  child.stdout.on("data", (chunk) => {
    stdoutBuffer += chunk;
    const lines = stdoutBuffer.split(/\r?\n/);
    stdoutBuffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      try {
        sendTaskEvent(event.sender, taskId, JSON.parse(line));
      } catch {
        sendTaskEvent(event.sender, taskId, { type: "log", message: line });
      }
    }
  });

  child.on("error", (error) => {
    sendTaskEvent(event.sender, taskId, { type: "error", message: error.message });
  });

  child.on("close", (code, signal) => {
    runningTasks.delete(taskId);
    if (stdoutBuffer.trim()) {
      try {
        sendTaskEvent(event.sender, taskId, JSON.parse(stdoutBuffer.trim()));
      } catch {
        sendTaskEvent(event.sender, taskId, { type: "log", message: stdoutBuffer.trim() });
      }
    }
    if (signal || code !== 0) {
      sendTaskEvent(event.sender, taskId, {
        type: "error",
        message: signal ? "Task cancelled." : (stderr.trim() || `Task exited with code ${code}`)
      });
    }
  });

  child.stdin.end(JSON.stringify(payload));
});

ipcMain.on("task:cancel", (_event, taskId) => {
  const child = runningTasks.get(taskId);
  if (child) {
    killProcessTree(child);
    runningTasks.delete(taskId);
  }
});

ipcMain.on("task:cancelAll", () => {
  cancelAllBridgeProcesses();
});

app.whenReady().then(() => {
  prepareWritableWorkspace();
  startBridgeWorker();
  createWindow();
});

app.on("before-quit", () => {
  isQuitting = true;
  cancelAllBridgeProcesses();
  if (workerRestartTimer) {
    clearTimeout(workerRestartTimer);
    workerRestartTimer = null;
  }
  killProcessTree(bridgeWorker);
  bridgeWorker = null;
});

app.on("will-quit", () => {
  try {
    fs.rmSync(cacheDir, { recursive: true, force: true });
  } catch {
    // Cache cleanup is best-effort; Chromium may still be releasing files.
  }
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
