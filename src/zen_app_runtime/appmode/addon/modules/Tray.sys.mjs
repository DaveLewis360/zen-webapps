/* zen-app-runtime app mode: the app's tray icon and unread badge.
 *
 * A StatusNotifierItem (with a com.canonical.dbusmenu menu) on a D-Bus
 * connection of its own, so switching the tray off removes the item from
 * every host at once, and the Unity launcher entry count for docks. Same
 * GIO-through-js-ctypes pattern as Mpris.sys.mjs: GLib calls the callbacks
 * below only while JS pumps the tray's own GMainContext, they only read the
 * current state and queue actions, and they never throw into C.
 */

import {
  INTROSPECTION_XML,
  ITEM_INTERFACE,
  ITEM_PATH,
  LAUNCHER_INTERFACE,
  MENU_INTERFACE,
  MENU_PATH,
  PIXMAP_PROPERTIES,
  WATCHER_INTERFACE,
  WATCHER_NAME,
  WATCHER_PATH,
  badgeGeometry,
  badgeLabel,
  groupPropertiesReply,
  itemAction,
  itemBusName,
  itemProperty,
  itemSignals,
  launcherEntryPath,
  launcherEntryText,
  layoutReply,
  menuAction,
  menuProperty,
  propertyReply,
  sameUnread,
  argbFromRgba,
  ICON_SIZES,
} from "resource://zenapp/modules/TrayModel.sys.mjs";
import { cleanText } from "resource://zenapp/modules/MprisModel.sys.mjs";

const lazy = {};
ChromeUtils.defineESModuleGetters(lazy, {
  clearTimeout: "resource://gre/modules/Timer.sys.mjs",
  ctypes: "resource://gre/modules/ctypes.sys.mjs",
  setTimeout: "resource://gre/modules/Timer.sys.mjs",
});

const HTML_NS = "http://www.w3.org/1999/xhtml";
const G_BUS_TYPE_SESSION = 2;
const G_DBUS_ERROR_FAILED = 0;
// G_DBUS_CONNECTION_FLAGS_AUTHENTICATION_CLIENT | _MESSAGE_BUS_CONNECTION
const CONNECTION_FLAGS = 1 | 8;
// G_BUS_NAME_OWNER_FLAGS_DO_NOT_QUEUE
const OWN_FLAGS = 4;
// G_DBUS_CALL_FLAGS_NO_AUTO_START
const CALL_FLAGS = 1;
/** Clicks and menus are answered this fast while a tray host is there… */
const PUMP_HOSTED_MS = 120;
/** …and this fast while there is none (only its arrival is waited for). */
const PUMP_IDLE_MS = 1000;
const PUMP_BATCH = 64;
const BADGE_COLOR = "#e5484d";
const BADGE_TEXT = "#ffffff";

