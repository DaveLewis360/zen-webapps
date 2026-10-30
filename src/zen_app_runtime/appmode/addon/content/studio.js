/* zen-app-runtime app mode — onboarding & settings studio (privileged page). */
/* global ChromeUtils, ZenShapes, ZenUI */
"use strict";

const { ZenApp } = ChromeUtils.importESModule("resource://zenapp/modules/ZenApp.sys.mjs");
const { t } = ChromeUtils.importESModule("resource://zenapp/modules/Strings.sys.mjs");
const { h, icon, svg, bind, detectSymbols, snackbar, segmented, toggle, listToggle, wavy, row } = ZenUI;

const MODE = new URLSearchParams(location.search).get("mode") === "settings" ? "settings" : "onboarding";
let model = ZenApp.getStudioModel();

/* ------------------------------------------------------------ helpers */

function refresh() {
  model = ZenApp.getStudioModel();
  applyTheme();
  ZenUI.runUpdaters();
}

function iconURL() {
  return ZenUI.fileURL(model.icon?.file);
}

function appIcon(className) {
  const url = iconURL();
  return url ? h("img", { class: className || "", src: url, alt: "" }) : null;
}

function applyTheme() {
  document.documentElement.dataset.mode = MODE;
  ZenUI.applyPalette(model);
  document.title = MODE === "settings" ? `${model.name} — ${t("settings.title")}` : model.name;
}

async function setSettings(patch, { restartHint = false, reloadHint = false } = {}) {
  try {
    await ZenApp.updateSettings(patch);
  } catch (error) {
    console.error(error);
    snackbar(String(error?.message || error));
    return;
  }
  refresh();
  if (MODE === "settings") {
    if (restartHint) {
      snackbar(t("settings.restartNeeded"), { label: t("settings.restart"), run: () => ZenApp.restart() });
    } else if (reloadHint) {
      snackbar(t("settings.reloadNeeded"), { label: t("settings.reload"), run: () => ZenApp.reloadApp() });
    } else {
      snackbar(t("settings.saved"));
    }
  }
}

/* ----------------------------------------------------------- controls */

function slider({ min, max, label, current, onPreview, onCommit }) {
  const fill = h("div", { class: "fill" });
  const knob = h("div", { class: "knob" });
  const track = h("div", { class: "track" });
  const bubble = h("div", { class: "bubble" });
  const element = h(
    "div",
    { class: "slider", role: "slider", tabindex: "0", "aria-label": label, "aria-valuemin": min, "aria-valuemax": max },
    fill,
    knob,
    track,
    bubble
  );
  let value = current();
  let frame = 0;
  const render = v => {
    const percent = ((v - min) / (max - min)) * 100;
    element.style.setProperty("--value", `${percent}%`);
    bubble.textContent = `${v}%`;
    element.setAttribute("aria-valuenow", String(v));
  };
  const setValue = (v, commit) => {
    value = Math.round(Math.min(max, Math.max(min, v)));
    render(value);
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => onPreview(value));
    if (commit) {
      onCommit(value);
    }
  };
  const fromEvent = event => {
    const rect = element.getBoundingClientRect();
    return min + ((event.clientX - rect.left) / rect.width) * (max - min);
  };
  element.addEventListener("pointerdown", event => {
    element.setPointerCapture(event.pointerId);
    element.classList.add("dragging");
    setValue(fromEvent(event), false);
  });
  element.addEventListener("pointermove", event => {
    if (element.hasPointerCapture(event.pointerId)) {
      setValue(fromEvent(event), false);
    }
  });
  const release = event => {
    if (element.hasPointerCapture(event.pointerId)) {
      element.releasePointerCapture(event.pointerId);
    }
    if (element.classList.contains("dragging")) {
      element.classList.remove("dragging");
      onCommit(value);
    }
  };
  element.addEventListener("pointerup", release);
  element.addEventListener("pointercancel", release);
  element.addEventListener("keydown", event => {
    const steps = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1, PageDown: -10, PageUp: 10 };
    if (event.key in steps) {
      setValue(value + steps[event.key], true);
    } else if (event.key === "Home") {
      setValue(min, true);
    } else if (event.key === "End") {
      setValue(max, true);
    } else {
      return;
    }
    event.preventDefault();
  });
  bind(() => {
    if (!element.classList.contains("dragging")) {
      value = current();
      render(value);
    }
  });
  return element;
}

function stepHead(title, subtitle) {
  return h("div", { class: "step-head" }, h("h2", { class: "headline" }, title), h("p", { class: "body" }, subtitle));
}

/* ------------------------------------------------------------ sections */

function preview(kind) {
  return h(
    "div",
    { class: `preview ${kind}`, "aria-hidden": "true" },
    h(
      "div",
      { class: "win" },
      h("div", { class: "col" }, h("div", { class: "line accent" }), h("div", { class: "line" }), h("div", { class: "line short" })),
      h("div", { class: "col" }, h("div", { class: "line" }), h("div", { class: "line short" }), h("div", { class: "line" }), h("div", { class: "line short" }))
    )
  );
}

