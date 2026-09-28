"use strict";

const ADAPTER_ID = "org.zen-app-runtime.app-shell";
const ADAPTER_PROTOCOL_VERSION = 2;
const HANDSHAKE_VERSION = 2;
const POLICY_VERSION = 2;
const POLICY_FD_ENV = "ZEN_APP_RUNTIME_APP_SHELL_POLICY_FD";
const HANDSHAKE_FD_ENV = "ZEN_APP_RUNTIME_APP_SHELL_HANDSHAKE_FD";
const TEST_AUTOSTART_ENV = "ZEN_APP_RUNTIME_APP_SHELL_TEST_AUTOSTART";
const TEST_AUTOSTOP_ENV = "ZEN_APP_RUNTIME_APP_SHELL_TEST_AUTOSTOP";
const MAX_POLICY_BYTES = 64 * 1024;
const MAX_FD = 1024 * 1024;
const CHROME_AUTOHIDE_MS = 2200;
const MEDIATION_REQUEST_TTL_MS = 30 * 1000;
const DOWNLOAD_DIALOG_CONTRACT = "@mozilla.org/helperapplauncherdialog;1";
const DOWNLOAD_DIALOG_CLASS_ID = Components.ID(
  "{91a20495-57e7-4d52-9509-717a15e39f8e}"
);
const CONTENT_PERMISSION_KINDS = new Map([
  ["desktop-notification", "notifications"],
]);
const DISCORD_ZEN_INTERNET_THEME_URI =
  "chrome://zenappshell/content/discord-zen-internet.css";
const DISCORD_ZEN_INTERNET_THEME_REVISION =
  "f3a6453942894205c8644c5b0aefb502706a462e";
const AUTH_RETURN_PARAMETERS = new Set([
  "callback",
  "callback_url",
  "continue",
  "next",
  "redirect",
  "redirect_uri",
  "redirect_url",
  "redirecturl",
  "return",
  "return_to",
  "return_url",
  "returnto",
]);
const AUTH_ENROLLMENT_PATH_SEGMENTS = new Set([
  "create-account",
  "register",
  "sign-up",
  "signup",
]);

const ALLOWED_SHORTCUTS = [
  "Alt+F4",
  "Ctrl++",
  "Ctrl+-",
  "Ctrl+0",
  "Ctrl+F",
  "Ctrl+K",
  "Ctrl+P",
  "Ctrl+R",
  "Ctrl+Shift+N",
  "Ctrl+Shift+R",
  "Escape",
];
const BLOCKED_SHORTCUTS = [
  "Ctrl+N",
  "Ctrl+Shift+I",
  "Ctrl+Shift+J",
  "Ctrl+Shift+P",
  "Ctrl+Shift+T",
  "Ctrl+T",
  "F11",
  "F12",
  "F6",
];
const REMAPPED_SHORTCUTS = {
  "Alt+Left": "same-origin-history-back",
  "Alt+Right": "same-origin-history-forward",
  "Ctrl+L": "show-origin-and-security-panel",
  "Ctrl+W": "close-app-window",
};

const state = {
  acceptedLocation: null,
  approvedDownloadLaunchers: new Set(),
  authFlow: null,
  browser: null,
  chromeHideTimer: null,
  displayName: null,
  downloadFactory: null,
  downloadOriginalCID: null,
  downloadRegistrar: null,
  filePickerObserver: null,
  handshakeComplete: false,
  mediationEpoch: 0,
  pendingAuthentication: null,
  pendingExternal: null,
  pendingMediation: null,
  pendingMediationTimer: null,
  policy: null,
  policyDigest: null,
  progressListener: null,
  siteNavigationActive: false,
  siteTheme: null,
  windowObserver: null,
};

function fail(message) {
  throw new Error(message);
}

function requireObject(value, label) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    fail(`${label} must be an object`);
  }
  return value;
}

function jsonString(value) {
  let output = '"';
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    switch (code) {
      case 0x08:
        output += "\\b";
        break;
      case 0x09:
        output += "\\t";
        break;
      case 0x0a:
        output += "\\n";
        break;
      case 0x0c:
        output += "\\f";
        break;
      case 0x0d:
        output += "\\r";
        break;
      case 0x22:
        output += '\\"';
        break;
      case 0x5c:
        output += "\\\\";
        break;
      default:
        if (code < 0x20 || code >= 0x7f) {
          output += `\\u${code.toString(16).padStart(4, "0")}`;
        } else {
          output += value[index];
        }
    }
  }
  return `${output}"`;
}

function canonicalJSONValue(value, depth) {
  if (value === null) {
    return "null";
  }
  if (value === true) {
    return "true";
  }
  if (value === false) {
    return "false";
  }
  if (typeof value === "string") {
    return jsonString(value);
  }
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) {
      fail("policy numbers must be safe integers");
    }
    return String(value);
  }
  const indentation = " ".repeat(depth * 2);
  const childIndentation = " ".repeat((depth + 1) * 2);
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return "[]";
    }
    const items = value.map(
      item => `${childIndentation}${canonicalJSONValue(item, depth + 1)}`
    );
    return `[\n${items.join(",\n")}\n${indentation}]`;
  }
  const object = requireObject(value, "policy value");
  const keys = Object.keys(object).sort();
  if (keys.length === 0) {
    return "{}";
  }
  const entries = keys.map(
    key =>
      `${childIndentation}${jsonString(key)}: ${canonicalJSONValue(
        object[key],
        depth + 1
      )}`
  );
  return `{\n${entries.join(",\n")}\n${indentation}}`;
}

function canonicalJSON(value) {
  return `${canonicalJSONValue(value, 0)}\n`;
}

function expectedPolicy(policy) {
  requireObject(policy, "app-shell policy");
  if (policy.appShellPolicyVersion !== POLICY_VERSION) {
    fail(`appShellPolicyVersion must be ${POLICY_VERSION}`);
  }
  if (
    typeof policy.appId !== "string" ||
    !/^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/.test(policy.appId)
  ) {
    fail("appId is invalid");
  }
  if (typeof policy.startUrl !== "string" || typeof policy.startOrigin !== "string") {
    fail("start URL and origin must be strings");
  }
  let parsed;
  try {
    parsed = new URL(policy.startUrl);
  } catch (_error) {
    fail("startUrl is invalid");
  }
  if (
    parsed.protocol !== "https:" ||
    parsed.username !== "" ||
    parsed.password !== "" ||
    parsed.origin !== policy.startOrigin
  ) {
    fail("startUrl does not match its credential-free HTTPS origin");
  }
  const presentation = requireObject(policy.presentation, "presentation");
  if (presentation.mode !== "Opaque" && presentation.mode !== "Transparent") {
    fail("presentation mode is invalid");
  }
  return {
    appId: policy.appId,
    appShellPolicyVersion: POLICY_VERSION,
    downloads: {
      defaultAction: "deny-unless-privileged-confirmation",
      destination: "system-file-picker",
      maxOutstandingRequests: 1,
      openAfterDownload: false,
      requestBinding: "exact-origin-document-navigation-epoch",
      requestExpiryMs: MEDIATION_REQUEST_TTL_MS,
      silentDownloads: false,
    },
    navigation: {
      authenticationCallbacks: "deny-unless-explicitly-pinned",
      customProtocols: "confirmation-required",
      externalTarget: "system-default-browser",
      internalNavigation: "same-origin",
      popups: "external-by-default",
      topLevelDecisionPoint: "final-commit-including-redirects",
      userOverride: false,
    },
    onboarding: {
      defaultProfileImport: false,
      requiredBeforeSiteContent: true,
      telemetryPrompt: false,
      version: 1,
    },
    permissions: {
      defaultAction: "deny-unless-privileged-prompt",
      maxOutstandingRequests: 1,
      requestBinding: "exact-origin-document-navigation-epoch",
      requestExpiryMs: MEDIATION_REQUEST_TTL_MS,
      requestKinds: {
        camera: "content-activation-and-session-prompt",
        "clipboard-read": "content-activation-and-upstream-paste-consent",
        "clipboard-write": "content-activation-only",
        "file-picker": "content-activation-and-system-picker",
        microphone: "content-activation-and-session-prompt",
        notifications: "content-activation-and-session-prompt",
        "screen-capture": "content-activation-shell-prompt-and-portal",
        "speaker-selection": "content-activation-and-session-prompt",
      },
      revokePendingOn: [
        "navigation",
        "reload",
        "window-close",
        "process-exit",
      ],
      sessionOnly: true,
      silentGrants: false,
    },
    portal: {
      directSourceSelection: false,
      persistence: "none",
      rePromptAfterStop: true,
      screenCast: "xdg-desktop-portal-required-on-wayland",
      transientUserActivation: true,
    },
    presentation: {
      addressBar: "hidden",
      mode: presentation.mode,
      originIndicator: "always-available",
      permissionIndicator: "always-available",
      securityChrome: "minimal",
      sidebar: "hidden",
      tabStrip: "hidden",
      topBar: "minimal-security-chrome",
    },
    shortcuts: {
      allowed: ALLOWED_SHORTCUTS,
      blocked: BLOCKED_SHORTCUTS,
      developerTools: "disabled",
      osReserved: "not-intercepted",
      privilegedPriority: true,
      remapped: REMAPPED_SHORTCUTS,
    },
    startOrigin: policy.startOrigin,
    startUrl: policy.startUrl,
    window: {
      additionalBrowserWindows: "blocked",
      identity: "app-specific",
      normalBrowserChrome: "blocked",
    },
  };
}

