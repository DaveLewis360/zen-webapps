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
  const seed = rgbToOklch(hexToRgb(seedHex));
  const hue = Number.isFinite(seed.h) ? seed.h : 280;
  const primaryChroma = Math.min(Math.max(seed.c, 0.05), 0.17);
  const pal = {
    p: t => toneColor(hue, primaryChroma, t),
    s: t => toneColor(hue, primaryChroma * 0.36, t),
    t: t => toneColor(hue + 60, primaryChroma * 0.62, t),
    e: t => toneColor(25, 0.19, t),
    n: t => toneColor(hue, Math.min(seed.c * 0.14, 0.018), t),
    v: t => toneColor(hue, Math.min(seed.c * 0.28, 0.034), t),
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
  return { dark, source: "seed", seed: seedHex, roles };
}

async function readScheme(paths) {
  for (const path of paths || []) {
    try {
      const data = await IOUtils.readJSON(path);
      const colours = data.colours || data.colors || data;
      if (!colours || typeof colours.primary !== "string") {
        continue;
      }
      const roles = {};
      for (const name of ROLE_NAMES) {
        const value = colours[name];
        if (typeof value === "string" && /^#?[0-9a-fA-F]{6}$/.test(value)) {
          roles[name] = value.startsWith("#") ? value.toLowerCase() : "#" + value.toLowerCase();
        }
      }
      if (!roles.surface || !roles.onSurface) {
        continue;
      }
      const mode = typeof data.mode === "string" ? data.mode : null;
      return { roles, dark: mode ? mode === "dark" : true, path };
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
 */
export async function resolvePalette(settings, accent, schemePaths, systemDark) {
  const wantDark =
    settings.colorScheme === "dark" ? true : settings.colorScheme === "light" ? false : systemDark;
  if (settings.palette === "system") {
    const scheme = await readScheme(schemePaths);
    if (scheme && scheme.dark === wantDark) {
      const derived = derivePalette(scheme.roles.primary || accent, wantDark);
      return { dark: wantDark, source: "system", seed: scheme.roles.primary, roles: { ...derived.roles, ...scheme.roles } };
    }
    if (scheme?.roles.primary) {
      return { ...derivePalette(scheme.roles.primary, wantDark), source: "system-seed" };
    }
  }
  return derivePalette(accent, wantDark);
}

/** CSS custom properties (kebab-case --zenapp-* names) for a palette. */
export function paletteToCSSVariables(palette) {
  const variables = {};
  for (const [name, value] of Object.entries(palette.roles)) {
    variables[`--zenapp-${name.replace(/[A-Z]/g, c => "-" + c.toLowerCase())}`] = value;
  }
  return variables;
}