function appearanceSection() {
  const choice = (value, kind, title, text) => {
    const button = h(
      "button",
      { class: "choice", type: "button", role: "radio", onclick: () => setSettings({ appearance: value }, { restartHint: true }) },
      h("span", { class: "check" }, icon("check")),
      preview(kind),
      h("div", { class: "choice-text" }, h("div", { class: "title" }, title), h("div", { class: "body" }, text))
    );
    bind(() => button.setAttribute("aria-checked", String(model.settings.appearance === value)));
    return button;
  };
  const choices = h(
    "div",
    { class: "choices", role: "radiogroup", "aria-label": t("appearance.title") },
    choice("transparent", "glass", t("appearance.transparent"), t("appearance.transparent.desc")),
    choice("opaque", "solid", t("appearance.opaque"), t("appearance.opaque.desc"))
  );

  const glassNames = ["ghostty", "vesktop", "zen"].filter(key => model.glass?.sources?.[key]);
  const glassRow = row({
    iconName: "blur_on",
    title: t("appearance.glass"),
    text: t("appearance.glass.desc"),
    hidden: () => model.settings.appearance !== "transparent",
  });
  glassRow.classList.add("with-control");
  const glassNote = h("div", { class: "body", style: "margin-top:8px" });
  glassRow.querySelector(".text").append(
    h(
      "div",
      { style: "margin-top:10px" },
      segmented(
        t("appearance.glass"),
        [
          ...glassNames.map(key => ({
            value: key,
            label: `${t(`appearance.glass.${key}`)} · ${Math.round(model.glass.sources[key].alpha * 100)}%`,
          })),
          { value: "custom", label: t("appearance.glass.custom") },
        ],
        () => model.glass?.choice || "custom",
        value => setSettings({ glass: value })
      )
    ),
    glassNote
  );
  bind(() => {
    const tint = model.glass?.tint;
    glassNote.textContent =
      tint && tint.source !== "custom"
        ? t("appearance.glass.matches", { source: t(`appearance.glass.${tint.source}`), color: tint.color, percent: tint.percent })
        : t("appearance.glass.custom.desc");
  });

  const tint = row({
    iconName: "opacity",
    title: t("appearance.tint"),
    text: t("appearance.tint.desc"),
    hidden: () => model.settings.appearance !== "transparent" || model.glass?.choice !== "custom",
  });
  tint.classList.add("with-control");
  tint.querySelector(".text").append(
    slider({
      min: 0,
      max: 90,
      label: t("appearance.tint"),
      current: () => model.settings.tint,
      onPreview: value => ZenApp.previewTint(value),
      onCommit: value => setSettings({ tint: value }),
    })
  );

  const controls = h(
    "div",
    { class: "row column" },
    h(
      "div",
      { class: "control-grid" },
      h(
        "div",
        { class: "control" },
        h("span", { class: "label" }, t("appearance.palette")),
        segmented(
          t("appearance.palette"),
          [
            { value: "system", label: t("appearance.palette.system") },
            { value: "app", label: t("appearance.palette.app", { app: model.name }) },
          ],
          () => model.settings.palette,
          value => setSettings({ palette: value })
        )
      ),
      h(
        "div",
        { class: "control" },
        h("span", { class: "label" }, t("appearance.scheme")),
        segmented(
          t("appearance.scheme"),
          [
            { value: "system", label: t("appearance.scheme.system") },
            { value: "dark", label: t("appearance.scheme.dark") },
            { value: "light", label: t("appearance.scheme.light") },
          ],
          () => model.settings.colorScheme,
          value => setSettings({ colorScheme: value })
        )
      ),
      h(
        "div",
        { class: "control", style: "grid-column: 1 / -1" },
        h("span", { class: "label" }, t("appearance.chrome")),
        segmented(
          t("appearance.chrome"),
          [
            { value: "auto", label: t("appearance.chrome.auto") },
            { value: "none", label: t("appearance.chrome.none") },
            { value: "titlebar", label: t("appearance.chrome.titlebar") },
          ],
          () => model.settings.windowChrome,
          value => setSettings({ windowChrome: value }, { restartHint: true })
        )
      )
    )
  );

  const notes = [];
  if (model.desktop) {
    notes.push(
      h(
        "div",
        { class: "note" },
        icon("blur_on"),
        h("span", {}, `${t("appearance.compositor", { desktop: model.desktop })} ${t("appearance.compositor.blur")}`)
      )
    );
  }
  return h(
    "div",
    {},
    choices,
    h("div", { class: "panel" }, glassRow, tint, controls),
    MODE === "settings" ? null : themePanel(),
    MODE === "settings" ? null : stylesPanel(false),
    MODE === "settings" ? null : customCssPanel(),
    ...notes
  );
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) {
    return "";
  }
  if (bytes < 1024) {
    return t("css.size.bytes", { n: bytes });
  }
  return t("css.size.kb", { n: (bytes / 1024).toLocaleString(model.locale, { maximumFractionDigits: 1 }) });
}

let cssBusy = false;

async function runCss(action, doneKey) {
  if (cssBusy) {
    return;
  }
  cssBusy = true;
  refresh();
  try {
    const result = await action();
    if (result !== null && doneKey) {
      snackbar(t(doneKey));
    }
  } catch (error) {
    console.error(error);
    snackbar(t(`css.error.${error?.code || "unknown"}`));
  } finally {
    cssBusy = false;
    refresh();
  }
}

