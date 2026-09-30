/* zen-app-runtime app mode: the app's own Firefox extensions.
 *
 * Every app profile runs its own extensions, separate from the browser and
 * from the other apps. They come from addons.mozilla.org (AMO) or from a
 * local .xpi file. App profiles accept unsigned add-ons (the runtime add-on
 * itself is one), so an AMO download is only installed when it is the add-on
 * that was asked for, matches AMO's hash and carries Mozilla's signature.
 *
 * The ad blocker behind the "adblock" setting is uBlock Origin from AMO.
 */

const lazy = {};
ChromeUtils.defineESModuleGetters(lazy, {
  AddonManager: "resource://gre/modules/AddonManager.sys.mjs",
  clearTimeout: "resource://gre/modules/Timer.sys.mjs",
  setTimeout: "resource://gre/modules/Timer.sys.mjs",
});

/** The runtime add-on itself: never listed, never replaced. */
export const SELF_ID = "zenapp@zen-app-runtime";
export const ADBLOCK = Object.freeze({ id: "uBlock0@raymondhill.net", slug: "ublock-origin", name: "uBlock Origin" });

const AMO_HOST = "addons.mozilla.org";
const AMO_API = `https://${AMO_HOST}/api/v5/addons/`;
const FETCH_TIMEOUT_MS = 20000;
export const SEARCH_LIMIT = 8;

/** A refused or failed extension action; `code` picks the message (ext.error.<code>). */
export class ExtensionError extends Error {
  constructor(code, detail = "") {
    super(`extension: ${code}${detail ? ` (${detail})` : ""}`);
    this.code = code;
  }
}

/** Text of an AMO translated field ({"hu": …, "_default": "en-US"}) or plain string. */
export function localized(field, locale = "en") {
  if (typeof field === "string") {
    return field;
  }
  if (!field || typeof field !== "object") {
    return "";
  }
  for (const key of [locale, field._default]) {
    if (key && typeof field[key] === "string" && field[key]) {
      return field[key];
    }
  }
  for (const [key, value] of Object.entries(field)) {
    if (key !== "_default" && typeof value === "string" && value) {
      return value;
    }
  }
  return "";
}

/** Slug (or id) of an addons.mozilla.org add-on link; null for anything else. */
export function amoReference(text) {
  let value = String(text ?? "").trim();
  if (/^addons\.mozilla\.org\//i.test(value)) {
    value = "https://" + value;
  }
  if (!/^https:\/\//i.test(value)) {
    return null;
  }
  let url;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.hostname !== AMO_HOST) {
    return null;
  }
  const match = /^\/(?:[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?\/)?(?:firefox|android)\/addon\/([^/?#]+)/.exec(url.pathname);
  if (!match) {
    return null;
  }
  try {
    const reference = decodeURIComponent(match[1]);
    return /^[\w.@{}+-]{1,200}$/.test(reference) ? reference : null;
  } catch {
    return null;
  }
}

function isAmoUrl(url, pathPrefix = "/") {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" && parsed.hostname === AMO_HOST && parsed.pathname.startsWith(pathPrefix);
  } catch {
    return false;
  }
}

function compatibleWith(range) {
  if (!range) {
    return true;
  }
  const version = Services.appinfo.platformVersion;
  const { min, max } = range;
  if (min && Services.vc.compare(version, min) < 0) {
    return false;
  }
  return !max || max === "*" || Services.vc.compare(version, max) <= 0;
}

/** The parts of an AMO add-on record the app uses (validated). */
export function describeAmo(data, locale = "en") {
  const version = data?.current_version || {};
  const file = version.file || {};
  const promoted = [].concat(data?.promoted || []);
  const icon = data?.icons?.["64"] || data?.icon_url || "";
  return {
    guid: typeof data?.guid === "string" ? data.guid : "",
    slug: typeof data?.slug === "string" ? data.slug : "",
    type: typeof data?.type === "string" ? data.type : "",
    name: localized(data?.name, locale) || String(data?.slug || ""),
    summary: localized(data?.summary, locale),
    icon: isAmoUrl(icon) ? icon : "",
    users: Number.isFinite(data?.average_daily_users) ? data.average_daily_users : null,
    recommended: promoted.some(entry => entry?.category === "recommended" || entry?.category === "line"),
    version: typeof version.version === "string" ? version.version : "",
    url: isAmoUrl(file.url, "/firefox/downloads/") ? file.url : "",
    hash: /^sha256:[0-9a-f]{64}$/.test(file.hash || "") ? file.hash : "",
    compatible: compatibleWith(version.compatibility?.firefox),
  };
}

async function amoFetch(path, params) {
  const url = new URL(path, AMO_API);
  for (const [key, value] of Object.entries(params)) {
    url.searchParams.set(key, String(value));
  }
  // AbortSignal.timeout() needs a window; system modules have none.
  const controller = new AbortController();
  const timer = lazy.setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(url.href, {
      credentials: "omit",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    });
    if (response.status === 404) {
      throw new ExtensionError("notFound");
    }
    if (!response.ok) {
      throw new ExtensionError("network", `HTTP ${response.status}`);
    }
    return await response.json();
  } catch (error) {
    if (error instanceof ExtensionError) {
      throw error;
    }
    throw new ExtensionError("network", String(error?.message || error));
  } finally {
    lazy.clearTimeout(timer);
  }
}