let native = null;
/** The open tray item (one per app process), read by the GLib callbacks. */
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
    const NameAppeared = ctypes.FunctionType(abi, ctypes.void_t, [ptr, str, str, ptr]);
    const NameVanished = ctypes.FunctionType(abi, ctypes.void_t, [ptr, str, ptr]);
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
      NameAppeared,
      NameVanished,
      VTable,
      libraries,
      addressForBus: gio.declare("g_dbus_address_get_for_bus_sync", abi, str, ctypes.int, ptr, errorOut),
      connectionNew: gio.declare("g_dbus_connection_new_for_address_sync", abi, ptr, str, ctypes.int, ptr, ptr, errorOut),
      connectionClose: gio.declare("g_dbus_connection_close_sync", abi, ctypes.int, ptr, ptr, errorOut),
      nodeInfoNewForXml: gio.declare("g_dbus_node_info_new_for_xml", abi, ptr, str, errorOut),
      nodeInfoLookupInterface: gio.declare("g_dbus_node_info_lookup_interface", abi, ptr, ptr, str),
      nodeInfoUnref: gio.declare("g_dbus_node_info_unref", abi, ctypes.void_t, ptr),
      registerObject: gio.declare(
        "g_dbus_connection_register_object", abi, ctypes.unsigned_int, ptr, str, ptr, VTable.ptr, ptr, ptr, errorOut
      ),
      unregisterObject: gio.declare("g_dbus_connection_unregister_object", abi, ctypes.int, ptr, ctypes.unsigned_int),
      ownName: gio.declare("g_bus_own_name_on_connection", abi, ctypes.unsigned_int, ptr, str, ctypes.int, ptr, ptr, ptr, ptr),
      unownName: gio.declare("g_bus_unown_name", abi, ctypes.void_t, ctypes.unsigned_int),
      watchName: gio.declare(
        "g_bus_watch_name_on_connection", abi, ctypes.unsigned_int, ptr, str, ctypes.int, NameAppeared.ptr, NameVanished.ptr, ptr, ptr
      ),
      unwatchName: gio.declare("g_bus_unwatch_name", abi, ctypes.void_t, ctypes.unsigned_int),
      call: gio.declare(
        "g_dbus_connection_call", abi, ctypes.void_t, ptr, str, str, str, str, ptr, ptr, ctypes.int, ctypes.int, ptr, ptr, ptr
      ),
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
      variantNChildren: glib.declare("g_variant_n_children", abi, ctypes.size_t, ptr),
      variantGetChildValue: glib.declare("g_variant_get_child_value", abi, ptr, ptr, ctypes.size_t),
      variantGetInt32: glib.declare("g_variant_get_int32", abi, ctypes.int32_t, ptr),
      variantGetString: glib.declare("g_variant_get_string", abi, str, ptr, ptr),
      variantNewInt32: glib.declare("g_variant_new_int32", abi, ptr, ctypes.int32_t),
      variantNewFixedArray: glib.declare("g_variant_new_fixed_array", abi, ptr, str, ptr, ctypes.size_t, ctypes.size_t),
      variantNewTuple: glib.declare("g_variant_new_tuple", abi, ptr, ptr.ptr, ctypes.size_t),
      variantNewArray: glib.declare("g_variant_new_array", abi, ptr, str, ptr.ptr, ctypes.size_t),
      setErrorLiteral: glib.declare("g_set_error_literal", abi, ctypes.void_t, errorOut, ctypes.uint32_t, ctypes.int, str),
      errorFree: glib.declare("g_error_free", abi, ctypes.void_t, GError.ptr),
      free: glib.declare("g_free", abi, ctypes.void_t, ptr),
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
    native = null;
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

/** A floating a(iiay) of the rendered icons ([{width, height, argb}]). */
function pixmapVariant(images) {
  const { ctypes } = native;
  const entries = [];
  for (const image of images || []) {
    const buffer = image.buffer;
    const children = ctypes.voidptr_t.array(3)();
    children[0] = native.variantNewInt32(image.width);
    children[1] = native.variantNewInt32(image.height);
    // Copied by GLib; the buffer stays ours.
    children[2] = native.variantNewFixedArray("y", ctypes.cast(buffer.addressOfElement(0), ctypes.voidptr_t), image.argb.length, 1);
    // The new values are floating: the tuple and the array take them over.
    entries.push(native.variantNewTuple(children.addressOfElement(0), 3));
  }
  const array = ctypes.voidptr_t.array(Math.max(1, entries.length))();
  entries.forEach((entry, index) => {
    array[index] = entry;
  });
  return native.variantNewArray("(iiay)", array.addressOfElement(0), entries.length);
}

function child(parameters, index) {
  return native.variantGetChildValue(parameters, index);
}

function withChild(parameters, index, read) {
  const value = child(parameters, index);
  try {
    return read(value);
  } finally {
    native.variantUnref(value);
  }
}

const int32Of = value => native.variantGetInt32(value);
const stringOf = value => native.variantGetString(value, null).readString();

function int32List(value) {
  const result = [];
  const count = Number(native.variantNChildren(value).toString());
  for (let i = 0; i < Math.min(count, 256); i++) {
    result.push(withChild(value, i, int32Of));
  }
  return result;
}

function stringList(value) {
  const result = [];
  const count = Number(native.variantNChildren(value).toString());
  for (let i = 0; i < Math.min(count, 64); i++) {
    result.push(withChild(value, i, stringOf));
  }
  return result;
}

