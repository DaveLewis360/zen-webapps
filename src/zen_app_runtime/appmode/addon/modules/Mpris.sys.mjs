/* zen-app-runtime app mode: the app's own MPRIS media player.
 *
 * Gecko's player would be called "Mozilla zen-app-<id>" (its Identity is the
 * vendor plus the remoting name), so app profiles switch it off
 * (media.hardwaremediakeys.enabled) and the app publishes its own: named
 * after the app, fed from Gecko's MediaController of the app's tabs, through
 * the same GIO D-Bus calls Gecko makes (via js-ctypes). If it cannot start,
 * Gecko's own player is switched back on.
 *
 * GLib calls the two callbacks below only while JS pumps the player's own
 * GMainContext (see MprisService.pump): a js-ctypes callback needs JS on the
 * stack, and one reached from Gecko's own GLib main loop would crash. They
 * only read the current state and queue actions, and they never throw into C.
 */

import {
  INTROSPECTION_XML,
  OBJECT_PATH,
  PLAYER_INTERFACE,
  PROPERTIES_INTERFACE,
  ROOT_INTERFACE,
  TRACK_PREFIX,
  busName,
  capabilities,
  changedProperties,
  cleanText,
  imageExtension,
  isJump,
  pickArtwork,
  playbackStatus,
  playerAction,
  positionSeconds,
  positionState,
  propertiesChangedText,
  propertyValue,
  seekedText,
  signalledValues,
  trackKey,
} from "resource://zenapp/modules/MprisModel.sys.mjs";

const lazy = {};
ChromeUtils.defineESModuleGetters(lazy, {
  clearInterval: "resource://gre/modules/Timer.sys.mjs",
  clearTimeout: "resource://gre/modules/Timer.sys.mjs",
  ctypes: "resource://gre/modules/ctypes.sys.mjs",
  setInterval: "resource://gre/modules/Timer.sys.mjs",
  setTimeout: "resource://gre/modules/Timer.sys.mjs",
});

const G_BUS_TYPE_SESSION = 2;
const G_DBUS_ERROR_FAILED = 0;
/** A player that stopped keeps its name this long (track changes, reloads). */
const RELEASE_DELAY_MS = 2000;
/** D-Bus calls are answered this fast while the player is on the bus… */
const PUMP_OWNED_MS = 100;
/** …and this fast otherwise (nobody expects it then). */
const PUMP_IDLE_MS = 1000;
/** Most D-Bus calls handled per pump. */
const PUMP_BATCH = 64;
/** How often tabs are checked for (new) media controllers. */
const SCAN_MS = 1000;
const MAX_ART_BYTES = 5 * 1024 * 1024;
const ART_TIMEOUT_MS = 15000;
const ART_FILES_KEPT = 6;

let native = null;
/** The open player (one per app process), read by the GLib callbacks. */
let current = null;
/** ctypes closures handed to GLib: kept for the whole process. */
let closures = null;

/* ------------------------------------------------------------ GIO */

