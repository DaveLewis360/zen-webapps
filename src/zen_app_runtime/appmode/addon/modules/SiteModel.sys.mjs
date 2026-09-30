/* zen-app-runtime app mode: suggestions for a new app from its website.
 *
 * Pure functions over plain data: the Web Apps wizard downloads the page
 * (Manager.sys.mjs), parses it inertly (DOMParser, in the page) into "facts"
 * and gets its suggestions here: a name, the icons worth trying, the sign-in
 * services the site uses, and whether the whole domain belongs to the app.
 */

/** Sign-in services an app may need to keep inside its window. */
export const SIGN_IN_PROVIDERS = [
  {
    key: "google",
    label: "Google",
    hosts: ["accounts.google.com", "accounts.youtube.com"],
    markers: ["accounts.google.com", "apis.google.com/js/platform", "gsi/client", "g_id_onload", "google-signin"],
  },
  {
    key: "microsoft",
    label: "Microsoft",
    hosts: ["login.microsoftonline.com", "login.live.com", "account.live.com"],
    markers: ["login.microsoftonline.com", "login.live.com", "alcdn.msauth.net"],
  },
  {
    key: "apple",
    label: "Apple",
    hosts: ["appleid.apple.com", "idmsa.apple.com"],
    markers: ["appleid.apple.com", "appleid.cdn-apple.com", "appleid.auth.js"],
  },
  {
    key: "facebook",
    label: "Facebook",
    hosts: ["www.facebook.com", "m.facebook.com"],
    markers: ["connect.facebook.net", "facebook.com/dialog/oauth", "fb-login-button"],
  },
  {
    key: "github",
    label: "GitHub",
    hosts: ["github.com"],
    markers: ["github.com/login/oauth"],
  },
];

const MAX_NAME = 80;

/** An address typed by the user, as https:// (or null). */
export function normalizeAddress(text) {
  let value = String(text ?? "").trim();
  if (!value) {
    return null;
  }
  if (/^http:\/\//i.test(value)) {
    value = "https://" + value.slice(7);
  } else if (!/^[a-z][a-z0-9+.-]*:/i.test(value)) {
    value = "https://" + value;
  }
  let url;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || !url.hostname || url.username || url.password || !url.hostname.includes(".")) {
    return null;
  }
  return url.href;
}

function hostLabels(host) {
  const labels = String(host || "").toLowerCase().split(".").filter(Boolean);
  return labels.length > 1 ? labels.slice(0, -1) : labels;
}

const GENERIC_LABELS = new Set(["www", "app", "apps", "web", "open", "m", "mobile", "my", "client", "portal", "go", "beta", "home", "secure"]);

/** The name the CLI gives a site ("app.slack.com" → "Slack", "my-site.dev" → "My Site"). */
export function nameFromHost(host) {
  const labels = hostLabels(String(host || "").split(":")[0]);
  const label = labels.find(value => !GENERIC_LABELS.has(value)) ?? labels[0] ?? "";
  return label
    .split(/[-_]+/)
    .filter(Boolean)
    .map(part => part[0].toUpperCase() + part.slice(1))
    .join(" ");
}

/** The part of a page title that names the site ("Slack | Home" → "Slack"). */
export function cleanTitle(title, host) {
  const text = String(title ?? "").replace(/\s+/g, " ").trim();
  if (!text) {
    return "";
  }
  const parts = text
    .split(/\s+[|·•—–-]\s+|:\s+/)
    .map(part => part.replace(/^\(\d+\)\s*/, "").trim())
    .filter(part => part.length >= 2);
  if (!parts.length) {
    return text.slice(0, MAX_NAME);
  }
  const labels = hostLabels(host).filter(label => !GENERIC_LABELS.has(label));
  const named = parts.find(part => labels.some(label => part.toLowerCase().replace(/[^a-z0-9]/g, "").includes(label.replace(/[^a-z0-9]/g, ""))));
  if (named) {
    return named.slice(0, MAX_NAME);
  }
  const last = parts[parts.length - 1];
  return (parts.length > 1 && last.length <= 24 ? last : parts[0]).slice(0, MAX_NAME);
}

/**
 * An app id from its name ("Proton Mail" → "proton-mail", "Példa" → "pelda"),
 * or "" when nothing of it is ASCII (the CLI then derives one from the host).
 */