function readArguments(method, parameters) {
  // GDBus has already checked the arguments against the introspection data.
  switch (method) {
    case "GetLayout":
      return {
        parentId: withChild(parameters, 0, int32Of),
        depth: withChild(parameters, 1, int32Of),
        names: withChild(parameters, 2, stringList),
      };
    case "GetGroupProperties":
      return { ids: withChild(parameters, 0, int32List), names: withChild(parameters, 1, stringList) };
    case "GetProperty":
      return { id: withChild(parameters, 0, int32Of), name: withChild(parameters, 1, stringOf) };
    case "Event":
      return { id: withChild(parameters, 0, int32Of), eventId: withChild(parameters, 1, stringOf) };
    case "EventGroup":
      return {
        events: withChild(parameters, 0, list => {
          const events = [];
          const count = Number(native.variantNChildren(list).toString());
          for (let i = 0; i < Math.min(count, 32); i++) {
            events.push(withChild(list, i, entry => ({ id: withChild(entry, 0, int32Of), eventId: withChild(entry, 1, stringOf) })));
          }
          return events;
        }),
      };
    case "AboutToShow":
      return { id: withChild(parameters, 0, int32Of) };
    case "ProvideXdgActivationToken":
      return { token: withChild(parameters, 0, stringOf) };
    default:
      return {};
  }
}

function onMethodCall(connection, sender, objectPath, interfaceName, methodName, parameters, invocation) {
  let reply;
  try {
    const method = methodName.readString();
    const iface = interfaceName.isNull() ? null : interfaceName.readString();
    reply = current
      ? current.acceptMethod(iface, method, readArguments(method, parameters))
      : { error: ["org.freedesktop.DBus.Error.Failed", "The tray icon is closed"] };
  } catch (error) {
    reply = { error: ["org.freedesktop.DBus.Error.Failed", String(error?.message || error)] };
  }
  try {
    if (reply?.error) {
      native.returnDbusError(invocation, reply.error[0], cleanText(reply.error[1]));
    } else if (reply?.value) {
      const value = parseVariant(reply.value[0], reply.value[1]);
      try {
        native.returnValue(invocation, value);
      } finally {
        native.variantUnref(value);
      }
    } else {
      native.returnValue(invocation, null);
    }
  } catch (error) {
    console.error("zen-app: tray reply", error);
  }
}

