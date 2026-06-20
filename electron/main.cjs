const { app, BrowserWindow, dialog, ipcMain, shell } = require("electron");
const { autoUpdater } = require("electron-updater");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");

const rootDir = path.resolve(__dirname, "..");
const appIconPath = path.join(rootDir, "assets", "jse-icon.png");
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
const legacyUserDataDir = path.join(rootDir, "settings");
const userDataDir = app.isPackaged ? app.getPath("userData") : legacyUserDataDir;
const runtimeRootDir = app.isPackaged ? userDataDir : rootDir;
const cacheDir = path.join(app.getPath("temp"), `JSECache-${process.pid}`);
const updateStatePath = path.join(userDataDir, "update-state.json");
const UPDATE_CHECK_INTERVAL_MS = 3 * 24 * 60 * 60 * 1000;
const SELENIUM_GC_INTERVAL_MS = 10 * 60 * 1000;
let updateCheckTimer = null;
let updateDownloadRequested = false;
let manualUpdateCheck = false;
let currentUpdateStatus = { status: "idle" };
let seleniumGcInterval = null;
let seleniumGcKickTimer = null;
let seleniumGcRunning = false;

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
  if (process.platform === "linux") {
    const linuxArch = process.arch === "arm64" ? "linux-arm64" : "linux-x64";
    const bundledLinuxPython = path.join(rootDir, "build", "python", linuxArch, "bin", "python3");
    if (fs.existsSync(bundledLinuxPython)) {
      return bundledLinuxPython;
    }
  }
  return process.env.PYTHON || "python";
}

function detectChrome() {
  const candidates = process.platform === "win32"
    ? [
        process.env.PROGRAMFILES && path.join(process.env.PROGRAMFILES, "Google", "Chrome", "Application", "chrome.exe"),
        process.env["PROGRAMFILES(X86)"] && path.join(process.env["PROGRAMFILES(X86)"], "Google", "Chrome", "Application", "chrome.exe"),
        process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, "Google", "Chrome", "Application", "chrome.exe")
      ]
    : process.platform === "darwin"
      ? ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
      : ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser"];
  const chromePath = candidates.filter(Boolean).find((candidate) => fs.existsSync(candidate));
  return { found: Boolean(chromePath), path: chromePath || "" };
}

function copySeedItem(source, destination) {
  if (!fs.existsSync(source) || fs.existsSync(destination)) return;
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.cpSync(source, destination, { recursive: true });
}

function migratePackagedWorkspace() {
  if (!app.isPackaged || path.resolve(userDataDir) === path.resolve(legacyUserDataDir)) return;
  fs.mkdirSync(userDataDir, { recursive: true });

  // Older builds wrote user data inside resources/app. An NSIS update replaces
  // that directory, so copy each legacy item to persistent app data before the
  // first update can be accepted. Existing destination items always win.
  if (fs.existsSync(legacyUserDataDir)) {
    for (const item of fs.readdirSync(legacyUserDataDir)) {
      copySeedItem(path.join(legacyUserDataDir, item), path.join(userDataDir, item));
    }
  }
  for (const item of ["applications", "older_applications"]) {
    copySeedItem(path.join(rootDir, item), path.join(runtimeRootDir, item));
  }
}

function prepareWritableWorkspace() {
  migratePackagedWorkspace();
  fs.mkdirSync(userDataDir, { recursive: true });
  fs.mkdirSync(path.join(runtimeRootDir, "applications"), { recursive: true });
  fs.mkdirSync(path.join(runtimeRootDir, "older_applications"), { recursive: true });
  for (const item of [
    "job_applications.db",
    "search_terms.json",
    "Application templates",
    "Resumes"
  ]) {
    copySeedItem(path.join(rootDir, item), path.join(userDataDir, item));
  }
}

function publishUpdateStatus(status) {
  currentUpdateStatus = status;
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) win.webContents.send("update:status", status);
  }
}

function readLastUpdateCheck() {
  try {
    const state = JSON.parse(fs.readFileSync(updateStatePath, "utf8"));
    const checkedAt = new Date(state.last_checked_at).getTime();
    return Number.isFinite(checkedAt) ? checkedAt : 0;
  } catch {
    return 0;
  }
}

function recordUpdateCheck() {
  try {
    fs.mkdirSync(path.dirname(updateStatePath), { recursive: true });
    fs.writeFileSync(updateStatePath, JSON.stringify({ last_checked_at: new Date().toISOString() }), "utf8");
  } catch {
    // A failed timestamp write should not prevent the update check itself.
  }
}

async function checkForUpdates({ manual = false } = {}) {
  if (isQuitting) return currentUpdateStatus;
  if (!app.isPackaged) {
    const status = {
      status: "development",
      version: app.getVersion(),
      message: "Update checks are available in installed builds of JSE."
    };
    if (manual) publishUpdateStatus(status);
    return status;
  }
  recordUpdateCheck();
  if (manual) {
    manualUpdateCheck = true;
    publishUpdateStatus({ status: "checking" });
  }
  try {
    await autoUpdater.checkForUpdates();
  } catch (error) {
    if (manualUpdateCheck) {
      manualUpdateCheck = false;
      publishUpdateStatus({
        status: "error",
        message: error?.message || "JSE could not check for updates."
      });
    }
  }
  return currentUpdateStatus;
}

function scheduleUpdateChecks() {
  if (!app.isPackaged) return;
  const elapsed = Math.max(0, Date.now() - readLastUpdateCheck());
  const delay = Math.max(0, UPDATE_CHECK_INTERVAL_MS - elapsed);
  updateCheckTimer = setTimeout(async () => {
    await checkForUpdates();
    if (!isQuitting) {
      updateCheckTimer = setInterval(checkForUpdates, UPDATE_CHECK_INTERVAL_MS);
    }
  }, delay);
}