function descriptorFromEnvironment(name) {
  const rendered = Services.env.get(name);
  if (!/^[0-9]+$/.test(rendered)) {
    fail(`${name} is missing or not a decimal descriptor`);
  }
  const descriptor = Number(rendered);
  if (
    !Number.isSafeInteger(descriptor) ||
    descriptor < 3 ||
    descriptor > MAX_FD ||
    String(descriptor) !== rendered
  ) {
    fail(`${name} is outside the accepted descriptor range`);
  }
  return descriptor;
}

function descriptorFile(descriptor) {
  const file = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
  file.initWithPath(`/proc/self/fd/${descriptor}`);
  return file;
}

function readDescriptor(descriptor, byteLimit) {
  const input = Cc["@mozilla.org/network/file-input-stream;1"].createInstance(
    Ci.nsIFileInputStream
  );
  const scriptable = Cc[
    "@mozilla.org/scriptableinputstream;1"
  ].createInstance(Ci.nsIScriptableInputStream);
  try {
    input.init(descriptorFile(descriptor), -1, 0, 0);
    scriptable.init(input);
    let payload = "";
    while (true) {
      const available = scriptable.available();
      if (available === 0) {
        break;
      }
      if (payload.length + available > byteLimit) {
        fail("app-shell policy exceeds the size limit");
      }
      payload += scriptable.readBytes(available);
    }
    if (payload.length === 0) {
      fail("app-shell policy is empty");
    }
    return payload;
  } finally {
    try {
      scriptable.close();
    } catch (_error) {
      try {
        input.close();
      } catch (_ignored) {
        // Startup is already failing or the stream was never initialized.
      }
    }
  }
}

function sha256Hex(asciiPayload) {
  const bytes = new Uint8Array(asciiPayload.length);
  for (let index = 0; index < asciiPayload.length; index += 1) {
    const code = asciiPayload.charCodeAt(index);
    if (code > 0x7f) {
      fail("canonical policy bytes must be ASCII");
    }
    bytes[index] = code;
  }
  const hash = Cc["@mozilla.org/security/hash;1"].createInstance(Ci.nsICryptoHash);
  hash.init(Ci.nsICryptoHash.SHA256);
  hash.update(bytes, bytes.length);
  const binary = hash.finish(false);
  let hexadecimal = "";
  for (let index = 0; index < binary.length; index += 1) {
    hexadecimal += binary.charCodeAt(index).toString(16).padStart(2, "0");
  }
  return hexadecimal;
}

function writeDescriptor(descriptor, payload) {
  const output = Cc["@mozilla.org/network/file-output-stream;1"].createInstance(
    Ci.nsIFileOutputStream
  );
  try {
    output.init(descriptorFile(descriptor), 0x02, 0o600, 0);
    let offset = 0;
    while (offset < payload.length) {
      const written = output.write(payload.slice(offset), payload.length - offset);
      if (written <= 0) {
        fail("cannot write app-shell startup handshake");
      }
      offset += written;
    }
  } finally {
    try {
      output.close();
    } catch (_error) {
      // Preserve the original startup failure when initialization did not finish.
    }
  }
}

function parseVerifiedPolicy() {
  const policyDescriptor = descriptorFromEnvironment(POLICY_FD_ENV);
  const handshakeDescriptor = descriptorFromEnvironment(HANDSHAKE_FD_ENV);
  if (policyDescriptor === handshakeDescriptor) {
    fail("policy and handshake descriptors must be distinct");
  }
  const rawPolicy = readDescriptor(policyDescriptor, MAX_POLICY_BYTES);
  let policy;
  try {
    policy = JSON.parse(rawPolicy);
  } catch (_error) {
    fail("app-shell policy is not valid JSON");
  }
  const canonical = canonicalJSON(policy);
  if (rawPolicy !== canonical) {
    fail("app-shell policy is not exact canonical duplicate-free JSON");
  }
  const expected = canonicalJSON(expectedPolicy(policy));
  if (rawPolicy !== expected) {
    fail("app-shell policy does not match the closed v2 contract");
  }
  return {
    digest: sha256Hex(rawPolicy),
    handshakeDescriptor,
    policy,
  };
}

function text(id, value) {
  document.getElementById(id).textContent = value;
}

function setLabel(id, value) {
  document.getElementById(id).value = value;
}

function deriveDisplayName() {
  const hostname = new URL(state.policy.startOrigin).hostname.toLowerCase();
  const genericLabels = new Set([
    "app",
    "apps",
    "listen",
    "mail",
    "music",
    "open",
    "play",
    "web",
    "www",
  ]);
  const labels = hostname.split(".").filter(Boolean);
  const meaningful = labels.filter(
    label => !genericLabels.has(label) && label !== "com" && label !== "org" && label !== "net"
  );
  const source = meaningful[0] || labels[0] || state.policy.appId;
  return source
    .split("-")
    .filter(Boolean)
    .map(part => `${part[0].toUpperCase()}${part.slice(1)}`)
    .join(" ");
}

function installSiteTheme() {
  if (
    state.policy.appId !== "discord" ||
    state.policy.startOrigin !== "https://discord.com" ||
    state.policy.presentation.mode !== "Transparent"
  ) {
    return;
  }
  const service = Cc[
    "@mozilla.org/content/style-sheet-service;1"
  ].getService(Ci.nsIStyleSheetService);
  const uri = Services.io.newURI(DISCORD_ZEN_INTERNET_THEME_URI);
  const registeredByShell = !service.sheetRegistered(uri, service.USER_SHEET);
  if (registeredByShell) {
    service.loadAndRegisterSheet(uri, service.USER_SHEET);
  }
  state.siteTheme = { registeredByShell, service, uri };
  document.documentElement.setAttribute(
    "zen-site-theme",
    "discord-zen-internet"
  );
  window.addEventListener(
    "unload",
    () => {
      const theme = state.siteTheme;
      state.siteTheme = null;
      if (
        theme &&
        theme.registeredByShell &&
        theme.service.sheetRegistered(theme.uri, theme.service.USER_SHEET)
      ) {
        theme.service.unregisterSheet(theme.uri, theme.service.USER_SHEET);
      }
    },
    { once: true }
  );
  dump(
    `ZEN_APP_RUNTIME_APP_SHELL_THEME_READY discord-zen-internet ${DISCORD_ZEN_INTERNET_THEME_REVISION}\n`
  );
}

function clearChromeHideTimer() {
  if (state.chromeHideTimer !== null) {
    window.clearTimeout(state.chromeHideTimer);
    state.chromeHideTimer = null;
  }
}

function chromeOverlayOpen() {
  return [
    "security-panel",
    "auth-panel",
    "external-panel",
    "permission-panel",
    "download-panel",
  ].some(id => !document.getElementById(id).hidden);
}

function hideChrome() {
  clearChromeHideTimer();
  document.documentElement.setAttribute("zen-chrome-visible", "false");
}

function scheduleChromeHide() {
  clearChromeHideTimer();
  if (!state.siteNavigationActive) {
    return;
  }
  state.chromeHideTimer = window.setTimeout(() => {
    state.chromeHideTimer = null;
    if (!chromeOverlayOpen()) {
      document.documentElement.setAttribute("zen-chrome-visible", "false");
    }
  }, CHROME_AUTOHIDE_MS);
}

function showChrome(autoHide = true) {
  if (!state.siteNavigationActive) {
    return;
  }
  clearChromeHideTimer();
  document.documentElement.setAttribute("zen-chrome-visible", "true");
  if (autoHide) {
    scheduleChromeHide();
  }
}

function setNavigationEnabled(enabled) {
  for (const id of ["back-button", "forward-button", "reload-button"]) {
    document.getElementById(id).disabled = !enabled;
  }
}

