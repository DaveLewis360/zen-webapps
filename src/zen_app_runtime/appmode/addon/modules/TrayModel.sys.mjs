/* zen-app-runtime app mode: the app's tray icon and unread badge (pure model).
 *
 * Tray.sys.mjs publishes a StatusNotifierItem (the tray protocol of KDE,
 * Quickshell/Caelestia, Waybar and most Wayland bars) with a small
 * com.canonical.dbusmenu menu, and the Unity launcher entry count that docks
 * show. This module holds only data, text and GVariant text
 * (https://docs.gtk.org/glib/gvariant-text-format.html), so it runs under node
 * in the tests.
 */

import { cleanText, gvBoolean, gvInt64, gvString } from "./MprisModel.sys.mjs";

export const ITEM_PATH = "/StatusNotifierItem";
export const MENU_PATH = "/MenuBar";
export const ITEM_INTERFACE = "org.kde.StatusNotifierItem";
export const MENU_INTERFACE = "com.canonical.dbusmenu";
export const WATCHER_NAME = "org.kde.StatusNotifierWatcher";
export const WATCHER_PATH = "/StatusNotifierWatcher";
export const WATCHER_INTERFACE = "org.kde.StatusNotifierWatcher";
export const LAUNCHER_INTERFACE = "com.canonical.Unity.LauncherEntry";
/** Icon sizes handed to the tray (hosts pick and scale the closest). */
export const ICON_SIZES = [32, 64];
export const MENU_VERSION = 3;

export const INTROSPECTION_XML = `<node>
  <interface name="${ITEM_INTERFACE}">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <method name="ProvideXdgActivationToken"><arg name="token" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
  <interface name="${MENU_INTERFACE}">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated"><arg name="revision" type="u"/><arg name="parent" type="i"/></signal>
    <signal name="ItemActivationRequested"><arg name="id" type="i"/><arg name="timestamp" type="u"/></signal>
  </interface>
</node>
`;

/** Pixmap properties: Tray.sys.mjs builds them from the rendered icons. */
export const PIXMAP_PROPERTIES = new Set(["IconPixmap", "AttentionIconPixmap", "OverlayIconPixmap"]);

/**
 * Bus name of the item: org.kde.StatusNotifierItem-<pid>-<n>, the
 * convention of the specification (hosts also accept the unique name).
 */
export function itemBusName(pid, instance = 1) {
  const p = Number.isInteger(pid) && pid > 0 ? pid : 0;
  const n = Number.isInteger(instance) && instance > 0 ? instance : 1;
  return `org.kde.StatusNotifierItem-${p}-${n}`;
}

/* ------------------------------------------------------- unread state */

const NONE = Object.freeze({ count: 0, more: false, dot: false });
const COUNT_FRONT = /^[([]\s*(\d{1,6})(\+?)\s*[)\]](?=\s|$)/;
const COUNT_BACK = /(?:^|\s)[([]\s*(\d{1,6})(\+?)\s*[)\]]$/;
const DOT_FRONT = /^[•●]\s/;

/**
 * What a page title says about unread messages: "(3) Discord | …",
 * "WhatsApp (12)", "(9+) Inbox" give a count; Discord's "• Discord | …"
 * (unread, no mention) gives a dot. Anything else: nothing unread.
 */
export function unreadFromTitle(title) {
  const text = cleanText(title, 400);
  const match = COUNT_FRONT.exec(text) || COUNT_BACK.exec(text);
  if (match) {
    const count = Number(match[1]);
    if (count > 0) {
      return { count, more: match[2] === "+", dot: false };
    }
    return NONE;
  }
  return DOT_FRONT.test(text) ? { count: 0, more: false, dot: true } : NONE;
}

export function sameUnread(a, b) {
  return (a?.count || 0) === (b?.count || 0) && !!a?.more === !!b?.more && !!a?.dot === !!b?.dot;
}

export function hasUnread(unread) {
  return (unread?.count || 0) > 0 || !!unread?.dot;
}

/** The text on the badge: "", "•", "3", "9+", "99+". */
export function badgeLabel(unread) {
  const count = unread?.count || 0;
  if (count > 0) {
    if (count > 99) {
      return "99+";
    }
    return unread.more ? `${count}+` : String(count);
  }
  return unread?.dot ? "•" : "";
}

/* ------------------------------------------------------------ pixmaps */

/**
 * Canvas RGBA (straight alpha) → the ARGB32 of the specification, in
 * network byte order: A, R, G, B for every pixel.
 */
