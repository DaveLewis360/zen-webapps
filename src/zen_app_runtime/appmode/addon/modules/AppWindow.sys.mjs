/* zen-app-runtime app mode: per-window customisation.
 *
 * Every browser window of an app profile becomes an app window: no browser
 * shortcuts, a reduced context menu, the page title as window title, centred
 * permission prompts and link routing (main window only).
 */

import { menuLabels, t } from "resource://zenapp/modules/Strings.sys.mjs";
import {
  canOpenExternally,
  isInternalPage,
  openExternally,
} from "resource://zenapp/modules/Routing.sys.mjs";

const HTML_NS = "http://www.w3.org/1999/xhtml";
const OWN_KEYSET_ID = "zenapp-keyset";
export const STUDIO_URL = "chrome://zenapp/content/studio.html";
export const MANAGER_URL = "chrome://zenapp/content/manager.html";

// Shortcuts an app keeps: editing, reload, zoom, full screen, history, quit.
const KEEP_KEY_IDS = new Set([
  "key_undo", "key_redo", "key_cut", "key_copy", "key_paste", "key_selectAll", "key_delete",
  "key_reload", "key_reload2", "key_reload_skip_cache", "key_reload_skip_cache2",
  "key_fullZoomReset", "key_fullZoomEnlarge", "key_fullZoomReduce",
  "goBackKb", "goForwardKb", "key_enterFullScreen", "key_exitFullScreen",
  "key_quitApplication",
]);
const KEEP_COMMANDS = new Set([
  "cmd_fullZoomReset", "cmd_fullZoomEnlarge", "cmd_fullZoomReduce",
  "cmd_handleBackspace", "cmd_handleShiftBackspace", "cmd_delete",
]);
const FIND_KEY_IDS = new Set([
  "key_find", "key_findAgain", "key_findPrevious", "key_findAgain2", "key_findPrevious2",
]);
const DEVTOOLS_KEYSETS = new Set(["devtoolsKeyset", "zen-devtoolsKeyset"]);
const DEVTOOLS_KEY_IDS = new Set([
  "key_toggleToolbox", "key_toggleToolboxF12", "key_browserToolbox", "key_browserConsole",
  "key_responsiveDesignMode", "key_inspector", "key_webconsole", "key_jsdebugger",
  "key_netmonitor", "key_styleeditor", "key_performance", "key_storage", "key_dom",
  "key_accessibility",
]);
const BROWSER_HOME_PAGES = new Set([
  "about:home", "about:newtab", "about:welcome", "about:privatebrowsing", "about:firefoxview",
]);

export class AppWindow {
  #keyObserver = null;
  #filteringKeys = false;
  #filterScheduled = false;
  #listeners = [];
  #tabsListener = null;
  #routedRecently = new Map();

  constructor(app, win, { main }) {
    this.app = app;
    this.win = win;
    this.doc = win.document;
    this.main = main;
    this.appTab = null;
    this.ready = false;
  }

  get gBrowser() {
    return this.win.gBrowser;
  }

  get appBrowser() {
    const tab = this.appTab && !this.appTab.closing ? this.appTab : this.gBrowser.selectedTab;
    return tab.linkedBrowser;
  }