function updateHistoryButtons() {
  if (!state.browser || !state.siteNavigationActive) {
    setNavigationEnabled(false);
    return;
  }
  document.getElementById("back-button").disabled = !state.browser.canGoBack;
  document.getElementById("forward-button").disabled = !state.browser.canGoForward;
  document.getElementById("reload-button").disabled = false;
}

function updateSecurity(connection, currentOrigin = null) {
  setLabel("security-state", connection);
  if (currentOrigin !== null) {
    setLabel("origin-indicator", currentOrigin);
    text("panel-current-origin", currentOrigin);
  }
  text("panel-connection", connection);
}

function monotonicMilliseconds() {
  return Math.floor(window.performance.now());
}

function requestToken() {
  return Services.uuid
    .generateUUID()
    .toString()
    .replace(/[{}-]/g, "")
    .toLowerCase();
}

function isWaylandSession() {
  const backend = Services.env.get("GDK_BACKEND").toLowerCase();
  return (
    backend.split(",").includes("wayland") &&
    Services.env.get("WAYLAND_DISPLAY") !== ""
  );
}

function currentMediationContext() {
  if (!state.browser || !state.siteNavigationActive) {
    return null;
  }
  let uri;
  let documentId;
  try {
    uri = state.browser.currentURI;
    documentId = String(
      state.browser.browsingContext.currentWindowGlobal.innerWindowId
    );
  } catch (_error) {
    return null;
  }
  if (!uri || !uri.schemeIs("https") || uri.prePath !== state.policy.startOrigin) {
    return null;
  }
  return {
    documentId,
    navigationEpoch: state.mediationEpoch,
    origin: uri.prePath,
  };
}

function sameMediationContext(first, second) {
  return Boolean(
    first &&
      second &&
      first.documentId === second.documentId &&
      first.navigationEpoch === second.navigationEpoch &&
      first.origin === second.origin
  );
}

function clearPendingMediationTimer() {
  if (state.pendingMediationTimer !== null) {
    window.clearTimeout(state.pendingMediationTimer);
    state.pendingMediationTimer = null;
  }
}

function updateMediationIndicator(label = "Ask", detail = "No request is pending") {
  setLabel("permission-button", label);
  document.getElementById("permission-button").setAttribute("tooltiptext", detail);
  text("panel-permission-state", detail);
}

function safelyDeny(callback) {
  try {
    callback();
  } catch (error) {
    dump(`ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_DENY_ERROR ${safeError(error)}\n`);
  }
}

function closeMediationPanels() {
  document.getElementById("permission-panel").hidden = true;
  document.getElementById("download-panel").hidden = true;
}

function invalidatePendingMediation(reason) {
  clearPendingMediationTimer();
  const pending = state.pendingMediation;
  state.pendingMediation = null;
  closeMediationPanels();
  if (pending) {
    safelyDeny(pending.deny);
    dump(
      `ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_INVALIDATED ${pending.surface} ${reason}\n`
    );
  }
  for (const launcher of state.approvedDownloadLaunchers) {
    try {
      launcher.cancel(Cr.NS_BINDING_ABORTED);
    } catch (_error) {
      // A completed or already cancelled launcher is harmless.
    }
  }
  state.approvedDownloadLaunchers.clear();
  updateMediationIndicator();
}

function advanceMediationEpoch(reason) {
  invalidatePendingMediation(reason);
  state.mediationEpoch += 1;
}

function contentUserActivation(requestWindow) {
  try {
    if (requestWindow.windowUtils.isHandlingUserInput) {
      return true;
    }
  } catch (_error) {
    // Fall through to the standardized transient-activation view.
  }
  try {
    return Boolean(requestWindow.navigator.userActivation.isActive);
  } catch (_error) {
    return false;
  }
}

function normalizedMediationKinds(kinds) {
  if (!Array.isArray(kinds) || kinds.length === 0) {
    return null;
  }
  const allowed = new Set([
    "camera",
    "download",
    "microphone",
    "notifications",
    "screen-capture",
    "speaker-selection",
  ]);
  if (!kinds.every(kind => typeof kind === "string" && allowed.has(kind))) {
    return null;
  }
  const normalized = [...new Set(kinds)].sort();
  if (normalized.length !== kinds.length) {
    return null;
  }
  if (
    normalized.length > 1 &&
    (normalized.includes("download") || normalized.includes("screen-capture"))
  ) {
    return null;
  }
  return normalized;
}

function mediationKindLabel(kinds) {
  const labels = {
    camera: "camera",
    download: "download",
    microphone: "microphone",
    notifications: "notifications",
    "screen-capture": "screen sharing",
    "speaker-selection": "audio output selection",
  };
  return kinds.map(kind => labels[kind]).join(" and ");
}

function populateDeviceSelector(id, rowId, devices, noun) {
  const row = document.getElementById(rowId);
  const selector = document.getElementById(id);
  while (selector.firstChild) {
    selector.firstChild.remove();
  }
  if (!Array.isArray(devices) || devices.length === 0) {
    row.hidden = true;
    return false;
  }
  for (let index = 0; index < devices.length; index += 1) {
    const device = devices[index];
    if (
      !device ||
      !Number.isSafeInteger(device.deviceIndex) ||
      typeof device.mediaSource !== "string"
    ) {
      row.hidden = true;
      return false;
    }
    const option = document.createElement("option");
    option.value = String(device.deviceIndex);
    option.textContent = `${noun} ${index + 1}`;
    selector.appendChild(option);
  }
  row.hidden = false;
  selector.selectedIndex = 0;
  return true;
}

function showPermissionMediation(pending) {
  hideChromeOverlays();
  hideChrome();
  text("permission-kind", mediationKindLabel(pending.kinds));
  text("permission-origin", pending.context.origin);
  text(
    "permission-lifetime",
    "One request only; cleared on navigation, reload, close, or 30-second expiry"
  );
  const portal = pending.kinds.includes("screen-capture");
  text(
    "permission-portal-state",
    portal
      ? isWaylandSession()
        ? "Wayland capture continues only through xdg-desktop-portal; no restore token"
        : "A source must be selected explicitly; no persistent capture grant"
      : "Not applicable"
  );
  text(
    "permission-allow",
    portal && isWaylandSession() ? "Continue to system chooser" : "Allow once"
  );
  const details = pending.details || {};
  populateDeviceSelector(
    "permission-camera-device",
    "permission-camera-row",
    details.cameraDevices,
    "Camera"
  );
  populateDeviceSelector(
    "permission-microphone-device",
    "permission-microphone-row",
    details.microphoneDevices,
    "Microphone"
  );
  populateDeviceSelector(
    "permission-shared-audio-device",
    "permission-shared-audio-row",
    details.audioCaptureDevices,
    "Shared audio source"
  );
  populateDeviceSelector(
    "permission-speaker-device",
    "permission-speaker-row",
    details.speakerDevices,
    "Audio output"
  );
  const showScreenSelector =
    portal &&
    !isWaylandSession() &&
    populateDeviceSelector(
      "permission-screen-device",
      "permission-screen-row",
      details.screenDevices,
      "Screen or window"
    );
  if (!showScreenSelector) {
    document.getElementById("permission-screen-row").hidden = true;
  }
  document.getElementById("permission-panel").hidden = false;
  document.getElementById("permission-deny").focus();
}

function showDownloadMediation(pending) {
  hideChromeOverlays();
  hideChrome();
  text("download-origin", pending.context.origin);
  text("download-name", pending.details.suggestedName);
  text("download-type", pending.details.mimeType);
  document.getElementById("download-panel").hidden = false;
  document.getElementById("download-cancel").focus();
}

function beginMediation({
  kinds,
  origin,
  contentActivation,
  surface,
  details,
  allow,
  deny,
}) {
  const normalizedKinds = normalizedMediationKinds(kinds);
  const context = currentMediationContext();
  if (
    !normalizedKinds ||
    typeof allow !== "function" ||
    typeof deny !== "function" ||
    (surface !== "permission" && surface !== "download") ||
    origin !== state.policy.startOrigin ||
    !context ||
    context.origin !== origin ||
    (surface === "permission" && !contentActivation)
  ) {
    safelyDeny(deny);
    dump("ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_REJECTED invalid-request\n");
    return false;
  }
  if (state.pendingMediation) {
    safelyDeny(deny);
    dump("ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_REJECTED request-outstanding\n");
    return false;
  }
  const now = monotonicMilliseconds();
  state.pendingMediation = {
    allow,
    context,
    deny,
    details: details || {},
    expiresAt: now + MEDIATION_REQUEST_TTL_MS,
    kinds: normalizedKinds,
    requestId: requestToken(),
    surface,
  };
  state.pendingMediationTimer = window.setTimeout(() => {
    if (state.pendingMediation) {
      invalidatePendingMediation("request-expired");
    }
  }, MEDIATION_REQUEST_TTL_MS);
  updateMediationIndicator(
    "1 request",
    `${mediationKindLabel(normalizedKinds)} request awaiting a one-shot decision`
  );
  if (surface === "download") {
    showDownloadMediation(state.pendingMediation);
  } else {
    showPermissionMediation(state.pendingMediation);
  }
  dump(
    `ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_PROMPT ${surface} ${normalizedKinds.join("+")}\n`
  );
  return true;
}