/** The user's own CSS: upload or paste, switch, open, remove (with undo). */
function customCssPanel() {
  const state = () => model.customCss || { state: "none" };
  const loaded = () => state().state === "loaded";
  const button = (className, iconName, label, onclick) =>
    h("button", { class: `btn ${className}`, type: "button", onclick }, icon(iconName), label);

  const summary = h("div", { class: "body" });
  const warnings = h("div", { class: "css-warnings" });
  const path = h("div", { class: "css-path" });
  const hint = h("div", { class: "css-hint" }, t("css.hint"));
  const upload = button("tonal", "upload_file", t("css.upload"), () => runCss(() => ZenApp.pickCustomCss(), "css.saved"));
  const paste = button("outlined", "content_paste", t("css.paste"), () => runCss(() => ZenApp.pasteCustomCss(), "css.saved"));
  const open = button("text", "open_in_new", t("css.open"), () => ZenApp.openCustomCss());
  const remove = button("text danger-text", "delete", t("css.remove"), () =>
    runCss(async () => {
      const previous = await ZenApp.removeCustomCss();
      snackbar(
        t("css.removed"),
        previous ? { label: t("css.undo"), run: () => runCss(() => ZenApp.importCustomCss(previous.text, { fileName: previous.fileName }), "css.restored") } : null
      );
      return null;
    })
  );
  const actions = h("div", { class: "css-actions" }, upload, paste, open, remove);
  const onOff = toggle(t("css.title"), () => model.settings.customCss !== false, value => ZenApp.setCustomCssEnabled(value).then(refresh));

  const element = row({
    iconName: "format_paint",
    title: t("css.title"),
    text: " ",
    on: () => loaded() && model.settings.customCss !== false,
    extra: h("div", {}, warnings, actions, MODE === "settings" ? path : null, hint),
    trailing: onOff,
  });
  element.classList.add("with-control", "custom-css");
  element.querySelector(".text > .body").replaceWith(summary);

  bind(() => {
    const css = state();
    const has = css.state === "loaded" || css.state === "error";
    if (css.state === "loaded") {
      const name = css.name || css.fileName || t("css.pasted");
      const scope = css.sections ? t("css.scope.sections", { matching: css.matching, total: css.sections }) : t("css.scope.app");
      const extras = [
        css.palette?.applied ? t("css.palette") : null,
        css.imports?.inlined?.length ? t("css.imports", { n: css.imports.inlined.length }) : null,
      ];
      summary.textContent = [name, formatBytes(css.bytes), scope, ...extras, ...(css.aliases || [])].filter(Boolean).join(" · ");
    } else if (css.state === "error") {
      summary.textContent = t(`css.error.${css.error || "unknown"}`);
    } else {
      summary.textContent = t("css.desc");
    }
    summary.classList.toggle("bad", css.state === "error");
    const hostsOf = urls => (urls || []).map(url => url.replace(/^[a-z]+:\/\/([^/]+).*$/i, "$1")).join(", ") || "?";
    warnings.replaceChildren(
      ...(css.state === "loaded" ? css.warnings || [] : []).map(code =>
        h(
          "div",
          { class: "css-warning" },
          icon("warning"),
          t(`css.warn.${code}`, { sites: code === "importFailed" ? hostsOf(css.imports?.failed) : (css.elsewhere || []).join(", ") || "?" })
        )
      )
    );
    upload.lastChild.textContent = has ? t("css.replace") : t("css.upload");
    open.hidden = !has;
    remove.hidden = !has;
    onOff.hidden = !loaded();
    path.textContent = has ? css.path || "" : "";
    path.hidden = !has;
    for (const control of [upload, paste, open, remove, onOff]) {
      control.disabled = cssBusy;
    }
  });
  return h("div", { class: "panel" }, element);
}

/** The preset's page theme (e.g. Midnight for Discord): on or off. */
function themePanel() {
  const themes = model.pageThemes || [];
  if (!themes.length) {
    return null;
  }
  const panel = h("div", { class: "panel" });
  for (const theme of themes) {
    const current = () => (model.pageThemes || []).find(entry => entry.id === theme.id) || theme;
    const title = t("theme.title", { name: theme.name });
    const element = row({
      iconName: "wallpaper",
      title,
      text: " ",
      on: () => current().active,
      trailing: toggle(title, () => current().enabled, value => ZenApp.setPageTheme(theme.id, value).then(refresh)),
    });
    const body = element.querySelector(".text > .body");
    bind(() => {
      const state = current();
      const notes = [t("theme.desc", { author: state.author || "" })];
      if (state.enabled && state.blocked) {
        notes.push(t(`theme.blocked.${state.blocked}`));
      } else if (state.waiting) {
        notes.push(t("theme.waiting", { name: state.name }));
      }
      body.textContent = notes.join(" ");
    });
    panel.append(element);
  }
  return panel;
}

function stylesPanel(detailed) {
  const catalogSites = model.stylesCatalog?.sites || "670";
  const master = row({
    iconName: "palette",
    title: t("styles.title"),
    text: t("styles.desc", { sites: catalogSites }),
    on: () => model.settings.zenInternet !== false,
    trailing: toggle(t("styles.title"), () => model.settings.zenInternet !== false, value => setSettings({ zenInternet: value })),
  });
  const panel = h("div", { class: "panel" }, master);
  const sites = model.styleSites || [];
  if (!sites.length) {
    panel.append(row({ iconName: "info", title: t("styles.none") }));
    return panel;
  }
  for (const site of sites) {
    const label = site.site.replace(/\.css$/, "").replace(/^[+-]/, "");
    const siteRow = row({
      iconName: "language",
      title: t("styles.site", { site: label, count: site.features.length }),
      text: site.hosts.join(", "),
      hidden: () => model.settings.zenInternet === false,
    });
    panel.append(siteRow);
    if (!detailed) {
      continue;
    }
    for (const feature of site.features) {
      const title = feature.name.replace(/^[a-z]+-(?=\S)/i, "").replace(/^./, c => c.toUpperCase());
      const current = () => {
        const entry = (model.styleSites || []).find(s => s.site === site.site);
        return entry?.features.find(f => f.name === feature.name)?.enabled !== false;
      };
      panel.append(
        row({
          iconName: feature.transparency ? "blur_on" : "tune",
          title,
          text: feature.transparency ? t("styles.glassOnly") : null,
          on: current,
          hidden: () => model.settings.zenInternet === false,
          trailing: toggle(title, current, async value => {
            await ZenApp.setStyleFeature(site.site, feature.name, value);
            refresh();
          }),
        })
      );
    }
  }
  return panel;
}