/** One AMO extension by slug, id or GUID, with its current download. */
export async function amoLookup(reference, locale = "en") {
  const data = await amoFetch(`addon/${encodeURIComponent(reference)}/`, { app: "firefox", lang: locale });
  const info = describeAmo(data, locale);
  if (info.type !== "extension") {
    throw new ExtensionError("notExtension");
  }
  if (!info.guid || !info.url || !info.hash) {
    throw new ExtensionError("notFound");
  }
  return info;
}

/** Extensions on AMO for a search text, most relevant first. */
export async function amoSearch(query, locale = "en") {
  const data = await amoFetch("search/", {
    app: "firefox",
    type: "extension",
    q: query,
    page_size: SEARCH_LIMIT,
    lang: locale,
  });
  return (Array.isArray(data?.results) ? data.results : [])
    .map(result => describeAmo(result, locale))
    .filter(result => result.guid && result.url && result.hash && result.type === "extension");
}

/** Extensions the user manages in the app (not the runtime, not system add-ons). */
export function isListed(addon) {
  return !!addon && addon.type === "extension" && addon.id !== SELF_ID && !addon.isSystem && !addon.isBuiltin && !addon.hidden;
}

export function isSigned(addon) {
  return (addon?.signedState ?? -1) >= lazy.AddonManager.SIGNEDSTATE_SIGNED;
}

/** Removed with undo (uninstall(true)): gone at the next start unless restored. */
export function isPendingUninstall(addon) {
  return !!addon && !!(addon.pendingOperations & lazy.AddonManager.PENDING_UNINSTALL);
}

/** Plain, UI-safe description of an installed add-on. */
export function describeAddon(addon) {
  const AM = lazy.AddonManager;
  const permissions = addon.permissions || 0;
  return {
    id: addon.id,
    name: addon.name || addon.id,
    version: addon.version || "",
    description: addon.description || "",
    icon: addon.iconURL || "",
    enabled: !addon.userDisabled,
    active: !!addon.isActive,
    appDisabled: !!addon.appDisabled,
    blocked: (addon.blocklistState || 0) > 0,
    signed: isSigned(addon),
    temporary: !!addon.temporarilyInstalled,
    pendingUninstall: isPendingUninstall(addon),
    options: !!addon.optionsURL,
    canToggle: !!(permissions & (AM.PERM_CAN_ENABLE | AM.PERM_CAN_DISABLE)),
    canRemove: !!(permissions & AM.PERM_CAN_UNINSTALL),
  };
}