  #on(target, type, handler, options) {
    target.addEventListener(type, handler, options);
    this.#listeners.push(() => target.removeEventListener(type, handler, options));
  }

  setup() {
    this.applySettings();
    this.#overrideTitle();
    this.#installKeys();
    this.#installContextMenu();
    this.#installPopupAnchor();
    for (const tab of this.gBrowser.tabs) {
      this.#adoptBrowser(tab.linkedBrowser);
    }
    this.#on(this.gBrowser.tabContainer, "TabOpen", event => this.#onTabOpen(event.target));
    if (this.main) {
      this.appTab = this.gBrowser.selectedTab;
      this.#installRouting();
    }
    // Returning to the app (e.g. from an editor) picks up CSS edits.
    this.#on(this.win, "activate", () => this.app.windowActivated().catch(console.error));
    this.#on(this.win, "unload", () => this.teardown(), { once: true });
  }

  teardown() {
    this.#keyObserver?.disconnect();
    this.#keyObserver = null;
    if (this.#tabsListener) {
      try {
        this.gBrowser.removeTabsProgressListener(this.#tabsListener);
      } catch {
        // window already gone
      }
    }
    for (const remove of this.#listeners.splice(0)) {
      try {
        remove();
      } catch {
        // ignore
      }
    }
    this.app.windowClosed(this);
  }

  /* ---------------------------------------------------------------- look */

  applySettings() {
    const { settings } = this.app;
    const root = this.doc.documentElement;
    root.setAttribute("zenapp", this.app.id);
    root.toggleAttribute("zenapp-popup", !this.main);
    root.setAttribute("zenapp-appearance", settings.appearance);
    root.toggleAttribute("zenapp-developer", settings.developer);
    root.toggleAttribute("zenapp-find", settings.findInPage);
    const palette = this.app.palette;
    if (palette) {
      root.setAttribute("zenapp-scheme", palette.dark ? "dark" : "light");
      for (const [name, value] of Object.entries(this.app.paletteVariables)) {
        root.style.setProperty(name, value);
      }
    }
    const tint = this.app.tint;
    root.setAttribute("zenapp-glass", tint.source);
    root.style.setProperty("--zenapp-tint-color", tint.color);
    root.style.setProperty("--zenapp-tint", String(tint.percent));
    if (this.#keyObserver) {
      this.#scheduleKeyFilter();
    }
    this.#syncTransparentBrowsers();
    this.gBrowser?.updateTitlebar?.();
  }

  #syncTransparentBrowsers() {
    const transparent = this.app.settings.appearance === "transparent";
    for (const tab of this.gBrowser?.tabs || []) {
      tab.linkedBrowser.toggleAttribute("transparent", transparent);
    }
  }

  #overrideTitle() {
    const app = this.app;
    this.gBrowser.getWindowTitleForBrowser = browser => {
      let title = "";
      try {
        title = (browser?.contentTitle || "").trim();
      } catch {
        title = "";
      }
      const uri = browser?.currentURI;
      if (!title || title === "about:blank" || (uri && uri.schemeIs("about"))) {
        return app.name;
      }
      return title;
    };
    this.gBrowser.updateTitlebar();
  }

  /* ---------------------------------------------------------------- keys */

  #installKeys() {
    this.#ensureOwnKeyset();
    this.#filterKeys();
    this.#keyObserver = new this.win.MutationObserver(mutations => {
      if (this.#filteringKeys) {
        return;
      }
      const relevant = mutations.some(
        m =>
          m.target.localName === "keyset" ||
          [...m.addedNodes].some(n => n.localName === "keyset" || n.localName === "key")
      );
      if (relevant) {
        this.#scheduleKeyFilter();
      }
    });
    this.#observeKeysets();
  }

  #observeKeysets() {
    const targets = new Set([this.doc.documentElement, this.doc.body]);
    for (const keyset of this.doc.querySelectorAll("keyset")) {
      targets.add(keyset);
      if (keyset.parentNode) {
        targets.add(keyset.parentNode);
      }
    }
    this.#keyObserver.disconnect();
    for (const target of targets) {
      if (target) {
        this.#keyObserver.observe(target, { childList: true });
      }
    }
  }

  #scheduleKeyFilter() {
    if (this.#filterScheduled) {
      return;
    }
    this.#filterScheduled = true;
    this.win.queueMicrotask(() => {
      this.#filterScheduled = false;
      this.#filterKeys();
      this.#observeKeysets();
    });
  }

  #keepKey(key, keyset) {
    const { settings } = this.app;
    const id = key.id || "";
    const command = key.getAttribute("command") || "";
    if (DEVTOOLS_KEYSETS.has(keyset.id) || DEVTOOLS_KEY_IDS.has(id)) {
      return settings.developer;
    }
    if (FIND_KEY_IDS.has(id) || command.startsWith("cmd_find")) {
      return settings.findInPage;
    }
    if (KEEP_KEY_IDS.has(id)) {
      return true;
    }
    return !id && KEEP_COMMANDS.has(command);
  }

  #filterKeys() {
    this.#filteringKeys = true;
    try {
      const touched = new Set();
      for (const key of this.doc.querySelectorAll("key")) {
        const keyset = key.parentElement;
        if (!keyset || keyset.id === OWN_KEYSET_ID) {
          continue;
        }
        const keep = this.#keepKey(key, keyset);
        const suppressed = key.hasAttribute("zenapp-suppressed");
        if (!keep && !suppressed) {
          key.setAttribute("zenapp-suppressed", "true");
          key.setAttribute("zenapp-was-disabled", key.getAttribute("disabled") || "");
          key.setAttribute("zenapp-was-reserved", key.getAttribute("reserved") || "");
          key.setAttribute("disabled", "true");
          key.removeAttribute("reserved");
          touched.add(keyset);
        } else if (keep && suppressed) {
          for (const name of ["disabled", "reserved"]) {
            const previous = key.getAttribute(`zenapp-was-${name}`);
            if (previous) {
              key.setAttribute(name, previous);
            } else {
              key.removeAttribute(name);
            }
            key.removeAttribute(`zenapp-was-${name}`);
          }
          key.removeAttribute("zenapp-suppressed");
          touched.add(keyset);
        }
      }
      // Gecko caches key handlers per keyset; re-inserting a keyset refreshes it.
      for (const keyset of touched) {
        const parent = keyset.parentNode;
        const next = keyset.nextSibling;
        keyset.remove();
        parent.insertBefore(keyset, next);
      }
    } finally {
      this.win.queueMicrotask(() => {
        this.#filteringKeys = false;
      });
    }
  }

  #ensureOwnKeyset() {
    if (this.doc.getElementById(OWN_KEYSET_ID)) {
      return;
    }
    const keyset = this.doc.createXULElement("keyset");
    keyset.id = OWN_KEYSET_ID;
    const addKey = (id, key, modifiers, handler) => {
      const element = this.doc.createXULElement("key");
      element.id = id;
      element.setAttribute("key", key);
      element.setAttribute("modifiers", modifiers);
      // Bug 371900: XUL keys only fire "command" when oncommand is present.
      element.setAttribute("oncommand", "void 0;");
      element.addEventListener("command", handler);
      keyset.append(element);
    };
    addKey("zenapp-key-close", "w", "accel", () => this.closeWindow());
    addKey("zenapp-key-settings", ",", "accel", () => this.app.openSettings(this.win));
    addKey("zenapp-key-settings-shift", ",", "accel,shift", () => this.app.openSettings(this.win));
    const mainKeyset = this.doc.getElementById("mainKeyset");
    (mainKeyset?.parentNode || this.doc.documentElement).append(keyset);
  }

  closeWindow() {
    this.win.close();
  }

  /* -------------------------------------------------------- context menu */

  #installContextMenu() {
    const menu = this.doc.getElementById("contentAreaContextMenu");
    if (!menu) {
      return;
    }
    const openExternal = this.doc.createXULElement("menuitem");
    openExternal.id = "zenapp-context-open-external";
    openExternal.setAttribute("label", t("context.openExternal"));
    this.#on(openExternal, "command", () => {
      const url = this.win.gContextMenu?.linkURL;
      if (url) {
        this.app.openExternal(url);
      }
    });
    const copyLink = this.doc.getElementById("context-copylink");
    if (copyLink) {
      copyLink.before(openExternal);
    } else {
      menu.prepend(openExternal);
    }

    if (!this.app.isManager) {
      const separator = this.doc.createXULElement("menuseparator");
      separator.id = "zenapp-context-sep-settings";
      const settings = this.doc.createXULElement("menuitem");
      settings.id = "zenapp-context-settings";
      settings.setAttribute("label", t("context.settings", { app: this.app.name }));
      this.#on(settings, "command", () => this.app.openSettings(this.win));
      menu.append(separator, settings);
    }

    // Speak the app's language in the few browser items an app keeps.
    for (const [id, label] of Object.entries(menuLabels())) {
      const item = this.doc.getElementById(id);
      if (item) {
        item.removeAttribute("data-l10n-id");
        item.removeAttribute("accesskey");
        item.setAttribute("label", label);
      }
    }

    // Bubble listeners run after nsContextMenu initialised the items.
    this.#on(menu, "popupshowing", event => {
      if (event.target !== menu) {
        return;
      }
      const context = this.win.gContextMenu;
      let showExternal = false;
      if (context?.onLink && context.linkURL) {
        try {
          showExternal = canOpenExternally(Services.io.newURI(context.linkURL));
        } catch {
          showExternal = false;
        }
      }
      openExternal.hidden = !showExternal;
      this.#updateSeparators(menu);
    });
    this.#on(menu, "popupshown", event => {
      if (event.target === menu) {
        this.#updateSeparators(menu);
      }
    });
  }

  #updateSeparators(menu) {
    let seenItem = false;
    let pending = null;
    for (const child of menu.children) {
      if (child.localName === "menuseparator") {
        child.removeAttribute("zenapp-visible");
        if (seenItem && !pending) {
          pending = child;
        }
        continue;
      }
      if (child.hidden || this.win.getComputedStyle(child).display === "none") {
        continue;
      }
      if (pending) {
        pending.setAttribute("zenapp-visible", "true");
        pending = null;
      }
      seenItem = true;
    }
  }

  /* ------------------------------------------------- permission prompts */

  #installPopupAnchor() {
    let anchor = this.doc.getElementById("zenapp-popup-anchor");
    if (!anchor) {
      anchor = this.doc.createElementNS(HTML_NS, "div");
      anchor.id = "zenapp-popup-anchor";
      this.doc.body.append(anchor);
    }
    try {
      const notifications = this.win.PopupNotifications;
      notifications._getVisibleAnchorElement = () => anchor;
    } catch (error) {
      console.error("zen-app: PopupNotifications unavailable", error);
    }
    const panel = this.doc.getElementById("notification-popup");
    const XULPopupElement = this.win.XULPopupElement;
    if (panel && XULPopupElement && !panel.hasOwnProperty("openPopup")) {
      const original = XULPopupElement.prototype.openPopup;
      panel.openPopup = function (anchorElement, position, ...rest) {
        if (anchorElement === anchor) {
          return original.call(this, anchorElement, "bottomcenter topcenter", 0, 0);
        }
        return original.call(this, anchorElement, position, ...rest);
      };
    }
  }

  /* ---------------------------------------------------------- routing */

  #adoptBrowser(browser) {
    if (!browser) {
      return;
    }
    browser.toggleAttribute("transparent", this.app.settings.appearance === "transparent");
    browser.droppedLinkHandler = (event, links) => {
      const link = Array.isArray(links) ? links[0] : null;
      const url = typeof link === "string" ? link : link?.url;
      if (url) {
        this.#routeUrl(url);
      }
    };
  }

  #installRouting() {
    const STATE_START = Ci.nsIWebProgressListener.STATE_START;
    const STATE_IS_DOCUMENT = Ci.nsIWebProgressListener.STATE_IS_DOCUMENT;
    this.#tabsListener = {
      QueryInterface: ChromeUtils.generateQI(["nsIWebProgressListener", "nsISupportsWeakReference"]),
      onStateChange: (browser, webProgress, request, flags) => {
        if (!webProgress?.isTopLevel || !(flags & STATE_START) || !(flags & STATE_IS_DOCUMENT)) {
          return;
        }
        let uri = null;
        try {
          uri = request.QueryInterface(Ci.nsIChannel).URI;
        } catch {
          return;
        }
        this.#onTopLevelStart(browser, uri);
      },
      onLocationChange: (browser, webProgress, request, location, flags) => {
        if (!webProgress?.isTopLevel || !location) {
          return;
        }
        if (flags & Ci.nsIWebProgressListener.LOCATION_CHANGE_SAME_DOCUMENT) {
          return;
        }
        this.#onTopLevelCommit(browser, location);
      },
    };
    this.gBrowser.addTabsProgressListener(this.#tabsListener);
    this.#on(this.gBrowser.tabContainer, "TabSelect", () => this.#keepAppTabSelected());
  }

  #isAppBrowser(browser) {
    return this.appTab && !this.appTab.closing && this.appTab.linkedBrowser === browser;
  }

  #onTopLevelStart(browser, uri) {
    const tab = this.gBrowser.getTabForBrowser(browser);
    if (!tab) {
      return;
    }
    if (this.#isAppBrowser(browser)) {
      this.#guardAppNavigation(browser, uri);
    } else if (tab.hasAttribute("zenapp-routing") && uri.spec !== "about:blank") {
      this.#routeSecondaryTab(tab, uri);
    }
  }

  #onTopLevelCommit(browser, location) {
    const tab = this.gBrowser.getTabForBrowser(browser);
    if (!tab) {
      return;
    }
    if (!this.#isAppBrowser(browser)) {
      if (tab.hasAttribute("zenapp-routing") && location.spec !== "about:blank") {
        this.#routeSecondaryTab(tab, location);
      }
      return;
    }
    if (BROWSER_HOME_PAGES.has(location.spec)) {
      this.app.loadInApp(this.app.scope.startURI, { replace: true });
      return;
    }
    if (isInternalPage(location) || location.schemeIs("chrome")) {
      return;
    }
    if (this.app.scope.isAllowedInside(location)) {
      return;
    }
    // A redirect landed outside the app: hand it to the browser, return home.
    if (this.#shouldRoute(location.spec)) {
      this.app.openExternal(location.spec);
    }
    if (browser.canGoBack) {
      browser.goBack();
    } else {
      this.app.loadInApp(this.app.scope.startURI, { replace: true });
    }
  }

  #shouldRoute(spec) {
    const now = Date.now();
    for (const [key, time] of this.#routedRecently) {
      if (now - time > 4000) {
        this.#routedRecently.delete(key);
      }
    }
    if (this.#routedRecently.has(spec)) {
      return false;
    }
    this.#routedRecently.set(spec, now);
    return true;
  }

  #guardAppNavigation(browser, uri) {
    if (BROWSER_HOME_PAGES.has(uri.spec)) {
      browser.stop();
      this.app.loadInApp(this.app.scope.startURI, { replace: true });
      return;
    }
    if (isInternalPage(uri)) {
      return;
    }
    if (uri.schemeIs("chrome")) {
      // Only our own studio pages may be shown inside an app tab.
      browser.stop();
      return;
    }
    const home = this.app.scope.rootRedirect(uri);
    if (home) {
      browser.stop();
      this.app.loadInApp(home);
      return;
    }
    if (this.app.scope.isAllowedInside(uri)) {
      return;
    }
    browser.stop();
    if (this.#shouldRoute(uri.spec)) {
      this.app.openExternal(uri.spec);
    }
  }

  #routeUrl(url) {
    let uri;
    try {
      uri = Services.io.newURI(url);
    } catch {
      return;
    }
    if (this.app.scope.isAllowedInside(uri) && this.main) {
      this.app.loadInApp(this.app.scope.rootRedirect(uri) || uri);
    } else {
      this.app.openExternal(uri.spec);
    }
  }

  #onTabOpen(tab) {
    this.#adoptBrowser(tab.linkedBrowser);
    if (!this.main || !this.ready || tab.hasAttribute("zen-empty-tab") || tab === this.appTab) {
      return;
    }
    const opener = tab.linkedBrowser?.browsingContext?.opener;
    if (opener) {
      // Scripted window.open() keeps its opener (OAuth, call windows):
      // give it its own app window instead of a hidden tab.
      this.win.setTimeout(() => {
        if (!tab.closing && tab.isConnected) {
          this.gBrowser.replaceTabWithWindow(tab);
        }
      }, 0);
      return;
    }
    tab.setAttribute("zenapp-routing", "pending");
    // Tabs that never navigate (e.g. window.open() without a URL) are discarded.
    this.win.setTimeout(() => {
      if (tab.hasAttribute("zenapp-routing") && !tab.closing && tab.isConnected) {
        this.#discardTab(tab);
      }
    }, 15000);
  }

  #routeSecondaryTab(tab, uri) {
    tab.removeAttribute("zenapp-routing");
    try {
      tab.linkedBrowser.stop();
    } catch {
      // ignore
    }
    const scope = this.app.scope;
    const home = scope.rootRedirect(uri);
    if (home || scope.isAllowedInside(uri)) {
      this.app.loadInApp(home || uri);
    } else if (this.#shouldRoute(uri.spec)) {
      this.app.openExternal(uri.spec);
    }
    this.#discardTab(tab);
  }

  #discardTab(tab) {
    tab.removeAttribute("zenapp-routing");
    if (tab.closing || !tab.isConnected) {
      return;
    }
    if (this.gBrowser.selectedTab === tab && this.appTab && !this.appTab.closing) {
      this.gBrowser.selectedTab = this.appTab;
    }
    this.gBrowser.removeTab(tab, { animate: false, skipSessionStore: true, skipPermitUnload: true });
  }

  #keepAppTabSelected() {
    if (!this.ready || !this.appTab || this.appTab.closing) {
      return;
    }
    const selected = this.gBrowser.selectedTab;
    if (selected !== this.appTab && !selected.hasAttribute("zen-empty-tab")) {
      this.win.setTimeout(() => {
        if (!this.appTab.closing) {
          this.gBrowser.selectedTab = this.appTab;
        }
      }, 0);
    }
  }

  /** Called once the browser (and Zen) finished its own startup logic. */
  finishStartup() {
    this.ready = true;
    if (!this.main) {
      return;
    }
    const gBrowser = this.gBrowser;
    let selected = gBrowser.selectedTab;
    if (!selected || selected.hasAttribute("zen-empty-tab")) {
      selected = gBrowser.addTrustedTab("about:blank", { inBackground: false, skipAnimation: true });
      gBrowser.selectedTab = selected;
    }
    this.appTab = selected;
    for (const tab of [...gBrowser.tabs]) {
      if (tab !== selected && !tab.hasAttribute("zen-empty-tab") && !tab.pinned) {
        gBrowser.removeTab(tab, { animate: false, skipSessionStore: true, skipPermitUnload: true });
      }
    }
    this.#adoptBrowser(selected.linkedBrowser);
  }

  loadInAppBrowser(uri, { replace = false } = {}) {
    const browser = this.appBrowser;
    const flags = replace ? Ci.nsIWebNavigation.LOAD_FLAGS_REPLACE_HISTORY : Ci.nsIWebNavigation.LOAD_FLAGS_NONE;
    browser.fixupAndLoadURIString(uri.spec, {
      triggeringPrincipal: Services.scriptSecurityManager.getSystemPrincipal(),
      loadFlags: flags,
    });
    if (this.gBrowser.selectedTab !== this.appTab && this.appTab) {
      this.gBrowser.selectedTab = this.appTab;
    }
  }

  get currentURI() {
    try {
      return this.appBrowser.currentURI;
    } catch {
      return null;
    }
  }

  isShowingStudio() {
    return this.currentURI?.spec.startsWith(STUDIO_URL) || false;
  }
}