function onGetProperty(connection, sender, objectPath, interfaceName, propertyName, error) {
  try {
    if (!current) {
      throw new Error("The tray icon is closed");
    }
    const iface = interfaceName.readString();
    const name = propertyName.readString();
    if (iface === ITEM_INTERFACE && PIXMAP_PROPERTIES.has(name)) {
      // A floating value: GDBus consumes it.
      return pixmapVariant(name === "OverlayIconPixmap" ? [] : current.images);
    }
    const value = iface === ITEM_INTERFACE ? itemProperty(name, current.state) : menuProperty(name);
    if (!value) {
      throw new Error(`No property ${name}`);
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

function onWatcherAppeared() {
  try {
    current?.watcherChanged(true);
  } catch {
    // never throw into C
  }
}

function onWatcherVanished() {
  try {
    current?.watcherChanged(false);
  } catch {
    // never throw into C
  }
}

/* --------------------------------------------------- D-Bus service */

/**
 * The tray item and the launcher entry on a private session-bus
 * connection. `item: false` keeps only the launcher entry (badge without a
 * tray icon).
 */
export class TrayService {
  #connection = null;
  #context = null;
  #node = null;
  #registrations = [];
  #ownerId = 0;
  #watchId = 0;
  #pumpTimer = null;
  #launcherText = null;

  constructor({ id, title, category, labels, pid, item = true, badge = true, onAction, onHostChange }) {
    this.busName = itemBusName(pid);
    this.item = item;
    this.onAction = onAction;
    this.onHostChange = onHostChange;
    this.hosted = false;
    this.activationToken = "";
    this.images = [];
    this.state = { id, title, category, labels, badge, unread: { count: 0, more: false, dot: false }, visible: true, revision: 1 };
  }

  get open() {
    return !!this.#connection;
  }

  open() {
    bind();
    if (current) {
      throw new Error("the app's tray icon is already open");
    }
    try {
      const address = checked(
        "session bus address",
        error => native.addressForBus(G_BUS_TYPE_SESSION, null, error),
        value => !value.isNull()
      );
      let addressText;
      try {
        addressText = address.readString();
      } finally {
        native.free(native.ctypes.cast(address, native.ctypes.voidptr_t));
      }
      // GDBus calls an object back in the context that was the thread's
      // default when it was registered: this private one, which only JS runs.
      this.#context = native.mainContextNew();
      native.pushThreadDefault(this.#context);
      try {
        this.#connection = checked(
          "session bus",
          error => native.connectionNew(addressText, CONNECTION_FLAGS, null, null, error),
          connection => !connection.isNull()
        );
        if (this.item) {
          this.#registerItem();
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

  #registerItem() {
    this.#node = checked("introspection", error => native.nodeInfoNewForXml(INTROSPECTION_XML, error), node => !node.isNull());
    closures ??= {
      methodCall: native.MethodCall.ptr(onMethodCall),
      getProperty: native.GetProperty.ptr(onGetProperty),
      appeared: native.NameAppeared.ptr(onWatcherAppeared),
      vanished: native.NameVanished.ptr(onWatcherVanished),
    };
    const vtable = new native.VTable();
    vtable.method_call = closures.methodCall;
    vtable.get_property = closures.getProperty;
    for (const [path, name] of [
      [ITEM_PATH, ITEM_INTERFACE],
      [MENU_PATH, MENU_INTERFACE],
    ]) {
      const info = native.nodeInfoLookupInterface(this.#node, name);
      if (info.isNull()) {
        throw new Error(`no interface ${name}`);
      }
      this.#registrations.push(
        checked(
          `register ${name}`,
          error => native.registerObject(this.#connection, path, info, vtable.address(), null, null, error),
          id => id > 0
        )
      );
    }
    // Name owning only runs GLib's own C callbacks (none of ours).
    this.#ownerId = native.ownName(this.#connection, this.busName, OWN_FLAGS, null, null, null, null);
    // The host (the watcher) may start after the app, or restart.
    this.#watchId = native.watchName(this.#connection, WATCHER_NAME, 0, closures.appeared, closures.vanished, null, null);
  }

  /** Answer the D-Bus calls waiting for the item; returns how many ran. */
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
          console.error("zen-app: tray", error);
        }
        this.#schedulePump();
      },
      this.hosted ? PUMP_HOSTED_MS : PUMP_IDLE_MS
    );
  }

  close() {
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
        console.error("zen-app: tray close", error);
      }
    };
    if (this.#launcherText && this.#connection) {
      // Docks forget the count when the app goes.
      attempt(() => this.#emit(launcherEntryPath(this.state.id), LAUNCHER_INTERFACE, "Update", "(sa{sv})", launcherEntryText(this.state.id, null)));
    }
    if (this.#watchId) {
      attempt(() => native.unwatchName(this.#watchId));
      this.#watchId = 0;
    }
    if (this.#ownerId) {
      attempt(() => native.unownName(this.#ownerId));
      this.#ownerId = 0;
    }
    for (const id of this.#registrations.splice(0)) {
      attempt(() => native.unregisterObject(this.#connection, id));
    }
    if (this.#context) {
      attempt(() => this.pump());
    }
    if (this.#connection) {
      // Closing the private connection drops the item from every host.
      attempt(() => checked("close", error => native.connectionClose(this.#connection, null, error), ok => !!ok));
      attempt(() => native.objectUnref(this.#connection));
      this.#connection = null;
    }
    if (this.#context) {
      attempt(() => this.pump());
      attempt(() => native.mainContextUnref(this.#context));
      this.#context = null;
    }
    if (this.#node) {
      attempt(() => native.nodeInfoUnref(this.#node));
      this.#node = null;
    }
    this.hosted = false;
  }

  /** The watcher (the tray host) appeared or went: register with it again. */
  watcherChanged(present) {
    this.hosted = present;
    // Out of the GLib callback; registered by object path, so the host
    // uses this connection's unique name (no race with the bus name).
    Services.tm.dispatchToMainThread(() => {
      if (present) {
        this.#registerWithWatcher();
      }
      try {
        this.onHostChange?.(present);
      } catch (error) {
        console.error("zen-app: tray host", error);
      }
    });
    this.#schedulePump();
  }

  #registerWithWatcher() {
    if (!this.#connection || !this.item) {
      return;
    }
    let parameters = null;
    try {
      parameters = parseVariant("(s)", `('${ITEM_PATH}',)`);
      // No reply wanted: nothing waits on the host.
      native.call(this.#connection, WATCHER_NAME, WATCHER_PATH, WATCHER_INTERFACE, "RegisterStatusNotifierItem", parameters, null, CALL_FLAGS, -1, null, null, null);
    } catch (error) {
      console.error("zen-app: tray host", error);
    } finally {
      if (parameters) {
        native.variantUnref(parameters);
      }
    }
  }

  /** New state (title, unread, badge, visible, labels): tell the hosts what changed. */
  update(patch) {
    const before = this.state;
    const after = { ...before, ...patch };
    if (patch.visible !== undefined && patch.visible !== before.visible) {
      after.revision = before.revision + 1;
    } else if (patch.labels && JSON.stringify(patch.labels) !== JSON.stringify(before.labels)) {
      after.revision = before.revision + 1;
    } else if (patch.title !== undefined && patch.title !== before.title) {
      after.revision = before.revision + 1;
    }
    this.state = after;
    if (!this.#connection) {
      return;
    }
    if (this.item) {
      for (const [signal, type, text] of itemSignals(before, after)) {
        this.#emit(ITEM_PATH, ITEM_INTERFACE, signal, type, text);
      }
      if (after.revision !== before.revision) {
        this.#emit(MENU_PATH, MENU_INTERFACE, "LayoutUpdated", "(ui)", `(uint32 ${after.revision}, 0)`);
      }
    }
    const launcher = launcherEntryText(after.id, after.unread, after.badge);
    if (launcher !== this.#launcherText && (this.#launcherText !== null || (after.badge && after.unread?.count))) {
      this.#launcherText = launcher;
      this.#emit(launcherEntryPath(after.id), LAUNCHER_INTERFACE, "Update", "(sa{sv})", launcher);
    }
  }

  /** New icon images ([{width, height, argb}]): hosts fetch them again. */
  setImages(images) {
    const { ctypes } = native || bind();
    this.images = images.map(image => {
      const buffer = ctypes.uint8_t.array(image.argb.length)();
      // One copy into C memory; every Get copies from here.
      for (let i = 0; i < image.argb.length; i++) {
        buffer[i] = image.argb[i];
      }
      return { width: image.width, height: image.height, argb: image.argb, buffer };
    });
    if (this.#connection && this.item) {
      this.#emit(ITEM_PATH, ITEM_INTERFACE, "NewIcon");
      this.#emit(ITEM_PATH, ITEM_INTERFACE, "NewAttentionIcon");
    }
  }

  /** A method call from D-Bus: {error}, {value: [type, text]} or null for an empty reply. */
  acceptMethod(interfaceName, method, args) {
    if (interfaceName === MENU_INTERFACE) {
      return this.#menuMethod(method, args);
    }
    if (interfaceName !== ITEM_INTERFACE) {
      return { error: ["org.freedesktop.DBus.Error.UnknownInterface", `No interface ${interfaceName}`] };
    }
    if (method === "ProvideXdgActivationToken") {
      this.activationToken = cleanText(args.token, 512);
      return null;
    }
    const action = itemAction(method);
    if (action) {
      this.#queue(action);
    }
    return null;
  }

  #menuMethod(method, args) {
    const unknown = { error: ["org.freedesktop.DBus.Error.InvalidArgs", "No such menu item"] };
    switch (method) {
      case "GetLayout": {
        const text = layoutReply(this.state, args.parentId, args.depth, args.names);
        return text ? { value: ["(u(ia{sv}av))", text] } : unknown;
      }
      case "GetGroupProperties":
        return { value: ["(a(ia{sv}))", groupPropertiesReply(this.state, args.ids, args.names)] };
      case "GetProperty": {
        const text = propertyReply(this.state, args.id, args.name);
        return text ? { value: ["(v)", text] } : unknown;
      }
      case "Event": {
        const action = menuAction(args.id, args.eventId);
        if (action) {
          this.#queue(action);
        }
        return null;
      }
      case "EventGroup": {
        for (const event of args.events || []) {
          const action = menuAction(event.id, event.eventId);
          if (action) {
            this.#queue(action);
          }
        }
        return { value: ["(ai)", "(@ai [],)"] };
      }
      case "AboutToShow":
        return { value: ["(b)", "(false,)"] };
      case "AboutToShowGroup":
        return { value: ["(aiai)", "(@ai [], @ai [])"] };
      default:
        return { error: ["org.freedesktop.DBus.Error.UnknownMethod", `No method ${method}`] };
    }
  }

  #queue(action) {
    // Out of the GLib callback: showing a window may spin the event loop.
    Services.tm.dispatchToMainThread(() => {
      try {
        this.onAction?.(action);
      } catch (error) {
        console.error("zen-app: tray action", error);
      }
    });
  }

  #emit(path, interfaceName, signal, type = null, text = null) {
    let body = null;
    try {
      body = type ? parseVariant(type, text) : null;
      checked(
        signal,
        error => native.emitSignal(this.#connection, null, path, interfaceName, signal, body, error),
        ok => !!ok
      );
    } catch (error) {
      console.error("zen-app: tray signal", error);
    } finally {
      // The message took its own reference.
      if (body) {
        native.variantUnref(body);
      }
    }
  }
}

/* --------------------------------------------------- icon drawing */

function roundedRect(context, x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height / 2);
  context.beginPath();
  context.moveTo(x + r, y);
  context.arcTo(x + width, y, x + width, y + height, r);
  context.arcTo(x + width, y + height, x, y + height, r);
  context.arcTo(x, y + height, x, y, r);
  context.arcTo(x, y, x + width, y, r);
  context.closePath();
}

async function loadImage(doc, url) {
  if (!url) {
    return null;
  }
  const image = new doc.defaultView.Image();
  image.src = url;
  try {
    await image.decode();
    return image;
  } catch {
    return null;
  }
}

/**
 * The app icon at the tray sizes, with the unread badge drawn on it, as
 * ARGB32 images. Without an icon file the app's letter on its accent.
 */
export async function renderTrayImages(doc, { iconURL, name, accent, label }) {
  const image = await loadImage(doc, iconURL);
  const images = [];
  for (const size of ICON_SIZES) {
    const canvas = doc.createElementNS(HTML_NS, "canvas");
    canvas.width = size;
    canvas.height = size;
    const context = canvas.getContext("2d");
    if (image) {
      context.drawImage(image, 0, 0, size, size);
    } else {
      context.fillStyle = accent || "#7b6cff";
      context.beginPath();
      context.arc(size / 2, size / 2, size / 2, 0, Math.PI * 2);
      context.fill();
      context.fillStyle = "#ffffff";
      context.font = `600 ${Math.round(size * 0.55)}px sans-serif`;
      context.textAlign = "center";
      context.textBaseline = "middle";
      context.fillText((String(name || "?").trim()[0] || "?").toUpperCase(), size / 2, size / 2 + size * 0.03);
    }
    if (label) {
      const probe = badgeGeometry(size, label, 0);
      let textWidth = 0;
      if (!probe.dot) {
        context.font = `700 ${probe.fontSize}px sans-serif`;
        textWidth = context.measureText(label).width;
      }
      const badge = badgeGeometry(size, label, textWidth);
      // A transparent ring keeps the badge apart from the icon.
      context.save();
      context.globalCompositeOperation = "destination-out";
      const ring = Math.max(1, Math.round(size / 24));
      if (badge.dot) {
        context.beginPath();
        context.arc(badge.x, badge.y, badge.radius + ring, 0, Math.PI * 2);
        context.fill();
      } else {
        roundedRect(context, badge.x - ring, badge.y - ring, badge.width + ring * 2, badge.height + ring * 2, badge.radius + ring);
        context.fill();
      }
      context.restore();
      context.fillStyle = BADGE_COLOR;
      if (badge.dot) {
        context.beginPath();
        context.arc(badge.x, badge.y, badge.radius, 0, Math.PI * 2);
        context.fill();
      } else {
        roundedRect(context, badge.x, badge.y, badge.width, badge.height, badge.radius);
        context.fill();
        context.fillStyle = BADGE_TEXT;
        context.font = `700 ${badge.fontSize}px sans-serif`;
        context.textAlign = "center";
        context.textBaseline = "middle";
        context.fillText(label, badge.x + badge.width / 2, badge.y + badge.height / 2 + badge.fontSize * 0.05);
      }
    }
    const pixels = context.getImageData(0, 0, size, size).data;
    images.push({ width: size, height: size, argb: argbFromRgba(pixels) });
  }
  return images;
}

/* ------------------------------------------------ tray of the app */

/**
 * The app's side of the tray: what the item shows (name, unread badge,
 * window shown or hidden) and what its clicks do. `app` provides
 * `trayAction(action)` and the main window's document for drawing.
 */
export class TrayBridge {
  #app;
  #service = null;
  #options;
  #unread = { count: 0, more: false, dot: false };
  #label = null;
  #drawing = null;
  #drawAgain = false;

  constructor(app, { id, title, category, labels, iconURL, accent, item, badge }) {
    this.#app = app;
    this.#options = { id, title, category, labels, iconURL, accent, item, badge };
  }

  get service() {
    return this.#service;
  }

  get state() {
    if (!this.#service) {
      return { state: "off" };
    }
    return {
      state: "on",
      item: this.#service.item,
      hosted: this.#service.hosted,
      busName: this.#service.item ? this.#service.busName : null,
    };
  }

  get unread() {
    return { ...this.#unread };
  }

  /** Throws when it cannot be published (no session bus, no GIO). */
  start({ visible = true } = {}) {
    const { id, title, category, labels, item, badge } = this.#options;
    this.#service = new TrayService({
      id,
      title,
      category,
      labels,
      item,
      badge,
      pid: Services.appinfo.processID,
      onAction: action => this.#app.trayAction(action),
      onHostChange: present => this.#app.trayHostChanged?.(present),
    });
    try {
      this.#service.open();
    } catch (error) {
      this.#service = null;
      throw error;
    }
    this.#service.update({ visible, unread: this.#unread });
    if (item) {
      this.#draw();
    }
  }

  stop() {
    this.#service?.close();
    this.#service = null;
  }

  setVisible(visible) {
    this.#service?.update({ visible });
  }

  setTitle(title) {
    this.#options.title = title;
    this.#service?.update({ title });
  }

  setUnread(unread) {
    if (sameUnread(unread, this.#unread)) {
      return;
    }
    this.#unread = { count: unread?.count || 0, more: !!unread?.more, dot: !!unread?.dot };
    this.#service?.update({ unread: this.#unread });
    this.#draw();
  }

  #draw() {
    const service = this.#service;
    if (!service?.item) {
      return;
    }
    const label = this.#options.badge ? badgeLabel(this.#unread) : "";
    if (label === this.#label && service.images.length) {
      return;
    }
    if (this.#drawing) {
      this.#drawAgain = true;
      return;
    }
    const doc = this.#app.drawingDocument;
    if (!doc) {
      return;
    }
    this.#drawing = renderTrayImages(doc, { iconURL: this.#options.iconURL, name: this.#options.title, accent: this.#options.accent, label })
      .then(images => {
        if (this.#service === service) {
          service.setImages(images);
          this.#label = label;
        }
      })
      .catch(error => console.error("zen-app: tray icon", error))
      .finally(() => {
        this.#drawing = null;
        if (this.#drawAgain) {
          this.#drawAgain = false;
          this.#draw();
        }
      });
  }
}