function consumePendingMediation({ allowRequest, event }) {
  const pending = state.pendingMediation;
  if (!pending) {
    return null;
  }
  const current = currentMediationContext();
  const expired = monotonicMilliseconds() > pending.expiresAt;
  const privilegedActivation = Boolean(event && event.isTrusted);
  if (
    !sameMediationContext(current, pending.context) ||
    expired ||
    (allowRequest && !privilegedActivation)
  ) {
    invalidatePendingMediation(expired ? "request-expired" : "navigation");
    return null;
  }
  clearPendingMediationTimer();
  state.pendingMediation = null;
  closeMediationPanels();
  updateMediationIndicator();
  return pending;
}

function denyPendingMediation(event) {
  const pending = consumePendingMediation({ allowRequest: false, event });
  if (!pending) {
    return;
  }
  safelyDeny(pending.deny);
  dump(`ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_DECISION ${pending.surface} deny\n`);
}

function selectedDeviceIndex(id) {
  const selector = document.getElementById(id);
  if (!selector || selector.hidden || selector.selectedIndex < 0) {
    return null;
  }
  const value = Number(selector.value);
  return Number.isSafeInteger(value) ? value : null;
}

function selectedWebRTCDevices(pending) {
  const details = pending.details;
  const selected = [];
  if (pending.kinds.includes("camera")) {
    const camera = selectedDeviceIndex("permission-camera-device");
    if (camera === null) {
      return null;
    }
    selected.push(camera);
  }
  if (pending.kinds.includes("microphone")) {
    const microphone = selectedDeviceIndex("permission-microphone-device");
    if (microphone === null) {
      return null;
    }
    selected.push(microphone);
  }
  if (details.sharedAudioRequested === true) {
    const sharedAudio = selectedDeviceIndex("permission-shared-audio-device");
    if (sharedAudio === null) {
      return null;
    }
    selected.push(sharedAudio);
  }
  if (pending.kinds.includes("speaker-selection")) {
    const speaker = selectedDeviceIndex("permission-speaker-device");
    if (speaker === null) {
      return null;
    }
    selected.push(speaker);
  }
  if (pending.kinds.includes("screen-capture")) {
    if (isWaylandSession()) {
      if (!Array.isArray(details.screenDevices) || details.screenDevices.length !== 1) {
        return null;
      }
      const selectedByPortal = details.screenDevices[0];
      if (
        !selectedByPortal ||
        !Number.isSafeInteger(selectedByPortal.deviceIndex) ||
        !["screen", "window"].includes(selectedByPortal.mediaSource)
      ) {
        return null;
      }
      selected.push(selectedByPortal.deviceIndex);
    } else {
      const screen = selectedDeviceIndex("permission-screen-device");
      if (screen === null) {
        return null;
      }
      selected.push(screen);
    }
  }
  return selected;
}

async function allowWebRTCRequest(pending, deviceIndices) {
  const request = pending.details.webRTCRequest;
  let actor;
  try {
    actor = state.browser.browsingContext.currentWindowGlobal.getActor("WebRTC");
  } catch (_error) {
    safelyDeny(pending.deny);
    return;
  }
  const camera = pending.kinds.includes("camera");
  const microphone = pending.kinds.includes("microphone");
  const screen = pending.kinds.includes("screen-capture");
  try {
    const osPermission = await actor.checkOSPermission(camera, microphone, screen);
    if (!osPermission || !sameMediationContext(currentMediationContext(), pending.context)) {
      actor.denyRequestNoPermission(request);
      return;
    }
    const selected = new Set(deviceIndices);
    for (const device of [
      ...(pending.details.cameraDevices || []),
      ...(pending.details.microphoneDevices || []),
      ...(pending.details.screenDevices || []),
    ]) {
      if (selected.has(device.deviceIndex) && device.rawId && device.mediaSource) {
        actor.activateDevicePerm(request.windowID, device.mediaSource, device.rawId);
      }
    }
    actor.sendAsyncMessage("webrtc:Allow", {
      callID: request.callID,
      devices: deviceIndices,
      suppressNotifications: false,
      windowID: request.windowID,
    });
  } catch (_error) {
    safelyDeny(pending.deny);
  }
}

function allowPendingPermission(event) {
  const pending = consumePendingMediation({ allowRequest: true, event });
  if (!pending || pending.surface !== "permission") {
    if (pending) {
      safelyDeny(pending.deny);
    }
    return;
  }
  if (pending.details.webRTCRequest) {
    const devices = selectedWebRTCDevices(pending);
    if (!devices || devices.length === 0) {
      safelyDeny(pending.deny);
      dump("ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_DECISION permission deny-invalid-source\n");
      return;
    }
    const outcome = pending.kinds.includes("screen-capture")
      ? "continue-to-portal"
      : "allow-once";
    void allowWebRTCRequest(pending, devices);
    dump(`ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_DECISION permission ${outcome}\n`);
    return;
  }
  try {
    pending.allow();
    dump("ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_DECISION permission allow-once\n");
  } catch (_error) {
    safelyDeny(pending.deny);
  }
}

function contentPermissionType(request) {
  try {
    const types = request.types.QueryInterface(Ci.nsIArray);
    if (types.length !== 1) {
      return null;
    }
    return types.queryElementAt(0, Ci.nsIContentPermissionType).type;
  } catch (_error) {
    return null;
  }
}

function mediateContentPermissionRequest(request) {
  const type = contentPermissionType(request);
  const kind = CONTENT_PERMISSION_KINDS.get(type);
  let origin = null;
  let requestWindow = null;
  let elementMatches = false;
  try {
    origin = request.principal.URI.prePath;
    requestWindow = request.window;
    elementMatches = request.element === state.browser;
  } catch (_error) {
    // The request is malformed and will be cancelled below.
  }
  beginMediation({
    allow: () => request.allow(),
    contentActivation: Boolean(requestWindow && contentUserActivation(requestWindow)),
    deny: () => request.cancel(),
    details: {},
    kinds: kind && elementMatches ? [kind] : [],
    origin,
    surface: "permission",
  });
}

function validWebRTCRequest(request) {
  return Boolean(
    request &&
      typeof request === "object" &&
      typeof request.callID === "string" &&
      request.callID.length > 0 &&
      request.callID.length <= 256 &&
      Number.isSafeInteger(request.windowID) &&
      request.windowID > 0 &&
      request.secure === true &&
      request.origin === state.policy.startOrigin &&
      (request.secondOrigin === undefined || request.secondOrigin === null) &&
      typeof request.isHandlingUserInput === "boolean" &&
      (request.sharingAudio === undefined ||
        request.sharingAudio === null ||
        typeof request.sharingAudio === "boolean") &&
      (request.sharingScreen === undefined ||
        request.sharingScreen === null ||
        typeof request.sharingScreen === "boolean") &&
      Array.isArray(request.requestTypes) &&
      Array.isArray(request.audioInputDevices) &&
      Array.isArray(request.audioOutputDevices) &&
      Array.isArray(request.videoInputDevices)
  );
}

function mediateWebRTCRequest(request) {
  if (!validWebRTCRequest(request)) {
    try {
      state.browser.browsingContext.currentWindowGlobal
        .getActor("WebRTC")
        .denyRequest(request);
    } catch (_error) {
      // A malformed actor request is already denied by not answering it.
    }
    return;
  }
  const requestTypes = new Set(request.requestTypes);
  const supported = new Set([
    "AudioCapture",
    "Camera",
    "Microphone",
    "Screen",
    "Speaker",
  ]);
  const actor = state.browser.browsingContext.currentWindowGlobal.getActor("WebRTC");
  if ([...requestTypes].some(type => !supported.has(type))) {
    actor.denyRequest(request);
    return;
  }
  const screenRequested = request.sharingScreen || requestTypes.has("Screen");
  const sharedAudioRequested =
    request.sharingAudio || requestTypes.has("AudioCapture");
  if (
    (sharedAudioRequested && !screenRequested) ||
    (screenRequested &&
      ["Camera", "Microphone", "Speaker"].some(type => requestTypes.has(type)))
  ) {
    actor.denyRequest(request);
    return;
  }
  const kinds = [];
  if (screenRequested) {
    kinds.push("screen-capture");
  } else {
    if (requestTypes.has("Camera")) {
      kinds.push("camera");
    }
    if (requestTypes.has("Microphone")) {
      kinds.push("microphone");
    }
    if (requestTypes.has("Speaker")) {
      kinds.push("speaker-selection");
    }
  }
  beginMediation({
    allow: () => {},
    contentActivation: request.isHandlingUserInput === true,
    deny: () => actor.denyRequest(request),
    details: {
      audioCaptureDevices: sharedAudioRequested ? request.audioInputDevices : [],
      cameraDevices: screenRequested ? [] : request.videoInputDevices,
      microphoneDevices: sharedAudioRequested ? [] : request.audioInputDevices,
      screenDevices: screenRequested ? request.videoInputDevices : [],
      sharedAudioRequested,
      speakerDevices: request.audioOutputDevices,
      webRTCRequest: request,
    },
    kinds,
    origin: request.origin,
    surface: "permission",
  });
}

