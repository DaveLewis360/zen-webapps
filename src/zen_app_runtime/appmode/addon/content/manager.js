/* zen-app-runtime app mode — Webappok, the app manager (privileged page).
 * Lists the apps, opens them and their settings, refreshes and removes
 * them; every action goes through the zen-app CLI (ZenApp.manager).
 */
/* global ChromeUtils, ZenShapes, ZenUI */
"use strict";

const { ZenApp } = ChromeUtils.importESModule("resource://zenapp/modules/ZenApp.sys.mjs");
const { t } = ChromeUtils.importESModule("resource://zenapp/modules/Strings.sys.mjs");
const { h, icon, svg, snackbar, listToggle, wavy } = ZenUI;

/** The list is re-read this often while the window is in front. */
const REFRESH_MS = 4000;

const state = {
  apps: null,
  error: null,
  busy: new Set(),
};

/* ------------------------------------------------------------ helpers */

function hostOf(url) {
  try {
    return new URL(url).host;
  } catch {
    return String(url || "");
  }
}

/** A CLI error in the user's words. */
function errorText(error) {
  const code = error?.code || "invalid";
  const message = String(error?.message || "");
  const key = `manager.error.${code}`;
  const text = t(key, { message });
  return text === key ? t("manager.error.invalid", { message: message || code }) : text;
}

function applyTheme() {
  ZenUI.applyPalette(ZenApp.getStudioModel());
  document.documentElement.dataset.mode = "manager";
  document.title = t("manager.title");
}

function shape(name, size = 48) {
  const element = svg("svg", { viewBox: "0 0 100 100", "aria-hidden": "true" });
  element.append(svg("path", { d: ZenShapes.toPath(ZenShapes.radii(name), 50, 50, size) }));
  return element;
}

function appIcon(app, className = "app-icon") {
  const url = ZenUI.fileURL(app.icon);
  if (url) {
    return h("img", { class: className, src: url, alt: "" });
  }
  return h("span", { class: `${className} placeholder`, "aria-hidden": "true" }, (app.name || "?").slice(0, 1).toUpperCase());
}

/* ------------------------------------------------------------ actions */

async function loadApps() {
  try {
    state.apps = await ZenApp.manager.apps();
    state.error = null;
  } catch (error) {
    console.error(error);
    state.error = error;
  }
  render();
}

/** Run one action for an app; the card shows it is busy meanwhile. */
async function act(app, work) {
  if (state.busy.has(app.id)) {
    return;
  }
  state.busy.add(app.id);
  render();
  try {
    await work();
  } catch (error) {
    console.error(error);
    snackbar(errorText(error));
  } finally {
    state.busy.delete(app.id);
    await loadApps();
  }
}

function openApp(app) {
  return act(app, async () => {
    const result = await ZenApp.manager.open(app.id);
    snackbar(t(result.action === "focused" ? "manager.focused" : "manager.started", { app: app.name }));
  });
}

function openAppSettings(app) {
  return act(app, () => ZenApp.manager.settings(app.id));
}

function refreshApp(app) {
  return act(app, async () => {
    const result = await ZenApp.manager.refresh(app.id);
    const entry = result.apps?.[0];
    if (entry?.skipped === "running") {
      snackbar(t("manager.error.running"));
    } else {
      snackbar(t(entry?.changed?.length ? "manager.refreshed" : "manager.upToDate", { app: app.name }));
    }
  });
}

/* --------------------------------------------------- overflow menu */

let openMenuElement = null;

function closeMenu() {
  if (openMenuElement) {
    const { menu, anchor } = openMenuElement;
    openMenuElement = null;
    menu.remove();
    anchor.setAttribute("aria-expanded", "false");
  }
}

function menuItem({ iconName, label, hint, onPick, disabled, danger }) {
  return h(
    "button",
    {
      class: "menu-item" + (danger ? " danger" : ""),
      type: "button",
      role: "menuitem",
      disabled: !!disabled,
      onclick: () => {
        closeMenu();
        onPick();
      },
    },
    icon(iconName),
    h("span", { class: "menu-text" }, h("span", { class: "label" }, label), hint ? h("span", { class: "hint" }, hint) : null)
  );
}