function configureAutoUpdater() {
  if (!app.isPackaged) return;
  autoUpdater.autoDownload = false;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.allowDowngrade = false;

  autoUpdater.on("update-available", (info) => {
    manualUpdateCheck = false;
    publishUpdateStatus({
      status: "available",
      version: info.version,
      releaseName: info.releaseName || "",
      releaseDate: info.releaseDate || ""
    });
  });
  autoUpdater.on("update-not-available", () => {
    const wasManual = manualUpdateCheck;
    manualUpdateCheck = false;
    updateDownloadRequested = false;
    if (wasManual) {
      publishUpdateStatus({
        status: "current",
        version: app.getVersion(),
        checkedAt: new Date().toISOString()
      });
    } else {
      currentUpdateStatus = { status: "idle" };
    }
  });
  autoUpdater.on("download-progress", (progress) => {
    publishUpdateStatus({
      status: "downloading",
      percent: Math.max(0, Math.min(100, Math.round(progress.percent || 0))),
      bytesPerSecond: progress.bytesPerSecond || 0
    });
  });
  autoUpdater.on("update-downloaded", (info) => {
    updateDownloadRequested = false;
    publishUpdateStatus({ status: "ready", version: info.version });
  });
  autoUpdater.on("error", (error) => {
    if (!updateDownloadRequested && !manualUpdateCheck) return;
    updateDownloadRequested = false;
    manualUpdateCheck = false;
    publishUpdateStatus({
      status: "error",
      message: error?.message || "The update could not be downloaded."
    });
  });

  scheduleUpdateChecks();
}

function collectOrphanedSeleniumTrees() {
  if (process.platform !== "win32" || isQuitting || seleniumGcRunning) return;
  seleniumGcRunning = true;
  const powershell = path.join(
    process.env.SystemRoot || "C:\\Windows",
    "System32",
    "WindowsPowerShell",
    "v1.0",
    "powershell.exe"
  );
  const collector = spawn(powershell, [
    "-NoProfile",
    "-NonInteractive",
    "-WindowStyle",
    "Hidden",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    path.join(__dirname, "selenium-gc.ps1")
  ], {
    windowsHide: true,
    stdio: "ignore"
  });
  const finished = () => { seleniumGcRunning = false; };
  collector.once("error", finished);
  collector.once("close", finished);
}

function scheduleSeleniumGarbageCollection(delayMs = 5000) {
  if (process.platform !== "win32" || isQuitting || seleniumGcKickTimer) return;
  seleniumGcKickTimer = setTimeout(() => {
    seleniumGcKickTimer = null;
    collectOrphanedSeleniumTrees();
  }, delayMs);
}

function startSeleniumGarbageCollector() {
  if (process.platform !== "win32") return;
  scheduleSeleniumGarbageCollection(15000);
  seleniumGcInterval = setInterval(collectOrphanedSeleniumTrees, SELENIUM_GC_INTERVAL_MS);
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
      JSE_RUNTIME_ROOT: runtimeRootDir,
      JSE_LEGACY_RUNTIME_ROOT: rootDir,
      PYTHONPATH: [rootDir, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
      PYTHONNOUSERSITE: "1"
    },
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true
  });
  bridgeChildren.add(child);
  child.on("close", () => {
    bridgeChildren.delete(child);
    scheduleSeleniumGarbageCollection();
  });
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
    icon: appIconPath,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  win.removeMenu();

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
      JSE_RUNTIME_ROOT: runtimeRootDir,
      JSE_LEGACY_RUNTIME_ROOT: rootDir,
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
ipcMain.handle("system:prerequisites", () => ({
  app_version: app.getVersion(),
  packaged: app.isPackaged,
  unsigned_build: true,
  data_dir: userDataDir,
  chrome: detectChrome(),
  python: { found: fs.existsSync(getPythonCommand()), path: getPythonCommand() }
}));
ipcMain.handle("update:getStatus", () => currentUpdateStatus);
ipcMain.handle("update:check", () => checkForUpdates({ manual: true }));
ipcMain.handle("update:download", async () => {
  if (!app.isPackaged || currentUpdateStatus.status !== "available") return false;
  updateDownloadRequested = true;
  publishUpdateStatus({ status: "downloading", percent: 0, bytesPerSecond: 0 });
  try {
    await autoUpdater.downloadUpdate();
    return true;
  } catch (error) {
    updateDownloadRequested = false;
    publishUpdateStatus({
      status: "error",
      message: error?.message || "The update could not be downloaded."
    });
    return false;
  }
});
ipcMain.handle("update:install", () => {
  if (!app.isPackaged || currentUpdateStatus.status !== "ready") return false;
  setImmediate(() => autoUpdater.quitAndInstall(false, true));
  return true;
});

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
  if (process.platform === "darwin" && app.dock) {
    app.dock.setIcon(appIconPath);
  }
  prepareWritableWorkspace();
  startBridgeWorker();
  createWindow();
  configureAutoUpdater();
  startSeleniumGarbageCollector();
});

app.on("before-quit", () => {
  isQuitting = true;
  cancelAllBridgeProcesses();
  if (workerRestartTimer) {
    clearTimeout(workerRestartTimer);
    workerRestartTimer = null;
  }
  if (updateCheckTimer) {
    clearTimeout(updateCheckTimer);
    clearInterval(updateCheckTimer);
    updateCheckTimer = null;
  }
  if (seleniumGcKickTimer) {
    clearTimeout(seleniumGcKickTimer);
    seleniumGcKickTimer = null;
  }
  if (seleniumGcInterval) {
    clearInterval(seleniumGcInterval);
    seleniumGcInterval = null;
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