function widevineExtra() {
  const status = h("span", { class: "status" });
  const progress = wavy();
  const install = h("button", { class: "btn tonal", type: "button", onclick: () => ZenApp.ensureWidevine({ force: true }) }, icon("download"), t("permissions.drm.install"));
  const box = h("div", { style: "display:grid;gap:8px;margin-top:6px;justify-items:start" }, status, progress, install);
  bind(() => {
    const state = model.widevine?.state || "unknown";
    status.className = "status" + (state === "failed" ? " bad" : "");
    status.textContent =
      state === "installed"
        ? `✓ ${t("permissions.drm.installed")}`
        : state === "installing"
          ? t("permissions.drm.installing")
          : state === "failed"
            ? t("permissions.drm.failed")
            : t("permissions.drm.missing");
    progress.hidden = state !== "installing";
    install.hidden = state === "installed" || state === "installing" || !model.settings.drm;
    box.hidden = !model.settings.drm && state !== "installed";
  });
  return box;
}

function permissionsSection() {
  const setting = (key, iconName) => {
    const title = t(`permissions.${key}`);
    return row({
      iconName,
      title,
      text: t(`permissions.${key}.desc`),
      on: () => model.settings[key],
      trailing: toggle(title, () => model.settings[key], value => setSettings({ [key]: value })),
      extra: key === "drm" ? widevineExtra() : null,
    });
  };
  // Site compatibility fixes of the preset (e.g. Messenger's encrypted calls).
  const compatRows = (model.compat || []).map(fix => {
    const title = t(`compat.${fix.id}`);
    return row({
      iconName: "phone_in_talk",
      title,
      text: fix.available ? t(`compat.${fix.id}.desc`) : t("compat.unavailable"),
      on: () => fix.available && model.settings.webcompat !== false,
      trailing: fix.available
        ? toggle(title, () => model.settings.webcompat !== false, value => setSettings({ webcompat: value }, { reloadHint: true }))
        : null,
    });
  });
  return h(
    "div",
    {},
    h(
      "div",
      { class: "panel" },
      setting("notifications", "notifications"),
      setting("media", "mic"),
      setting("autoplay", "play_circle"),
      setting("drm", "verified_user"),
      compatRows
    ),
    runningPanel(),
    // Settings has a tab of its own for these.
    MODE === "settings" ? null : adblockPanel(),
    MODE === "settings" ? null : extensionsPanel(),
    h(
      "div",
      { class: "panel" },
      row({ iconName: "open_in_new", title: t("permissions.links"), text: t("permissions.links.desc") }),
      row({ iconName: "screen_share", title: t("permissions.screen"), text: t("permissions.screen.desc") })
    )
  );
}

/** Tray icon, running in the background and the unread badge. */
function runningPanel() {
  const option = (key, iconName, fallback, extra = null) => {
    const title = t(`permissions.${key}`);
    const current = () => model.settings[key] ?? fallback;
    return row({
      iconName,
      title,
      text: t(`permissions.${key}.desc`),
      on: current,
      trailing: toggle(title, current, value => setSettings({ [key]: value })),
      extra,
    });
  };
  const trayStatus = h("span", { class: "status" });
  bind(() => {
    const tray = model.tray || { state: "off" };
    const failed = tray.state === "failed";
    const waiting = tray.state === "on" && tray.item && !tray.hosted;
    trayStatus.className = "status" + (failed ? " bad" : "");
    trayStatus.textContent = failed ? t("permissions.tray.failed") : waiting ? t("permissions.tray.noHost") : "";
    trayStatus.hidden = model.settings.tray === false || (!failed && !waiting);
  });
  return h(
    "div",
    { class: "panel" },
    option("tray", "keep", true, trayStatus),
    option("background", "minimize", false),
    option("unreadBadge", "app_badging", true)
  );
}

/* --------------------------------------------------------- extensions */

async function runExtension(action, doneText) {
  try {
    const result = await action();
    if (result && doneText) {
      snackbar(doneText(result));
    }
  } catch (error) {
    console.error(error);
    snackbar(t(`ext.error.${error?.code || "unknown"}`));
  } finally {
    refresh();
  }
}

function percent(progress) {
  return Number.isFinite(progress) ? Math.round(progress * 100) : null;
}

