const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("jobAssistant", {
  invoke: (command, payload) => ipcRenderer.invoke("bridge:invoke", command, payload),
  chooseResume: () => ipcRenderer.invoke("dialog:resume"),
  chooseTemplate: () => ipcRenderer.invoke("dialog:template"),
  chooseScraperPlugin: () => ipcRenderer.invoke("dialog:scraperPlugin"),
  chooseFolder: (title) => ipcRenderer.invoke("dialog:folder", title),
  getPathForFile: (file) => webUtils.getPathForFile(file),
  openExternal: (url) => ipcRenderer.invoke("shell:openExternal", url),
  showPath: (filePath) => ipcRenderer.invoke("shell:showPath", filePath),
  downloadFile: (filePath) => ipcRenderer.invoke("shell:downloadFile", filePath),
  stopAllTasks: () => ipcRenderer.send("task:cancelAll"),
  startTask: (command, payload, onEvent) => {
    const taskId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const channel = `task:event:${taskId}`;
    const listener = (_event, message) => onEvent(message);
    ipcRenderer.on(channel, listener);
    ipcRenderer.send("task:start", taskId, command, payload);
    return {
      taskId,
      cancel: () => ipcRenderer.send("task:cancel", taskId),
      unsubscribe: () => ipcRenderer.removeListener(channel, listener)
    };
  }
});
