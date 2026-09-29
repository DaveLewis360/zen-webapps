/* zen-app-runtime app mode: the privileged runtime of one app profile. */

import { AppWindow, STUDIO_URL } from "resource://zenapp/modules/AppWindow.sys.mjs";
import {
  ConfigStore,
  applyPrefs,
  resolvedWindowChrome,
  settingsToPrefs,
} from "resource://zenapp/modules/Config.sys.mjs";
import { paletteColors, paletteToCSSVariables, resolvePalette } from "resource://zenapp/modules/Palette.sys.mjs";
import { Scope, openExternally } from "resource://zenapp/modules/Routing.sys.mjs";
import { getLocale, setLocale, t } from "resource://zenapp/modules/Strings.sys.mjs";
import { StyleCatalog, dataSheetURI, featureKey } from "resource://zenapp/modules/Styles.sys.mjs";
import {
  CUSTOM_CSS_KEY,
  MAX_CUSTOM_CSS_BYTES,
  MAX_IMPORT_BYTES,
  MAX_IMPORT_DEPTH,
  compileUserCss,
  importHeader,
  importedFrom,
  stripImportHeader,
} from "resource://zenapp/modules/UserCss.sys.mjs";

const lazy = {};
ChromeUtils.defineESModuleGetters(lazy, {
  Downloads: "resource://gre/modules/Downloads.sys.mjs",
  GMPInstallManager: "resource://gre/modules/GMPInstallManager.sys.mjs",
  clearInterval: "resource://gre/modules/Timer.sys.mjs",
  clearTimeout: "resource://gre/modules/Timer.sys.mjs",
  setInterval: "resource://gre/modules/Timer.sys.mjs",
  setTimeout: "resource://gre/modules/Timer.sys.mjs",
});

/** How long a downloaded @import of a custom CSS is used before re-fetching. */
const IMPORT_MAX_AGE_MS = 24 * 60 * 60 * 1000;
/** How often the wallpaper colour scheme is checked for changes. */
const SCHEME_POLL_MS = 2000;

function sha256Hex(text) {
  const hasher = Cc["@mozilla.org/security/hash;1"].createInstance(Ci.nsICryptoHash);
  hasher.init(Ci.nsICryptoHash.SHA256);
  const bytes = new TextEncoder().encode(text);
  hasher.update(bytes, bytes.length);
  return Array.from(hasher.finish(false), c => c.charCodeAt(0).toString(16).padStart(2, "0")).join("");
}

const AlertNotification = Components.Constructor(
  "@mozilla.org/alert-notification;1",
  "nsIAlertNotification",
  "initWithObject"
);

const COMMAND_LINE_CONTRACT = "@zen-app-runtime/app-mode-command-line;1";
const COMMAND_LINE_CID = Components.ID("{6f1a4b52-86a3-4f6e-9a39-2d3a1d7c5e41}");
const COMMAND_LINE_CATEGORY_ENTRY = "a-zen-app-runtime";

/** A custom CSS the app refuses; `code` picks the message (css.error.<code>). */
export class CustomCssError extends Error {
  constructor(code) {
    super(`custom CSS: ${code}`);
    this.code = code;
  }
}

class ZenAppRuntime {
  started = false;
  store = null;
  scope = null;
  palette = null;
  paletteVariables = {};
  windows = new Set();
  mainWindow = null;
  addonVersion = "";
  widevine = { state: "unknown" };
  _listeners = new Set();
  _sheets = [];
  styles = null;
  customCss = { state: "none" };
  _customCssText = null;
  _customCssStamp = null;
  _customCssLoading = null;
  _customCssKey = null;
  _cssImports = {};
  _cssImportsChecked = new Set();
  _cssActor = false;
  _schemeStamp = null;
  _schemeTimer = null;
  _observers = [];
  _pendingAction = null;
  _widevinePromise = null;
  _commandLineFactory = null;

  get id() {
    return this.store.config.appId;
  }
  get name() {
    return this.store.definition.name;
  }
  get settings() {
    return this.store.settings;
  }
  get definition() {
    return this.store.definition;
  }

  async startup({ version }) {
    if (this.started) {
      return;
    }
    this.started = true;
    this.addonVersion = version;
    this.store = await ConfigStore.load();
    this.#nameProcess();
    setLocale(this.store.config.locale || Services.locale.appLocaleAsBCP47);
    this.scope = new Scope(this.store.definition);
    applyPrefs(settingsToPrefs(this.settings, this.store.config));
    this.#applyPermissions();
    const catalog = this.store.config.zenInternet || {};
    this.styles = catalog.path ? await StyleCatalog.load(catalog.path, catalog) : new StyleCatalog({});
    this.#registerCssActor();
    await this.#reloadCustomCss();
    this.#registerCommandLine();
    await this.refreshPalette();
    this.#registerStyles();
    this.#refreshCssImports();
    this.#watchScheme();
    this._pendingAction = await this.store.takePendingAction();
    this.#observeWindows();
    this.#watchDownloads().catch(error => console.error("zen-app: downloads", error));
    this.#refreshWidevineState();
    if (this.settings.drm && this.widevine.state !== "installed") {
      this.ensureWidevine().catch(() => {});
    }
    await this.#writeStatus();
  }