function openMenu(anchor, app) {
  const reopen = openMenuElement?.anchor === anchor;
  closeMenu();
  if (reopen) {
    return;
  }
  const running = app.running;
  const menu = h(
    "div",
    { class: "menu", role: "menu", "aria-label": t("manager.more") },
    menuItem({
      iconName: "sync",
      label: t("manager.refresh"),
      hint: running ? t("manager.closeFirst") : t("manager.refresh.desc"),
      disabled: running,
      onPick: () => refreshApp(app),
    }),
    menuItem({
      iconName: "delete",
      label: t("manager.remove"),
      hint: running ? t("manager.closeFirst") : null,
      disabled: running,
      danger: true,
      onPick: () => confirmRemove(app),
    })
  );
  document.body.append(menu);
  const box = anchor.getBoundingClientRect();
  const width = menu.offsetWidth;
  const height = menu.offsetHeight;
  const left = Math.max(8, Math.min(window.innerWidth - width - 8, box.right - width));
  const below = box.bottom + 4 + height <= window.innerHeight - 8;
  menu.style.left = `${left}px`;
  menu.style.top = `${below ? box.bottom + 4 : Math.max(8, box.top - 4 - height)}px`;
  menu.classList.toggle("above", !below);
  anchor.setAttribute("aria-expanded", "true");
  openMenuElement = { menu, anchor };
  menu.querySelector("button:not(:disabled)")?.focus({ focusVisible: false });
}

document.addEventListener("pointerdown", event => {
  if (openMenuElement && !openMenuElement.menu.contains(event.target) && !openMenuElement.anchor.contains(event.target)) {
    closeMenu();
  }
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && openMenuElement) {
    const { anchor } = openMenuElement;
    closeMenu();
    anchor.focus();
    event.preventDefault();
  }
});
window.addEventListener("blur", closeMenu);
window.addEventListener("resize", closeMenu);

/* ------------------------------------------------------ remove dialog */

function confirmRemove(app) {
  let purge = false;
  const confirm = h("button", { class: "btn filled danger-fill", type: "button" }, t("manager.remove.confirm"));
  const cancel = h("button", { class: "btn text", type: "button" }, t("manager.remove.cancel"));
  const purgeSwitch = listToggle(t("manager.remove.purge"), false, value => {
    purge = value;
    confirm.textContent = t(purge ? "manager.remove.confirmPurge" : "manager.remove.confirm");
    purgeRow.classList.toggle("on", purge);
  });
  const purgeRow = h(
    "div",
    { class: "row purge-row" },
    h("div", { class: "lead" }, icon("delete_forever")),
    h(
      "div",
      { class: "text" },
      h("div", { class: "title" }, t("manager.remove.purge")),
      h("div", { class: "body" }, t("manager.remove.purge.desc"))
    ),
    purgeSwitch
  );
  const dialog = h(
    "dialog",
    { class: "m3-dialog", "aria-labelledby": "remove-title" },
    h("div", { class: "dialog-icon" }, appIcon(app, "dialog-app-icon")),
    h("h2", { class: "headline", id: "remove-title" }, t("manager.remove.title", { app: app.name })),
    h("p", { class: "body" }, t("manager.remove.text")),
    purgeRow,
    h("div", { class: "dialog-actions" }, cancel, confirm)
  );
  cancel.addEventListener("click", () => dialog.close());
  confirm.addEventListener("click", () => {
    dialog.close();
    act(app, async () => {
      await ZenApp.manager.remove(app.id, { purge });
      snackbar(t(purge ? "manager.purged" : "manager.removed", { app: app.name }));
    });
  });
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  cancel.focus();
}

/* ------------------------------------------------------------- views */

let listRoot = null;
let countText = null;
let renderedKey = "";

function appCard(app) {
  const busy = state.busy.has(app.id);
  const more = h(
    "button",
    {
      class: "icon-btn more",
      type: "button",
      "aria-label": t("manager.more"),
      title: t("manager.more"),
      "aria-haspopup": "menu",
      "aria-expanded": "false",
      disabled: busy,
    },
    icon("more_vert")
  );
  more.addEventListener("click", () => openMenu(more, app));
  const chips = [
    app.running ? h("span", { class: "status-chip running" }, t("manager.running")) : null,
    app.onboarded === false ? h("span", { class: "status-chip" }, icon("auto_awesome"), t("manager.notOnboarded")) : null,
  ].filter(Boolean);
  const progress = wavy();
  progress.hidden = !busy;
  return h(
    "article",
    { class: "app-card" + (app.running ? " running" : "") + (busy ? " busy" : ""), "data-app": app.id },
    h(
      "div",
      { class: "card-top" },
      appIcon(app),
      h("div", { class: "card-titles" }, h("div", { class: "name", title: app.name }, app.name), h("div", { class: "host", title: app.url }, hostOf(app.url))),
      more
    ),
    h("div", { class: "card-chips" }, chips),
    progress,
    h(
      "div",
      { class: "card-actions" },
      h(
        "button",
        { class: "btn tonal", type: "button", disabled: busy, onclick: () => openApp(app) },
        icon(app.running ? "open_in_new" : "play_arrow"),
        t("manager.open")
      ),
      h(
        "button",
        {
          class: "icon-btn",
          type: "button",
          "aria-label": t("manager.settings"),
          title: t("manager.settings"),
          disabled: busy,
          onclick: () => openAppSettings(app),
        },
        icon("settings")
      )
    )
  );
}

