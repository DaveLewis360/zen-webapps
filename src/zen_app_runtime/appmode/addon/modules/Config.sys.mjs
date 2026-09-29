/* zen-app-runtime app mode: runtime configuration and definition persistence.
 *
 * zen-app/app.json is rendered by the Python launcher for every start. The app
 * definition file it points to is the single source of truth; this module may
 * only rewrite its "settings" and "onboarding" members, validated with the
 * same rules as zen_app_runtime.appmode.definition.
 */

export const SETTINGS_ENUMS = {
  appearance: ["transparent", "opaque"],
  glass: ["auto", "ghostty", "vesktop", "zen", "custom"],
  palette: ["system", "app"],
  colorScheme: ["system", "dark", "light"],
  windowChrome: ["auto", "none", "titlebar"],
};
export const BOOLEAN_SETTINGS = ["notifications", "media", "autoplay", "drm", "findInPage", "developer", "zenInternet"];
const FEATURE_KEY = /^[^|\u0000-\u001f\u007f]{1,120}\|[^\u0000-\u001f\u007f]{1,120}$/;
export const TINT_RANGE = [0, 90];
export const ONBOARDING_VERSION = 1;

const SETTINGS_BLOCK_BEGIN = "// >>> zen-app settings (managed; rewritten by the app)";
const SETTINGS_BLOCK_END = "// <<< zen-app settings";

export function sanitizeSettings(candidate, base) {
  const result = { ...base };
  for (const [key, allowed] of Object.entries(SETTINGS_ENUMS)) {
    if (key in candidate && allowed.includes(candidate[key])) {
      result[key] = candidate[key];
    }
  }
  for (const key of BOOLEAN_SETTINGS) {
    if (typeof candidate[key] === "boolean") {
      result[key] = candidate[key];
    }
  }
  if (Number.isInteger(candidate.tint)) {
    result.tint = Math.min(TINT_RANGE[1], Math.max(TINT_RANGE[0], candidate.tint));
  }
  if (Array.isArray(candidate.disabledFeatures)) {
    result.disabledFeatures = [...new Set(candidate.disabledFeatures.filter(k => typeof k === "string" && FEATURE_KEY.test(k)))].slice(0, 400);
  }
  return result;
}

function renderPrefValue(value) {
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (Number.isInteger(value)) {
    return String(value);
  }
  return JSON.stringify(String(value));
}

export function resolvedWindowChrome(settings, config) {
  return settings.windowChrome === "auto" ? config.windowChromeAuto || "none" : settings.windowChrome;
}

/** Pref values derived from settings, via the table rendered by Python. */
export function settingsToPrefs(settings, config) {
  const table = config.settingsPrefs || {};
  const prefs = {};
  for (const [setting, byValue] of Object.entries(table)) {
    let value = settings[setting];
    if (setting === "windowChrome") {
      value = resolvedWindowChrome(settings, config);
    }
    const entry = byValue[String(value)];
    if (entry) {
      Object.assign(prefs, entry);
    }
  }
  return prefs;
}

export function applyPrefs(prefs) {
  for (const [name, value] of Object.entries(prefs)) {
    try {
      if (typeof value === "boolean") {
        Services.prefs.setBoolPref(name, value);
      } else if (Number.isInteger(value)) {
        Services.prefs.setIntPref(name, value);
      } else {
        Services.prefs.setStringPref(name, String(value));
      }
    } catch (error) {
      console.error(`zen-app: could not set ${name}`, error);
    }
  }
}

export class ConfigStore {
  constructor(config, definition) {
    this.config = config;
    this.definition = definition;
  }

  static get directory() {
    return PathUtils.join(PathUtils.profileDir, "zen-app");
  }

  static async load() {
    const config = await IOUtils.readJSON(PathUtils.join(ConfigStore.directory, "app.json"));
    let definition = config.definition;
    try {
      const fromLibrary = await IOUtils.readJSON(config.definitionPath);
      if (fromLibrary?.id === config.appId && fromLibrary?.schema === "zen-app/1") {
        definition = fromLibrary;
      }
    } catch (error) {
      console.warn("zen-app: using the launch snapshot of the definition", error);
    }
    definition.settings = sanitizeSettings(definition.settings || {}, config.definition.settings || {});
    return new ConfigStore(config, definition);
  }

  get settings() {
    return this.definition.settings;
  }

  get needsOnboarding() {
    const onboarding = this.definition.onboarding || {};
    return !onboarding.completed || (onboarding.version || 0) < ONBOARDING_VERSION;
  }

  async #writeDefinition(mutate) {
    let current;
    try {
      current = await IOUtils.readJSON(this.config.definitionPath);
    } catch {
      current = structuredClone(this.definition);
    }
    if (current.id !== this.config.appId || current.schema !== "zen-app/1") {
      throw new Error("the app definition on disk no longer matches this app");
    }
    mutate(current);
    const text = JSON.stringify(current, null, 2) + "\n";
    await IOUtils.writeUTF8(this.config.definitionPath, text, {
      tmpPath: this.config.definitionPath + ".tmp",
      flush: true,
    });
    await IOUtils.setPermissions(this.config.definitionPath, 0o600);
    this.definition = current;
  }

  async updateSettings(patch) {
    const next = sanitizeSettings(patch, this.settings);
    await this.#writeDefinition(data => {
      data.settings = next;
    });
    await this.writeSettingsBlock();
    return next;
  }

  async setOnboardingCompleted(completed) {
    await this.#writeDefinition(data => {
      data.onboarding = { completed, version: ONBOARDING_VERSION };
    });
  }

  /** Keep user.js in sync so in-app restarts start with the current settings. */
  async writeSettingsBlock() {
    const userJs = PathUtils.join(PathUtils.profileDir, "user.js");
    let text;
    try {
      text = await IOUtils.readUTF8(userJs);
    } catch {
      return;
    }
    const begin = text.indexOf(SETTINGS_BLOCK_BEGIN);
    const end = text.indexOf(SETTINGS_BLOCK_END);
    if (begin < 0 || end < begin) {
      return;
    }
    const prefs = settingsToPrefs(this.settings, this.config);
    const lines = Object.keys(prefs)
      .sort()
      .map(name => `user_pref(${JSON.stringify(name)}, ${renderPrefValue(prefs[name])});`);
    const block = [SETTINGS_BLOCK_BEGIN, ...lines, ""].join("\n");
    const updated = text.slice(0, begin) + block + text.slice(end);
    if (updated !== text) {
      await IOUtils.writeUTF8(userJs, updated, { tmpPath: userJs + ".tmp" });
    }
  }

  async writeStatus(status) {
    const path = PathUtils.join(ConfigStore.directory, "status.json");
    await IOUtils.writeUTF8(path, JSON.stringify(status, null, 2) + "\n", { tmpPath: path + ".tmp" });
  }

  async takePendingAction() {
    const path = PathUtils.join(ConfigStore.directory, "pending.json");
    try {
      const pending = await IOUtils.readJSON(path);
      await IOUtils.remove(path);
      return pending;
    } catch {
      return null;
    }
  }
}