  shutdown() {
    for (const { topic, observer } of this._observers.splice(0)) {
      try {
        Services.obs.removeObserver(observer, topic);
      } catch {
        // ignore
      }
    }
    if (this._schemeTimer) {
      lazy.clearInterval(this._schemeTimer);
      this._schemeTimer = null;
    }
    this.#unregisterStyles();
    try {
      Services.ppmm.sharedData.delete(CUSTOM_CSS_KEY);
      Services.ppmm.sharedData.flush();
    } catch {
      // ignore
    }
    if (this._cssActor) {
      try {
        ChromeUtils.unregisterWindowActor("ZenAppCss");
      } catch {
        // ignore
      }
      this._cssActor = false;
    }
    try {
      Services.catMan.deleteCategoryEntry("command-line-handler", COMMAND_LINE_CATEGORY_ENTRY, false);
      Components.manager
        .QueryInterface(Ci.nsIComponentRegistrar)
        .unregisterFactory(COMMAND_LINE_CID, this._commandLineFactory);
    } catch {
      // ignore
    }
    for (const appWindow of [...this.windows]) {
      appWindow.teardown();
    }
    this.started = false;
  }

  /* ------------------------------------------------------------ windows */

  /**
   * Run as `zen-app-<id>` instead of `zen-bin`, so `killall zen-bin` (aimed at
   * the browser) leaves the app alone. The launcher already starts the app
   * through a link with that name; this covers in-app restarts, which
   * re-execute the real zen-bin. Renaming the main thread renames the
   * process (/proc/<pid>/comm, 15 characters at most).
   */
  #nameProcess() {
    const name = String(this.store.config.desktopId || "").slice(0, 15);
    if (Services.appinfo.OS !== "Linux" || !/^[\w.-]+$/.test(name)) {
      return;
    }
    let libc = null;
    try {
      const { ctypes } = ChromeUtils.importESModule("resource://gre/modules/ctypes.sys.mjs");
      libc = ctypes.open("libc.so.6");
      const pthreadSelf = libc.declare("pthread_self", ctypes.default_abi, ctypes.unsigned_long);
      const setName = libc.declare(
        "pthread_setname_np",
        ctypes.default_abi,
        ctypes.int,
        ctypes.unsigned_long,
        ctypes.char.ptr
      );
      const result = setName(pthreadSelf(), name);
      if (result !== 0) {
        console.warn(`zen-app: could not rename the process (error ${result})`);
      }
    } catch (error) {
      console.warn("zen-app: could not rename the process", error);
    } finally {
      libc?.close();
    }
  }

  #observe(topic, observer) {
    Services.obs.addObserver(observer, topic);
    this._observers.push({ topic, observer });
  }

  #observeWindows() {
    const adopt = win => this.#adoptWindow(win);
    this.#observe("browser-window-before-show", adopt);
    this.#observe("browser-delayed-startup-finished", win => this.#windowReady(win));
    for (const win of Services.wm.getEnumerator("navigator:browser")) {
      if (win.gBrowser) {
        adopt(win);
        if (win.gBrowserInit?.delayedStartupFinished) {
          this.#windowReady(win);
        }
      }
    }
  }

  #adoptWindow(win) {
    if (!win?.gBrowser || [...this.windows].some(w => w.win === win)) {
      return;
    }
    const main = !this.mainWindow || this.mainWindow.win.closed;
    const appWindow = new AppWindow(this, win, { main });
    this.windows.add(appWindow);
    if (main) {
      this.mainWindow = appWindow;
    }
    try {
      appWindow.setup();
    } catch (error) {
      console.error("zen-app: window setup failed", error);
    }
  }

  async #windowReady(win) {
    const appWindow = [...this.windows].find(w => w.win === win);
    if (!appWindow || appWindow.ready) {
      return;
    }
    const zenReady = win.gZenWorkspaces?.promiseInitialized;
    if (zenReady) {
      await Promise.race([zenReady, new Promise(resolve => win.setTimeout(resolve, 4000))]);
    }
    await new Promise(resolve => win.setTimeout(resolve, 50));
    if (win.closed) {
      return;
    }
    appWindow.finishStartup();
    if (appWindow.main) {
      this.#showInitialPage(appWindow);
      const pending = this._pendingAction;
      this._pendingAction = null;
      if (pending?.action === "settings" && !this.store.needsOnboarding) {
        this.openSettings(win);
      } else if (pending?.action === "open" && pending.url) {
        this.openDeepLink(pending.url);
      }
      if (Services.prefs.getBoolPref("zenapp.debug", false)) {
        win.setTimeout(() => this.dumpDebugState(appWindow).catch(console.error), 2500);
      }
    }
  }

  /** Diagnostics for `zen-app` developers: what the window really looks like. */
  async dumpDebugState(appWindow = this.mainWindow) {
    const doc = appWindow.doc;
    const keys = [...doc.querySelectorAll("key")].map(key => ({
      id: key.id || null,
      keyset: key.parentElement?.id || null,
      key: key.getAttribute("key") || key.getAttribute("keycode") || null,
      modifiers: key.getAttribute("modifiers") || "",
      active: key.getAttribute("disabled") !== "true",
    }));
    const visible = id => {
      const element = doc.getElementById(id);
      return element ? element.checkVisibility() : null;
    };
    const state = {
      title: doc.title,
      uri: appWindow.currentURI?.spec || null,
      tabs: appWindow.gBrowser.tabs.length,
      root: Object.fromEntries([...doc.documentElement.attributes].filter(a => a.name.startsWith("zenapp")).map(a => [a.name, a.value])),
      activeKeys: keys.filter(k => k.active),
      suppressedKeys: keys.filter(k => !k.active).length,
      chromeVisible: {
        "navigator-toolbox": visible("navigator-toolbox"),
        "nav-bar": visible("nav-bar"),
        "zen-appcontent-navbar-wrapper": visible("zen-appcontent-navbar-wrapper"),
        "toolbar-menubar": visible("toolbar-menubar"),
      },
      prefs: Object.fromEntries(
        ["browser.tabs.allow_transparent_browser", "zen.widget.linux.transparency", "browser.tabs.inTitlebar"].map(name => [
          name,
          Services.prefs.getPrefType(name) === Services.prefs.PREF_BOOL ? Services.prefs.getBoolPref(name) : Services.prefs.getIntPref(name, -1),
        ])
      ),
      remotingName: Services.appinfo.remotingName,
      windows: this.windows.size,
    };
    const path = PathUtils.join(ConfigStore.directory, "debug.json");
    await IOUtils.writeUTF8(path, JSON.stringify(state, null, 2) + "\n");
    return state;
  }

  windowClosed(appWindow) {
    this.windows.delete(appWindow);
    if (appWindow === this.mainWindow) {
      this.mainWindow = null;
      const others = [...this.windows].filter(w => !w.win.closed);
      if (others.length) {
        // The main window is the app; secondary windows (popups) go with it.
        Services.startup.quit(Ci.nsIAppStartup.eAttemptQuit);
      }
    }
  }

  #showInitialPage(appWindow) {
    if (this.store.needsOnboarding) {
      appWindow.loadInAppBrowser(Services.io.newURI(`${STUDIO_URL}?mode=onboarding`), { replace: true });
      return;
    }
    const current = appWindow.currentURI;
    const blank = !current || current.spec === "about:blank";
    if (blank && !appWindow.appBrowser.webProgress?.isLoadingDocument) {
      appWindow.loadInAppBrowser(this.scope.startURI, { replace: true });
    }
  }

  loadInApp(uri, options = {}) {
    const target = typeof uri === "string" ? Services.io.newURI(uri) : uri;
    this.mainWindow?.loadInAppBrowser(target, options);
  }

  openExternal(url) {
    try {
      return openExternally(Services.io.newURI(url));
    } catch (error) {
      console.error("zen-app: bad external URL", error);
      return false;
    }
  }

  openDeepLink(url) {
    let uri;
    try {
      uri = Services.io.newURI(url);
    } catch {
      return;
    }
    if (this.scope.isAllowedInside(uri)) {
      this.loadInApp(this.scope.rootRedirect(uri) || uri);
    } else {
      openExternally(uri);
    }
  }

  openSettings(win) {
    const target = win && !win.closed ? win : this.mainWindow?.win;
    if (!target) {
      return;
    }
    const appWindow = [...this.windows].find(w => w.win === target) || this.mainWindow;
    if (appWindow?.isShowingStudio()) {
      return;
    }
    target.focus();
    const browser = target.gBrowser.selectedBrowser;
    const box = target.gBrowser.getTabDialogBox(browser);
    const { dialog } = box.open(`${STUDIO_URL}?mode=settings`, {
      features: "resizable=no",
      allowDuplicateDialogs: false,
      keepOpenSameOriginNav: true,
    });
    dialog?._box?.setAttribute("zenapp-dialog", "");
  }

  /* --------------------------------------------------- command line */

  #registerCommandLine() {
    const app = this;
    const handler = {
      QueryInterface: ChromeUtils.generateQI(["nsICommandLineHandler"]),
      handle(cmdLine) {
        app.#handleCommandLine(cmdLine);
      },
      helpInfo: "",
    };
    this._commandLineFactory = {
      QueryInterface: ChromeUtils.generateQI(["nsIFactory"]),
      createInstance(iid) {
        return handler.QueryInterface(iid);
      },
    };
    const registrar = Components.manager.QueryInterface(Ci.nsIComponentRegistrar);
    if (!registrar.isCIDRegistered(COMMAND_LINE_CID)) {
      registrar.registerFactory(COMMAND_LINE_CID, "zen-app command line", COMMAND_LINE_CONTRACT, this._commandLineFactory);
    }
    Services.catMan.addCategoryEntry(
      "command-line-handler",
      COMMAND_LINE_CATEGORY_ENTRY,
      COMMAND_LINE_CONTRACT,
      false,
      true
    );
  }

  #handleCommandLine(cmdLine) {
    const action = cmdLine.handleFlagWithParam("zen-app-action", false);
    const openUrl = cmdLine.handleFlagWithParam("zen-app-open", false);
    if (cmdLine.state === Ci.nsICommandLine.STATE_INITIAL_LAUNCH) {
      if (action || openUrl) {
        this._pendingAction = { action: action || "open", url: openUrl || null };
      }
      return;
    }
    // Remote invocation of a running app: focus it, never open browser tabs.
    const extra = [];
    for (let i = 0; i < cmdLine.length; i++) {
      extra.push(cmdLine.getArgument(i));
    }
    if (cmdLine.length) {
      cmdLine.removeArguments(0, cmdLine.length - 1);
    }
    cmdLine.preventDefault = true;
    const win = this.mainWindow?.win;
    if (!win) {
      return;
    }
    win.focus();
    const url = openUrl || extra.find(arg => /^https?:\/\//i.test(arg));
    if (url) {
      this.openDeepLink(url);
    }
    if (action === "settings") {
      this.openSettings(win);
    }
  }

  /* ------------------------------------------------------- settings */

  async updateSettings(patch) {
    const before = { ...this.settings };
    const settings = await this.store.updateSettings(patch);
    applyPrefs(settingsToPrefs(settings, this.store.config));
    this.#applyPermissions();
    if (
      before.appearance !== settings.appearance ||
      before.zenInternet !== settings.zenInternet ||
      before.customCss !== settings.customCss ||
      JSON.stringify(before.disabledFeatures || []) !== JSON.stringify(settings.disabledFeatures || [])
    ) {
      this.#registerStyles();
    }
    if (
      before.palette !== settings.palette ||
      before.colorScheme !== settings.colorScheme
    ) {
      await this.refreshPalette();
    }
    for (const appWindow of this.windows) {
      appWindow.applySettings();
    }
    if (settings.drm && !before.drm && this.widevine.state !== "installed") {
      this.ensureWidevine().catch(() => {});
    }
    this.#emit("settings");
    return settings;
  }

  previewTint(value) {
    for (const appWindow of this.windows) {
      appWindow.doc.documentElement.style.setProperty("--zenapp-tint", String(value));
    }
  }

  async refreshPalette() {
    const win = Services.wm.getMostRecentWindow("navigator:browser");
    const systemDark = win
      ? win.matchMedia("(prefers-color-scheme: dark)").matches
      : Services.appinfo.chromeColorSchemeIsDark ?? true;
    this.palette = await resolvePalette(
      this.settings,
      this.definition.accent || "#7b6cff",
      this.store.config.schemePaths || [],
      systemDark
    );
    this.paletteVariables = paletteToCSSVariables(this.palette);
    for (const appWindow of this.windows) {
      appWindow.applySettings();
    }
    // A theme with a Caelestia colour block follows the new colours too.
    this.#publishCustomCss();
    this.#emit("palette");
  }

  #applyPermissions() {
    const settings = this.settings;
    const perms = Services.perms;
    for (const origin of this.scope.grantOrigins()) {
      let principal;
      try {
        principal = Services.scriptSecurityManager.createContentPrincipalFromOrigin(origin);
      } catch {
        continue;
      }
      const set = (type, allow) =>
        perms.addFromPrincipal(principal, type, allow ? perms.ALLOW_ACTION : perms.DENY_ACTION, perms.EXPIRE_NEVER);
      set("desktop-notification", settings.notifications);
      set("camera", settings.media);
      set("microphone", settings.media);
      set("autoplay-media", settings.autoplay);
      set("popup", true);
      set("persistent-storage", true);
    }
  }

  /** Hosts the app shows: its own scope and the sign-in hops. */
  get styledHosts() {
    return [...new Set([...(this.scope.hosts || []), ...(this.scope.authHosts || [])])];
  }

  #registerStyles() {
    this.#unregisterStyles();
    const service = Cc["@mozilla.org/content/style-sheet-service;1"].getService(Ci.nsIStyleSheetService);
    const translucent = this.settings.appearance === "transparent";
    const register = uri => {
      try {
        // User sheets: @-moz-document scoping only works there, and the
        // catalog is !important-based (96% of declarations), like Zen Internet.
        service.loadAndRegisterSheet(uri, service.USER_SHEET);
        this._sheets.push(uri);
      } catch (error) {
        console.error(`zen-app: could not load style ${uri.spec.slice(0, 80)}`, error);
      }
    };
    if (this.settings.zenInternet !== false && this.styles) {
      for (const sheet of this.styles.sheets(this.styledHosts, {
        translucent,
        disabled: this.settings.disabledFeatures,
        exactOnly: new Set(this.scope.authHosts || []),
        aliases: this.store.config.styleAliases || {},
      })) {
        register(dataSheetURI(sheet.css));
      }
    }
    const extras = this.store.config.extras || {};
    for (const id of this.definition.extras || []) {
      const extra = extras[id];
      if (extra && (extra.when === "always" || (extra.when === "glass" && translucent))) {
        register(Services.io.newURI(`resource://zenapp/themes/${extra.file}`));
      }
    }
    // The user's own CSS goes into the pages themselves (ZenAppCss actor).
    this.#publishCustomCss();
  }

  #unregisterStyles() {
    const service = Cc["@mozilla.org/content/style-sheet-service;1"].getService(Ci.nsIStyleSheetService);
    for (const uri of this._sheets.splice(0)) {
      try {
        if (service.sheetRegistered(uri, service.USER_SHEET)) {
          service.unregisterSheet(uri, service.USER_SHEET);
        }
      } catch {
        // ignore
      }
    }
  }

  styleSites() {
    if (!this.styles) {
      return [];
    }
    const off = new Set(this.settings.disabledFeatures || []);
    return this.styles.match(this.styledHosts, new Set(this.scope.authHosts || [])).map(entry => ({
      ...entry,
      features: entry.features.map(feature => ({
        ...feature,
        enabled: !off.has(featureKey(entry.site, feature.name)),
      })),
    }));
  }

  async setStyleFeature(site, feature, enabled) {
    const key = featureKey(site, feature);
    const off = new Set(this.settings.disabledFeatures || []);
    if (enabled) {
      off.delete(key);
    } else {
      off.add(key);
    }
    return this.updateSettings({ disabledFeatures: [...off] });
  }

  /* ----------------------------------------------------- custom CSS */

  get customCssPath() {
    return this.store.config.customCss?.path || null;
  }

  #customCssScope() {
    return {
      hosts: this.styledHosts,
      aliases: this.store.config.styleAliases || {},
      startUrl: this.definition.startUrl || "",
    };
  }

  /** Re-read <library>/<id>.css if it changed on disk; true when it did. */
  #reloadCustomCss() {
    this._customCssLoading ??= this.#loadCustomCss().finally(() => {
      this._customCssLoading = null;
    });
    return this._customCssLoading;
  }

  async #loadCustomCss() {
    const path = this.customCssPath;
    if (!path) {
      this.customCss = { state: "unsupported" };
      return false;
    }
    let stat;
    try {
      stat = await IOUtils.stat(path);
    } catch {
      const had = this._customCssText !== null || this.customCss.state !== "none";
      this._customCssText = null;
      this._customCssStamp = null;
      this.customCss = { state: "none" };
      return had;
    }
    const stamp = `${stat.lastModified}:${stat.size}`;
    if (stamp === this._customCssStamp) {
      return false;
    }
    this._customCssStamp = stamp;
    this._customCssText = null;
    if (stat.size > MAX_CUSTOM_CSS_BYTES) {
      this.customCss = { state: "error", error: "tooLarge", bytes: stat.size };
      return true;
    }
    try {
      const text = await IOUtils.readUTF8(path);
      this._customCssText = text;
      this.customCss = { state: "loaded", bytes: stat.size, fileName: importedFrom(text) };
      // Imports already downloaded earlier: no flash of the unstyled theme.
      await this.#loadCssImports({ network: false });
    } catch (error) {
      console.error("zen-app: cannot read the custom CSS", error);
      this.customCss = { state: "error", error: "unreadable", bytes: stat.size };
    }
    return true;
  }

  async #customCssChanged() {
    this.#publishCustomCss();
    this.#refreshCssImports();
    this.#emit("customCss");
    await this.#writeStatus();
  }

  #compileCustomCss() {
    return compileUserCss(this._customCssText, {
      ...this.#customCssScope(),
      colors: paletteColors(this.palette),
      imports: this._cssImports,
    });
  }

  #registerCssActor() {
    try {
      ChromeUtils.registerWindowActor("ZenAppCss", {
        parent: { esModuleURI: "resource://zenapp/actors/ZenAppCssParent.sys.mjs" },
        child: {
          esModuleURI: "resource://zenapp/actors/ZenAppCssChild.sys.mjs",
          events: { DOMDocElementInserted: {} },
        },
        allFrames: true,
        messageManagerGroups: ["browsers"],
        // It only reads the published CSS and loads it into its own page.
        safeForUntrustedWebProcess: true,
      });
      this._cssActor = true;
    } catch (error) {
      console.error("zen-app: cannot register the custom CSS actor", error);
    }
  }

  /**
   * Compile the custom CSS and hand it to the pages: sharedData for pages
   * that load from now on, a message for the ones already open. Returns true
   * when what the pages get changed.
   */
  #publishCustomCss() {
    let program = null;
    if (this._customCssText !== null && this.customCss.state === "loaded") {
      const { program: compiled, info } = this.#compileCustomCss();
      const { state, bytes, fileName } = this.customCss;
      this.customCss = { state, bytes, fileName, ...info };
      if (this.settings.customCss !== false) {
        program = compiled;
      }
    }
    const key = program ? JSON.stringify(program) : "";
    if (key === this._customCssKey) {
      return false;
    }
    this._customCssKey = key;
    try {
      Services.ppmm.sharedData.set(CUSTOM_CSS_KEY, program);
      Services.ppmm.sharedData.flush();
    } catch (error) {
      console.error("zen-app: cannot publish the custom CSS", error);
    }
    for (const appWindow of this.windows) {
      for (const browser of appWindow.gBrowser?.browsers || []) {
        for (const context of browser.browsingContext?.getAllBrowsingContextsInSubtree() || []) {
          try {
            context.currentWindowGlobal?.getActor("ZenAppCss").sendAsyncMessage("ZenAppCss:update", program);
          } catch {
            // no page actor here (chrome page, about:blank, closing)
          }
        }
      }
    }
    return true;
  }

  /* ------------------------------------------- custom CSS: @imports */

  get #importCacheDir() {
    return PathUtils.join(ConfigStore.directory, "css-imports");
  }

  /** Download the @imports the custom CSS needs, then re-publish. */
  #refreshCssImports() {
    this.#loadCssImports({ network: true })
      .then(async changed => {
        if (changed && this.#publishCustomCss()) {
          this.#emit("customCss");
          await this.#writeStatus();
        }
      })
      .catch(error => console.error("zen-app: custom CSS imports", error));
  }

  /**
   * Fill this._cssImports for the current custom CSS (nested imports too):
   * from the cache, and with `network` also downloading missing or old ones.
   */
  async #loadCssImports({ network }) {
    if (this._customCssText === null) {
      return false;
    }
    let changed = false;
    for (let round = 0; round <= MAX_IMPORT_DEPTH; round++) {
      const { info } = this.#compileCustomCss();
      const wanted = info.imports.pending.concat(network ? info.imports.inlined : []);
      const todo = wanted.filter(url => !(network ? this._cssImportsChecked.has(url) : url in this._cssImports));
      if (!todo.length) {
        break;
      }
      await Promise.all(
        todo.map(async url => {
          const text = await this.#importText(url, { network });
          if (network) {
            this._cssImportsChecked.add(url);
          }
          if (text !== null && text !== this._cssImports[url]) {
            this._cssImports[url] = text;
            changed = true;
          }
        })
      );
    }
    return changed;
  }

  /** Text of an https @import: cached copy, refreshed daily. "" = unavailable. */
  async #importText(url, { network }) {
    const path = PathUtils.join(this.#importCacheDir, sha256Hex(url) + ".css");
    let cached = null;
    let age = Infinity;
    try {
      const stat = await IOUtils.stat(path);
      cached = await IOUtils.readUTF8(path);
      age = Date.now() - stat.lastModified;
    } catch {
      // not downloaded yet
    }
    if (!network || (cached !== null && age < IMPORT_MAX_AGE_MS)) {
      return cached;
    }
    try {
      // AbortSignal.timeout() needs a window; system modules have none.
      const controller = new AbortController();
      const timer = lazy.setTimeout(() => controller.abort(), 20000);
      let text;
      try {
        const response = await fetch(url, { credentials: "omit", redirect: "follow", signal: controller.signal });
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        text = await response.text();
      } finally {
        lazy.clearTimeout(timer);
      }
      if (new TextEncoder().encode(text).length > MAX_IMPORT_BYTES) {
        throw new Error("too large");
      }
      await IOUtils.makeDirectory(this.#importCacheDir, { ignoreExisting: true });
      await IOUtils.writeUTF8(path, text, { tmpPath: path + ".tmp" });
      return text;
    } catch (error) {
      console.warn(`zen-app: cannot download ${url}`, error);
      return cached ?? "";
    }
  }

  /* --------------------------------------- wallpaper colour changes */

  /** Poll the wallpaper scheme files; a new wallpaper re-colours the app. */
  #watchScheme() {
    const paths = this.store.config.schemePaths || [];
    if (!paths.length || this._schemeTimer) {
      return;
    }
    let busy = false;
    const check = async () => {
      if (busy) {
        return;
      }
      busy = true;
      try {
        const parts = await Promise.all(
          paths.map(path =>
            IOUtils.stat(path).then(
              stat => `${stat.lastModified}:${stat.size}`,
              () => "-"
            )
          )
        );
        const stamp = parts.join("|");
        if (this._schemeStamp !== null && stamp !== this._schemeStamp && this.settings.palette === "system") {
          this._schemeStamp = stamp;
          await this.refreshPalette();
        }
        this._schemeStamp = stamp;
      } catch (error) {
        console.error("zen-app: wallpaper scheme", error);
      } finally {
        busy = false;
      }
    };
    check();
    this._schemeTimer = lazy.setInterval(check, SCHEME_POLL_MS);
  }

  /** The app window came to the front: pick up edits made in an editor. */
  async windowActivated() {
    if (this.store && (await this.#reloadCustomCss())) {
      await this.#customCssChanged();
    }
  }

  /** Save CSS text (a file's content or pasted text) as this app's CSS. */
  async importCustomCss(text, { fileName = null } = {}) {
    const path = this.customCssPath;
    if (!path) {
      throw new CustomCssError("unknown");
    }
    const body = stripImportHeader(String(text ?? "").replace(/^\uFEFF/, ""));
    if (!body.trim()) {
      throw new CustomCssError("empty");
    }
    if (body.includes("\u0000")) {
      throw new CustomCssError("binary");
    }
    const data = importHeader(fileName) + body;
    if (new TextEncoder().encode(data).length > MAX_CUSTOM_CSS_BYTES) {
      throw new CustomCssError("tooLarge");
    }
    await this._customCssLoading;
    await IOUtils.writeUTF8(path, data, { tmpPath: path + ".tmp", flush: true });
    await IOUtils.setPermissions(path, 0o600);
    this._customCssStamp = null; // same size within the same millisecond is still new
    if (this.settings.customCss === false) {
      await this.store.updateSettings({ customCss: true });
    }
    await this.#reloadCustomCss();
    await this.#customCssChanged();
    return this.customCss;
  }

  /** Native file picker; null when cancelled. */
  async pickCustomCss() {
    const win = this.mainWindow?.win || Services.wm.getMostRecentWindow("navigator:browser");
    const picker = Cc["@mozilla.org/filepicker;1"].createInstance(Ci.nsIFilePicker);
    picker.init(win.browsingContext, t("css.picker.title"), Ci.nsIFilePicker.modeOpen);
    picker.appendFilter(t("css.picker.filter"), "*.css");
    picker.appendFilters(Ci.nsIFilePicker.filterAll);
    const result = await new Promise(resolve => picker.open(resolve));
    if (result !== Ci.nsIFilePicker.returnOK || !picker.file) {
      return null;
    }
    const file = picker.file;
    if (file.fileSize > MAX_CUSTOM_CSS_BYTES) {
      throw new CustomCssError("tooLarge");
    }
    let text;
    try {
      text = await IOUtils.readUTF8(file.path);
    } catch {
      throw new CustomCssError("binary");
    }
    return this.importCustomCss(text, { fileName: file.leafName });
  }

  #clipboardText() {
    const transferable = Cc["@mozilla.org/widget/transferable;1"].createInstance(Ci.nsITransferable);
    transferable.init(null);
    transferable.addDataFlavor("text/plain");
    const context = this.mainWindow?.win?.browsingContext?.currentWindowContext || null;
    Services.clipboard.getData(transferable, Services.clipboard.kGlobalClipboard, context);
    const data = {};
    try {
      transferable.getTransferData("text/plain", data);
    } catch {
      return "";
    }
    return data.value?.QueryInterface(Ci.nsISupportsString).data || "";
  }

  async pasteCustomCss() {
    const text = this.#clipboardText();
    if (!text.trim()) {
      throw new CustomCssError("emptyClipboard");
    }
    return this.importCustomCss(text);
  }

  /** Delete the app's CSS; returns what is needed to undo it. */
  async removeCustomCss() {
    const path = this.customCssPath;
    if (!path) {
      return null;
    }
    await this._customCssLoading;
    let previous = null;
    try {
      const text = await IOUtils.readUTF8(path);
      previous = { text: stripImportHeader(text), fileName: importedFrom(text) };
    } catch {
      // unreadable or already gone: nothing to restore
    }
    await IOUtils.remove(path, { ignoreAbsent: true });
    await this.#reloadCustomCss();
    await this.#customCssChanged();
    return previous;
  }

  setCustomCssEnabled(enabled) {
    return this.updateSettings({ customCss: !!enabled });
  }

  /** Open the CSS file with the desktop's default application. */
  openCustomCss() {
    const path = this.customCssPath;
    if (!path) {
      return;
    }
    const file = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
    file.initWithPath(path);
    try {
      file.launch();
    } catch {
      file.reveal();
    }
  }

  /* ---------------------------------------------------------- glass */

  get glassSources() {
    return this.store.config.glass?.sources || {};
  }

  get glassChoice() {
    const choice = this.settings.glass || "auto";
    if (choice === "auto") {
      return this.store.config.glass?.resolved || "custom";
    }
    return choice === "custom" || this.glassSources[choice] ? choice : "custom";
  }

  /** Window background: exactly the chosen desktop source, or the custom tint. */
  get tint() {
    const source = this.glassSources[this.glassChoice];
    if (this.glassChoice !== "custom" && source) {
      return { color: source.color, percent: Math.round(source.alpha * 100), source: this.glassChoice };
    }
    return { color: this.palette?.roles.surface || "#000000", percent: this.settings.tint, source: "custom" };
  }

  /* ------------------------------------------------------- Widevine */

  #refreshWidevineState() {
    if (this.widevine.state === "installing") {
      return;
    }
    const version = Services.prefs.getStringPref("media.gmp-widevinecdm.version", "");
    this.widevine = { state: version ? "installed" : "missing", version };
  }

  ensureWidevine({ force = false } = {}) {
    this.#refreshWidevineState();
    if (this.widevine.state === "installed") {
      return Promise.resolve(this.widevine);
    }
    if (this._widevinePromise) {
      return this._widevinePromise;
    }
    if (force) {
      Services.prefs.clearUserPref("media.gmp-manager.lastCheck");
    }
    this.widevine = { state: "installing" };
    this.#emit("widevine");
    const manager = new lazy.GMPInstallManager();
    this._widevinePromise = manager
      .simpleCheckAndInstall()
      .catch(error => {
        console.error("zen-app: Widevine install failed", error);
      })
      .finally(() => {
        try {
          manager.uninit();
        } catch {
          // ignore
        }
        this._widevinePromise = null;
        this.widevine = { state: "unknown" };
        this.#refreshWidevineState();
        if (this.widevine.state !== "installed") {
          this.widevine = { state: "failed" };
        }
        this.#emit("widevine");
        return this.widevine;
      });
    return this._widevinePromise;
  }

  /* ------------------------------------------------------ downloads */

  async #watchDownloads() {
    const list = await lazy.Downloads.getList(lazy.Downloads.ALL);
    const known = new Set((await list.getAll()).filter(d => d.stopped));
    this._downloadView = {
      onDownloadChanged: download => {
        if (!download.stopped || known.has(download)) {
          return;
        }
        known.add(download);
        if (download.succeeded) {
          this.#notify(t("download.done"), PathUtils.filename(download.target.path), () => {
            download.showContainingDirectory?.().catch?.(() => {});
          });
        } else if (download.error && !download.canceled) {
          this.#notify(t("download.failed"), download.source.url);
        }
      },
    };
    await list.addView(this._downloadView);
  }

  #notify(title, text, onClick) {
    try {
      const service = Cc["@mozilla.org/alerts-service;1"].getService(Ci.nsIAlertsService);
      const alert = new AlertNotification({
        title,
        text,
        textClickable: !!onClick,
        name: `zen-app-${Date.now()}`,
      });
      service.showAlert(alert, {
        observe(_subject, topic) {
          if (topic === "alertclickcallback") {
            onClick?.();
          }
        },
      });
    } catch (error) {
      console.error("zen-app: notification failed", error);
    }
  }

  /* ------------------------------------------------ studio bridge */

  addListener(listener) {
    this._listeners.add(listener);
    return () => this._listeners.delete(listener);
  }

  #emit(what) {
    for (const listener of [...this._listeners]) {
      try {
        listener(what);
      } catch (error) {
        console.error(error);
      }
    }
  }

  getStudioModel() {
    const config = this.store.config;
    const definition = this.definition;
    this.#refreshWidevineState();
    return {
      appId: this.id,
      name: definition.name,
      tagline: config.tagline || "",
      locale: getLocale(),
      icon: config.icon || {},
      accent: definition.accent,
      extras: [...(definition.extras || [])],
      settings: { ...this.settings },
      windowChromeResolved: resolvedWindowChrome(this.settings, config),
      desktop: Services.env.get("XDG_CURRENT_DESKTOP") || Services.env.get("XDG_SESSION_DESKTOP") || "",
      sessionType: Services.env.get("XDG_SESSION_TYPE") || "",
      palette: this.palette,
      paletteVariables: { ...this.paletteVariables },
      glass: { choice: this.glassChoice, sources: this.glassSources, tint: this.tint },
      styleSites: this.styleSites(),
      stylesCatalog: { origin: this.store.config.zenInternet?.origin, sites: this.store.config.zenInternet?.sites },
      customCss: { ...this.customCss, enabled: this.settings.customCss !== false, path: this.customCssPath },
      widevine: { ...this.widevine },
      needsOnboarding: this.store.needsOnboarding,
      versions: {
        addon: this.addonVersion,
        engine: `${Services.appinfo.name} ${Services.appinfo.version} (Gecko ${Services.appinfo.platformVersion})`,
        package: config.package?.version || "",
      },
      paths: {
        profile: PathUtils.profileDir,
        definition: config.definitionPath,
      },
    };
  }

  async completeOnboarding() {
    await this.store.setOnboardingCompleted(true);
    this.#applyPermissions();
    this.loadInApp(this.scope.startURI, { replace: true });
    await this.#writeStatus();
  }

  async restartOnboarding(win) {
    await this.store.setOnboardingCompleted(false);
    this.loadInApp(Services.io.newURI(`${STUDIO_URL}?mode=onboarding`), { replace: true });
    win?.close?.();
  }

  restart() {
    Services.startup.quit(Ci.nsIAppStartup.eAttemptQuit | Ci.nsIAppStartup.eRestart);
  }

  async clearData() {
    const hosts = new Set(this.scope.hosts.filter(h => !h.startsWith("*.")).concat(this.scope.authHosts));
    const sites = new Set();
    for (const host of hosts) {
      try {
        sites.add(Services.eTLD.getBaseDomainFromHost(host));
      } catch {
        sites.add(host);
      }
    }
    await Promise.all(
      [...sites].map(
        site =>
          new Promise(resolve =>
            Services.clearData.deleteDataFromSite(site, {}, true, Ci.nsIClearDataService.CLEAR_ALL, resolve)
          )
      )
    );
    this.#applyPermissions();
    this.loadInApp(this.scope.startURI, { replace: true });
  }

  openAddons() {
    const win = this.mainWindow?.win;
    win?.openTrustedLinkIn("about:addons", "window");
  }

  async #writeStatus() {
    try {
      await this.store.writeStatus({
        statusVersion: 1,
        appId: this.id,
        addonVersion: this.addonVersion,
        engine: { name: Services.appinfo.name, version: Services.appinfo.version, gecko: Services.appinfo.platformVersion },
        startedAt: new Date().toISOString(),
        onboardingCompleted: !this.store.needsOnboarding,
        widevine: this.widevine.state,
        customCss: {
          state: this.customCss.state,
          enabled: this.settings.customCss !== false,
          ...(this.customCss.state === "loaded"
            ? {
                bytes: this.customCss.bytes,
                sections: this.customCss.sections,
                matching: this.customCss.matching,
                rules: this.customCss.rules,
                warnings: this.customCss.warnings,
                aliases: this.customCss.aliases,
                paletteColors: this.customCss.palette?.applied || 0,
                imports: this.customCss.imports,
              }
            : {}),
        },
      });
    } catch (error) {
      console.error("zen-app: status", error);
    }
  }
}

export const ZenApp = new ZenAppRuntime();