export function argbFromRgba(rgba) {
  const length = rgba.length - (rgba.length % 4);
  const argb = new Uint8Array(length);
  for (let i = 0; i < length; i += 4) {
    argb[i] = rgba[i + 3];
    argb[i + 1] = rgba[i];
    argb[i + 2] = rgba[i + 1];
    argb[i + 3] = rgba[i + 2];
  }
  return argb;
}

/**
 * Where the badge goes on an icon of `size` pixels: a pill in the top right
 * corner, wide enough for its label (`textWidth`, measured by the caller).
 */
export function badgeGeometry(size, label, textWidth = 0) {
  if (!label) {
    return null;
  }
  if (label === "•") {
    const radius = Math.max(3, Math.round(size * 0.2));
    return { dot: true, x: size - radius, y: radius, radius, height: radius * 2, width: radius * 2 };
  }
  const height = Math.max(10, Math.round(size * 0.5));
  const padding = Math.round(height * 0.22);
  const width = Math.min(size, Math.max(height, Math.ceil(textWidth) + padding * 2));
  return {
    dot: false,
    x: size - width,
    y: 0,
    width,
    height,
    radius: height / 2,
    fontSize: Math.round(height * 0.72),
  };
}

/* ------------------------------------------------- item properties */

/** SNI Category from the desktop categories of the app. */
export function itemCategory(categories) {
  const chat = (categories || []).some(c => c === "InstantMessaging" || c === "Chat" || c === "Email");
  return chat ? "Communications" : "ApplicationStatus";
}

/** Active, or NeedsAttention while something is waiting (counted unread). */
export function itemStatus(state) {
  return state.badge && (state.unread?.count || 0) > 0 ? "NeedsAttention" : "Active";
}

/** The tooltip's second line: "3 unread" etc., or nothing. */
export function tooltipText(state) {
  if (!state.badge || !hasUnread(state.unread)) {
    return "";
  }
  const labels = state.labels || {};
  if ((state.unread.count || 0) > 0) {
    const template = labels.unread || "{count} unread";
    return template.replace("{count}", badgeLabel(state.unread));
  }
  return labels.unreadDot || "Unread messages";
}

/**
 * [GVariant type, text] of an item property; [type, null] for the pixmaps
 * (Tray.sys.mjs builds those), null for an unknown property.
 */
export function itemProperty(name, state) {
  switch (name) {
    case "Category":
      return ["s", gvString(state.category || "ApplicationStatus")];
    case "Id":
      return ["s", gvString(state.id)];
    case "Title":
      return ["s", gvString(state.title)];
    case "Status":
      return ["s", gvString(itemStatus(state))];
    case "WindowId":
      return ["i", "0"];
    case "IconThemePath":
    case "IconName":
    case "OverlayIconName":
    case "AttentionIconName":
    case "AttentionMovieName":
      return ["s", "''"];
    case "IconPixmap":
    case "AttentionIconPixmap":
    case "OverlayIconPixmap":
      return ["a(iiay)", null];
    case "ToolTip":
      return ["(sa(iiay)ss)", `('', @a(iiay) [], ${gvString(state.title)}, ${gvString(tooltipText(state))})`];
    case "ItemIsMenu":
      return ["b", "false"];
    case "Menu":
      return ["o", `'${MENU_PATH}'`];
    default:
      return null;
  }
}

/** Item signals to send when the state changes from `before` to `after`. */
export function itemSignals(before, after) {
  const signals = [];
  if (!before || before.title !== after.title) {
    signals.push(["NewTitle"]);
    signals.push(["NewToolTip"]);
  } else if (tooltipText(before) !== tooltipText(after)) {
    signals.push(["NewToolTip"]);
  }
  if (!before || itemStatus(before) !== itemStatus(after)) {
    signals.push(["NewStatus", "(s)", `(${gvString(itemStatus(after))},)`]);
  }
  return signals;
}

/* --------------------------------------------------------------- menu */

export const MENU_SHOW = 1;
export const MENU_HIDE = 2;
export const MENU_SETTINGS = 4;
export const MENU_QUIT = 6;

/** The menu: Show or Hide the window, Settings, Quit. */
export function menuItems(state) {
  const labels = state.labels || {};
  const name = state.title || "";
  const fill = (template, fallback) => (template || fallback).replace("{app}", name);
  return [
    state.visible
      ? { id: MENU_HIDE, label: fill(labels.hide, "Hide {app}") }
      : { id: MENU_SHOW, label: fill(labels.show, "Show {app}") },
    { id: 3, type: "separator" },
    { id: MENU_SETTINGS, label: fill(labels.settings, "Settings…") },
    { id: 5, type: "separator" },
    { id: MENU_QUIT, label: fill(labels.quit, "Quit {app}") },
  ];
}

