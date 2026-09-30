/* zen-app-runtime app mode: what the app's own media player says on D-Bus.
 *
 * The app publishes an MPRIS player named after itself ("Spotify") instead of
 * Gecko's ("Mozilla zen-app-spotify"). This module is its pure part: it turns
 * the media state Gecko reports (MediaController) into MPRIS property values
 * in GVariant text format, which Mpris.sys.mjs hands to g_variant_parse().
 * No Gecko or GLib access here, so node runs the tests.
 */

export const OBJECT_PATH = "/org/mpris/MediaPlayer2";
export const ROOT_INTERFACE = "org.mpris.MediaPlayer2";
export const PLAYER_INTERFACE = "org.mpris.MediaPlayer2.Player";
export const PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties";
export const NO_TRACK = "/org/mpris/MediaPlayer2/TrackList/NoTrack";
export const TRACK_PREFIX = "/org/mpris/MediaPlayer2/Track/";
/** Longest text sent (titles, names, URLs). */
export const MAX_TEXT = 2000;
/** A reported position this far (seconds) from the expected one is a seek. */
export const JUMP_SECONDS = 1;
/** Artwork this big (px) is enough for any shell; bigger is only used if nothing else fits. */
export const ART_TARGET = 256;

// Rate and Volume are read-only here: the app does not change them.
export const INTROSPECTION_XML = `<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition"><arg direction="in" name="TrackId" type="o"/><arg direction="in" name="Position" type="x"/></method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="read"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="read"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
`;

/**
 * Bus name of the app's player: org.mpris.MediaPlayer2.<id>.instance<pid>,
 * the MPRIS convention for players that can run more than once
 * (`playerctl -p spotify` finds it; it never takes the real client's name).
 */
export function busName(appId, pid) {
  let element = String(appId || "").replace(/[^A-Za-z0-9_]/g, "_").slice(0, 64);
  if (!element || /^[0-9]/.test(element)) {
    element = "_" + element;
  }
  const instance = Number.isInteger(pid) && pid > 0 ? `.instance${pid}` : "";
  return `org.mpris.MediaPlayer2.${element}${instance}`;
}

function wellFormed(text) {
  return typeof text.toWellFormed === "function" ? text.toWellFormed() : text;
}

/** One line of valid text: no control characters, no lone surrogates, bounded. */
export function cleanText(value, limit = MAX_TEXT) {
  let text = wellFormed(String(value ?? ""));
  text = text.replace(/[\u0000-\u001f\u007f]+/g, " ").trim();
  return text.length > limit ? wellFormed(text.slice(0, limit)) : text;
}

/* GVariant text format (https://docs.gtk.org/glib/gvariant-text-format.html). */

export function gvString(value) {
  return "'" + cleanText(value).replace(/\\/g, "\\\\").replace(/'/g, "\\'") + "'";
}

export function gvBoolean(value) {
  return value ? "true" : "false";
}

export function gvDouble(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "0.0";
  }
  return Number.isInteger(number) ? number.toFixed(1) : String(number);
}

export function gvInt64(value) {
  const number = Math.round(Number(value));
  if (!Number.isFinite(number)) {
    return "0";
  }
  return String(Math.max(-Number.MAX_SAFE_INTEGER, Math.min(Number.MAX_SAFE_INTEGER, number)));
}

export function gvStringArray(values) {
  return "[" + [...(values || [])].map(gvString).join(", ") + "]";
}

const OBJECT_PATH_RE = /^\/(?:[A-Za-z0-9_]+(?:\/[A-Za-z0-9_]+)*)?$/;

/** A D-Bus object path value (typed "o" by its context). */
export function gvObjectPath(path) {
  if (!OBJECT_PATH_RE.test(String(path))) {
    throw new Error(`not an object path: ${path}`);
  }
  return `'${path}'`;
}

/** A variant holding a value of `type` written as `text`. */
export function gvVariant(type, text) {
  return `<@${type} ${text}>`;
}

/* ------------------------------------------------------ media state */

/** MPRIS PlaybackStatus of a controller's state ({active, playing, state}). */
export function playbackStatus(media) {
  if (!media?.active) {
    return "Stopped";
  }
  if (media.state === "playing") {
    return "Playing";
  }
  if (media.state === "paused") {
    return "Paused";
  }
  return media.playing ? "Playing" : "Paused";
}

/** What an MPRIS client may do, from the controller's supported keys. */
export function capabilities(keys, { seekable = true } = {}) {
  const has = new Set(keys || []);
  return {
    canPlay: has.has("play") || has.has("playpause"),
    canPause: has.has("pause") || has.has("playpause"),
    canGoNext: has.has("nexttrack"),
    canGoPrevious: has.has("previoustrack"),
    canSeek: seekable && (has.has("seekto") || has.has("seekforward") || has.has("seekbackward")),
    canSeekTo: seekable && has.has("seekto"),
  };
}