function mediatePermissionRequest(request) {
  if (request && typeof request === "object" && "callID" in request) {
    mediateWebRTCRequest(request);
  } else {
    mediateContentPermissionRequest(request);
  }
}

function sanitizeDownloadLeafName(value) {
  if (typeof value !== "string") {
    return "download";
  }
  const sanitized = value
    .replace(/[\\/\u0000-\u001f\u007f]+/g, "_")
    .replace(/^\.+$/, "download")
    .trim()
    .slice(0, 160);
  return sanitized || "download";
}

function downloadSourceOrigin(launcher) {
  try {
    const parsed = new URL(launcher.source.spec);
    if (parsed.protocol === "https:" || parsed.protocol === "blob:") {
      return parsed.origin;
    }
  } catch (_error) {
    // Unsupported or malformed download source.
  }
  return null;
}

function cancelLauncher(launcher) {
  launcher.cancel(Cr.NS_BINDING_ABORTED);
}

function mediateDownloadRequest(launcher, context) {
  let contextMatches = false;
  try {
    const requestWindow = context.getInterface(Ci.nsIDOMWindow);
    contextMatches =
      requestWindow.browsingContext.top === state.browser.browsingContext;
  } catch (_error) {
    contextMatches = false;
  }
  const origin = downloadSourceOrigin(launcher);
  if (!contextMatches) {
    safelyDeny(() => cancelLauncher(launcher));
    return;
  }
  let mimeType = "Unknown file type";
  try {
    mimeType = String(launcher.MIMEInfo.MIMEType || mimeType).slice(0, 120);
  } catch (_error) {
    // Keep the bounded fallback.
  }
  beginMediation({
    allow: () => {
      state.approvedDownloadLaunchers.add(launcher);
      launcher.saveToDisk(null, false);
    },
    contentActivation: false,
    deny: () => cancelLauncher(launcher),
    details: {
      mimeType,
      suggestedName: sanitizeDownloadLeafName(launcher.suggestedFileName),
    },
    kinds: ["download"],
    origin,
    surface: "download",
  });
}

function allowPendingDownload(event) {
  const pending = consumePendingMediation({ allowRequest: true, event });
  if (!pending || pending.surface !== "download") {
    if (pending) {
      safelyDeny(pending.deny);
    }
    return;
  }
  try {
    pending.allow();
    dump("ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_DECISION download allow-once\n");
  } catch (_error) {
    safelyDeny(pending.deny);
  }
}

function chooseDownloadDestination(
  launcher,
  defaultFileName,
  suggestedFileExtension
) {
  if (
    !state.approvedDownloadLaunchers.has(launcher) ||
    downloadSourceOrigin(launcher) !== state.policy.startOrigin
  ) {
    try {
      launcher.saveDestinationAvailable(null);
    } finally {
      safelyDeny(() => cancelLauncher(launcher));
    }
    return;
  }
  const binding = currentMediationContext();
  let picker;
  try {
    picker = Cc["@mozilla.org/filepicker;1"].createInstance(Ci.nsIFilePicker);
    picker.init(
      window.browsingContext,
      "Save download",
      Ci.nsIFilePicker.modeSave
    );
    picker.defaultString = sanitizeDownloadLeafName(defaultFileName);
    if (
      typeof suggestedFileExtension === "string" &&
      /^\.[A-Za-z0-9]{1,16}$/.test(suggestedFileExtension)
    ) {
      picker.defaultExtension = suggestedFileExtension.slice(1);
    }
    picker.appendFilters(Ci.nsIFilePicker.filterAll);
    picker.open(result => {
      state.approvedDownloadLaunchers.delete(launcher);
      if (
        result === Ci.nsIFilePicker.returnCancel ||
        !sameMediationContext(binding, currentMediationContext()) ||
        !picker.file
      ) {
        launcher.saveDestinationAvailable(null);
        return;
      }
      launcher.saveDestinationAvailable(picker.file);
    });
  } catch (_error) {
    state.approvedDownloadLaunchers.delete(launcher);
    try {
      launcher.saveDestinationAvailable(null);
    } finally {
      safelyDeny(() => cancelLauncher(launcher));
    }
  }
}

function AppShellHelperAppLauncherDialog() {
  this.wrappedJSObject = this;
  this.zenAppShellDownloadMediator = true;
}

AppShellHelperAppLauncherDialog.prototype = {
  QueryInterface: ChromeUtils.generateQI(["nsIHelperAppLauncherDialog"]),

  promptForSaveToFileAsync(
    launcher,
    _context,
    defaultFileName,
    suggestedFileExtension,
    _forcePrompt
  ) {
    chooseDownloadDestination(
      launcher,
      defaultFileName,
      suggestedFileExtension
    );
  },

  show(launcher, context, _reason) {
    mediateDownloadRequest(launcher, context);
  },
};

function runNativeMediationSelfTest() {
  if (
    Services.env.get("ZEN_APP_RUNTIME_APP_SHELL_TEST_MEDIATION") !== "1"
  ) {
    return;
  }

  let noActivationDenials = 0;
  const acceptedWithoutActivation = beginMediation({
    allow: () => fail("no-activation permission self-test was allowed"),
    contentActivation: false,
    deny: () => {
      noActivationDenials += 1;
    },
    details: {},
    kinds: ["microphone"],
    origin: state.policy.startOrigin,
    surface: "permission",
  });
  if (
    acceptedWithoutActivation ||
    noActivationDenials !== 1 ||
    state.pendingMediation
  ) {
    fail("no-activation permission self-test did not fail closed");
  }

  let malformedDenials = 0;
  const acceptedMalformed = beginMediation({
    allow: () => fail("malformed permission self-test was allowed"),
    contentActivation: true,
    deny: () => {
      malformedDenials += 1;
    },
    details: {},
    kinds: ["unreviewed-permission"],
    origin: state.policy.startOrigin,
    surface: "permission",
  });
  if (acceptedMalformed || malformedDenials !== 1 || state.pendingMediation) {
    fail("malformed permission self-test did not fail closed");
  }

  let downloadCancellations = 0;
  const launcher = {
    cancel(reason) {
      if (reason !== Cr.NS_BINDING_ABORTED) {
        fail("download self-test used an unexpected cancellation reason");
      }
      downloadCancellations += 1;
    },
    source: { spec: state.policy.startUrl },
  };
  mediateDownloadRequest(launcher, null);
  if (downloadCancellations !== 1 || state.pendingMediation) {
    fail("download cancellation self-test did not fail closed");
  }

  dump(
    "ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_SELFTEST permission-no-activation-denied permission-malformed-denied download-cancelled\n"
  );
}