export function idFromName(name) {
  const slug = String(name ?? "")
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+/, "")
    .slice(0, 64)
    .replace(/-+$/, "");
  return slug;
}

/** The app's name: the web manifest's, else the page's own names, else its title, else its host. */
export function suggestName({ manifest, meta = {}, title = "", host = "" } = {}) {
  const candidates = [
    typeof manifest?.short_name === "string" && manifest.short_name.trim().length <= 24 ? manifest.short_name : "",
    typeof manifest?.name === "string" && manifest.name.trim().length <= 40 ? manifest.name : "",
    meta["application-name"],
    meta["apple-mobile-web-app-title"],
    meta["og:site_name"],
    cleanTitle(title, host),
    nameFromHost(host),
  ];
  for (const candidate of candidates) {
    const text = String(candidate ?? "").replace(/[\u0000-\u001f\u007f]+/g, " ").replace(/\s+/g, " ").trim();
    if (text) {
      return text.slice(0, MAX_NAME);
    }
  }
  return "";
}

function iconSize(sizes) {
  let best = 0;
  for (const token of String(sizes || "").toLowerCase().split(/\s+/)) {
    if (token === "any") {
      return Infinity;
    }
    const match = /^(\d+)x(\d+)$/.exec(token);
    if (match) {
      best = Math.max(best, Math.min(Number(match[1]), Number(match[2])));
    }
  }
  return best;
}