/** uBlock Origin behind the "adblock" setting. */
function adblockPanel() {
  const status = h("span", { class: "status" });
  const progress = wavy();
  const retry = h(
    "button",
    { class: "btn tonal", type: "button", onclick: () => runExtension(() => ZenApp.retryAdblock()) },
    icon("refresh"),
    t("adblock.retry")
  );
  const options = h(
    "button",
    { class: "btn text", type: "button", onclick: () => ZenApp.openExtensionOptions(model.adblock.id) },
    icon("tune"),
    t("adblock.options")
  );
  const actions = h("div", { class: "css-actions" }, retry, options);
  const box = h("div", { class: "ext-status" }, status, progress, actions);
  const element = row({
    iconName: "ad_off",
    title: t("adblock.title"),
    text: t("adblock.desc"),
    on: () => model.settings.adblock !== false && model.adblock?.state === "active",
    trailing: toggle(t("adblock.title"), () => model.settings.adblock !== false, value => setSettings({ adblock: value })),
    extra: box,
  });
  element.classList.add("with-control");
  bind(() => {
    const adblock = model.adblock || { state: "unknown" };
    const wanted = model.settings.adblock !== false;
    const state = adblock.state;
    const done = percent(adblock.progress);
    status.className = "status" + (state === "failed" || state === "blocked" ? " bad" : "");
    status.textContent =
      state === "active"
        ? `✓ ${t("adblock.active", { name: adblock.name, version: adblock.version || "" }).trim()}`
        : state === "installing"
          ? done === null
            ? t("adblock.installing")
            : t("adblock.installingPercent", { percent: done })
          : state === "failed"
            ? `${t(`ext.error.${adblock.error || "unknown"}`)} ${t("adblock.retryLater")}`
            : state === "blocked"
              ? t("adblock.blocked")
              : state === "missing"
                ? t("adblock.missing")
                : wanted
                  ? t("adblock.starting")
                  : t("adblock.off");
    progress.hidden = state !== "installing";
    retry.hidden = !wanted || !(state === "failed" || state === "missing");
    options.hidden = state !== "active";
    actions.hidden = retry.hidden && options.hidden;
    box.hidden = !wanted && state !== "installing";
  });
  return h("div", { class: "panel" }, element);
}