function installMediationAuthority() {
  const registrar = Components.manager.QueryInterface(Ci.nsIComponentRegistrar);
  const originalCID = registrar.contractIDToCID(DOWNLOAD_DIALOG_CONTRACT);
  const factory = {
    QueryInterface: ChromeUtils.generateQI(["nsIFactory"]),
    createInstance(iid) {
      return new AppShellHelperAppLauncherDialog().QueryInterface(iid);
    },
  };
  registrar.registerFactory(
    DOWNLOAD_DIALOG_CLASS_ID,
    "Zen App Shell download mediator",
    DOWNLOAD_DIALOG_CONTRACT,
    factory
  );
  const restoreOriginalFactory = () => {
    try {
      registrar.unregisterFactory(DOWNLOAD_DIALOG_CLASS_ID, factory);
    } finally {
      registrar.registerFactory(
        originalCID,
        "",
        DOWNLOAD_DIALOG_CONTRACT,
        null
      );
    }
  };
  let probe;
  try {
    probe = Cc[DOWNLOAD_DIALOG_CONTRACT].createInstance(
      Ci.nsIHelperAppLauncherDialog
    );
  } catch (error) {
    restoreOriginalFactory();
    throw error;
  }
  if (!probe.wrappedJSObject?.zenAppShellDownloadMediator) {
    restoreOriginalFactory();
    fail("download mediator did not own the helper-app contract");
  }
  state.downloadFactory = factory;
  state.downloadOriginalCID = originalCID;
  state.downloadRegistrar = registrar;
  text("panel-download-state", "Privileged confirmation, then system file picker");
  text(
    "panel-portal-state",
    "xdg-desktop-portal required for every Wayland screen-capture request"
  );
  window.addEventListener(
    "unload",
    () => {
      invalidatePendingMediation("process-exit");
      if (
        state.downloadRegistrar &&
        state.downloadFactory &&
        state.downloadOriginalCID
      ) {
        try {
          state.downloadRegistrar.unregisterFactory(
            DOWNLOAD_DIALOG_CLASS_ID,
            state.downloadFactory
          );
          state.downloadRegistrar.registerFactory(
            state.downloadOriginalCID,
            "",
            DOWNLOAD_DIALOG_CONTRACT,
            null
          );
          dump("ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_RESTORED download\n");
        } catch (_error) {
          // The process-local registrar is going away with the application.
        }
      }
      state.downloadFactory = null;
      state.downloadOriginalCID = null;
      state.downloadRegistrar = null;
    },
    { once: true }
  );
  runNativeMediationSelfTest();
  dump("ZEN_APP_RUNTIME_APP_SHELL_MEDIATION_READY permission download xdg-desktop-portal\n");
}

function hideChromeOverlays() {
  for (const id of [
    "security-panel",
    "auth-panel",
    "external-panel",
    "permission-panel",
    "download-panel",
  ]) {
    document.getElementById(id).hidden = true;
  }
}

function showSecurityPanel() {
  state.pendingAuthentication = null;
  state.pendingExternal = null;
  if (state.pendingMediation) {
    if (state.pendingMediation.surface === "download") {
      showDownloadMediation(state.pendingMediation);
    } else {
      showPermissionMediation(state.pendingMediation);
    }
    return;
  }
  hideChromeOverlays();
  showChrome(false);
  document.getElementById("security-panel").hidden = false;
  document.getElementById("security-panel-close").focus();
}

function closeSecurityPanel() {
  document.getElementById("security-panel").hidden = true;
  document.getElementById("security-button").focus();
  scheduleChromeHide();
}

function authenticationOrigins() {
  const origins = new Set([state.policy.startOrigin]);
  if (state.authFlow) {
    for (const origin of state.authFlow.origins) {
      origins.add(origin);
    }
  }
  return origins;
}

function uriAllowedInside(uri) {
  if (!uri || !uri.schemeIs("https")) {
    return false;
  }
  return authenticationOrigins().has(uri.prePath);
}

function parseCredentialFreeHTTPS(value) {
  try {
    const parsed = new URL(value);
    if (
      parsed.protocol === "https:" &&
      parsed.username === "" &&
      parsed.password === ""
    ) {
      return parsed;
    }
  } catch (_error) {
    // A callback candidate must be an absolute URL.
  }
  return null;
}

function authenticationCandidate(uri) {
  if (
    !uri ||
    !uri.schemeIs("https") ||
    uri.prePath === state.policy.startOrigin
  ) {
    return null;
  }
  let target;
  try {
    target = new URL(uri.spec);
  } catch (_error) {
    return null;
  }
  if (target.username !== "" || target.password !== "") {
    return null;
  }
  const trustedReturns = authenticationOrigins();
  for (const [rawName, rawValue] of target.searchParams.entries()) {
    const name = rawName.toLowerCase();
    if (!AUTH_RETURN_PARAMETERS.has(name)) {
      continue;
    }
    let candidateValue = rawValue;
    for (let depth = 0; depth < 3; depth += 1) {
      const callback = parseCredentialFreeHTTPS(candidateValue);
      if (callback && trustedReturns.has(callback.origin)) {
        return {
          returnOrigin: callback.origin,
          targetHost: target.hostname,
          targetOrigin: target.origin,
          targetSpec: target.href,
        };
      }
      let decoded;
      try {
        decoded = decodeURIComponent(candidateValue);
      } catch (_error) {
        break;
      }
      if (decoded === candidateValue) {
        break;
      }
      candidateValue = decoded;
    }
  }
  return null;
}

function registrableDomain(hostname) {
  try {
    return Services.eTLD.getBaseDomainFromHost(hostname);
  } catch (_error) {
    // IP literals, public suffixes, and malformed hosts are not related sites.
    return null;
  }
}

function authenticationEnrollmentCandidate(uri) {
  if (
    !state.authFlow ||
    !state.authFlow.currentOrigin ||
    !uri ||
    !uri.schemeIs("https")
  ) {
    return null;
  }
  let target;
  let source;
  let start;
  try {
    target = new URL(uri.spec);
    source = new URL(state.authFlow.currentOrigin);
    start = new URL(state.policy.startOrigin);
  } catch (_error) {
    return null;
  }
  if (
    target.username !== "" ||
    target.password !== "" ||
    source.origin === start.origin ||
    !authenticationOrigins().has(source.origin)
  ) {
    return null;
  }
  const enrollmentPath = target.pathname
    .split("/")
    .filter(segment => segment !== "")
    .some(segment => AUTH_ENROLLMENT_PATH_SEGMENTS.has(segment.toLowerCase()));
  if (!enrollmentPath) {
    return null;
  }
  const startDomain = registrableDomain(start.hostname);
  const sourceDomain = registrableDomain(source.hostname);
  const targetDomain = registrableDomain(target.hostname);
  if (
    !startDomain ||
    sourceDomain !== startDomain ||
    targetDomain !== startDomain
  ) {
    return null;
  }
  return {
    returnOrigin: source.origin,
    targetHost: target.hostname,
    targetOrigin: target.origin,
    targetSpec: target.href,
  };
}

function externalService() {
  return Cc[
    "@mozilla.org/uriloader/external-protocol-service;1"
  ].getService(Ci.nsIExternalProtocolService);
}

function delegateExternally(uri) {
  externalService().loadURI(uri, null);
}

function stopTopLevelNavigation() {
  try {
    state.browser.stop();
  } catch (_error) {
    // The channel may already have stopped itself.
  }
}

function showAuthenticationPrompt(candidate) {
  if (
    state.pendingAuthentication &&
    state.pendingAuthentication.targetSpec === candidate.targetSpec
  ) {
    return;
  }
  state.pendingAuthentication = candidate;
  state.pendingExternal = null;
  hideChromeOverlays();
  hideChrome();
  text("auth-host", candidate.targetHost);
  text("auth-origin", candidate.targetOrigin);
  text("auth-return-origin", candidate.returnOrigin);
  text("auth-target", candidate.targetSpec);
  document.querySelector(".target-details").open = false;
  document.getElementById("auth-panel").hidden = false;
  document.getElementById("auth-cancel").focus();
  dump(
    `ZEN_APP_RUNTIME_APP_SHELL_AUTH_PROMPT ${candidate.targetOrigin} ${candidate.returnOrigin}\n`
  );
}

function cancelPendingAuthentication() {
  state.pendingAuthentication = null;
  document.getElementById("auth-panel").hidden = true;
  if (state.acceptedLocation) {
    state.browser.hidden = false;
  }
}

function beginPendingAuthentication() {
  const candidate = state.pendingAuthentication;
  if (!candidate) {
    return;
  }
  if (!state.authFlow) {
    state.authFlow = {
      currentOrigin: state.policy.startOrigin,
      origins: [],
    };
  }
  if (!state.authFlow.origins.includes(candidate.targetOrigin)) {
    state.authFlow.origins.push(candidate.targetOrigin);
  }
  state.pendingAuthentication = null;
  document.getElementById("auth-panel").hidden = true;
  state.browser.hidden = false;
  showChrome(false);
  updateSecurity("Pinned sign-in HTTPS", candidate.targetOrigin);
  const target = Services.io.newURI(candidate.targetSpec);
  const principal = Services.scriptSecurityManager.getSystemPrincipal();
  state.browser.loadURI(target, { triggeringPrincipal: principal });
  dump(
    `ZEN_APP_RUNTIME_APP_SHELL_AUTH_STARTED ${candidate.targetOrigin} ${candidate.returnOrigin}\n`
  );
}

function showExternalPrompt(uri, reason) {
  state.pendingAuthentication = null;
  state.pendingExternal = uri;
  hideChromeOverlays();
  hideChrome();
  text("external-reason", reason);
  text("external-target", uri.spec);
  document.getElementById("external-panel").hidden = false;
  document.getElementById("external-cancel").focus();
}

