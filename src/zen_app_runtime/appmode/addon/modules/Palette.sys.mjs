/* zen-app-runtime app mode: Material 3 colour roles.
 *
 * "system" uses the wallpaper scheme written by Caelestia/matugen-style
 * generators when available; otherwise (or for "app") a tonal palette is
 * derived from a seed colour in OKLCH, mapping M3 tones to perceptual
 * lightness (tone = CIE L*).
 */

const ROLE_NAMES = [
  "primary", "onPrimary", "primaryContainer", "onPrimaryContainer",
  "secondary", "onSecondary", "secondaryContainer", "onSecondaryContainer",
  "tertiary", "onTertiary", "tertiaryContainer", "onTertiaryContainer",
  "error", "onError", "errorContainer", "onErrorContainer",
  "surface", "surfaceDim", "surfaceBright", "surfaceContainerLowest",
  "surfaceContainerLow", "surfaceContainer", "surfaceContainerHigh",
  "surfaceContainerHighest", "onSurface", "onSurfaceVariant", "outline",
  "outlineVariant", "inverseSurface", "inverseOnSurface", "inversePrimary",
];

function srgbToLinear(value) {
  return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
}

function linearToSrgb(value) {
  return value <= 0.0031308 ? 12.92 * value : 1.055 * value ** (1 / 2.4) - 0.055;
}

function hexToRgb(hex) {
  const clean = hex.replace(/^#/, "");
  return [0, 2, 4].map(i => parseInt(clean.slice(i, i + 2), 16) / 255);
}

function rgbToHex(rgb) {
  return (
    "#" +
    rgb
      .map(v => Math.round(Math.min(1, Math.max(0, v)) * 255).toString(16).padStart(2, "0"))
      .join("")
  );
}

function rgbToOklch([r, g, b]) {
  const [lr, lg, lb] = [r, g, b].map(srgbToLinear);
  const l = Math.cbrt(0.4122214708 * lr + 0.5363325363 * lg + 0.0514459929 * lb);
  const m = Math.cbrt(0.2119034982 * lr + 0.6806995451 * lg + 0.1073969566 * lb);
  const s = Math.cbrt(0.0883024619 * lr + 0.2817188376 * lg + 0.6299787005 * lb);
  const L = 0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s;
  const A = 1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s;
  const B = 0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s;
  return { l: L, c: Math.hypot(A, B), h: (Math.atan2(B, A) * 180) / Math.PI };
}

function oklchToLinear(L, C, H) {
  const hr = (H * Math.PI) / 180;
  const A = C * Math.cos(hr);
  const B = C * Math.sin(hr);
  const l = (L + 0.3963377774 * A + 0.2158037573 * B) ** 3;
  const m = (L - 0.1055613458 * A - 0.0638541728 * B) ** 3;
  const s = (L - 0.0894841775 * A - 1.291485548 * B) ** 3;
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ];
}

function toneToOkL(tone) {
  const y = tone > 8 ? ((tone + 16) / 116) ** 3 : tone / 903.2963;
  return Math.cbrt(y);
}

/** Colour at an M3 tone with the requested hue/chroma, chroma-clipped into sRGB. */
export function toneColor(hue, chroma, tone) {
  if (tone >= 100) {
    return "#ffffff";
  }
  if (tone <= 0) {
    return "#000000";
  }
  const L = toneToOkL(tone);
  let low = 0;
  let high = chroma;
  let best = oklchToLinear(L, 0, hue);
  for (let i = 0; i < 18; i++) {
    const mid = (low + high) / 2;
    const rgb = oklchToLinear(L, mid, hue);
    if (rgb.every(v => v >= -0.0005 && v <= 1.0005)) {
      best = rgb;
      low = mid;
    } else {
      high = mid;
    }
  }
  const inGamut = oklchToLinear(L, chroma, hue);
  if (inGamut.every(v => v >= 0 && v <= 1)) {
    best = inGamut;
  }
  return rgbToHex(best.map(linearToSrgb));
}

export function derivePalette(seedHex, dark) {
  return deriveFromKeys({ primary: seedHex }, dark);
}

function hueChroma(hex) {
  if (typeof hex !== "string" || !/^#?[0-9a-f]{6}$/i.test(hex)) {
    return null;
  }
  const { c, h } = rgbToOklch(hexToRgb(hex.startsWith("#") ? hex : "#" + hex));
  return { c, h: Number.isFinite(h) ? h : 280 };
}