/** The app's own extensions: the installed ones, and adding more from AMO or a file. */
function extensionsPanel() {
  const installed = h("div", { class: "ext-installed" });
  const input = h("input", {
    type: "text",
    placeholder: t("ext.search.placeholder"),
    "aria-label": t("ext.search"),
    spellcheck: "false",
    autocomplete: "off",
  });
  const results = h("div", { class: "ext-results", "aria-live": "polite" });
  const fromFile = h(
    "button",
    {
      class: "btn outlined",
      type: "button",
      onclick: () => runExtension(() => ZenApp.pickExtensionFile(), result => t("ext.added", { name: result.name })),
    },
    icon("upload_file"),
    t("ext.file")
  );
  const manage =
    MODE === "settings"
      ? h(
          "button",
          {
            class: "btn text",
            type: "button",
            onclick: () => {
              ZenApp.openAddons();
              window.close();
            },
          },
          icon("open_in_new"),
          t("ext.manage")
        )
      : null;
  const add = h(
    "div",
    { class: "ext-add" },
    h("label", { class: "ext-search" }, icon("search"), input),
    results,
    h("div", { class: "css-actions" }, fromFile, manage),
    h("div", { class: "css-hint" }, t("ext.hint"))
  );
  const head = h(
    "div",
    { class: "row" },
    h("div", { class: "lead" }, icon("extension")),
    h("div", { class: "text" }, h("div", { class: "title" }, t("ext.title")), h("div", { class: "body" }, t("ext.desc")))
  );

  const installedIds = () => {
    const ids = new Set((model.extensions || []).map(extension => extension.id));
    if (model.adblock?.state === "active") {
      ids.add(model.adblock.id);
    }
    return ids;
  };

  /* Installed extensions. */
  const remove = extension =>
    runExtension(async () => {
      const removed = await ZenApp.removeExtension(extension.id);
      snackbar(t("ext.removed", { name: removed.name }), {
        label: t("css.undo"),
        run: () => runExtension(() => ZenApp.undoRemoveExtension(removed.id)),
      });
      return null;
    });
  const installedRow = extension => {
    const notes = [
      extension.version ? `v${extension.version}` : null,
      extension.signed ? null : t("ext.unsigned"),
      extension.temporary ? t("ext.temporary") : null,
      extension.appDisabled ? t(extension.blocked ? "ext.blocked" : "ext.incompatible") : null,
    ].filter(Boolean);
    const buttons = [
      extension.options && extension.active
        ? h("button", { class: "btn text", type: "button", onclick: () => ZenApp.openExtensionOptions(extension.id) }, icon("tune"), t("ext.options"))
        : null,
      extension.canRemove
        ? h("button", { class: "btn text danger-text", type: "button", onclick: () => remove(extension) }, icon("delete"), t("ext.remove"))
        : null,
    ].filter(Boolean);
    const onOff = listToggle(extension.name, extension.enabled, value =>
      runExtension(() => ZenApp.setExtensionEnabled(extension.id, value))
    );
    onOff.disabled = !extension.canToggle;
    return h(
      "div",
      { class: "row with-control" + (extension.enabled && extension.active ? " on" : "") },
      h("div", { class: "lead ext-lead" }, extension.icon ? h("img", { src: extension.icon, alt: "" }) : icon("extension")),
      h(
        "div",
        { class: "text" },
        h("div", { class: "title" }, extension.name),
        notes.length ? h("div", { class: "body" + (extension.appDisabled ? " bad" : "") }, notes.join(" · ")) : null,
        extension.description ? h("div", { class: "ext-desc" }, extension.description) : null,
        buttons.length ? h("div", { class: "css-actions" }, buttons) : null
      ),
      onOff
    );
  };
  let installedKey = "";
  const renderInstalled = () => {
    const list = model.extensions || [];
    const fileJob = model.extensionJobs?.file || null;
    const key = JSON.stringify([list, fileJob, model.extensionsReady]);
    if (key === installedKey) {
      return;
    }
    installedKey = key;
    const rows = list.map(installedRow);
    if (fileJob) {
      const progress = wavy();
      progress.hidden = false;
      rows.push(
        h(
          "div",
          { class: "row" },
          h("div", { class: "lead" }, icon("upload_file")),
          h("div", { class: "text" }, h("div", { class: "title" }, t("ext.fileInstalling", { name: fileJob.name || "" })), progress)
        )
      );
    }
    if (!rows.length) {
      rows.push(h("div", { class: "ext-empty body" }, t(model.extensionsReady ? "ext.none" : "ext.loading")));
    }
    installed.replaceChildren(...rows);
  };

  /* Search on addons.mozilla.org (or an AMO link). */
  const compact = new Intl.NumberFormat(model.locale, { notation: "compact", maximumFractionDigits: 1 });
  let query = "";
  let found = [];
  let searching = false;
  let searchError = null;
  let generation = 0;
  let timer = 0;
  const search = async text => {
    const mine = ++generation;
    query = text.trim();
    if (query.length < 2) {
      found = [];
      searching = false;
      searchError = null;
      renderResults();
      return;
    }
    searching = true;
    searchError = null;
    renderResults();
    let list = [];
    let failure = null;
    try {
      list = await ZenApp.searchExtensions(query);
    } catch (error) {
      failure = error?.code || "unknown";
    }
    if (mine !== generation) {
      return;
    }
    found = list;
    searchError = failure;
    searching = false;
    renderResults();
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => search(input.value), 350);
  });
  input.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      clearTimeout(timer);
      search(input.value);
    } else if (event.key === "Escape" && input.value) {
      // Clear the field instead of closing the settings dialog.
      event.preventDefault();
      event.stopPropagation();
      input.value = "";
      clearTimeout(timer);
      search("");
    }
  });
  const resultRow = (result, ids, jobs) => {
    const job = jobs[result.guid];
    const has = ids.has(result.guid);
    const label = has
      ? t("ext.installed")
      : job
        ? percent(job.progress) === null
          ? t("ext.installing")
          : `${percent(job.progress)}%`
        : result.compatible
          ? t("ext.add")
          : t("ext.incompatible");
    const button = h(
      "button",
      {
        class: `btn ${has || !result.compatible ? "text" : "tonal"}`,
        type: "button",
        disabled: has || !!job || !result.compatible,
        onclick: () => runExtension(() => ZenApp.installExtension(result.guid), done => t("ext.added", { name: done.name })),
      },
      has ? icon("check") : job ? null : result.compatible ? icon("add") : null,
      label
    );
    const meta = [result.users !== null ? t("ext.users", { n: compact.format(result.users) }) : null, result.version ? `v${result.version}` : null]
      .filter(Boolean)
      .join(" · ");
    return h(
      "div",
      { class: "ext-result" },
      result.icon
        ? h("img", { class: "ext-icon", src: result.icon, alt: "", referrerpolicy: "no-referrer" })
        : h("span", { class: "ext-icon placeholder" }, icon("extension")),
      h(
        "div",
        { class: "text" },
        h("div", { class: "name" }, h("span", {}, result.name), result.recommended ? h("span", { class: "badge" }, t("ext.recommended")) : null),
        result.summary ? h("div", { class: "summary" }, result.summary) : null,
        meta ? h("div", { class: "meta" }, meta) : null
      ),
      button
    );
  };
  let resultsKey = "";
  const renderResults = () => {
    const ids = installedIds();
    const jobs = model.extensionJobs || {};
    const key = JSON.stringify([query, searching, searchError, found.map(result => result.guid), [...ids], jobs]);
    if (key === resultsKey) {
      return;
    }
    resultsKey = key;
    const content = [];
    if (searching && !found.length) {
      content.push(h("div", { class: "ext-message" }, t("ext.searching")));
    } else if (searchError) {
      content.push(h("div", { class: "ext-message bad" }, t(`ext.error.${searchError}`)));
    } else if (query.length >= 2 && !found.length) {
      content.push(h("div", { class: "ext-message" }, t("ext.noResults")));
    }
    content.push(...found.map(result => resultRow(result, ids, jobs)));
    results.replaceChildren(...content);
  };

  bind(() => {
    renderInstalled();
    renderResults();
  });
  return h("div", { class: "panel extensions" }, head, installed, add);
}

function keysSection() {
  const combos = [
    ["keys.reload", [["Ctrl", "R"], ["F5"]]],
    ["keys.zoom", [["Ctrl", "+"], ["Ctrl", "−"]]],
    ["keys.zoomReset", [["Ctrl", "0"]]],
    ["keys.fullscreen", [["F11"]]],
    ["keys.history", [["Alt", "←"], ["Alt", "→"]]],
    ["keys.close", [["Ctrl", "W"]]],
    ["keys.quit", [["Ctrl", "Q"]]],
    ["keys.settings", [["Ctrl", ","]]],
  ];
  const grid = h(
    "div",
    { class: "keys-grid" },
    combos.map(([label, sets]) =>
      h(
        "div",
        { class: "key-row" },
        h("span", {}, t(label)),
        h("span", { class: "combo" }, sets.map((keys, index) => [index ? h("span", { class: "body" }, "/") : null, keys.map(key => h("kbd", {}, key))]))
      )
    )
  );
  const option = (key, settingKey, iconName) =>
    row({
      iconName,
      title: t(key),
      text: t(`${key}.desc`),
      on: () => model.settings[settingKey],
      trailing: toggle(t(key), () => model.settings[settingKey], value => setSettings({ [settingKey]: value })),
    });
  return h(
    "div",
    {},
    grid,
    h("div", { class: "panel" }, option("keys.find", "findInPage", "search"), option("keys.developer", "developer", "code"))
  );
}

