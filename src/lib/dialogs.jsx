/** In-app confirm/notice/prompt. window.confirm freezes input in this Electron build. */
import React from "react";

// In-app replacements for window.confirm / prompt / alert. Electron's native
// synchronous dialogs intermittently break mouse/keyboard input in the window
// after they close (a long-standing Chromium-on-Windows focus bug), which left
// parts of the UI unclickable until the window was refocused. The App mounts a
// dialog host into dialogBridge; these helpers fall back to the native dialogs
// only if the host is somehow not mounted.
const dialogBridge = { current: null };

function requestDialog(request) {
  if (dialogBridge.current) return dialogBridge.current(request);
  if (request.kind === "confirm") return Promise.resolve(window.confirm(request.message));
  if (request.kind === "prompt") return Promise.resolve(window.prompt(request.message || request.title));
  window.alert(request.message);
  return Promise.resolve(true);
}

const appConfirm = (options) => requestDialog({ kind: "confirm", ...options });

const appNotice = (options) => requestDialog({ kind: "notice", ...options });

// Kept alongside the other two even with no current caller: window.prompt()
// freezes input in this Electron build, so the in-app prompt has to stay
// available and obvious rather than be rediscovered the hard way.
const appPrompt = (options) => requestDialog({ kind: "prompt", ...options });

export { dialogBridge, requestDialog, appConfirm, appNotice, appPrompt };