function itemPropertiesText(item, names = null) {
  const wanted = names?.length ? new Set(names) : null;
  const entries = [];
  const add = (key, text) => {
    if (!wanted || wanted.has(key)) {
      entries.push(`${gvString(key)}: ${text}`);
    }
  };
  if (item.type === "separator") {
    add("type", "<'separator'>");
  } else {
    add("label", `<${gvString(item.label)}>`);
    add("enabled", `<${gvBoolean(item.enabled !== false)}>`);
  }
  return entries.length ? `{${entries.join(", ")}}` : "@a{sv} {}";
}

function itemLayoutText(item, names) {
  return `(${item.id}, ${itemPropertiesText(item, names)}, @av [])`;
}

/** GetLayout reply ((u(ia{sv}av))) for `parentId`, or null for an unknown id. */
export function layoutReply(state, parentId, recursionDepth = -1, names = []) {
  const revision = Math.max(1, Math.trunc(state.revision || 1));
  const items = menuItems(state);
  if (parentId === 0) {
    const children = recursionDepth === 0 ? "@av []" : `[${items.map(item => `<${itemLayoutText(item, names)}>`).join(", ")}]`;
    const root = `(0, {'children-display': <'submenu'>}, ${children})`;
    return `(uint32 ${revision}, ${root})`;
  }
  const item = items.find(entry => entry.id === parentId);
  return item ? `(uint32 ${revision}, ${itemLayoutText(item, names)})` : null;
}

/** GetGroupProperties reply ((a(ia{sv}))): every item when `ids` is empty. */
export function groupPropertiesReply(state, ids = [], names = []) {
  const items = menuItems(state);
  const wanted = ids.length ? items.filter(item => ids.includes(item.id)) : items;
  if (!wanted.length) {
    return "(@a(ia{sv}) [],)";
  }
  return `([${wanted.map(item => `(${item.id}, ${itemPropertiesText(item, names)})`).join(", ")}],)`;
}

/** GetProperty reply ((v)), or null when the item or property is unknown. */
export function propertyReply(state, id, name) {
  const item = menuItems(state).find(entry => entry.id === id);
  if (!item) {
    return null;
  }
  if (item.type === "separator") {
    return name === "type" ? "(<'separator'>,)" : null;
  }
  if (name === "label") {
    return `(<${gvString(item.label)}>,)`;
  }
  if (name === "enabled") {
    return `(<${gvBoolean(item.enabled !== false)}>,)`;
  }
  return null;
}

/** Menu properties of the com.canonical.dbusmenu object. */
export function menuProperty(name) {
  switch (name) {
    case "Version":
      return ["u", `uint32 ${MENU_VERSION}`];
    case "TextDirection":
      return ["s", "'ltr'"];
    case "Status":
      return ["s", "'normal'"];
    case "IconThemePath":
      return ["as", "@as []"];
    default:
      return null;
  }
}

/** The action a clicked menu item asks for, or null. */
export function menuAction(id, eventId) {
  if (eventId !== "clicked") {
    return null;
  }
  switch (id) {
    case MENU_SHOW:
      return { type: "show" };
    case MENU_HIDE:
      return { type: "hide" };
    case MENU_SETTINGS:
      return { type: "settings" };
    case MENU_QUIT:
      return { type: "quit" };
    default:
      return null;
  }
}

/** The action of an item method (Activate: show; SecondaryActivate: show or hide). */
export function itemAction(method) {
  switch (method) {
    case "Activate":
      return { type: "show" };
    case "SecondaryActivate":
      return { type: "toggle" };
    default:
      return null;
  }
}

/* ---------------------------------------------- Unity launcher entry */

/** Object path of the launcher entry signal (a stable one per desktop id). */
export function launcherEntryPath(desktopId) {
  const element = String(desktopId || "app").replace(/[^A-Za-z0-9_]/g, "_").slice(0, 120) || "app";
  return `/com/canonical/unity/launcherentry/${element}`;
}

/**
 * com.canonical.Unity.LauncherEntry.Update arguments ((sa{sv})): the count
 * docks and launchers (Plank, Dash to Dock, KDE, nwg-dock) show on the app.
 */
export function launcherEntryText(desktopId, unread, enabled = true) {
  const count = enabled ? unread?.count || 0 : 0;
  const uri = gvString(`application://${desktopId}.desktop`);
  return `(${uri}, {'count': <int64 ${gvInt64(count)}>, 'count-visible': <${gvBoolean(count > 0)}>})`;
}