function advancedSection() {
  let confirmArmed = false;
  const clear = h("button", { class: "btn danger", type: "button" }, icon("delete"), t("settings.clearData"));
  clear.addEventListener("click", async () => {
    if (!confirmArmed) {
      confirmArmed = true;
      clear.lastChild.textContent = t("settings.clearData.confirm");
      setTimeout(() => {
        confirmArmed = false;
        clear.lastChild.textContent = t("settings.clearData");
      }, 5000);
      return;
    }
    await ZenApp.clearData();
    window.close();
  });
  const about = h(
    "dl",
    { class: "about" },
    h("dt", {}, t("settings.version")),
    h("dd", {}, `${model.versions.package || "—"} · add-on ${model.versions.addon}`),
    h("dt", {}, t("settings.engine")),
    h("dd", {}, model.versions.engine),
    h("dt", {}, t("settings.profile")),
    h("dd", {}, model.paths.profile),
    h("dt", {}, t("settings.definition")),
    h("dd", {}, model.paths.definition)
  );
  return h(
    "div",
    {},
    h(
      "div",
      { class: "panel" },
      row({
        iconName: "restart_alt",
        title: t("settings.restart"),
        text: t("settings.restart.desc"),
        trailing: h("button", { class: "btn tonal", type: "button", onclick: () => ZenApp.restart() }, t("settings.restart")),
      }),
      row({
        iconName: "auto_awesome",
        title: t("settings.onboarding"),
        text: t("settings.onboarding.desc"),
        trailing: h("button", { class: "btn tonal", type: "button", onclick: () => ZenApp.restartOnboarding(window) }, icon("arrow_forward")),
      }),
      row({
        iconName: "delete_sweep",
        title: t("settings.clearData"),
        text: t("settings.clearData.desc", { app: model.name }),
        trailing: clear,
      })
    ),
    h("div", { class: "panel" }, about)
  );
}

/* ----------------------------------------------------------- onboarding */

const STEP_SHAPES = {
  welcome: ["cookie9", "clover8"],
  appearance: ["softBurst", "cookie6"],
  permissions: ["clover4", "flower"],
  keys: ["squircle", "cookie12"],
  done: ["burst", "sunny"],
};

function heroShape() {
  const container = h("div", { class: "hero" });
  const back = svg("svg", { viewBox: "0 0 100 100", "aria-hidden": "true" });
  const backPath = svg("path", { class: "shape-back" });
  back.append(backPath);
  const front = svg("svg", { viewBox: "0 0 100 100", "aria-hidden": "true" });
  const frontPath = svg("path", { class: "shape-front" });
  front.append(frontPath);
  container.append(back, front);
  const img = appIcon();
  if (img) {
    container.append(img);
  } else {
    container.append(h("span", { class: "icon", style: "font-size:120px;position:relative;color:var(--zenapp-on-primary-container)" }, "apps"));
  }
  const frontMorph = new ZenShapes.Morph(frontPath, "circle", { size: 44 });
  const backMorph = new ZenShapes.Morph(backPath, "circle", { size: 49 });
  return { element: container, morph: (a, b) => { frontMorph.to(a, 950); backMorph.to(b, 1250); } };
}