/**
 * M3 roles from key colours ({primary, secondary, tertiary, neutral,
 * neutralVariant, error}; only primary is required). With a scheme's own
 * key colours this is that scheme in the other brightness: the wallpaper's
 * hues and chroma in dark (or light) tones.
 */
export function deriveFromKeys(keys, dark) {
  const seed = hueChroma(keys.primary) || { h: 280, c: 0.12 };
  const hue = seed.h;
  const primaryChroma = Math.min(Math.max(seed.c, 0.05), 0.17);
  const own = (key, fallbackHue, fallbackChroma, cap) => {
    const found = hueChroma(keys[key]);
    return found ? { h: found.h, c: Math.min(found.c, cap) } : { h: fallbackHue, c: fallbackChroma };
  };
  const secondary = own("secondary", hue, primaryChroma * 0.36, 0.12);
  const tertiary = own("tertiary", hue + 60, primaryChroma * 0.62, 0.14);
  const neutral = own("neutral", hue, Math.min(seed.c * 0.14, 0.018), 0.03);
  const variant = own("neutralVariant", hue, Math.min(seed.c * 0.28, 0.034), 0.05);
  const error = own("error", 25, 0.19, 0.2);
  const tone = family => t => toneColor(family.h, family.c, t);
  const pal = {
    p: tone({ h: hue, c: primaryChroma }),
    s: tone(secondary),
    t: tone(tertiary),
    e: tone(error),
    n: tone(neutral),
    v: tone(variant),
  };
  const roles = dark
    ? {
        primary: pal.p(80), onPrimary: pal.p(20), primaryContainer: pal.p(30), onPrimaryContainer: pal.p(90),
        secondary: pal.s(80), onSecondary: pal.s(20), secondaryContainer: pal.s(30), onSecondaryContainer: pal.s(90),
        tertiary: pal.t(80), onTertiary: pal.t(20), tertiaryContainer: pal.t(30), onTertiaryContainer: pal.t(90),
        error: pal.e(80), onError: pal.e(20), errorContainer: pal.e(30), onErrorContainer: pal.e(90),
        surface: pal.n(6), surfaceDim: pal.n(6), surfaceBright: pal.n(24), surfaceContainerLowest: pal.n(4),
        surfaceContainerLow: pal.n(10), surfaceContainer: pal.n(12), surfaceContainerHigh: pal.n(17),
        surfaceContainerHighest: pal.n(22), onSurface: pal.n(90), onSurfaceVariant: pal.v(80),
        outline: pal.v(60), outlineVariant: pal.v(30), inverseSurface: pal.n(90), inverseOnSurface: pal.n(20),
        inversePrimary: pal.p(40),
      }
    : {
        primary: pal.p(40), onPrimary: pal.p(100), primaryContainer: pal.p(90), onPrimaryContainer: pal.p(10),
        secondary: pal.s(40), onSecondary: pal.s(100), secondaryContainer: pal.s(90), onSecondaryContainer: pal.s(10),
        tertiary: pal.t(40), onTertiary: pal.t(100), tertiaryContainer: pal.t(90), onTertiaryContainer: pal.t(10),
        error: pal.e(40), onError: pal.e(100), errorContainer: pal.e(90), onErrorContainer: pal.e(10),
        surface: pal.n(98), surfaceDim: pal.n(87), surfaceBright: pal.n(98), surfaceContainerLowest: pal.n(100),
        surfaceContainerLow: pal.n(96), surfaceContainer: pal.n(94), surfaceContainerHigh: pal.n(92),
        surfaceContainerHighest: pal.n(90), onSurface: pal.n(10), onSurfaceVariant: pal.v(30),
        outline: pal.v(50), outlineVariant: pal.v(80), inverseSurface: pal.n(20), inverseOnSurface: pal.n(95),
        inversePrimary: pal.p(80),
      };
  return { dark, source: "seed", seed: keys.primary, roles };
}

/** The scheme's key colours (Caelestia writes both spellings), or null. */
function keyColors(all) {
  const pick = name => all[`${name}PaletteKeyColor`] || all[`${name.replace(/[A-Z]/g, c => "_" + c.toLowerCase())}_paletteKeyColor`];
  const keys = {
    primary: pick("primary"),
    secondary: pick("secondary"),
    tertiary: pick("tertiary"),
    neutral: pick("neutral"),
    neutralVariant: pick("neutralVariant"),
    error: pick("error"),
  };
  return keys.primary ? keys : null;
}