function bind() {
  if (native) {
    return native;
  }
  const { ctypes } = lazy;
  const libraries = [];
  const open = name => {
    const library = ctypes.open(name);
    libraries.push(library);
    return library;
  };
  try {
    const gio = open("libgio-2.0.so.0");
    const glib = open("libglib-2.0.so.0");
    const gobject = open("libgobject-2.0.so.0");
    const ptr = ctypes.voidptr_t;
    const str = ctypes.char.ptr;
    const abi = ctypes.default_abi;
    const GError = ctypes.StructType("GError", [{ domain: ctypes.uint32_t }, { code: ctypes.int }, { message: ctypes.char.ptr }]);
    const errorOut = GError.ptr.ptr;
    const MethodCall = ctypes.FunctionType(abi, ctypes.void_t, [ptr, str, str, str, str, ptr, ptr, ptr]);
    const GetProperty = ctypes.FunctionType(abi, ptr, [ptr, str, str, str, str, errorOut, ptr]);
    const VTable = ctypes.StructType("GDBusInterfaceVTable", [
      { method_call: MethodCall.ptr },
      { get_property: GetProperty.ptr },
      { set_property: ptr },
      { padding: ctypes.ArrayType(ptr, 8) },
    ]);
    native = {
      ctypes,
      GError,
      MethodCall,
      GetProperty,
      VTable,
      libraries,
      busGetSync: gio.declare("g_bus_get_sync", abi, ptr, ctypes.int, ptr, errorOut),
      nodeInfoNewForXml: gio.declare("g_dbus_node_info_new_for_xml", abi, ptr, str, errorOut),
      nodeInfoLookupInterface: gio.declare("g_dbus_node_info_lookup_interface", abi, ptr, ptr, str),
      nodeInfoUnref: gio.declare("g_dbus_node_info_unref", abi, ctypes.void_t, ptr),
      registerObject: gio.declare(
        "g_dbus_connection_register_object", abi, ctypes.unsigned_int, ptr, str, ptr, VTable.ptr, ptr, ptr, errorOut
      ),
      unregisterObject: gio.declare("g_dbus_connection_unregister_object", abi, ctypes.int, ptr, ctypes.unsigned_int),
      ownName: gio.declare("g_bus_own_name_on_connection", abi, ctypes.unsigned_int, ptr, str, ctypes.int, ptr, ptr, ptr, ptr),
      unownName: gio.declare("g_bus_unown_name", abi, ctypes.void_t, ctypes.unsigned_int),
      emitSignal: gio.declare("g_dbus_connection_emit_signal", abi, ctypes.int, ptr, str, str, str, str, ptr, errorOut),
      returnValue: gio.declare("g_dbus_method_invocation_return_value", abi, ctypes.void_t, ptr, ptr),
      returnDbusError: gio.declare("g_dbus_method_invocation_return_dbus_error", abi, ctypes.void_t, ptr, str, str),
      dbusErrorQuark: gio.declare("g_dbus_error_quark", abi, ctypes.uint32_t),
      mainContextNew: glib.declare("g_main_context_new", abi, ptr),
      mainContextUnref: glib.declare("g_main_context_unref", abi, ctypes.void_t, ptr),
      mainContextIteration: glib.declare("g_main_context_iteration", abi, ctypes.int, ptr, ctypes.int),
      pushThreadDefault: glib.declare("g_main_context_push_thread_default", abi, ctypes.void_t, ptr),
      popThreadDefault: glib.declare("g_main_context_pop_thread_default", abi, ctypes.void_t, ptr),
      variantParse: glib.declare("g_variant_parse", abi, ptr, str, str, str, ptr, errorOut),
      variantUnref: glib.declare("g_variant_unref", abi, ctypes.void_t, ptr),
      variantGetChildValue: glib.declare("g_variant_get_child_value", abi, ptr, ptr, ctypes.size_t),
      variantGetInt64: glib.declare("g_variant_get_int64", abi, ctypes.int64_t, ptr),
      variantGetString: glib.declare("g_variant_get_string", abi, str, ptr, ptr),
      setErrorLiteral: glib.declare("g_set_error_literal", abi, ctypes.void_t, errorOut, ctypes.uint32_t, ctypes.int, str),
      errorFree: glib.declare("g_error_free", abi, ctypes.void_t, GError.ptr),
      objectUnref: gobject.declare("g_object_unref", abi, ctypes.void_t, ptr),
    };
    return native;
  } catch (error) {
    for (const library of libraries) {
      try {
        library.close();
      } catch {
        // ignore
      }
    }
    throw error;
  }
}

/** Call `fn(GError**)`; throw with GLib's message when `ok(result)` is false. */
function checked(what, fn, ok) {
  const error = new native.GError.ptr();
  const result = fn(error.address());
  if (ok(result)) {
    return result;
  }
  let message = "unknown error";
  if (!error.isNull()) {
    try {
      const text = error.contents.message;
      message = text.isNull() ? message : text.readString();
    } finally {
      native.errorFree(error);
    }
  }
  throw new Error(`${what}: ${message}`);
}

