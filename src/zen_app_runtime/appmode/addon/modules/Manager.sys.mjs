/* zen-app-runtime app mode: Web Apps' link to the zen-app CLI.
 *
 * The manager never edits an app itself: every action is a `zen-app … --json`
 * call, so the CLI stays the single source of truth for definitions,
 * profiles and launchers. Calls run without a shell (an argument array,
 * values as --flag=value so they are never read as options) and time out.
 */

import { t } from "resource://zenapp/modules/Strings.sys.mjs";

const lazy = {};
ChromeUtils.defineESModuleGetters(lazy, {
  Subprocess: "resource://gre/modules/Subprocess.sys.mjs",
  clearTimeout: "resource://gre/modules/Timer.sys.mjs",
  setTimeout: "resource://gre/modules/Timer.sys.mjs",
});

const CLI_TIMEOUT_MS = 120000;
/* What the new-app wizard may download from a website. */
const FETCH_TIMEOUT_MS = 15000;
const PAGE_LIMIT = 2 * 1024 * 1024;
const MANIFEST_LIMIT = 256 * 1024;
const IMAGE_LIMIT = 1024 * 1024;
const PICKED_LIMIT = 4 * 1024 * 1024;
const ICONS_KEPT = 12;
const PNG_MAGIC = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
/** This window's identity: the apps the CLI starts must not inherit it. */
const DROP_ENV = new Set([
  "MOZ_APP_REMOTINGNAME",
  "MOZ_DESKTOP_FILE_NAME",
  "MOZ_LAUNCHED_CHILD",
  "MOZ_NO_REMOTE",
  "ZEN_APP_ID",
  "PULSE_PROP_OVERRIDE",
  "DESKTOP_STARTUP_ID",
  "XDG_ACTIVATION_TOKEN",
]);
const PRESET_KEY = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;

/** A failed CLI call; `code` is the CLI's error code (or noCli / io). */
export class CliError extends Error {
  constructor(code, message) {
    super(message || code);
    this.code = code;
  }
}

function cliEnvironment() {
  const environment = lazy.Subprocess.getEnvironment();
  for (const key of Object.keys(environment)) {
    if (DROP_ENV.has(key) || key.startsWith("XRE_") || key.startsWith("MOZ_CRASHREPORTER")) {
      delete environment[key];
    }
  }
  return environment;
}

async function readAll(pipe) {
  let text = "";
  for (;;) {
    const chunk = await pipe.readString();
    if (!chunk) {
      return text;
    }
    text += chunk;
  }
}

/** Run `shim ARGS… --json`; the parsed output, or a CliError. */
export async function runCli(shim, args, { timeoutMs = CLI_TIMEOUT_MS } = {}) {
  if (!shim) {
    throw new CliError("noCli", "zen-app is not installed");
  }
  let process;
  try {
    process = await lazy.Subprocess.call({
      command: shim,
      arguments: [...args, "--json"],
      environment: cliEnvironment(),
      stderr: "pipe",
    });
  } catch (error) {
    throw new CliError("noCli", String(error?.message || error));
  }
  process.stdin.close().catch(() => {});
  const timer = lazy.setTimeout(() => process.kill(), timeoutMs);
  try {
    const [stdout, stderr, { exitCode }] = await Promise.all([readAll(process.stdout), readAll(process.stderr), process.wait()]);
    let data;
    try {
      data = JSON.parse(stdout);
    } catch {
      throw new CliError("io", (stderr.trim() || stdout.trim() || `zen-app exited with ${exitCode}`).slice(0, 600));
    }
    if (data && data.ok === false) {
      throw new CliError(data.code || "invalid", data.message || "");
    }
    return data;
  } finally {
    lazy.clearTimeout(timer);
  }
}

/**
 * Download `url` (https, or a data: image) without cookies, following
 * redirects only to https, within `limit` bytes and FETCH_TIMEOUT_MS.
 */