function emptyState() {
  const mark = h("div", { class: "empty-shape" }, shape("cookie12", 48), icon("apps"));
  return h(
    "div",
    { class: "empty" },
    mark,
    h("h2", { class: "headline" }, t("manager.empty.title")),
    h("p", { class: "body" }, t("manager.empty.text")),
    h("button", { class: "btn filled medium", type: "button", onclick: () => openWizard() }, icon("add"), t("manager.empty.cta"))
  );
}

function render() {
  if (!listRoot) {
    return;
  }
  const apps = state.apps;
  countText.textContent = !apps
    ? t("manager.subtitle")
    : apps.length === 1
      ? `${t("manager.count.one")} · ${t("manager.subtitle")}`
      : `${t("manager.count", { n: apps.length })} · ${t("manager.subtitle")}`;
  const key = JSON.stringify([apps, state.error?.code, state.error?.message, [...state.busy]]);
  if (key === renderedKey) {
    return;
  }
  renderedKey = key;
  if (state.error && !apps) {
    listRoot.replaceChildren(h("div", { class: "note error-note" }, icon("error"), h("span", {}, errorText(state.error))));
    return;
  }
  if (!apps) {
    const progress = wavy();
    progress.hidden = false;
    listRoot.replaceChildren(h("div", { class: "loading" }, progress, h("span", { class: "body" }, t("manager.loading"))));
    return;
  }
  const valid = apps.filter(app => !app.error);
  if (!valid.length) {
    listRoot.replaceChildren(emptyState());
    return;
  }
  listRoot.replaceChildren(h("section", { class: "app-grid" }, valid.map(appCard)));
}

function renderShell(root) {
  countText = h("p", { class: "body" });
  const header = h(
    "header",
    { class: "manager-head" },
    h("div", { class: "manager-mark" }, shape("cookie9", 49), icon("apps")),
    h("div", { class: "titles" }, h("h1", { class: "manager-title" }, t("manager.title")), countText),
    h("button", { class: "btn filled medium new-app", type: "button", onclick: () => openWizard() }, icon("add"), t("manager.new"))
  );
  listRoot = h("div", { class: "manager-list" });
  root.append(header, h("main", { class: "manager-body" }, listRoot));
}

/* ------------------------------------------------------------ wizard */

function openWizard() {
  closeMenu();
  window.ZenAppWizard?.open({ onClose: () => loadApps(), errorText });
}

/** Whether the list is on screen (not behind the wizard or in the background). */
function listShown() {
  return document.visibilityState === "visible" && !document.getElementById("app").hidden;
}

/* ------------------------------------------------------------- boot */

function boot() {
  ZenUI.detectSymbols();
  applyTheme();
  const root = document.getElementById("app");
  root.className = "manager";
  renderShell(root);
  render();
  loadApps();
  let timer = 0;
  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      if (listShown() && document.hasFocus() && !document.querySelector("dialog[open]")) {
        await loadApps();
      }
      schedule();
    }, REFRESH_MS);
  };
  schedule();
  window.addEventListener("focus", () => listShown() && loadApps());
  const unsubscribe = ZenApp.addListener(what => {
    if (what === "palette" || what === "settings") {
      applyTheme();
    } else if (what === "manager:new") {
      openWizard();
    }
  });
  window.addEventListener("unload", unsubscribe, { once: true });
  document.addEventListener("keydown", event => {
    if (event.key === "n" && (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey) {
      event.preventDefault();
      openWizard();
    }
  });
  if (new URLSearchParams(location.search).get("view") === "new") {
    openWizard();
  }
}

boot();