/** A position state event (seconds) with the time (ms) it was received. */
export function positionState(raw, at) {
  const number = value => (typeof value === "number" && Number.isFinite(value) ? value : null);
  const duration = typeof raw?.duration === "number" && raw.duration > 0 ? raw.duration : null;
  return {
    duration,
    rate: number(raw?.playbackRate) ?? 1,
    position: Math.max(0, number(raw?.position) ?? 0),
    at,
  };
}

/** Where playback is at `now` (ms), in seconds; paused media does not move. */
export function positionSeconds(state, now, playing = true) {
  if (!state) {
    return 0;
  }
  const rate = playing ? state.rate : 0;
  let seconds = state.position + (Math.max(0, now - state.at) / 1000) * rate;
  if (Number.isFinite(state.duration) && state.duration > 0) {
    seconds = Math.min(seconds, state.duration);
  }
  return Math.max(0, seconds);
}

/** Whether `next` jumped away from where `previous` would have been (a seek). */
export function isJump(previous, next, playing) {
  if (!previous || !next) {
    return false;
  }
  return Math.abs(next.position - positionSeconds(previous, next.at, playing)) > JUMP_SECONDS;
}

/** The track a metadata object describes (a new one gets a new trackid). */
export function trackKey(metadata) {
  return JSON.stringify([metadata?.title || "", metadata?.artist || "", metadata?.album || ""]);
}

/**
 * The artwork to show: the smallest image of at least ART_TARGET pixels, else
 * a scalable one, else the biggest. Only https and data: images can be
 * downloaded (blob: URLs belong to the page).
 */
export function pickArtwork(artwork, target = ART_TARGET) {
  const candidates = [];
  for (const image of artwork || []) {
    const src = typeof image?.src === "string" ? image.src : "";
    if (!/^(?:https:\/\/|data:image\/)/i.test(src) || src.length > 2_000_000) {
      continue;
    }
    let size = 0;
    for (const token of String(image.sizes || "").toLowerCase().split(/\s+/)) {
      if (token === "any") {
        size = Infinity;
        break;
      }
      const match = /^(\d+)x(\d+)$/.exec(token);
      if (match) {
        size = Math.max(size, Math.min(Number(match[1]), Number(match[2])));
      }
    }
    candidates.push({ src, size });
  }
  const enough = candidates.filter(c => c.size >= target && c.size !== Infinity).sort((a, b) => a.size - b.size);
  if (enough.length) {
    return enough[0].src;
  }
  const scalable = candidates.find(c => c.size === Infinity);
  if (scalable) {
    return scalable.src;
  }
  return candidates.sort((a, b) => b.size - a.size)[0]?.src || "";
}

const IMAGE_TYPES = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/jpg": "jpg",
  "image/webp": "webp",
  "image/gif": "gif",
  "image/avif": "avif",
  "image/svg+xml": "svg",
};

/** File extension of a downloaded image (from its bytes, else its declared type); "" if not an image. */
export function imageExtension(contentType, bytes) {
  const b = bytes || [];
  if (b[0] === 0x89 && b[1] === 0x50 && b[2] === 0x4e && b[3] === 0x47) {
    return "png";
  }
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) {
    return "jpg";
  }
  if (b[0] === 0x47 && b[1] === 0x49 && b[2] === 0x46) {
    return "gif";
  }
  if (b[0] === 0x52 && b[1] === 0x49 && b[2] === 0x46 && b[3] === 0x46 && b[8] === 0x57 && b[9] === 0x45 && b[10] === 0x42 && b[11] === 0x50) {
    return "webp";
  }
  const type = String(contentType || "").split(";")[0].trim().toLowerCase();
  return type === "image/svg+xml" || type === "image/avif" ? IMAGE_TYPES[type] : "";
}

/* ---------------------------------------------------- property values */

function rateOf(player) {
  return Number.isFinite(player?.rate) && player.rate > 0 ? player.rate : 1;
}

/** The Metadata dictionary (a{sv}) of a player. */
export function metadataText(metadata = {}) {
  const entries = [];
  const add = (key, type, text) => entries.push(`${gvString(key)}: ${gvVariant(type, text)}`);
  add("mpris:trackid", "o", gvObjectPath(metadata.trackId || NO_TRACK));
  if (Number.isFinite(metadata.lengthUs) && metadata.lengthUs > 0) {
    add("mpris:length", "x", gvInt64(metadata.lengthUs));
  }
  for (const [key, field] of [
    ["xesam:title", "title"],
    ["xesam:album", "album"],
    ["mpris:artUrl", "artUrl"],
  ]) {
    if (cleanText(metadata[field])) {
      add(key, "s", gvString(metadata[field]));
    }
  }
  if (cleanText(metadata.artist)) {
    add("xesam:artist", "as", gvStringArray([metadata.artist]));
  }
  return "{" + entries.join(", ") + "}";
}