function installErrorCode(install) {
  const AM = lazy.AddonManager;
  switch (install?.error) {
    case AM.ERROR_NETWORK_FAILURE:
      return "network";
    case AM.ERROR_INCORRECT_HASH:
    case AM.ERROR_CORRUPT_FILE:
      return "corrupt";
    case AM.ERROR_FILE_ACCESS:
      return "file";
    case AM.ERROR_SIGNEDSTATE_REQUIRED:
      return "unsigned";
    case AM.ERROR_BLOCKLISTED:
    case AM.ERROR_SOFT_BLOCKED:
      return "blocked";
    case AM.ERROR_INCOMPATIBLE:
      return "incompatible";
    case AM.ERROR_UNSUPPORTED_ADDON_TYPE:
    case AM.ERROR_UNEXPECTED_ADDON_TYPE:
      return "notExtension";
    case AM.ERROR_ADMIN_INSTALL_ONLY:
      return "policy";
    default:
      return "unknown";
  }
}

/**
 * Take an AddonInstall to the end. `vet(addon)` sees the downloaded add-on
 * before anything is installed and throws an ExtensionError to refuse it.
 * Resolves with the installed (and started) add-on.
 */
async function runInstall(install, { vet, onProgress } = {}) {
  if (!install) {
    throw new ExtensionError("corrupt");
  }
  let refusal = null;
  install.promptHandler = async ({ addon }) => {
    try {
      vet?.(addon);
    } catch (error) {
      refusal = error instanceof ExtensionError ? error : new ExtensionError("unknown", String(error));
      // A rejection without a value cancels the install quietly.
      throw undefined; // eslint-disable-line no-throw-literal
    }
  };
  const listener = {
    onDownloadProgress: current => {
      onProgress?.(current.maxProgress > 0 ? Math.min(1, current.progress / current.maxProgress) : null);
    },
  };
  install.addListener(listener);
  try {
    return await install.install();
  } catch (error) {
    throw refusal || new ExtensionError(installErrorCode(install), String(error?.message || error));
  } finally {
    install.removeListener(listener);
  }
}

function vetExtension(addon) {
  if (addon?.type !== "extension") {
    throw new ExtensionError("notExtension");
  }
  if (addon.id === SELF_ID) {
    throw new ExtensionError("self");
  }
}

/** Install an AMO extension described by amoLookup(). */
export async function installFromAmo(info, { onProgress } = {}) {
  if (!info?.guid || !isAmoUrl(info.url, "/firefox/downloads/") || !/^sha256:[0-9a-f]{64}$/.test(info.hash || "")) {
    throw new ExtensionError("notFound");
  }
  const install = await lazy.AddonManager.getInstallForURL(info.url, {
    hash: info.hash,
    name: info.name || undefined,
    icons: info.icon ? { 64: info.icon } : undefined,
    version: info.version || undefined,
    telemetryInfo: { source: "internal" },
  });
  return runInstall(install, {
    onProgress,
    vet: addon => {
      vetExtension(addon);
      if (addon.id !== info.guid) {
        throw new ExtensionError("mismatch");
      }
      if (!isSigned(addon)) {
        throw new ExtensionError("unsigned");
      }
    },
  });
}

/** Install a local .xpi the user picked. Unsigned files are allowed (and shown as such). */
export async function installFromFile(file, { onProgress } = {}) {
  let install;
  try {
    install = await lazy.AddonManager.getInstallForFile(file, null, { source: "internal" });
  } catch (error) {
    throw new ExtensionError("file", String(error?.message || error));
  }
  if (install?.state === lazy.AddonManager.STATE_DOWNLOAD_FAILED) {
    throw new ExtensionError(installErrorCode(install));
  }
  return runInstall(install, { onProgress, vet: vetExtension });
}