/** A GVariant (one full reference) of `type` from GVariant text. */
function parseVariant(type, text) {
  return checked(`GVariant ${type}`, error => native.variantParse(type, text, null, null, error), value => !value.isNull());
}

function readArguments(method, parameters) {
  const child = index => native.variantGetChildValue(parameters, index);
  const int64At = index => {
    const value = child(index);
    try {
      return Number(native.variantGetInt64(value).toString());
    } finally {
      native.variantUnref(value);
    }
  };
  const stringAt = index => {
    const value = child(index);
    try {
      return native.variantGetString(value, null).readString();
    } finally {
      native.variantUnref(value);
    }
  };
  // GDBus has already checked the arguments against the introspection data.
  switch (method) {
    case "Seek":
      return { offset: int64At(0) };
    case "SetPosition":
      return { trackId: stringAt(0), position: int64At(1) };
    case "OpenUri":
      return { uri: stringAt(0) };
    default:
      return {};
  }
}

function onMethodCall(connection, sender, objectPath, interfaceName, methodName, parameters, invocation) {
  let reply;
  try {
    const method = methodName.readString();
    const iface = interfaceName.isNull()
      ? method === "Raise" || method === "Quit"
        ? ROOT_INTERFACE
        : PLAYER_INTERFACE
      : interfaceName.readString();
    reply = current
      ? current.acceptMethod(iface, method, readArguments(method, parameters))
      : ["org.freedesktop.DBus.Error.Failed", "The player is closed"];
  } catch (error) {
    reply = ["org.freedesktop.DBus.Error.Failed", String(error?.message || error)];
  }
  try {
    if (reply) {
      native.returnDbusError(invocation, reply[0], cleanText(reply[1]));
    } else {
      native.returnValue(invocation, null);
    }
  } catch (error) {
    console.error("zen-app: MPRIS reply", error);
  }
}

function onGetProperty(connection, sender, objectPath, interfaceName, propertyName, error) {
  try {
    const value = current?.propertyValue(interfaceName.readString(), propertyName.readString());
    if (!value) {
      throw new Error("The player is closed");
    }
    // GLib takes over this reference.
    return parseVariant(value[0], value[1]);
  } catch (failure) {
    try {
      native.setErrorLiteral(error, native.dbusErrorQuark(), G_DBUS_ERROR_FAILED, cleanText(failure?.message || failure));
    } catch {
      // nothing else can be done here
    }
    return native.ctypes.voidptr_t(0);
  }
}

/* --------------------------------------------------- D-Bus service */

/** The org.mpris.MediaPlayer2 object; owns its bus name while media is active. */
export class MprisService {
  #connection = null;
  #context = null;
  #node = null;
  #registrations = [];
  #ownerId = 0;
  #values = null;
  #releaseTimer = null;
  #pumpTimer = null;

  constructor({ appId, identity, desktopEntry, pid, onAction }) {
    this.busName = busName(appId, pid);
    this.identity = identity;
    this.desktopEntry = desktopEntry;
    this.onAction = onAction;
    this.player = { status: "Stopped", metadata: {}, caps: {}, identity, desktopEntry };
  }

  get owned() {
    return this.#ownerId > 0;
  }

