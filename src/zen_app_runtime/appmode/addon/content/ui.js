/* zen-app-runtime app mode — UI helpers shared by the studio and Web Apps pages.
 * A classic script: it defines window.ZenUI (like shapes.js defines ZenShapes).
 */
"use strict";

(function () {
  /** Functions that re-read the model; runUpdaters() calls them all. */
  const updaters = [];

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

  function runUpdaters() {
    for (const update of updaters) {
      update();
    }
  }

  /** file:// URL of an absolute path. */
  function fileURL(path) {
    return path ? "file://" + String(path).split("/").map(encodeURIComponent).join("/") : null;
  }

  /** Appearance, colour scheme and palette variables of a studio model on <html>. */
  function applyPalette(model) {
    const root = document.documentElement;
    root.dataset.appearance = model.settings?.appearance || "transparent";
    root.dataset.scheme = model.palette && model.palette.dark === false ? "light" : "dark";
    root.lang = model.locale || "en";
    for (const [name, value] of Object.entries(model.paletteVariables || {})) {
      root.style.setProperty(name, value);
    }
  }

  /** Hide Material Symbols ligatures when the font is missing. */
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

  /** A switch inside a list that is rebuilt on change (no bind()). */
  function listToggle(label, checked, onChange) {
    const button = h(
      "button",
      { class: "switch", type: "button", role: "switch", "aria-label": label, "aria-checked": String(!!checked) },
      h("span", { class: "handle" }, icon("check"))
    );
    button.addEventListener("click", () => {
      const next = button.getAttribute("aria-checked") !== "true";
      button.setAttribute("aria-checked", String(next));
      onChange(next);
    });
    return button;
  }

  /** Indeterminate wavy progress (M3 Expressive). */
  function wavy() {
    const progress = h("div", { class: "wavy", hidden: true });
    const wave = svg("svg", { viewBox: "0 0 440 12", preserveAspectRatio: "none" });
    let d = "M2 6";
    for (let x = 2; x < 438; x += 20) {
      d += ` Q ${x + 5} 0 ${x + 10} 6 T ${x + 20} 6`;
    }
    wave.append(svg("path", { d }));
    progress.append(wave);
    return progress;
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

  window.ZenUI = Object.freeze({
    h,
    icon,
    svg,
    bind,
    runUpdaters,
    fileURL,
    applyPalette,
    detectSymbols,
    snackbar,
    segmented,
    toggle,
    listToggle,
    wavy,
    row,
  });
})();