function rejectTopLevelNavigation(uri) {
  stopTopLevelNavigation();
  if (
    state.pendingAuthentication &&
    uri &&
    state.pendingAuthentication.targetSpec === uri.spec
  ) {
    return;
  }
  if (uri && uri.schemeIs("https")) {
    const candidate =
      authenticationCandidate(uri) || authenticationEnrollmentCandidate(uri);
    if (candidate) {
      showAuthenticationPrompt(candidate);
      return;
    }
    showExternalPrompt(
      uri,
      "This HTTPS destination is outside the app and has neither a trusted return callback nor an active related-site account-enrollment route."
    );
    return;
  }
  if (uri) {
    showExternalPrompt(
      uri,
      "A custom protocol requires explicit confirmation before leaving the app."
    );
    return;
  }
  updateSecurity("Navigation blocked", state.policy.startOrigin);
  dump("ZEN_APP_RUNTIME_APP_SHELL_NAVIGATION_BLOCKED unknown\n");
}

function acceptedLocation(uri) {
  const returnedFromAuthentication = state.authFlow !== null;
  state.acceptedLocation = uri.spec;
  state.authFlow = null;
  updateSecurity("Secure HTTPS", uri.prePath);
  document.title = `${state.displayName} — Zen App Shell`;
  updateHistoryButtons();
  scheduleChromeHide();
  if (returnedFromAuthentication) {
    dump(`ZEN_APP_RUNTIME_APP_SHELL_AUTH_RETURNED ${uri.prePath}\n`);
  }
  dump(`ZEN_APP_RUNTIME_APP_SHELL_CONTENT_COMMITTED ${uri.spec}\n`);
  if (Services.env.get(TEST_AUTOSTOP_ENV) === "1") {
    setTimeout(
      () => Services.startup.quit(Ci.nsIAppStartup.eAttemptQuit),
      800
    );
  }
}

function acceptedAuthenticationLocation(uri) {
  state.authFlow.currentOrigin = uri.prePath;
  updateSecurity("Pinned sign-in HTTPS", uri.prePath);
  document.title = `Sign in · ${state.displayName}`;
  updateHistoryButtons();
  showChrome(false);
  dump(`ZEN_APP_RUNTIME_APP_SHELL_AUTH_COMMITTED ${uri.spec}\n`);
}

function createContentBrowser() {
  const host = document.getElementById("content-browser-host");
  if (!host || host.childElementCount !== 0 || state.browser !== null) {
    fail("content browser host is not empty");
  }
  const browser = document.createXULElement("browser");
  browser.id = "content-browser";
  browser.setAttribute("type", "content");
  browser.setAttribute("remote", "true");
  browser.setAttribute("maychangeremoteness", "true");
  browser.setAttribute("nodefaultsrc", "true");
  browser.setAttribute("transparent", "true");
  browser.setAttribute("flex", "1");
  browser.setAttribute("hidden", "true");
  browser.fxrPermissionPrompt = mediatePermissionRequest;
  host.appendChild(browser);
  state.browser = browser;
}

function installContentWindowAuthority() {
  if (!state.browser) {
    fail("content browser is missing before window authority installation");
  }
  state.browser.addEventListener(
    "DOMWindowOpen",
    event => {
      consume(event);
    },
    true
  );
}

function installProgressListener() {
  state.progressListener = {
    QueryInterface: ChromeUtils.generateQI([
      "nsIWebProgressListener",
      "nsISupportsWeakReference",
    ]),

    onStateChange(webProgress, request, flags) {
      if (!state.siteNavigationActive || !webProgress || !webProgress.isTopLevel) {
        return;
      }
      const start = Ci.nsIWebProgressListener.STATE_START;
      const documentState = Ci.nsIWebProgressListener.STATE_IS_DOCUMENT;
      if ((flags & start) === 0 || (flags & documentState) === 0 || !request) {
        return;
      }
      advanceMediationEpoch("navigation");
      let uri = null;
      try {
        uri = request.QueryInterface(Ci.nsIChannel).URI;
      } catch (_error) {
        rejectTopLevelNavigation(null);
        return;
      }
      if (!uriAllowedInside(uri)) {
        rejectTopLevelNavigation(uri);
      }
    },

    onLocationChange(webProgress, _request, location) {
      if (
        !state.siteNavigationActive ||
        !webProgress ||
        !webProgress.isTopLevel ||
        !location
      ) {
        return;
      }
      if (uriAllowedInside(location)) {
        document.getElementById("content-browser").hidden = false;
        if (location.prePath === state.policy.startOrigin) {
          acceptedLocation(location);
        } else {
          acceptedAuthenticationLocation(location);
        }
        return;
      }
      if (location.schemeIs("about") && location.spec.startsWith("about:neterror")) {
        updateSecurity("Load error", state.policy.startOrigin);
        return;
      }
      document.getElementById("content-browser").hidden = true;
      rejectTopLevelNavigation(location);
    },

    onSecurityChange(webProgress, _request, securityState) {
      if (!webProgress || !webProgress.isTopLevel || !state.siteNavigationActive) {
        return;
      }
      if (securityState & Ci.nsIWebProgressListener.STATE_IS_SECURE) {
        updateSecurity("Secure HTTPS");
      } else if (securityState & Ci.nsIWebProgressListener.STATE_IS_BROKEN) {
        updateSecurity("HTTPS warning");
      } else {
        updateSecurity("Not secure");
      }
    },

    onContentBlockingEvent(webProgress, _request, event) {
      if (webProgress && webProgress.isTopLevel && event) {
        document
          .getElementById("permission-button")
          .setAttribute("tooltiptext", "Permissions prompt; tracking protection active");
      }
    },

    onProgressChange() {},
    onStatusChange() {},
  };
  state.browser.addProgressListener(
    state.progressListener,
    Ci.nsIWebProgress.NOTIFY_STATE_DOCUMENT |
      Ci.nsIWebProgress.NOTIFY_LOCATION |
      Ci.nsIWebProgress.NOTIFY_SECURITY
  );
}

function closeApp() {
  invalidatePendingMediation("window-close");
  window.close();
}

function goBack() {
  if (state.siteNavigationActive && state.browser.canGoBack) {
    state.browser.goBack();
  }
}

function goForward() {
  if (state.siteNavigationActive && state.browser.canGoForward) {
    state.browser.goForward();
  }
}

function reloadApp(bypassCache = false) {
  if (!state.siteNavigationActive) {
    return;
  }
  invalidatePendingMediation("reload");
  if (bypassCache) {
    state.browser.reloadWithFlags(Ci.nsIWebNavigation.LOAD_FLAGS_BYPASS_CACHE);
  } else {
    state.browser.reload();
  }
}

function consume(event) {
  event.preventDefault();
  event.stopPropagation();
  if (typeof event.stopImmediatePropagation === "function") {
    event.stopImmediatePropagation();
  }
}

function dismissOverlayOrHideChrome() {
  if (
    !document.getElementById("permission-panel").hidden ||
    !document.getElementById("download-panel").hidden
  ) {
    denyPendingMediation(null);
  } else if (!document.getElementById("auth-panel").hidden) {
    cancelPendingAuthentication();
  } else if (!document.getElementById("external-panel").hidden) {
    state.pendingExternal = null;
    document.getElementById("external-panel").hidden = true;
  } else if (!document.getElementById("security-panel").hidden) {
    closeSecurityPanel();
  } else {
    hideChrome();
  }
}

function keyCommand(id, callback = null) {
  document.getElementById(id).addEventListener("command", event => {
    consume(event);
    if (callback) {
      callback();
    }
  });
}

function installShortcutAuthority() {
  keyCommand("key-close", closeApp);
  keyCommand("key-security", showSecurityPanel);
  keyCommand("key-hide-chrome", dismissOverlayOrHideChrome);
  keyCommand("key-back", goBack);
  keyCommand("key-forward", goForward);
  keyCommand("key-reload", () => reloadApp(false));
  keyCommand("key-reload-bypass", () => reloadApp(true));
  for (const id of [
    "key-block-window",
    "key-block-tab",
    "key-block-restore-tab",
    "key-block-devtools",
    "key-block-browser-console",
    "key-block-private",
    "key-block-location",
    "key-block-fullscreen",
    "key-block-devtools-f12",
  ]) {
    keyCommand(id);
  }
  window.addEventListener(
    "keydown",
    event => {
      const key = event.key.toLowerCase();
      if (
        event.ctrlKey &&
        !event.shiftKey &&
        !event.altKey &&
        (key === "t" || key === "n")
      ) {
        consume(event);
      } else if (
        event.ctrlKey &&
        event.shiftKey &&
        !event.altKey &&
        ["i", "j", "p", "t"].includes(key)
      ) {
        consume(event);
      } else if (["f6", "f11", "f12"].includes(key)) {
        consume(event);
      } else if (key === "escape") {
        dismissOverlayOrHideChrome();
      }
    },
    true
  );
}