  open() {
    bind();
    if (current) {
      throw new Error("the app's media player is already open");
    }
    try {
      this.#connection = checked(
        "session bus",
        error => native.busGetSync(G_BUS_TYPE_SESSION, null, error),
        connection => !connection.isNull()
      );
      this.#node = checked(
        "introspection",
        error => native.nodeInfoNewForXml(INTROSPECTION_XML, error),
        node => !node.isNull()
      );
      closures ??= {
        methodCall: native.MethodCall.ptr(onMethodCall),
        getProperty: native.GetProperty.ptr(onGetProperty),
      };
      const vtable = new native.VTable();
      vtable.method_call = closures.methodCall;
      vtable.get_property = closures.getProperty;
      // GDBus calls an object back in the context that was the thread's
      // default when it was registered: this private one, which only JS runs.
      this.#context = native.mainContextNew();
      native.pushThreadDefault(this.#context);
      try {
        for (const name of [ROOT_INTERFACE, PLAYER_INTERFACE]) {
          const info = native.nodeInfoLookupInterface(this.#node, name);
          if (info.isNull()) {
            throw new Error(`no interface ${name}`);
          }
          this.#registrations.push(
            checked(
              `register ${name}`,
              error => native.registerObject(this.#connection, OBJECT_PATH, info, vtable.address(), null, null, error),
              id => id > 0
            )
          );
        }
      } finally {
        native.popThreadDefault(this.#context);
      }
      current = this;
      this.#schedulePump();
    } catch (error) {
      this.close();
      throw error;
    }
  }

  /** Answer the D-Bus calls waiting for the player; returns how many ran. */
  pump() {
    let handled = 0;
    while (this.#context && handled < PUMP_BATCH && native.mainContextIteration(this.#context, 0)) {
      handled++;
    }
    return handled;
  }

  #schedulePump() {
    lazy.clearTimeout(this.#pumpTimer);
    this.#pumpTimer = null;
    if (!this.#context) {
      return;
    }
    this.#pumpTimer = lazy.setTimeout(
      () => {
        this.#pumpTimer = null;
        try {
          this.pump();
        } catch (error) {
          console.error("zen-app: MPRIS", error);
        }
        this.#schedulePump();
      },
      this.owned ? PUMP_OWNED_MS : PUMP_IDLE_MS
    );
  }