async function limitedFetch(url, { limit, accept }) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new CliError("badUrl", `not an address: ${url}`);
  }
  const inline = parsed.protocol === "data:";
  if (!inline && (parsed.protocol !== "https:" || parsed.username || parsed.password)) {
    throw new CliError("badUrl", "only https:// addresses are read");
  }
  // AbortSignal.timeout() needs a window; system modules have none.
  const abort = new AbortController();
  const timer = lazy.setTimeout(() => abort.abort(), FETCH_TIMEOUT_MS);
  try {
    let response;
    try {
      response = await fetch(parsed.href, { credentials: "omit", redirect: "follow", signal: abort.signal, headers: { Accept: accept } });
    } catch (error) {
      throw new CliError("network", String(error?.message || error));
    }
    if (!inline && !String(response.url || parsed.href).startsWith("https://")) {
      throw new CliError("badUrl", "the site redirected away from https");
    }
    if (!response.ok) {
      throw new CliError("network", `HTTP ${response.status}`);
    }
    if (Number(response.headers.get("content-length") || 0) > limit) {
      throw new CliError("tooLarge", `more than ${limit} bytes`);
    }
    let bytes;
    if (response.body?.getReader) {
      const reader = response.body.getReader();
      const chunks = [];
      let size = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        size += value.length;
        if (size > limit) {
          abort.abort();
          throw new CliError("tooLarge", `more than ${limit} bytes`);
        }
        chunks.push(value);
      }
      bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) {
        bytes.set(chunk, offset);
        offset += chunk.length;
      }
    } else {
      bytes = new Uint8Array(await response.arrayBuffer());
      if (bytes.length > limit) {
        throw new CliError("tooLarge", `more than ${limit} bytes`);
      }
    }
    return {
      url: response.url || parsed.href,
      status: response.status,
      contentType: response.headers.get("content-type") || "",
      bytes,
    };
  } finally {
    lazy.clearTimeout(timer);
  }
}

/** Arguments of `zen-app new` for the wizard's choices. */
export function newAppArguments({ source, id, name, hosts, authHosts, accent, icon, appearance }) {
  const text = String(source ?? "").trim();
  if (!/^https:\/\//i.test(text) && !PRESET_KEY.test(text)) {
    throw new CliError("badSource", "not an https:// address");
  }
  const args = ["new", text];
  const option = (flag, value) => {
    if (value !== undefined && value !== null && String(value) !== "") {
      args.push(`${flag}=${value}`);
    }
  };
  option("--id", id);
  option("--name", typeof name === "string" ? name.trim() : name);
  for (const host of hosts || []) {
    option("--host", host);
  }
  for (const host of authHosts || []) {
    option("--auth-host", host);
  }
  option("--accent", accent);
  option("--icon", icon);
  if (appearance === "transparent" || appearance === "opaque") {
    args.push(`--${appearance}`);
  }
  return args;
}

export class Manager {
  constructor(app) {
    this.app = app;
  }

  get shim() {
    return this.app.store?.config.launcher?.shim || null;
  }

  /* ------------------------------------------------------ website */

  /** The page at `url` (redirects followed; https only), with its hosts. */
  async fetchPage(url) {
    const response = await limitedFetch(url, { limit: PAGE_LIMIT, accept: "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5" });
    const charset = /charset=([\w-]+)/i.exec(response.contentType)?.[1] || "utf-8";
    let html;
    try {
      html = new TextDecoder(charset).decode(response.bytes);
    } catch {
      html = new TextDecoder("utf-8").decode(response.bytes);
    }
    const describe = address => {
      const host = new URL(address).hostname.toLowerCase();
      let baseDomain = null;
      try {
        baseDomain = Services.eTLD.getBaseDomainFromHost(host);
      } catch {
        baseDomain = null;
      }
      return { host, baseDomain };
    };
    return {
      url: response.url,
      status: response.status,
      contentType: response.contentType,
      html: /html|xml/i.test(response.contentType) || !response.contentType ? html : "",
      start: describe(url),
      final: describe(response.url),
    };
  }