function installWindowAuthority() {
  state.windowObserver = {
    observe(subject, topic) {
      if (topic !== "domwindowopened" || subject === window) {
        return;
      }
      try {
        subject.addEventListener(
          "load",
          () => {
            try {
              const documentURI = subject.document?.documentURI;
              if (
                state.siteNavigationActive &&
                documentURI === "chrome://browser/content/webrtcIndicator.xhtml"
              ) {
                dump("ZEN_APP_RUNTIME_APP_SHELL_WEBRTC_INDICATOR_ALLOWED\n");
                return;
              }
              subject.close();
            } catch (_error) {
              // The additional window may have already closed.
            }
          },
          { once: true }
        );
      } catch (_error) {
        try {
          subject.close();
        } catch (_ignored) {
          // Failing closed is best effort after a malformed window subject.
        }
      }
    },
  };
  Services.ww.registerNotification(state.windowObserver);
  window.addEventListener(
    "unload",
    () => {
      if (state.windowObserver) {
        try {
          Services.ww.unregisterNotification(state.windowObserver);
        } catch (_error) {
          // The observer is process-local and dies with the app.
        }
      }
    },
    { once: true }
  );
}

function installControls() {
  document.getElementById("back-button").addEventListener("command", goBack);
  document.getElementById("forward-button").addEventListener("command", goForward);
  document
    .getElementById("reload-button")
    .addEventListener("command", () => reloadApp(false));
  document.getElementById("close-button").addEventListener("command", closeApp);
  document.getElementById("hide-chrome-button").addEventListener("command", hideChrome);
  document.getElementById("security-button").addEventListener("command", showSecurityPanel);
  document.getElementById("permission-button").addEventListener("command", () => {
    if (state.pendingMediation) {
      if (state.pendingMediation.surface === "download") {
        showDownloadMediation(state.pendingMediation);
      } else {
        showPermissionMediation(state.pendingMediation);
      }
    } else {
      showSecurityPanel();
    }
  });
  document
    .getElementById("security-panel-close")
    .addEventListener("click", closeSecurityPanel);
  document
    .getElementById("permission-deny")
    .addEventListener("click", denyPendingMediation);
  document
    .getElementById("permission-allow")
    .addEventListener("click", allowPendingPermission);
  document
    .getElementById("download-cancel")
    .addEventListener("click", denyPendingMediation);
  document
    .getElementById("download-save")
    .addEventListener("click", allowPendingDownload);

  const reveal = document.getElementById("chrome-reveal-button");
  reveal.addEventListener("command", () => showChrome(true));
  reveal.addEventListener("mouseenter", () => showChrome(true));
  const chrome = document.getElementById("security-bar");
  chrome.addEventListener("mouseenter", clearChromeHideTimer);
  chrome.addEventListener("mouseleave", scheduleChromeHide);
  window.addEventListener(
    "mousemove",
    event => {
      if (state.siteNavigationActive && event.clientY <= 7) {
        showChrome(true);
      }
    },
    true
  );

  document
    .getElementById("auth-cancel")
    .addEventListener("click", cancelPendingAuthentication);
  document
    .getElementById("auth-continue")
    .addEventListener("click", beginPendingAuthentication);

  document.getElementById("external-cancel").addEventListener("click", () => {
    state.pendingExternal = null;
    document.getElementById("external-panel").hidden = true;
    if (state.acceptedLocation) {
      state.browser.hidden = false;
    }
  });
  document.getElementById("external-open").addEventListener("click", () => {
    const target = state.pendingExternal;
    if (!target) {
      return;
    }
    try {
      delegateExternally(target);
      state.pendingExternal = null;
      document.getElementById("external-panel").hidden = true;
      state.browser.hidden = false;
      dump(`ZEN_APP_RUNTIME_APP_SHELL_EXTERNAL_OPENED ${target.scheme}\n`);
    } catch (error) {
      text(
        "external-reason",
        `The system handler could not be opened: ${safeError(error)}`
      );
      dump(`ZEN_APP_RUNTIME_APP_SHELL_EXTERNAL_ERROR ${safeError(error)}\n`);
    }
  });
}

function openSite() {
  if (!state.handshakeComplete || state.siteNavigationActive) {
    return;
  }
  try {
    createContentBrowser();
    installContentWindowAuthority();
    installProgressListener();
    state.siteNavigationActive = true;
    document.documentElement.setAttribute("zen-site-active", "true");
    document.getElementById("continue-button").disabled = true;
    document.getElementById("onboarding").hidden = true;
    document.getElementById("security-bar").hidden = false;
    document.getElementById("chrome-reveal-button").hidden = false;
    hideChrome();
    state.browser.hidden = false;
    setNavigationEnabled(true);
    updateSecurity("Connecting", state.policy.startOrigin);
    const target = Services.io.newURI(state.policy.startUrl);
    const principal = Services.scriptSecurityManager.getSystemPrincipal();
    state.browser.loadURI(target, { triggeringPrincipal: principal });
    dump("ZEN_APP_RUNTIME_APP_SHELL_CONTENT_REQUESTED\n");
  } catch (error) {
    failClosed(error);
  }
}

function acknowledgeReady(digest, handshakeDescriptor) {
  const handshake = {
    adapterId: ADAPTER_ID,
    adapterProtocolVersion: ADAPTER_PROTOCOL_VERSION,
    handshakeVersion: HANDSHAKE_VERSION,
    policySha256: digest,
    status: "ready-before-site-content",
  };
  writeDescriptor(handshakeDescriptor, `${JSON.stringify(handshake)}\n`);
}

function safeError(error) {
  const rendered = error && error.message ? error.message : String(error);
  return rendered.replace(/[\u0000-\u001f\u007f]+/g, " ").slice(0, 400);
}

function failClosed(error) {
  const rendered = safeError(error);
  try {
    if (state.browser) {
      state.browser.hidden = true;
      state.browser.stop();
    }
    document.getElementById("onboarding").hidden = true;
    hideChromeOverlays();
    text("fatal-error-message", `${rendered}. No website content was loaded.`);
    document.getElementById("fatal-error").hidden = false;
    updateSecurity("Startup blocked", "No site loaded");
  } catch (_displayError) {
    // The shell may have failed before the document was fully constructed.
  }
  dump(`ZEN_APP_RUNTIME_APP_SHELL_FATAL ${rendered}\n`);
  setTimeout(() => Services.startup.quit(Ci.nsIAppStartup.eAttemptQuit), 50);
}

function initialize() {
  try {
    const verified = parseVerifiedPolicy();
    state.policy = verified.policy;
    state.policyDigest = verified.digest;
    state.displayName = deriveDisplayName();
    document.documentElement.setAttribute(
      "zen-presentation-mode",
      state.policy.presentation.mode
    );
    document.documentElement.setAttribute("zen-chrome-visible", "false");
    document.documentElement.setAttribute("zen-site-active", "false");
    document.documentElement.style.backgroundColor = "transparent";
    installSiteTheme();
    text("onboarding-app-id", state.policy.appId);
    text("onboarding-name", state.displayName);
    text("onboarding-icon", state.displayName.slice(0, 1).toUpperCase());
    text("onboarding-origin", state.policy.startOrigin);
    text(
      "onboarding-appearance",
      state.siteTheme
        ? "Alpha window · Zen Internet Discord theme"
        : state.policy.presentation.mode === "Transparent"
          ? "Alpha window · glass controls"
          : "Full canvas · glass controls"
    );
    text("continue-button", `Open ${state.displayName}`);
    text("panel-allowed-origin", state.policy.startOrigin);
    setLabel("origin-indicator", state.policy.startOrigin);
    document.title = `${state.displayName} — Zen App Shell`;

    installControls();
    installShortcutAuthority();
    installWindowAuthority();
    installMediationAuthority();

    acknowledgeReady(verified.digest, verified.handshakeDescriptor);
    state.handshakeComplete = true;
    document.getElementById("continue-button").disabled = false;
    document.getElementById("continue-button").addEventListener("click", openSite);
    document.getElementById("continue-button").focus();
    updateSecurity("Ready · site not loaded", state.policy.startOrigin);
    dump("ZEN_APP_RUNTIME_APP_SHELL_ONBOARDING_READY\n");

    if (Services.env.get(TEST_AUTOSTART_ENV) === "1") {
      setTimeout(openSite, 100);
    }
  } catch (error) {
    failClosed(error);
  }
}

window.addEventListener("load", initialize, { once: true });