const ROOT_PROPERTIES = {
  CanQuit: () => ["b", "false"],
  CanRaise: () => ["b", "true"],
  HasTrackList: () => ["b", "false"],
  Identity: player => ["s", gvString(player.identity)],
  DesktopEntry: player => ["s", gvString(player.desktopEntry)],
  SupportedUriSchemes: () => ["as", "[]"],
  SupportedMimeTypes: () => ["as", "[]"],
};

const PLAYER_PROPERTIES = {
  PlaybackStatus: player => ["s", gvString(player.status || "Stopped")],
  Rate: player => ["d", gvDouble(rateOf(player))],
  Metadata: player => ["a{sv}", metadataText(player.metadata)],
  Volume: () => ["d", "1.0"],
  Position: (player, now) => ["x", gvInt64(positionSeconds(player.position, now, player.status === "Playing") * 1e6)],
  MinimumRate: player => ["d", gvDouble(Math.min(1, rateOf(player)))],
  MaximumRate: player => ["d", gvDouble(Math.max(1, rateOf(player)))],
  CanGoNext: player => ["b", gvBoolean(player.caps?.canGoNext)],
  CanGoPrevious: player => ["b", gvBoolean(player.caps?.canGoPrevious)],
  CanPlay: player => ["b", gvBoolean(player.caps?.canPlay)],
  CanPause: player => ["b", gvBoolean(player.caps?.canPause)],
  CanSeek: player => ["b", gvBoolean(player.caps?.canSeek)],
  CanControl: () => ["b", "true"],
};

/** [GVariant type, text] of a property, or null for an unknown one. */
export function propertyValue(interfaceName, name, player, now = Date.now()) {
  const table = interfaceName === ROOT_INTERFACE ? ROOT_PROPERTIES : interfaceName === PLAYER_INTERFACE ? PLAYER_PROPERTIES : null;
  const getter = table && Object.hasOwn(table, name) ? table[name] : null;
  return getter ? getter(player || {}, now) : null;
}

/** Player properties announced with PropertiesChanged (Position never is). */
export const SIGNALLED = [
  "PlaybackStatus", "Rate", "Metadata", "MinimumRate", "MaximumRate",
  "CanGoNext", "CanGoPrevious", "CanPlay", "CanPause", "CanSeek",
];

export function signalledValues(player) {
  return Object.fromEntries(SIGNALLED.map(name => [name, propertyValue(PLAYER_INTERFACE, name, player)]));
}

/** Names whose value differs between two signalledValues(). */
export function changedProperties(before, after) {
  return SIGNALLED.filter(name => !before || before[name]?.[1] !== after[name]?.[1]);
}

/** The PropertiesChanged body, type (sa{sv}as). */
export function propertiesChangedText(names, values) {
  const changed = names.map(name => `${gvString(name)}: ${gvVariant(values[name][0], values[name][1])}`);
  return `(${gvString(PLAYER_INTERFACE)}, {${changed.join(", ")}}, [])`;
}

/** The Seeked body, type (x). */
export function seekedText(positionUs) {
  return `(${gvInt64(positionUs)},)`;
}

/* ------------------------------------------------------------ methods */

/**
 * What a Player method call asks the app to do, or null when there is
 * nothing to do (not supported now, a stale SetPosition, …).
 * Actions: play, pause, stop, next, previous, seek {seconds}.
 */
export function playerAction(method, args, player, now = Date.now()) {
  const caps = player?.caps || {};
  const lengthUs = player?.metadata?.lengthUs;
  const seekTo = (targetUs, { pastEnd }) => {
    if (!caps.canSeek) {
      return null;
    }
    if (Number.isFinite(lengthUs) && targetUs > lengthUs) {
      // Seek past the end acts like Next; SetPosition past the end does nothing.
      return pastEnd === "next" && caps.canGoNext ? { type: "next" } : null;
    }
    return { type: "seek", seconds: Math.max(0, targetUs) / 1e6 };
  };
  switch (method) {
    case "Play":
      return caps.canPlay ? { type: "play" } : null;
    case "Pause":
      return caps.canPause ? { type: "pause" } : null;
    case "PlayPause":
      if (player?.status === "Playing") {
        return caps.canPause ? { type: "pause" } : null;
      }
      return caps.canPlay ? { type: "play" } : null;
    case "Stop":
      return player?.status && player.status !== "Stopped" ? { type: "stop" } : null;
    case "Next":
      return caps.canGoNext ? { type: "next" } : null;
    case "Previous":
      return caps.canGoPrevious ? { type: "previous" } : null;
    case "Seek": {
      const offset = Number(args?.offset);
      if (!Number.isFinite(offset)) {
        return null;
      }
      const current = positionSeconds(player?.position, now, player?.status === "Playing") * 1e6;
      return seekTo(current + offset, { pastEnd: "next" });
    }
    case "SetPosition": {
      const position = Number(args?.position);
      if (args?.trackId !== player?.metadata?.trackId || !Number.isFinite(position) || position < 0) {
        return null;
      }
      return seekTo(position, { pastEnd: "ignore" });
    }
    default:
      return null;
  }
}