  /** A web app manifest (JSON), or null. */
  async fetchManifest(url) {
    try {
      const response = await limitedFetch(url, { limit: MANIFEST_LIMIT, accept: "application/manifest+json,application/json;q=0.9,*/*;q=0.5" });
      const data = JSON.parse(new TextDecoder("utf-8").decode(response.bytes));
      return data && typeof data === "object" && !Array.isArray(data) ? data : null;
    } catch (error) {
      console.warn(`zen-app: no web manifest (${error?.message || error})`);
      return null;
    }
  }

  /** An image of the site (https or data:), as bytes for the page to decode. */
  async fetchImage(url) {
    const response = await limitedFetch(url, { limit: IMAGE_LIMIT, accept: "image/*" });
    return { bytes: response.bytes, type: response.contentType.split(";")[0].trim() };
  }

  /** Store the wizard's 256 px PNG icon; the path goes to `zen-app new --icon`. */
  async saveIcon(bytes) {
    const data = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    if (data.length > IMAGE_LIMIT || !PNG_MAGIC.every((value, index) => data[index] === value)) {
      throw new CliError("badIcon", "not a PNG");
    }
    const directory = this.#iconDirectory;
    await IOUtils.makeDirectory(directory, { ignoreExisting: true, permissions: 0o700 });
    const path = PathUtils.join(directory, `icon-${Date.now()}-${Math.floor(Math.random() * 1e6)}.png`);
    await IOUtils.write(path, data, { tmpPath: path + ".tmp" });
    await IOUtils.setPermissions(path, 0o600);
    await this.#pruneIcons();
    return path;
  }

  /** Native file picker for an icon image; null when cancelled. */
  async pickIconFile() {
    const win = this.app.mainWindow?.win || Services.wm.getMostRecentWindow("navigator:browser");
    const picker = Cc["@mozilla.org/filepicker;1"].createInstance(Ci.nsIFilePicker);
    picker.init(win.browsingContext, t("manager.icon.pick"), Ci.nsIFilePicker.modeOpen);
    picker.appendFilter("PNG, SVG, ICO, JPEG, WebP", "*.png; *.svg; *.ico; *.jpg; *.jpeg; *.webp");
    picker.appendFilters(Ci.nsIFilePicker.filterAll);
    const result = await new Promise(resolve => picker.open(resolve));
    if (result !== Ci.nsIFilePicker.returnOK || !picker.file) {
      return null;
    }
    if (picker.file.fileSize > PICKED_LIMIT) {
      throw new CliError("badIcon", "too large");
    }
    const bytes = await IOUtils.read(picker.file.path);
    const extension = picker.file.leafName.split(".").pop().toLowerCase();
    const type = { png: "image/png", svg: "image/svg+xml", ico: "image/x-icon", jpg: "image/jpeg", jpeg: "image/jpeg", webp: "image/webp" }[extension] || "";
    return { bytes, type, name: picker.file.leafName };
  }

  get #iconDirectory() {
    return PathUtils.join(PathUtils.profileDir, "zen-app", "wizard");
  }

  async #pruneIcons() {
    let children = [];
    try {
      children = (await IOUtils.getChildren(this.#iconDirectory)).filter(path => /icon-[\d-]+\.png$/.test(path));
    } catch {
      return;
    }
    children.sort();
    for (const path of children.slice(0, Math.max(0, children.length - ICONS_KEPT))) {
      await IOUtils.remove(path, { ignoreAbsent: true });
    }
  }

  /* ---------------------------------------------------------- CLI */

  run(args) {
    return runCli(this.shim, args);
  }

  apps() {
    return this.run(["apps"]);
  }

  presets() {
    return this.run(["presets"]);
  }

  open(id) {
    return this.run(["run", String(id)]);
  }

  settings(id) {
    return this.run(["settings", String(id)]);
  }

  refresh(id) {
    return this.run(["refresh", String(id)]);
  }

  remove(id, { purge = false } = {}) {
    return this.run(["rm", String(id), "--yes", ...(purge ? ["--purge"] : [])]);
  }

  dryRun(choices) {
    return this.run([...newAppArguments(choices), "--dry-run"]);
  }

  create(choices) {
    return this.run(newAppArguments(choices));
  }
}