function renderOnboarding(root) {
  const steps = ["welcome", "appearance", "permissions", "keys", "done"];
  let index = 0;
  const hero = heroShape();

  const pills = h("div", { class: "pills", "aria-hidden": "true" }, steps.map(() => h("span")));
  const skip = h("button", { class: "btn text", type: "button", onclick: () => go(steps.length - 1) }, t("studio.skip"));
  const topbar = h(
    "header",
    { class: "topbar" },
    h("div", { class: "brand" }, appIcon(), h("span", { class: "name" }, model.name)),
    pills,
    h("div", { class: "end" }, skip)
  );

  const tagline = model.tagline || t("welcome.fallbackTagline");
  const welcome = h(
    "div",
    { class: "welcome" },
    hero.element,
    h(
      "div",
      { class: "welcome-text" },
      h("span", { class: "kicker" }, t("welcome.kicker")),
      h("h1", { class: "display" }, model.name),
      h("p", { class: "body", style: "font-size:18px;line-height:27px" }, tagline),
      h(
        "div",
        { class: "chips" },
        h("span", { class: "chip" }, icon("shield_person"), t("welcome.chip.profile")),
        h("span", { class: "chip" }, icon("blur_on"), t("welcome.chip.glass")),
        h("span", { class: "chip" }, icon("keyboard"), t("welcome.chip.keys"))
      ),
      h("button", { class: "btn filled large cta", type: "button", onclick: () => go(1) }, t("welcome.cta"), icon("arrow_forward")),
      h("span", { class: "runtime" }, t("welcome.runtime", { runtime: model.versions.engine }))
    )
  );

  const burst = svg("svg", { viewBox: "0 0 100 100", "aria-hidden": "true" });
  const burstPath = svg("path", { d: ZenShapes.toPath(ZenShapes.radii("softBurst"), 50, 50, 48) });
  burst.append(burstPath);
  const finish = h(
    "button",
    {
      class: "btn filled large",
      type: "button",
      onclick: async () => {
        finish.disabled = true;
        await ZenApp.completeOnboarding();
      },
    },
    t("done.cta", { app: model.name }),
    icon("rocket_launch")
  );
  const done = h(
    "div",
    { class: "done" },
    h("div", { class: "burst" }, burst, icon("check")),
    h("h2", { class: "display", style: "font-size:clamp(40px,5vw,60px)" }, t("done.title")),
    h("p", { class: "body", style: "font-size:17px;line-height:26px" }, t("done.subtitle", { app: model.name })),
    finish
  );

  const pages = {
    welcome,
    appearance: h("div", {}, stepHead(t("appearance.title"), t("appearance.subtitle")), appearanceSection()),
    permissions: h("div", {}, stepHead(t("permissions.title"), t("permissions.subtitle")), permissionsSection()),
    keys: h("div", {}, stepHead(t("keys.title"), t("keys.subtitle")), keysSection()),
    done,
  };
  const sections = steps.map(name => h("section", { class: "step", "data-step": name }, h("div", { class: "step-inner" }, pages[name])));
  const stage = h("main", { class: "stage" }, sections);

  const back = h("button", { class: "btn text", type: "button", onclick: () => go(index - 1) }, icon("arrow_back"), t("studio.back"));
  const next = h("button", { class: "btn filled medium", type: "button", onclick: () => go(index + 1) }, t("studio.next"), icon("arrow_forward"));
  const actions = h("footer", { class: "actions" }, back, h("span", { class: "spacer" }), next);

  root.append(topbar, stage, actions);

  function go(target) {
    index = Math.max(0, Math.min(steps.length - 1, target));
    sections.forEach((section, i) => {
      section.classList.toggle("active", i === index);
      section.classList.toggle("before", i < index);
      section.inert = i !== index;
    });
    [...pills.children].forEach((pill, i) => {
      pill.classList.toggle("active", i === index);
      pill.classList.toggle("done", i < index);
    });
    const name = steps[index];
    const middle = index > 0 && index < steps.length - 1;
    actions.style.visibility = middle ? "visible" : "hidden";
    skip.style.visibility = index < steps.length - 1 && index > 0 ? "visible" : "hidden";
    const [front, rear] = STEP_SHAPES[name];
    hero.morph(front, rear);
    const focusTarget = sections[index].querySelector("button, [tabindex]");
    focusTarget?.focus({ preventScroll: true, focusVisible: false });
  }

  requestAnimationFrame(() => go(0));
}

/* ------------------------------------------------------------ settings */

function renderSettings(root) {
  const head = h(
    "header",
    { class: "settings-head" },
    appIcon(),
    h("div", { class: "titles" }, h("h1", { class: "title", style: "font-size:22px;line-height:28px" }, model.name), h("div", { class: "body" }, t("settings.title"))),
    h("button", { class: "icon-btn", type: "button", "aria-label": t("studio.close"), onclick: () => window.close() }, icon("close"))
  );
  const names = [
    ["appearance", t("settings.appearance"), appearanceSection],
    ["styles", t("settings.styles"), () => h("div", {}, themePanel(), customCssPanel(), stylesPanel(true))],
    ["permissions", t("settings.permissions"), permissionsSection],
    ["extensions", t("settings.extensions"), () => h("div", {}, adblockPanel(), extensionsPanel())],
    ["keys", t("settings.keys"), keysSection],
    ["advanced", t("settings.advanced"), advancedSection],
  ];
  const sections = names.map(([name, , build]) => h("section", { "data-section": name }, build()));
  const tabs = h(
    "nav",
    { class: "tabs", role: "tablist" },
    names.map(([name, label], i) =>
      h("button", { class: "tab", type: "button", role: "tab", "data-tab": name, onclick: () => select(i) }, label)
    )
  );
  const body = h("main", { class: "settings-body" }, sections);
  root.append(head, tabs, body);

  function select(i) {
    [...tabs.children].forEach((tab, j) => tab.setAttribute("aria-selected", String(i === j)));
    sections.forEach((section, j) => section.classList.toggle("active", i === j));
    body.scrollTop = 0;
  }
  select(0);

  document.documentElement.style.width = "640px";
  document.documentElement.style.height = "620px";
  window.resizeDialog?.();
}

/* ----------------------------------------------------------------- boot */

/* The settings dialog is a SubDialog, which appends in-content/common.css
   after our stylesheet (it wins every tie and widens each button to 6.3em).
   The studio styles itself completely, so drop the injected sheet: the dialog
   then renders exactly like the standalone onboarding. */
const INJECTED_COMMON_CSS = "chrome://global/skin/in-content/common.css";

function dropInjectedCommonCss() {
  const links = document.head.querySelectorAll(`link[rel="stylesheet"][href="${INJECTED_COMMON_CSS}"]`);
  links.forEach(link => link.remove());
  return links.length > 0;
}

function keepSubDialogStylesOut() {
  if (dropInjectedCommonCss()) {
    return;
  }
  const observer = new MutationObserver(() => {
    if (dropInjectedCommonCss()) {
      observer.disconnect();
    }
  });
  observer.observe(document.head, { childList: true });
  window.addEventListener("unload", () => observer.disconnect(), { once: true });
}

function boot() {
  if (MODE === "settings") {
    keepSubDialogStylesOut();
  }
  detectSymbols();
  applyTheme();
  const root = document.getElementById("app");
  root.style.cssText = "display:contents";
  if (MODE === "settings") {
    renderSettings(root);
  } else {
    renderOnboarding(root);
  }
  const unsubscribe = ZenApp.addListener(() => refresh());
  window.addEventListener("unload", unsubscribe, { once: true });
}

boot();