  close() {
    lazy.clearTimeout(this.#releaseTimer);
    this.#releaseTimer = null;
    lazy.clearTimeout(this.#pumpTimer);
    this.#pumpTimer = null;
    if (current === this) {
      current = null;
    }
    if (!native) {
      return;
    }
    const attempt = fn => {
      try {
        fn();
      } catch (error) {
        console.error("zen-app: MPRIS close", error);
      }
    };
    if (this.#ownerId) {
      attempt(() => native.unownName(this.#ownerId));
      this.#ownerId = 0;
    }
    for (const id of this.#registrations.splice(0)) {
      attempt(() => native.unregisterObject(this.#connection, id));
    }
    if (this.#context) {
      // Calls still queued get GDBus's own "no such object" reply.
      attempt(() => this.pump());
      attempt(() => native.mainContextUnref(this.#context));
      this.#context = null;
    }
    if (this.#node) {
      attempt(() => native.nodeInfoUnref(this.#node));
      this.#node = null;
    }
    if (this.#connection) {
      attempt(() => native.objectUnref(this.#connection));
      this.#connection = null;
    }
  }

  /** New player state: announce what changed; own the name while there is media. */
  update(player) {
    this.player = { ...player, identity: this.identity, desktopEntry: this.desktopEntry };
    const values = signalledValues(this.player);
    const active = this.player.status !== "Stopped";
    if (active) {
      lazy.clearTimeout(this.#releaseTimer);
      this.#releaseTimer = null;
      if (!this.owned) {
        this.#own();
      }
    }
    if (this.owned && this.#values) {
      const changed = changedProperties(this.#values, values);
      if (changed.length) {
        this.#emit(PROPERTIES_INTERFACE, "PropertiesChanged", "(sa{sv}as)", propertiesChangedText(changed, values));
      }
    }
    this.#values = values;
    if (!active) {
      this.#scheduleRelease();
    }
  }

  /** The position jumped (a seek): MPRIS clients re-read it. */
  seeked(positionUs) {
    if (this.owned) {
      this.#emit(PLAYER_INTERFACE, "Seeked", "(x)", seekedText(positionUs));
    }
  }

  propertyValue(interfaceName, name) {
    return propertyValue(interfaceName, name, this.player, Date.now());
  }

  /** A method call from D-Bus: null to reply OK, or [error name, message]. */
  acceptMethod(interfaceName, method, args) {
    if (interfaceName === ROOT_INTERFACE) {
      if (method === "Raise") {
        this.#queue({ type: "raise" });
      }
      // Quit: CanQuit is false, so it does nothing.
      return null;
    }
    if (interfaceName !== PLAYER_INTERFACE) {
      return ["org.freedesktop.DBus.Error.UnknownInterface", `No interface ${interfaceName}`];
    }
    if (method === "OpenUri") {
      return ["org.freedesktop.DBus.Error.NotSupported", "This player cannot open URIs"];
    }
    const action = playerAction(method, args, this.player, Date.now());
    if (action) {
      this.#queue(action);
    }
    return null;
  }

  #queue(action) {
    // Out of the GLib callback: the page may react synchronously.
    Services.tm.dispatchToMainThread(() => {
      try {
        this.onAction?.(action);
      } catch (error) {
        console.error("zen-app: MPRIS action", error);
      }
    });
  }

  #own() {
    try {
      // Name owning only runs GLib's own C callbacks (none of ours).
      this.#ownerId = native.ownName(this.#connection, this.busName, 0, null, null, null, null);
    } catch (error) {
      console.error("zen-app: MPRIS name", error);
      this.#ownerId = 0;
    }
    // Clients read everything when the name appears.
    this.#values = null;
    // Answer them quickly from now on.
    this.#schedulePump();
  }

  #scheduleRelease() {
    if (!this.owned || this.#releaseTimer) {
      return;
    }
    this.#releaseTimer = lazy.setTimeout(() => {
      this.#releaseTimer = null;
      if (this.player.status === "Stopped" && this.#ownerId) {
        try {
          native.unownName(this.#ownerId);
        } catch (error) {
          console.error("zen-app: MPRIS name", error);
        }
        this.#ownerId = 0;
      }
    }, RELEASE_DELAY_MS);
  }

  #emit(interfaceName, signal, type, text) {
    let body = null;
    try {
      body = parseVariant(type, text);
      checked(
        signal,
        error => native.emitSignal(this.#connection, null, OBJECT_PATH, interfaceName, signal, body, error),
        ok => !!ok
      );
    } catch (error) {
      console.error("zen-app: MPRIS signal", error);
    } finally {
      // The message took its own reference.
      if (body) {
        native.variantUnref(body);
      }
    }
  }
}

/* ------------------------------------------------ media of the app */

function sha256Hex(text) {
  const hasher = Cc["@mozilla.org/security/hash;1"].createInstance(Ci.nsICryptoHash);
  hasher.init(Ci.nsICryptoHash.SHA256);
  const bytes = new TextEncoder().encode(text);
  hasher.update(bytes, bytes.length);
  return Array.from(hasher.finish(false), c => c.charCodeAt(0).toString(16).padStart(2, "0")).join("");
}

function readPosition(controller) {
  try {
    const state = controller.getPositionState?.();
    return state ? positionState(state, Date.now()) : null;
  } catch {
    return null;
  }
}

const MEDIA_EVENTS = ["metadatachange", "playbackstatechange", "supportedkeyschange"];

/**
 * Feeds the MPRIS service from the media controllers of every tab of the app
 * (the app tab, call windows, …): the most recently activated one is the
 * player, as in Gecko. Downloads artwork into the profile, since shells
 * show local files best (and then never fetch from the site themselves).
 */
export class MediaBridge {
  #app;
  #service = null;
  #tracked = new Map();
  #current = null;
  #timer = null;
  #track = { key: null, number: 0 };
  #art = { source: "", url: "" };
  #artFiles = new Map();
  #rate = 1;

  constructor(app, { appId, identity, desktopEntry, artDirectory }) {
    this.#app = app;
    this.options = { appId, identity, desktopEntry };
    this.artDirectory = artDirectory;
  }

  get busName() {
    return this.#service?.busName || null;
  }

  get service() {
    return this.#service;
  }

  /** Throws when the player cannot be published (no session bus, no GIO). */
  start() {
    this.#service = new MprisService({
      ...this.options,
      pid: Services.appinfo.processID,
      onAction: action => this.#perform(action),
    });
    try {
      this.#service.open();
    } catch (error) {
      this.#service = null;
      throw error;
    }
    IOUtils.remove(this.artDirectory, { recursive: true, ignoreAbsent: true }).catch(() => {});
    this.#scan();
    this.#timer = lazy.setInterval(() => this.#scan(), SCAN_MS);
  }

  stop() {
    lazy.clearInterval(this.#timer);
    this.#timer = null;
    for (const controller of [...this.#tracked.keys()]) {
      this.#untrack(controller);
    }
    this.#current = null;
    this.#service?.close();
    this.#service = null;
    IOUtils.remove(this.artDirectory, { recursive: true, ignoreAbsent: true }).catch(() => {});
  }

  #scan() {
    if (!this.#service) {
      return;
    }
    const seen = new Set();
    for (const appWindow of this.#app.windows) {
      if (appWindow.win?.closed) {
        continue;
      }
      for (const browser of appWindow.gBrowser?.browsers || []) {
        let controller = null;
        try {
          controller = browser.browsingContext?.mediaController || null;
        } catch {
          controller = null;
        }
        if (!controller) {
          continue;
        }
        seen.add(controller);
        const entry = this.#tracked.get(controller);
        if (entry) {
          entry.appWindow = appWindow;
          entry.browser = browser;
        } else {
          this.#watch(controller, appWindow, browser);
        }
      }
    }
    for (const controller of [...this.#tracked.keys()]) {
      if (!seen.has(controller)) {
        this.#untrack(controller);
      }
    }
    this.#refresh();
  }

  #watch(controller, appWindow, browser) {
    const entry = {
      controller,
      appWindow,
      browser,
      activatedAt: controller.isActive ? Date.now() : 0,
      position: controller.isActive ? readPosition(controller) : null,
      listeners: [],
    };
    const listen = (type, handler) => {
      const guarded = event => {
        try {
          handler(event);
        } catch (error) {
          console.error(`zen-app: media ${type}`, error);
        }
      };
      controller.addEventListener(type, guarded);
      entry.listeners.push([type, guarded]);
    };
    listen("activated", () => {
      entry.activatedAt = Date.now();
      entry.position = readPosition(controller);
      this.#refresh();
    });
    listen("deactivated", () => {
      entry.activatedAt = 0;
      this.#refresh();
    });
    listen("positionstatechange", event => {
      const next = positionState(event, Date.now());
      const jumped = this.#current === entry && isJump(entry.position, next, this.#service?.player.status === "Playing");
      entry.position = next;
      this.#refresh();
      if (jumped && this.#current === entry) {
        this.#service?.seeked(Math.round(next.position * 1e6));
      }
    });
    for (const type of MEDIA_EVENTS) {
      listen(type, () => this.#refresh());
    }
    this.#tracked.set(controller, entry);
  }

  #untrack(controller) {
    const entry = this.#tracked.get(controller);
    this.#tracked.delete(controller);
    for (const [type, handler] of entry?.listeners || []) {
      try {
        controller.removeEventListener(type, handler);
      } catch {
        // the controller is gone
      }
    }
    if (this.#current === entry) {
      this.#current = null;
    }
  }

  #currentEntry() {
    let best = null;
    for (const entry of this.#tracked.values()) {
      let active = false;
      try {
        active = entry.controller.isActive;
      } catch {
        active = false;
      }
      if (active && (!best || entry.activatedAt > best.activatedAt)) {
        best = entry;
      }
    }
    return best;
  }

  #refresh() {
    const service = this.#service;
    if (!service) {
      return;
    }
    const entry = this.#currentEntry();
    this.#current = entry;
    if (!entry) {
      service.update({ status: "Stopped", rate: this.#rate, position: null, caps: {}, metadata: {} });
      return;
    }
    const controller = entry.controller;
    let raw = {};
    let keys = [];
    try {
      raw = controller.getMetadata() || {};
    } catch {
      raw = {};
    }
    try {
      keys = [...controller.supportedKeys];
    } catch {
      keys = [];
    }
    const key = trackKey(raw);
    if (key !== this.#track.key) {
      this.#track = { key, number: this.#track.number + 1 };
    }
    this.#showArtwork(pickArtwork(raw.artwork));
    const position = entry.position;
    if (position?.rate > 0) {
      this.#rate = position.rate;
    }
    const lengthUs = position?.duration ? Math.round(position.duration * 1e6) : null;
    service.update({
      status: playbackStatus({ active: true, playing: controller.isPlaying, state: controller.playbackState }),
      rate: this.#rate,
      position,
      caps: capabilities(keys, { seekable: !!lengthUs }),
      metadata: {
        trackId: TRACK_PREFIX + this.#track.number,
        title: raw.title,
        artist: raw.artist,
        album: raw.album,
        lengthUs,
        artUrl: this.#art.url,
      },
    });
  }

  #perform(action) {
    const entry = this.#current;
    if (!entry) {
      return;
    }
    const controller = entry.controller;
    switch (action.type) {
      case "raise": {
        const { appWindow, browser } = entry;
        const tab = appWindow.gBrowser?.getTabForBrowser(browser);
        if (tab && appWindow.gBrowser.selectedTab !== tab) {
          appWindow.gBrowser.selectedTab = tab;
        }
        appWindow.win?.focus();
        break;
      }
      case "play":
        controller.play();
        break;
      case "pause":
        controller.pause("user");
        break;
      case "stop":
        controller.stop();
        break;
      case "next":
        controller.nextTrack();
        break;
      case "previous":
        controller.prevTrack();
        break;
      case "seek": {
        if (this.#service?.player.caps?.canSeekTo) {
          controller.seekTo(action.seconds);
          break;
        }
        const now = positionSeconds(entry.position, Date.now(), this.#service?.player.status === "Playing");
        const delta = action.seconds - now;
        if (delta > 0) {
          controller.seekForward(delta);
        } else if (delta < 0) {
          controller.seekBackward(-delta);
        }
        break;
      }
    }
  }

  /* Artwork: downloaded once per image, kept for the last few tracks. */

  #showArtwork(source) {
    if (source === this.#art.source) {
      return;
    }
    const cached = this.#artFiles.get(source);
    this.#art = { source, url: cached?.url || "" };
    if (!source || cached) {
      return;
    }
    this.#downloadArtwork(source).then(
      url => {
        if (url && this.#art.source === source) {
          this.#art.url = url;
          this.#refresh();
        }
      },
      error => console.warn(`zen-app: no artwork for the media player (${error?.message || error})`)
    );
  }

  async #downloadArtwork(source) {
    const abort = new AbortController();
    const timer = lazy.setTimeout(() => abort.abort(), ART_TIMEOUT_MS);
    let type;
    let bytes;
    try {
      const response = await fetch(source, { credentials: "omit", signal: abort.signal });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      if (Number(response.headers.get("content-length") || 0) > MAX_ART_BYTES) {
        throw new Error("too large");
      }
      type = response.headers.get("content-type") || "";
      bytes = new Uint8Array(await response.arrayBuffer());
    } finally {
      lazy.clearTimeout(timer);
    }
    if (bytes.length > MAX_ART_BYTES) {
      throw new Error("too large");
    }
    const extension = imageExtension(type, bytes);
    if (!extension) {
      throw new Error("not an image");
    }
    await IOUtils.makeDirectory(this.artDirectory, { ignoreExisting: true, permissions: 0o700 });
    const path = PathUtils.join(this.artDirectory, `${sha256Hex(source).slice(0, 32)}.${extension}`);
    await IOUtils.write(path, bytes, { tmpPath: path + ".tmp" });
    const url = PathUtils.toFileURI(path);
    this.#artFiles.set(source, { url, path });
    this.#pruneArtwork();
    return url;
  }

  #pruneArtwork() {
    for (const [source, file] of this.#artFiles) {
      if (this.#artFiles.size <= ART_FILES_KEPT) {
        break;
      }
      if (source === this.#art.source) {
        continue;
      }
      this.#artFiles.delete(source);
      IOUtils.remove(file.path, { ignoreAbsent: true }).catch(() => {});
    }
  }
}