function isSvg(src, type) {
  return /svg/i.test(type || "") || /\.svg(?:[?#]|$)/i.test(src) || /^data:image\/svg\+xml/i.test(src);
}

/**
 * Icons worth trying, best first: scalable or big "any" icons from the web
 * manifest, SVG and sized page icons, the Apple touch icon, the favicon.
 * `links` are the page's <link> elements ({rel: [...], href, sizes, type}),
 * already resolved to absolute URLs.
 */
export function iconCandidates({ manifest, manifestUrl, links = [], pageUrl } = {}, limit = 6) {
  const found = new Map();
  const add = (src, size, bonus, source) => {
    if (typeof src !== "string" || !/^(?:https:\/\/|data:image\/)/i.test(src) || src.length > 1_000_000) {
      return;
    }
    // Tiny icons look blurry when blown up; they are a last resort.
    const score = (size === Infinity ? 1000 : Math.min(size, 512)) + bonus - (size && size < 64 ? 400 : 0);
    const previous = found.get(src);
    if (!previous || previous.score < score) {
      found.set(src, { src, size, score, source });
    }
  };
  for (const icon of Array.isArray(manifest?.icons) ? manifest.icons : []) {
    if (typeof icon?.src !== "string") {
      continue;
    }
    const purposes = String(icon.purpose || "any").toLowerCase().split(/\s+/);
    let src;
    try {
      src = new URL(icon.src, manifestUrl || pageUrl).href;
    } catch {
      continue;
    }
    const size = isSvg(src, icon.type) ? Infinity : iconSize(icon.sizes);
    // Maskable-only icons carry a solid safe-zone background: a plain square.
    add(src, size, purposes.includes("any") ? 100 : -400, "manifest");
  }
  for (const link of links) {
    const rel = (link.rel || []).map(value => String(value).toLowerCase());
    if (!link.href) {
      continue;
    }
    if (rel.includes("icon") || rel.includes("shortcut")) {
      const size = isSvg(link.href, link.type) ? Infinity : iconSize(link.sizes);
      add(link.href, size, size === Infinity ? 80 : 50, "icon");
    } else if (rel.includes("apple-touch-icon") || rel.includes("apple-touch-icon-precomposed")) {
      add(link.href, iconSize(link.sizes) || 180, 40, "apple");
    } else if (rel.includes("mask-icon")) {
      // Monochrome Safari pinned-tab icons: not the app's colours.
      continue;
    }
  }
  if (pageUrl) {
    try {
      add(new URL("/favicon.ico", pageUrl).href, 32, 0, "favicon");
    } catch {
      // ignore
    }
  }
  return [...found.values()].sort((a, b) => b.score - a.score).slice(0, limit);
}

/** Sign-in services the page refers to (its HTML, lowercased markers). */
export function detectSignIns(html) {
  const text = String(html ?? "").toLowerCase();
  return SIGN_IN_PROVIDERS.filter(provider => provider.markers.some(marker => text.includes(marker))).map(provider => provider.key);
}

/** Whether `host` matches a scope pattern ("*.x.com" or an exact host). */
export function hostMatches(host, pattern) {
  const value = String(host || "").toLowerCase().replace(/\.$/, "");
  if (pattern.startsWith("*.")) {
    const suffix = pattern.slice(2);
    return value === suffix || value.endsWith("." + suffix);
  }
  return value === pattern;
}

const HOST_LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

/** Whether the CLI takes `value` as a host pattern (lowercase "x.com" or "*.x.com"). */
export function isHostPattern(value) {
  const text = String(value ?? "");
  const host = text.startsWith("*.") ? text.slice(2) : text;
  const labels = host.split(".");
  return (
    !!host &&
    host.length <= 253 &&
    host === host.toLowerCase() &&
    (labels.length >= 2 || host === "localhost") &&
    labels.every(label => HOST_LABEL.test(label))
  );
}

/**
 * The scope for a site: the whole base domain as an option (on by default
 * when the start page moved to another subdomain of it), and hosts outside it
 * that the start page redirected to (they must stay in the app too).
 */
export function scopeSuggestion({ startHost, finalHost, baseDomain }) {
  const start = String(startHost || "").toLowerCase();
  const final = String(finalHost || start).toLowerCase();
  const base = String(baseDomain || "").toLowerCase();
  const wholeDomain = base ? `*.${base}` : null;
  const sameBase = base && (final === base || final.endsWith("." + base));
  return {
    wholeDomain,
    wholeDomainDefault: !!(wholeDomain && final !== start && sameBase),
    extraHosts: final && final !== start && !sameBase ? [final] : [],
  };
}

/** Sign-in hosts for the chosen providers, minus what the app's scope already covers. */
export function signInHosts(keys, scope = []) {
  const hosts = [];
  for (const provider of SIGN_IN_PROVIDERS) {
    if (!keys.includes(provider.key)) {
      continue;
    }
    for (const host of provider.hosts) {
      if (!scope.some(pattern => hostMatches(host, pattern)) && !hosts.includes(host)) {
        hosts.push(host);
      }
    }
  }
  return hosts;
}

/** #rrggbb from #rgb / #rrggbb / rgb(a)() text; null otherwise (or when transparent). */
export function hexColor(value) {
  const text = String(value ?? "").trim().toLowerCase();
  let match = /^#([0-9a-f]{3})$/.exec(text);
  if (match) {
    return "#" + [...match[1]].map(c => c + c).join("");
  }
  match = /^#([0-9a-f]{6})(?:[0-9a-f]{2})?$/.exec(text);
  if (match) {
    return "#" + match[1];
  }
  match = /^rgba?\(\s*(\d{1,3})[\s,]+(\d{1,3})[\s,]+(\d{1,3})(?:[\s,/]+([\d.]+%?))?\s*\)$/.exec(text);
  if (match) {
    const alpha = match[4] === undefined ? 1 : match[4].endsWith("%") ? parseFloat(match[4]) / 100 : parseFloat(match[4]);
    if (!(alpha > 0)) {
      return null;
    }
    const channels = match.slice(1, 4).map(Number);
    if (channels.some(c => c > 255)) {
      return null;
    }
    return "#" + channels.map(c => c.toString(16).padStart(2, "0")).join("");
  }
  return null;
}

/** Whether a colour works as an accent (not grey, not near white or black). */
export function isColorful(value) {
  const hex = hexColor(value);
  if (!hex) {
    return false;
  }
  const [r, g, b] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255);
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const lightness = (max + min) / 2;
  const saturation = max === min ? 0 : (max - min) / (1 - Math.abs(2 * lightness - 1));
  return saturation >= 0.25 && lightness >= 0.15 && lightness <= 0.85;
}

/** Colour choices without near-duplicates; accent-worthy ones first, in their order. */
export function colorChoices(values, fallback = "#7b6cff", limit = 5) {
  const out = [];
  const distance = (a, b) => {
    const rgb = hex => [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16));
    const [x, y] = [rgb(a), rgb(b)];
    return Math.hypot(x[0] - y[0], x[1] - y[1], x[2] - y[2]);
  };
  for (const value of [...values, fallback]) {
    const hex = hexColor(value);
    if (hex && !out.some(existing => distance(existing, hex) < 24)) {
      out.push(hex);
    }
  }
  const colorful = out.filter(isColorful);
  return [...colorful, ...out.filter(hex => !colorful.includes(hex))].slice(0, limit);
}