async function readScheme(paths) {
  for (const path of paths || []) {
    try {
      const data = await IOUtils.readJSON(path);
      const colours = data.colours || data.colors || data;
      if (!colours || typeof colours.primary !== "string") {
        continue;
      }
      // Every colour of the scheme (Caelestia also writes background,
      // success, terminal colours, ...), for themes that use more than the
      // M3 roles below.
      const all = {};
      for (const [name, value] of Object.entries(colours)) {
        if (/^[A-Za-z][\w-]{0,40}$/.test(name) && typeof value === "string" && /^#?[0-9a-fA-F]{6}$/.test(value)) {
          all[name] = value.startsWith("#") ? value.toLowerCase() : "#" + value.toLowerCase();
        }
      }
      const roles = {};
      for (const name of ROLE_NAMES) {
        if (all[name]) {
          roles[name] = all[name];
        }
      }
      if (!roles.surface || !roles.onSurface) {
        continue;
      }
      const mode = typeof data.mode === "string" ? data.mode : null;
      return { roles, all, keys: keyColors(all), dark: mode ? mode === "dark" : true, path };
    } catch {
      // missing or unreadable scheme; try the next candidate
    }
  }
  return null;
}

/**
 * Resolve the palette for the given settings.
 * @param {object} settings  app settings (palette, colorScheme)
 * @param {string} accent    app accent (#rrggbb)
 * @param {string[]} schemePaths candidate wallpaper scheme files
 * @param {boolean} systemDark whether the desktop currently prefers dark
 *
 * With the wallpaper scheme ("system" palette) and the "system" colour
 * scheme, dark or light is the scheme's own mode, as in the Caelestia shell
 * (the desktop's GTK setting only follows it a moment later). A mode the
 * scheme is not in (an explicit dark/light choice, or a dark-only page theme)
 * gets the scheme's hues in that mode's tones.
 */
export async function resolvePalette(settings, accent, schemePaths, systemDark) {
  const scheme = settings.palette === "system" ? await readScheme(schemePaths) : null;
  const wantDark =
    settings.colorScheme === "dark" ? true : settings.colorScheme === "light" ? false : scheme ? scheme.dark : systemDark;
  if (scheme && scheme.dark === wantDark) {
    const derived = deriveFromKeys(scheme.keys || { primary: scheme.roles.primary || accent }, wantDark);
    return {
      dark: wantDark,
      source: "system",
      seed: scheme.roles.primary,
      roles: { ...derived.roles, ...scheme.roles },
      scheme: scheme.all,
    };
  }
  if (scheme) {
    return { ...deriveFromKeys(scheme.keys || { primary: scheme.roles.primary }, wantDark), source: "system-seed" };
  }
  return derivePalette(accent, wantDark);
}

/** Caelestia's success colours, which do not follow the wallpaper. */
const SUCCESS = {
  dark: { success: "#b5ccba", onSuccess: "#213528", successContainer: "#374b3e", onSuccessContainer: "#d1e9d6" },
  light: { success: "#4f6354", onSuccess: "#ffffff", successContainer: "#d1e8d5", onSuccessContainer: "#0c1f13" },
};

/**
 * Colours for a Caelestia colour block (--primary, --onSurface, ...): the
 * whole wallpaper scheme when the palette comes from it, else the M3 roles
 * plus the few non-M3 names such themes also use.
 */
export function paletteColors(palette) {
  if (!palette?.roles) {
    return null;
  }
  const roles = palette.roles;
  const colors = { ...(palette.source === "system" ? palette.scheme || {} : {}), ...roles };
  colors.background ??= roles.surface;
  colors.onBackground ??= roles.onSurface;
  colors.surfaceVariant ??= roles.surfaceContainerHighest;
  colors.surfaceTint ??= roles.primary;
  colors.primaryDim ??= roles.primary;
  colors.shadow ??= "#000000";
  colors.scrim ??= "#000000";
  for (const [name, value] of Object.entries(SUCCESS[palette.dark === false ? "light" : "dark"])) {
    colors[name] ??= value;
  }
  for (const [name, value] of Object.entries(colors)) {
    colors[name] = String(value).toLowerCase();
  }
  return colors;
}

/** CSS custom properties (kebab-case --zenapp-* names) for a palette. */
export function paletteToCSSVariables(palette) {
  const variables = {};
  for (const [name, value] of Object.entries(palette.roles)) {
    variables[`--zenapp-${name.replace(/[A-Z]/g, c => "-" + c.toLowerCase())}`] = value;
  }
  return variables;
}
