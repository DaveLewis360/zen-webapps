/* zen-app-runtime app mode — onboarding & settings studio (privileged page). */
/* global ChromeUtils, ZenShapes */
"use strict";

const { ZenApp } = ChromeUtils.importESModule("resource://zenapp/modules/ZenApp.sys.mjs");
const { t } = ChromeUtils.importESModule("resource://zenapp/modules/Strings.sys.mjs");

const MODE = new URLSearchParams(location.search).get("mode") === "settings" ? "settings" : "onboarding";
let model = ZenApp.getStudioModel();
const updaters = [];

/* ------------------------------------------------------------ helpers */

function h(tag, props = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) {
      continue;
    }
    if (key === "class") {
      element.className = value;
    } else if (key.startsWith("on") && typeof value === "function") {
      element.addEventListener(key.slice(2), value);
    } else if (key === "text") {
      element.textContent = value;
    } else {
      element.setAttribute(key, value === true ? "" : String(value));
    }
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) {
      continue;
    }
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

const icon = name => h("span", { class: "icon", "aria-hidden": "true" }, name);

function svg(tag, attrs = {}) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) {
    element.setAttribute(key, value);
  }
  return element;
}

function bind(update) {
  updaters.push(update);
  update();
}

function refresh() {
  model = ZenApp.getStudioModel();
  applyTheme();
  for (const update of updaters) {
    update();
  }
}

function iconURL() {
  const file = model.icon?.file;
  return file ? "file://" + file.split("/").map(encodeURIComponent).join("/") : null;
}

function appIcon(className) {
  const url = iconURL();
  return url ? h("img", { class: className || "", src: url, alt: "" }) : null;
}

function applyTheme() {
  const root = document.documentElement;
  root.dataset.mode = MODE;
  root.dataset.appearance = model.settings.appearance;
  root.dataset.scheme = model.palette && model.palette.dark === false ? "light" : "dark";
  root.lang = model.locale;
  for (const [name, value] of Object.entries(model.paletteVariables || {})) {
    root.style.setProperty(name, value);
  }
  document.title = MODE === "settings" ? `${model.name} — ${t("settings.title")}` : model.name;
}

function detectSymbols() {
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d");
  context.font = '24px "Material Symbols Rounded", monospace';
  const withFont = context.measureText("settings").width;
  context.font = "24px monospace";
  const fallback = context.measureText("settings").width;
  if (Math.abs(withFont - fallback) < 1) {
    document.documentElement.classList.add("no-symbols");
  }
}

let snackTimer = 0;
function snackbar(text, action) {
  const bar = document.getElementById("snackbar");
  const button = document.getElementById("snackbar-action");
  document.getElementById("snackbar-text").textContent = text;
  button.hidden = !action;
  button.onclick = null;
  if (action) {
    button.textContent = action.label;
    button.onclick = () => {
      bar.classList.remove("shown");
      action.run();
    };
  }
  bar.classList.add("shown");
  clearTimeout(snackTimer);
  snackTimer = setTimeout(() => bar.classList.remove("shown"), action ? 6000 : 2400);
}

async function setSettings(patch, { restartHint = false } = {}) {
  try {
    await ZenApp.updateSettings(patch);
  } catch (error) {
    console.error(error);
    snackbar(String(error?.message || error));
    return;
  }
  refresh();
  if (MODE === "settings") {
    snackbar(
      restartHint ? t("settings.restartNeeded") : t("settings.saved"),
      restartHint ? { label: t("settings.restart"), run: () => ZenApp.restart() } : null
    );
  }
}

/* ----------------------------------------------------------- controls */

function segmented(name, options, current, onPick) {
  const group = h("div", { class: "seg", role: "radiogroup", "aria-label": name });
  const buttons = options.map(option =>
    h(
      "button",
      {
        type: "button",
        role: "radio",
        onclick: () => onPick(option.value),
      },
      icon("check"),
      option.label
    )
  );
  group.append(...buttons);
  bind(() => {
    const value = current();
    buttons.forEach((button, index) => {
      const on = options[index].value === value;
      button.setAttribute("aria-pressed", String(on));
      button.setAttribute("aria-checked", String(on));
    });
  });
  return group;
}

function toggle(label, current, onChange) {
  const button = h(
    "button",
    { class: "switch", type: "button", role: "switch", "aria-label": label, onclick: () => onChange(!current()) },
    h("span", { class: "handle" }, icon("check"))
  );
  bind(() => button.setAttribute("aria-checked", String(!!current())));
  return button;
}

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

function row({ iconName, title, text, trailing, on, extra, hidden }) {
  const element = h(
    "div",
    { class: "row" },
    h("div", { class: "lead" }, icon(iconName)),
    h("div", { class: "text" }, h("div", { class: "title" }, title), text ? h("div", { class: "body" }, text) : null, extra || null),
    trailing || null
  );
  if (on || hidden) {
    bind(() => {
      if (on) {
        element.classList.toggle("on", !!on());
      }
      if (hidden) {
        element.hidden = !!hidden();
      }
    });
  }
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
  return h("div", {}, choices, h("div", { class: "panel" }, glassRow, tint, controls), MODE === "settings" ? null : stylesPanel(false), MODE === "settings" ? null : customCssPanel(), ...notes);
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
  const progress = h("div", { class: "wavy", hidden: true });
  const wave = svg("svg", { viewBox: "0 0 440 12", preserveAspectRatio: "none" });
  let d = "M2 6";
  for (let x = 2; x < 438; x += 20) {
    d += ` Q ${x + 5} 0 ${x + 10} 6 T ${x + 20} 6`;
  }
  wave.append(svg("path", { d }));
  progress.append(wave);
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
  return h(
    "div",
    {},
    h(
      "div",
      { class: "panel" },
      setting("notifications", "notifications"),
      setting("media", "mic"),
      setting("autoplay", "play_circle"),
      setting("drm", "verified_user")
    ),
    h(
      "div",
      { class: "panel" },
      row({ iconName: "open_in_new", title: t("permissions.links"), text: t("permissions.links.desc") }),
      row({ iconName: "screen_share", title: t("permissions.screen"), text: t("permissions.screen.desc") })
    )
  );
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
        iconName: "extension",
        title: t("settings.addons"),
        text: t("settings.addons.desc"),
        trailing: h("button", { class: "btn outlined", type: "button", onclick: () => { ZenApp.openAddons(); window.close(); } }, icon("open_in_new")),
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
    ["styles", t("settings.styles"), () => h("div", {}, customCssPanel(), stylesPanel(true))],
    ["permissions", t("settings.permissions"), permissionsSection],
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
