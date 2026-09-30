/* zen-app-runtime app mode: navigation scope and external link routing. */

const lazy = {};
ChromeUtils.defineESModuleGetters(lazy, {
  Subprocess: "resource://gre/modules/Subprocess.sys.mjs",
});

// Schemes that must never be handed to the desktop (they are either internal
// to Gecko or would let content reach local files/privileged surfaces).
const NEVER_EXTERNAL = new Set([
  "about",
  "blob",
  "chrome",
  "data",
  "file",
  "jar",
  "javascript",
  "moz-extension",
  "moz-icon",
  "moz-page-thumb",
  "moz-src",
  "resource",
  "view-source",
  "wyciwyg",
]);

// Environment variables that carry this app's window identity. They must not
// leak into the default browser that xdg-open may start.
const IDENTITY_ENV = [
  "MOZ_APP_REMOTINGNAME",
  "MOZ_DESKTOP_FILE_NAME",
  "DESKTOP_STARTUP_ID",
  "XDG_ACTIVATION_TOKEN",
  "ZEN_APP_ID",
  "PULSE_PROP_OVERRIDE",
];

export function hostMatches(host, pattern) {
  host = (host || "").toLowerCase().replace(/\.$/, "");
  if (pattern.startsWith("*.")) {
    const suffix = pattern.slice(2);
    return host === suffix || host.endsWith("." + suffix);
  }
  return host === pattern;
}

const ROOT_PATHS = new Set(["", "/", "/home.php", "/index.php"]);

export function pathOf(uri) {
  try {
    return uri.pathQueryRef.split(/[?#]/)[0] || "/";
  } catch {
    return "/";
  }
}

export function pathMatchesPrefix(path, prefix) {
  if (prefix.endsWith("/")) {
    return path.startsWith(prefix) || path === prefix.slice(0, -1);
  }
  return path.startsWith(prefix) && (path.length === prefix.length || "/.".includes(path[prefix.length]));
}

export class Scope {
  constructor(definition) {
    this.hosts = definition.scope.hosts || [];
    this.paths = definition.scope.paths || [];
    this.authHosts = definition.authHosts || [];
    this.startURI = Services.io.newURI(definition.startUrl);
  }

  #host(uri) {
    try {
      return uri.host;
    } catch {
      return "";
    }
  }

  /** The app itself: stays in the main window. */
  isInApp(uri) {
    if (!uri || !uri.schemeIs("https")) {
      return false;
    }
    const host = this.#host(uri);
    if (!this.hosts.some(pattern => hostMatches(host, pattern))) {
      return false;
    }
    if (!this.paths.length) {
      return true;
    }
    const path = pathOf(uri);
    return ROOT_PATHS.has(path) || this.paths.some(prefix => pathMatchesPrefix(path, prefix));
  }

  /** Sign-in and consent hops that must return to the app. */
  isAuth(uri) {
    if (!uri || !uri.schemeIs("https")) {
      return false;
    }
    const host = this.#host(uri);
    return this.authHosts.some(pattern => hostMatches(host, pattern));
  }

  isAllowedInside(uri) {
    return this.isInApp(uri) || this.isAuth(uri);
  }

  /** Scoped root ("/") of a path-limited app goes to the start page instead. */
  rootRedirect(uri) {
    if (!this.paths.length || !uri || !uri.schemeIs("https")) {
      return null;
    }
    if (ROOT_PATHS.has(pathOf(uri)) && this.hosts.some(p => hostMatches(this.#host(uri), p))) {
      return this.startURI;
    }
    return null;
  }

  /** Origins that receive the app's permission grants. */
  grantOrigins() {
    const origins = new Set([this.startURI.prePath]);
    for (const pattern of this.hosts) {
      if (!pattern.startsWith("*.")) {
        origins.add(`https://${pattern}`);
      }
    }
    return [...origins];
  }
}

export function isInternalPage(uri) {
  if (!uri) {
    return true;
  }
  if (uri.schemeIs("about")) {
    return true;
  }
  return uri.schemeIs("chrome") && uri.spec.startsWith("chrome://zenapp/");
}

export function canOpenExternally(uri) {
  if (!uri) {
    return false;
  }
  const scheme = uri.scheme.toLowerCase();
  return /^[a-z][a-z0-9+.-]*$/.test(scheme) && !NEVER_EXTERNAL.has(scheme);
}

let xdgOpenPath = null;

async function findOpener() {
  if (xdgOpenPath) {
    return xdgOpenPath;
  }
  for (const name of ["xdg-open", "gio"]) {
    try {
      xdgOpenPath = { path: await lazy.Subprocess.pathSearch(name), name };
      return xdgOpenPath;
    } catch {
      // try the next candidate
    }
  }
  return null;
}

/** Open a URI with the desktop's default handler (never through a shell). */
export async function openExternally(uri) {
  if (!canOpenExternally(uri)) {
    console.warn("zen-app: refusing to open externally:", uri?.spec);
    return false;
  }
  const opener = await findOpener();
  if (!opener) {
    console.error("zen-app: neither xdg-open nor gio is available");
    return false;
  }
  const environment = lazy.Subprocess.getEnvironment();
  for (const name of IDENTITY_ENV) {
    delete environment[name];
  }
  const args = opener.name === "gio" ? ["open", uri.spec] : [uri.spec];
  try {
    const process = await lazy.Subprocess.call({
      command: opener.path,
      arguments: args,
      environment,
    });
    process.wait().catch(() => {});
    return true;
  } catch (error) {
    console.error("zen-app: external open failed", error);
    return false;
  }
}
