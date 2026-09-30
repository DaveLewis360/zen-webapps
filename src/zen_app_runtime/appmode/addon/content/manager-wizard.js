/* zen-app-runtime app mode — Web Apps: the new-app wizard.
 *
 * Source (an address or a ready-made app) → details (name, icon, colour,
 * scope, sign-ins; checked live with `zen-app new --dry-run`) → create (the
 * CLI makes the app and starts it; the app greets with its own onboarding).
 * The site is read by Manager.sys.mjs and parsed here inertly (DOMParser):
 * nothing of it runs or is put into this page except text and images.
 */
/* global ChromeUtils, ZenShapes, ZenUI */
"use strict";

(function () {
  const { ZenApp } = ChromeUtils.importESModule("resource://zenapp/modules/ZenApp.sys.mjs");
  const { t } = ChromeUtils.importESModule("resource://zenapp/modules/Strings.sys.mjs");
  const Site = ChromeUtils.importESModule("resource://zenapp/modules/SiteModel.sys.mjs");
  const { h, icon, svg, snackbar, listToggle, wavy } = ZenUI;

  /** Icon candidates downloaded per site (near-duplicates are dropped after). */
  const ICON_TRIES = 6;
  const ICON_SIZE = 256;
  const PREVIEW_DELAY_MS = 350;
  const DEFAULT_ACCENT = "#7b6cff";

  let layer = null;
  let context = {};
  /** Bumped on every open/restart: late results of an older session are dropped. */
  let session = 0;
  let state = null;
  let presets = null;
  const blobUrls = new Set();

  /* ---------------------------------------------------------- helpers */

  function errorText(error) {
    return context.errorText ? context.errorText(error) : String(error?.message || error);
  }

  function hostOf(url) {
    try {
      return new URL(url).host;
    } catch {
      return "";
    }
  }

  function trackBlob(blob) {
    const url = URL.createObjectURL(blob);
    blobUrls.add(url);
    return url;
  }

  function releaseBlobs() {
    for (const url of blobUrls) {
      URL.revokeObjectURL(url);
    }
    blobUrls.clear();
  }

  function shapeSvg(name, radius = 48) {
    const element = svg("svg", { viewBox: "0 0 100 100", "aria-hidden": "true" });
    element.append(svg("path", { d: ZenShapes.toPath(ZenShapes.radii(name), 50, 50, radius) }));
    return element;
  }

  let colorContext = null;
  /** #rrggbb of any CSS colour text (via canvas), or null. */
  function cssHex(value) {
    colorContext ??= document.createElement("canvas").getContext("2d");
    const sentinel = "#010203";
    colorContext.fillStyle = sentinel;
    colorContext.fillStyle = String(value || "");
    const result = colorContext.fillStyle;
    return result === sentinel && String(value).trim().toLowerCase() !== sentinel ? null : Site.hexColor(result);
  }

  function contrastText(hex) {
    const [r, g, b] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255);
    const channel = v => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b) > 0.42 ? "#1b1b1f" : "#ffffff";
  }

  /** The letter icon the CLI generates, drawn the same way. */
  function letterIcon(name, accent) {
    const element = svg("svg", { viewBox: "0 0 100 100", class: "letter-icon", "aria-hidden": "true" });
    element.append(svg("path", { d: ZenShapes.toPath(ZenShapes.radii("cookie9"), 50, 50, 46), fill: accent }));
    const text = svg("text", {
      x: "50",
      y: "50",
      dy: "0.35em",
      "text-anchor": "middle",
      fill: contrastText(accent),
      "font-family": "Google Sans Flex, Google Sans, Roboto Flex, Roboto, sans-serif",
      "font-size": "46",
      "font-weight": "700",
    });
    // The first character (not UTF-16 unit), as the CLI's name.strip()[:1].
    text.textContent = ([...name.trim()][0] || "?").toUpperCase();
    element.append(text);
    return element;
  }

  /** What the page says about itself (text and addresses only). */
  function readFacts(html, pageUrl) {
    const doc = new DOMParser().parseFromString(String(html || ""), "text/html");
    let base = pageUrl;
    const baseHref = doc.querySelector("base[href]")?.getAttribute("href");
    if (baseHref) {
      try {
        base = new URL(baseHref, pageUrl).href;
      } catch {
        base = pageUrl;
      }
    }
    const absolute = href => {
      try {
        return new URL(href, base).href;
      } catch {
        return null;
      }
    };
    const meta = {};
    for (const element of doc.querySelectorAll("meta[name], meta[property]")) {
      const key = (element.getAttribute("name") || element.getAttribute("property") || "").toLowerCase();
      if (key && !(key in meta)) {
        meta[key] = element.getAttribute("content") || "";
      }
    }
    const links = [...doc.querySelectorAll("link[rel][href]")].map(link => ({
      rel: (link.getAttribute("rel") || "").toLowerCase().split(/\s+/).filter(Boolean),
      href: absolute(link.getAttribute("href")),
      sizes: link.getAttribute("sizes") || "",
      type: link.getAttribute("type") || "",
    }));
    const manifestUrl = links.find(link => link.rel.includes("manifest") && link.href?.startsWith("https://"))?.href || null;
    const themeColors = [...doc.querySelectorAll("meta[name='theme-color' i]")]
      .map(element => cssHex(element.getAttribute("content")))
      .filter(Boolean);
    return { title: doc.title || "", meta, links, manifestUrl, themeColors };
  }

  function rgbHue(r, g, b) {
    const max = Math.max(r, g, b);
    const min = Math.min(r, g, b);
    if (max === min) {
      return 0;
    }
    const d = max - min;
    const hue = max === r ? (g - b) / d + (g < b ? 6 : 0) : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
    return hue * 60;
  }

  /** The icon's main colour (of its saturated pixels), or null. */
  function dominantColor(context2d, size) {
    const data = context2d.getImageData(0, 0, size, size).data;
    const buckets = new Map();
    for (let i = 0; i < data.length; i += 16) {
      const [r, g, b, a] = [data[i], data[i + 1], data[i + 2], data[i + 3]];
      if (a < 200) {
        continue;
      }
      const max = Math.max(r, g, b) / 255;
      const min = Math.min(r, g, b) / 255;
      const lightness = (max + min) / 2;
      const saturation = max === min ? 0 : (max - min) / (1 - Math.abs(2 * lightness - 1));
      if (saturation < 0.3 || lightness < 0.15 || lightness > 0.85) {
        continue;
      }
      const key = Math.round(rgbHue(r, g, b) / 30) % 12;
      const bucket = buckets.get(key) || { n: 0, r: 0, g: 0, b: 0 };
      bucket.n++;
      bucket.r += r;
      bucket.g += g;
      bucket.b += b;
      buckets.set(key, bucket);
    }
    let best = null;
    for (const bucket of buckets.values()) {
      if (!best || bucket.n > best.n) {
        best = bucket;
      }
    }
    if (!best || best.n < 20) {
      return null;
    }
    return "#" + [best.r, best.g, best.b].map(v => Math.round(v / best.n).toString(16).padStart(2, "0")).join("");
  }

  /** A 16×16 average hash (on white) and the mean colour: near-identical icons look alike. */
  function fingerprint(source) {
    const small = document.createElement("canvas");
    small.width = small.height = 16;
    const context2d = small.getContext("2d");
    context2d.drawImage(source, 0, 0, 16, 16);
    const data = context2d.getImageData(0, 0, 16, 16).data;
    const values = [];
    const sum = [0, 0, 0];
    for (let i = 0; i < data.length; i += 4) {
      const alpha = data[i + 3] / 255;
      const rgb = [data[i], data[i + 1], data[i + 2]].map(v => v * alpha + 255 * (1 - alpha));
      rgb.forEach((v, c) => (sum[c] += v));
      values.push(0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]);
    }
    const mean = values.reduce((total, v) => total + v, 0) / values.length;
    return { bits: values.map(v => v > mean), color: sum.map(v => v / values.length) };
  }

  function sameImage(a, b) {
    let differing = 0;
    a.bits.forEach((bit, index) => (differing += bit === b.bits[index] ? 0 : 1));
    return differing <= 16 && Math.hypot(...a.color.map((v, c) => v - b.color[c])) < 32;
  }

  /** Decode an image (PNG, JPEG, WebP, GIF, ICO, SVG) and make it a 256 px PNG. */
  async function toPng(bytes, type) {
    const source = URL.createObjectURL(new Blob([bytes], { type: type || "application/octet-stream" }));
    try {
      const image = new Image();
      image.src = source;
      await image.decode();
      const vector = /svg/i.test(type || "");
      const width = image.naturalWidth || ICON_SIZE;
      const height = image.naturalHeight || ICON_SIZE;
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = ICON_SIZE;
      const context2d = canvas.getContext("2d");
      context2d.imageSmoothingEnabled = true;
      context2d.imageSmoothingQuality = "high";
      const scale = Math.min(ICON_SIZE / width, ICON_SIZE / height);
      const w = Math.round(width * scale);
      const hh = Math.round(height * scale);
      context2d.drawImage(image, (ICON_SIZE - w) / 2, (ICON_SIZE - hh) / 2, w, hh);
      const blob = await new Promise((resolve, reject) =>
        canvas.toBlob(result => (result ? resolve(result) : reject(new Error("cannot encode"))), "image/png")
      );
      return {
        png: new Uint8Array(await blob.arrayBuffer()),
        blobUrl: trackBlob(blob),
        natural: Math.min(width, height),
        vector,
        color: dominantColor(context2d, ICON_SIZE),
        print: fingerprint(canvas),
      };
    } finally {
      URL.revokeObjectURL(source);
    }
  }

  /** Hosts typed by the user: "https://x.com/path, *.y.com" → ["x.com", "*.y.com"]. */
  function parseHosts(text) {
    const hosts = String(text || "")
      .toLowerCase()
      .split(/[\s,;]+/)
      .map(value => value.replace(/^[a-z][a-z0-9+.-]*:\/\//, "").split(/[/?#]/)[0].replace(/:\d+$/, "").replace(/\.$/, ""))
      .filter(Boolean);
    return [...new Set(hosts)];
  }

  /** The letter icon as a 256 px PNG (a ready-made app keeps its own icon without one). */
  function letterPng(name, accent) {
    const element = letterIcon(name, accent);
    element.setAttribute("width", String(ICON_SIZE));
    element.setAttribute("height", String(ICON_SIZE));
    return toPng(new XMLSerializer().serializeToString(element), "image/svg+xml");
  }

  /* ------------------------------------------------------------ state */

  function freshChoice() {
    return {
      name: "",
      icon: "letter",
      accent: DEFAULT_ACCENT,
      wholeDomain: false,
      signIns: new Set(),
      id: "",
      hosts: "",
      appearance: "auto",
    };
  }

  function freshState() {
    return {
      step: "source",
      address: "",
      addressError: "",
      source: null,
      inspect: "idle",
      inspectError: null,
      site: null,
      siteColors: [],
      icons: [],
      upload: null,
      colors: [DEFAULT_ACCENT],
      scope: { wholeDomain: null, wholeDomainDefault: false, extraHosts: [] },
      detected: [],
      choice: freshChoice(),
      touched: {},
      preview: null,
      previewError: null,
      previewPending: false,
      createError: null,
      created: null,
      ui: {},
    };
  }

  /* ------------------------------------------------------------- flow */

  function onKey(event) {
    if (!layer || event.key !== "Escape" || event.defaultPrevented || document.querySelector("dialog[open]")) {
      return;
    }
    event.preventDefault();
    if (state.step === "details") {
      backToSource();
    } else if (state.step !== "creating") {
      close();
    }
  }

  function open(options = {}) {
    context = options;
    if (layer) {
      layer.querySelector("input")?.focus();
      return;
    }
    session++;
    state = freshState();
    layer = h("section", { class: "wizard", role: "dialog", "aria-modal": "true", "aria-label": t("wizard.title") });
    document.addEventListener("keydown", onKey);
    const app = document.getElementById("app");
    app.hidden = true;
    app.inert = true;
    document.body.append(layer);
    render();
    loadPresets();
  }

  function close() {
    if (!layer) {
      return;
    }
    session++;
    clearTimeout(previewTimer);
    document.removeEventListener("keydown", onKey);
    layer.remove();
    layer = null;
    releaseBlobs();
    const app = document.getElementById("app");
    app.hidden = false;
    app.inert = false;
    app.querySelector(".new-app")?.focus({ focusVisible: false });
    context.onClose?.();
  }

  function restart() {
    session++;
    releaseBlobs();
    state = freshState();
    render();
    loadPresets();
  }

  function backToSource() {
    session++;
    state.step = "source";
    state.preview = null;
    state.previewError = null;
    render();
    loadPresets();
  }

  async function loadPresets() {
    const mine = session;
    if (!presets) {
      try {
        presets = await ZenApp.manager.presets();
      } catch (error) {
        console.error(error);
        presets = [];
      }
    }
    if (mine === session) {
      renderPresets();
    }
  }

  function startDetails(source) {
    session++;
    releaseBlobs();
    const address = state.address;
    state = freshState();
    state.address = address;
    state.source = source;
    state.step = "details";
  }

  function submitAddress() {
    const url = Site.normalizeAddress(state.address);
    if (!url) {
      state.addressError = t("wizard.source.invalid");
      render();
      return;
    }
    startDetails({ kind: "url", url });
    state.inspect = "loading";
    // The CLI's own name for the site until the page tells a better one.
    state.choice.name = Site.nameFromHost(new URL(url).hostname);
    render();
    inspect(url);
    schedulePreview();
  }

  function choosePreset(preset) {
    startDetails({ kind: "preset", preset });
    state.inspect = "preset";
    state.choice.name = preset.name;
    state.choice.icon = preset.icon ? "preset" : "letter";
    // The preset's own colour stays the first choice (an uploaded icon adds its own).
    state.siteColors = [Site.hexColor(preset.accent) || DEFAULT_ACCENT];
    recomputeColors();
    render();
    schedulePreview();
  }

  function recomputeColors() {
    const values = [...state.siteColors, ...state.icons.map(image => image.color), state.upload?.color].filter(Boolean);
    state.colors = Site.colorChoices(values, DEFAULT_ACCENT);
    // A ready-made app keeps its own colour (even a grey one) until another is picked.
    const own = state.source?.kind === "preset" ? state.siteColors[0] : null;
    if (own && !state.colors.includes(own)) {
      state.colors = [own, ...state.colors].slice(0, 5);
    }
    if (!state.touched.accent) {
      state.choice.accent = own || state.colors[0];
    }
  }

  async function inspect(url) {
    const mine = session;
    const stale = () => mine !== session;
    let site;
    try {
      site = await ZenApp.manager.fetchPage(url);
    } catch (error) {
      if (stale()) {
        return;
      }
      state.inspect = "failed";
      state.inspectError = error;
      updateDetails();
      schedulePreview();
      return;
    }
    if (stale()) {
      return;
    }
    state.site = site;
    const facts = readFacts(site.html, site.url);
    const manifest = facts.manifestUrl ? await ZenApp.manager.fetchManifest(facts.manifestUrl) : null;
    if (stale()) {
      return;
    }
    state.detected = Site.detectSignIns(site.html);
    state.scope = Site.scopeSuggestion({
      startHost: site.start.host,
      finalHost: site.final.host,
      baseDomain: site.final.baseDomain || site.start.baseDomain,
    });
    if (!state.touched.name) {
      state.choice.name = Site.suggestName({ manifest, meta: facts.meta, title: facts.title, host: site.final.host });
    }
    if (!state.touched.scope) {
      state.choice.wholeDomain = state.scope.wholeDomainDefault;
    }
    if (!state.touched.signIns) {
      state.choice.signIns = new Set(state.detected);
    }
    state.siteColors = [...facts.themeColors, cssHex(manifest?.theme_color), cssHex(manifest?.background_color)].filter(Boolean);
    recomputeColors();
    state.inspect = "icons";
    updateDetails();
    schedulePreview();

    const candidates = Site.iconCandidates({ manifest, manifestUrl: facts.manifestUrl, links: facts.links, pageUrl: site.url }).slice(0, ICON_TRIES);
    const images = await Promise.all(
      candidates.map(async candidate => {
        try {
          const { bytes, type } = await ZenApp.manager.fetchImage(candidate.src);
          const image = await toPng(bytes, type);
          const size = candidate.size === Infinity ? Infinity : Math.max(candidate.size || 0, image.natural);
          return { ...candidate, ...image, small: !image.vector && size !== Infinity && image.natural < 96 };
        } catch (error) {
          console.warn(`zen-app: icon ${candidate.src}: ${error?.message || error}`);
          return null;
        }
      })
    );
    if (stale()) {
      return;
    }
    // Best first; the same picture in another size or format is shown once.
    state.icons = [];
    for (const image of images) {
      if (image && !state.icons.some(other => sameImage(other.print, image.print))) {
        state.icons.push(image);
      }
    }
    recomputeColors();
    if (!state.touched.icon) {
      const best = state.icons.findIndex(image => !image.small);
      state.choice.icon = best >= 0 ? best : "letter";
    }
    state.inspect = "done";
    updateDetails();
    schedulePreview();
  }

  async function uploadIcon() {
    const mine = session;
    try {
      const picked = await ZenApp.manager.pickIconFile();
      if (!picked || mine !== session) {
        return;
      }
      const image = await toPng(picked.bytes, picked.type);
      if (mine !== session) {
        return;
      }
      state.upload = image;
      state.choice.icon = "upload";
      state.touched.icon = true;
      recomputeColors();
      updateDetails();
    } catch (error) {
      console.error(error);
      snackbar(errorText(error?.code ? error : { code: "badIcon" }));
    }
  }

  /** What `zen-app new` gets. For a check (not the creation) the id is left for the CLI to judge. */
  function cliChoices(forCreate) {
    const choice = state.choice;
    const preset = state.source.kind === "preset";
    const hosts = [];
    if (!preset) {
      if (choice.wholeDomain && state.scope.wholeDomain) {
        hosts.push(state.scope.wholeDomain);
      }
      hosts.push(...state.scope.extraHosts);
    }
    hosts.push(...parseHosts(choice.hosts));
    const startHost = preset ? null : new URL(state.source.url).hostname;
    const name = choice.name.replace(/[\u0000-\u001f\u007f]+/g, " ").replace(/\s+/g, " ").trim();
    // A new site's app is named after itself ("GitHub" → zen-app-github); a
    // ready-made app keeps its preset's id (window rules may refer to it).
    let id = choice.id || (preset ? "" : Site.idFromName(name)) || undefined;
    if (forCreate && !choice.id && state.preview?.idTaken && state.preview.suggestedId) {
      id = state.preview.suggestedId;
    }
    return {
      source: preset ? state.source.preset.key : state.source.url,
      id,
      name: name || undefined,
      hosts: [...new Set(hosts)],
      authHosts: preset ? [] : Site.signInHosts([...choice.signIns], [startHost, ...hosts]),
      accent: choice.accent,
      appearance: choice.appearance === "auto" ? undefined : choice.appearance,
    };
  }

  /*
   * Every change is checked with `zen-app new --dry-run` after a short pause.
   * Only the newest check counts: `previewSeq` is bumped on each change, and a
   * result that arrives for an older one is dropped (the check stays pending).
   */
  let previewTimer = 0;
  let previewSeq = 0;
  let previewRun = null;

  function schedulePreview({ keepCreateError = false } = {}) {
    clearTimeout(previewTimer);
    const seq = ++previewSeq;
    state.previewPending = true;
    if (!keepCreateError) {
      state.createError = null;
    }
    updateLook();
    previewTimer = setTimeout(() => runPreview(seq), PREVIEW_DELAY_MS);
  }

  /** Run check `seq` now (or join it when it already runs). */
  function runPreview(seq) {
    if (previewRun?.seq === seq) {
      return previewRun.promise;
    }
    clearTimeout(previewTimer);
    const mine = session;
    const promise = (async () => {
      if (!state || state.step !== "details" || seq !== previewSeq) {
        return;
      }
      let preview = null;
      let failure = null;
      try {
        const choices = cliChoices(false);
        preview = await ZenApp.manager.dryRun(choices);
        if (preview?.idTaken && state.choice.id) {
          // An id given by hand is not replaced: it has to be changed.
          failure = { code: "exists", message: state.choice.id };
        }
      } catch (error) {
        failure = error;
      }
      if (mine !== session || seq !== previewSeq || state.step !== "details") {
        return;
      }
      // A failed check keeps the last good preview (its look is still shown).
      if (preview) {
        state.preview = preview;
      }
      state.previewError = failure;
      state.previewPending = false;
      updateDetails();
    })();
    previewRun = { seq, promise };
    return promise;
  }

  async function iconPath() {
    const choice = state.choice.icon;
    let image = choice === "upload" ? state.upload : Number.isInteger(choice) ? state.icons[choice] : null;
    if (!image && choice === "letter" && state.source.kind === "preset" && state.source.preset.icon) {
      image = await letterPng(state.choice.name || state.preview?.app?.name || state.source.preset.name, state.choice.accent);
    }
    // Without --icon the CLI draws the letter icon itself (as SVG), or uses the preset's.
    return image ? ZenApp.manager.saveIcon(image.png) : undefined;
  }

  let creating = false;

  async function createApp() {
    if (creating || state.step !== "details") {
      return;
    }
    creating = true;
    const mine = session;
    try {
      if (state.previewPending) {
        // Clicked right after a change: check it now instead of after the pause.
        const seq = previewSeq;
        state.ui.create?.classList.add("waiting");
        await runPreview(seq);
        state.ui.create?.classList.remove("waiting");
        if (mine !== session || seq !== previewSeq || state.previewPending) {
          return;
        }
      }
      if (!state.preview || state.previewError) {
        updateLook();
        return;
      }
      const choices = cliChoices(true);
      const name = choices.name || state.preview.app.name;
      state.step = "creating";
      state.creatingName = name;
      render();
      try {
        const icon = await iconPath();
        if (icon) {
          choices.icon = icon;
        }
        const result = await ZenApp.manager.create(choices);
        if (mine !== session) {
          return;
        }
        presets = null; // their "already added" marks changed
        state.created = result;
        state.step = "done";
        render();
      } catch (error) {
        console.error(error);
        if (mine !== session) {
          return;
        }
        state.step = "details";
        state.createError = error;
        render();
        snackbar(errorText(error));
        // Re-check (the id may have been taken meanwhile); the error stays until a change.
        schedulePreview({ keepCreateError: true });
      }
    } finally {
      creating = false;
    }
  }

  /* ------------------------------------------------------------ views */

  function field(label, control, ...notes) {
    return h("div", { class: "field" }, h("div", { class: "field-label" }, label), control, notes.filter(Boolean));
  }

  function note(text) {
    return h("div", { class: "field-note" }, text);
  }

  function stepPills() {
    const order = ["source", "details", "done"];
    const current = state.step === "creating" ? 1 : order.indexOf(state.step);
    return h(
      "div",
      { class: "pills", "aria-hidden": "true" },
      order.map((_, index) => h("span", { class: index === current ? "active" : index < current ? "done" : "" }))
    );
  }

  function render() {
    if (!layer) {
      return;
    }
    const details = state.step === "details";
    const bar = h(
      "header",
      { class: "wizard-bar" },
      h(
        "button",
        {
          class: "icon-btn",
          type: "button",
          "aria-label": details ? t("wizard.back") : t("wizard.close"),
          title: details ? t("wizard.back") : t("wizard.close"),
          disabled: state.step === "creating",
          onclick: () => (details ? backToSource() : close()),
        },
        icon(details ? "arrow_back" : "close")
      ),
      h("div", { class: "wizard-title" }, t("wizard.title")),
      stepPills()
    );
    const content =
      state.step === "source" ? sourceStep() : details ? detailsStep() : state.step === "creating" ? creatingStep() : doneStep();
    const body = h("div", { class: "wizard-body" }, h("div", { class: "wizard-inner" }, content));
    layer.replaceChildren(bar, body, details ? detailsActions() : "");
    if (details) {
      updateDetails();
    }
    const focus = layer.querySelector(
      state.step === "source" ? ".source-input" : details ? ".details-form input" : state.step === "done" ? ".done-actions .btn.filled" : ".btn"
    );
    focus?.focus({ focusVisible: false });
  }

  function sourceStep() {
    const input = h("input", {
      class: "source-input",
      type: "text",
      inputmode: "url",
      placeholder: t("wizard.source.placeholder"),
      "aria-label": t("wizard.source.title"),
      spellcheck: "false",
      autocomplete: "off",
    });
    input.value = state.address;
    const errorLine = h("div", { class: "field-error", role: "alert" }, state.addressError);
    input.addEventListener("input", () => {
      state.address = input.value;
      if (state.addressError) {
        state.addressError = "";
        errorLine.textContent = "";
      }
    });
    input.addEventListener("keydown", event => {
      if (event.key === "Enter") {
        event.preventDefault();
        submitAddress();
      }
    });
    const grid = h("div", { class: "preset-grid" });
    state.ui = { presetGrid: grid };
    renderPresets();
    return h(
      "div",
      { class: "source-step" },
      h("div", { class: "step-head" }, h("h2", { class: "headline" }, t("wizard.source.title")), h("p", { class: "body" }, t("wizard.source.text"))),
      h(
        "div",
        {
          class: "source-field",
          onclick: event => {
            if (!event.target.closest("button, input")) {
              input.focus();
            }
          },
        },
        icon("language"),
        input,
        h("button", { class: "btn filled", type: "button", onclick: () => submitAddress() }, t("wizard.source.next"), icon("arrow_forward"))
      ),
      errorLine,
      h("h3", { class: "section-label" }, t("wizard.presets")),
      grid
    );
  }

  function renderPresets() {
    const grid = state?.ui?.presetGrid;
    if (!grid) {
      return;
    }
    if (!presets) {
      const progress = wavy();
      progress.hidden = false;
      grid.replaceChildren(progress);
      return;
    }
    grid.replaceChildren(
      ...presets.map(preset => {
        const url = ZenUI.fileURL(preset.icon);
        const installed = preset.installed?.length > 0;
        return h(
          "button",
          { class: "preset-tile", type: "button", "data-preset": preset.key, onclick: () => choosePreset(preset) },
          url ? h("img", { src: url, alt: "" }) : letterIcon(preset.name, Site.hexColor(preset.accent) || DEFAULT_ACCENT),
          h("span", { class: "preset-name" }, preset.name),
          installed ? h("span", { class: "preset-installed" }, icon("check"), t("wizard.preset.installed")) : null
        );
      })
    );
  }

  function detailsStep() {
    const ui = {};
    ui.previewIcon = h("div", { class: "preview-icon" });
    ui.previewName = h("div", { class: "preview-name" });
    ui.previewHost = h("div", { class: "preview-host" });
    ui.previewId = h("div", { class: "preview-id" });
    const card = h("aside", { class: "preview-card" }, ui.previewIcon, ui.previewName, ui.previewHost, ui.previewId);
    ui.previewCard = card;

    ui.inspectNote = h("div", { class: "inspect-note" });
    const name = h("input", { class: "text-field", type: "text", maxlength: "80", "aria-label": t("wizard.name") });
    name.value = state.choice.name;
    name.addEventListener("input", () => {
      state.choice.name = name.value;
      state.touched.name = true;
      updatePreviewCard();
      updateIcons();
      schedulePreview();
    });
    ui.name = name;
    ui.icons = h("div", { class: "icon-choices", role: "radiogroup", "aria-label": t("wizard.icon") });
    ui.colors = h("div", { class: "swatches", role: "radiogroup", "aria-label": t("wizard.color") });
    ui.scope = h("div", { class: "field" });
    ui.signIns = h("div", { class: "field" });
    ui.look = h("div", { class: "look-row" });

    const id = h("input", { class: "text-field", type: "text", spellcheck: "false", maxlength: "64", "aria-label": t("wizard.id") });
    id.value = state.choice.id;
    id.addEventListener("input", () => {
      state.choice.id = id.value.trim().toLowerCase();
      state.touched.id = true;
      schedulePreview();
    });
    ui.id = id;
    ui.idNote = note("");
    const hosts = h("input", { class: "text-field", type: "text", spellcheck: "false", placeholder: "*.example-cdn.com", "aria-label": t("wizard.hosts") });
    hosts.value = state.choice.hosts;
    const hostsNote = note(t("wizard.hosts.desc"));
    const checkHosts = () => {
      const bad = parseHosts(hosts.value).filter(host => !Site.isHostPattern(host));
      hostsNote.textContent = bad.length ? t("wizard.hosts.invalid", { hosts: bad.join(", ") }) : t("wizard.hosts.desc");
      hostsNote.classList.toggle("bad", bad.length > 0);
    };
    checkHosts();
    hosts.addEventListener("input", () => {
      state.choice.hosts = hosts.value;
      checkHosts();
      schedulePreview();
    });
    const appearance = h("div", { class: "seg", role: "radiogroup", "aria-label": t("wizard.appearance") });
    for (const value of ["auto", "transparent", "opaque"]) {
      const button = h(
        "button",
        {
          type: "button",
          role: "radio",
          "aria-checked": String(state.choice.appearance === value),
          "aria-pressed": String(state.choice.appearance === value),
          onclick: () => {
            state.choice.appearance = value;
            for (const other of appearance.children) {
              const on = other === button;
              other.setAttribute("aria-checked", String(on));
              other.setAttribute("aria-pressed", String(on));
            }
            schedulePreview();
          },
        },
        icon("check"),
        t(`wizard.appearance.${value}`)
      );
      appearance.append(button);
    }
    const advanced = h(
      "details",
      { class: "advanced" },
      h("summary", {}, icon("tune"), h("span", {}, t("wizard.advanced"))),
      h(
        "div",
        { class: "advanced-body" },
        field(t("wizard.id"), id, ui.idNote),
        field(t("wizard.hosts"), hosts, hostsNote),
        field(t("wizard.appearance"), appearance)
      )
    );
    ui.advanced = advanced;
    ui.takenNote = h("div", { class: "taken-note" });
    state.ui = ui;
    const form = h(
      "div",
      { class: "details-form" },
      ui.inspectNote,
      ui.takenNote,
      field(t("wizard.name"), name),
      field(t("wizard.icon"), ui.icons),
      field(t("wizard.color"), ui.colors),
      ui.scope,
      ui.signIns,
      field(t("wizard.look"), ui.look),
      advanced
    );
    // Enter in a text field creates the app (after the pending check).
    form.addEventListener("keydown", event => {
      if (event.key === "Enter" && !event.isComposing && event.target.matches("input.text-field")) {
        event.preventDefault();
        createApp();
      }
    });
    return h(
      "div",
      { class: "details-step" },
      h("div", { class: "step-head" }, h("h2", { class: "headline" }, t("wizard.details.title")), h("p", { class: "body" }, t("wizard.details.text"))),
      h("div", { class: "details-grid" }, card, form)
    );
  }

  function detailsActions() {
    const create = h("button", { class: "btn filled medium create", type: "button", onclick: () => createApp() }, icon("rocket_launch"), t("wizard.create"));
    state.ui.create = create;
    state.ui.error = h("div", { class: "wizard-error", role: "alert" });
    return h(
      "footer",
      { class: "wizard-actions" },
      h("button", { class: "btn text", type: "button", onclick: () => backToSource() }, icon("arrow_back"), t("wizard.back")),
      state.ui.error,
      create
    );
  }

  function selectedIconElement(className) {
    const choice = state.choice.icon;
    const name = state.choice.name || state.preview?.app?.name || hostOf(state.source?.url) || "?";
    if (choice === "preset" && state.source?.preset?.icon) {
      return h("img", { class: className, src: ZenUI.fileURL(state.source.preset.icon), alt: "" });
    }
    const image = choice === "upload" ? state.upload : Number.isInteger(choice) ? state.icons[choice] : null;
    if (image) {
      return h("img", { class: className, src: image.blobUrl, alt: "" });
    }
    const letter = letterIcon(name, state.choice.accent);
    letter.classList.add(className);
    return letter;
  }

  function updatePreviewCard() {
    const ui = state.ui;
    if (!ui.previewIcon) {
      return;
    }
    ui.previewIcon.replaceChildren(selectedIconElement("big-icon"));
    ui.previewCard?.style.setProperty("--accent", state.choice.accent);
    ui.previewName.textContent = state.choice.name.trim() || state.preview?.app?.name || "…";
    const url = state.source.kind === "preset" ? state.source.preset.url : state.source.url;
    ui.previewHost.textContent = hostOf(url);
    const id = state.choice.id || (state.preview?.idTaken ? state.preview.suggestedId : state.preview?.app?.id);
    ui.previewId.textContent = id ? `zen-app-${id}` : "";
  }

  function updateIcons() {
    const ui = state.ui;
    if (!ui.icons) {
      return;
    }
    const tile = (key, content, label, extraClass = "") =>
      h(
        "button",
        {
          class: `icon-choice ${extraClass}`.trim(),
          type: "button",
          role: "radio",
          "aria-checked": String(state.choice.icon === key),
          "aria-label": label,
          title: label,
          onclick: () => {
            state.choice.icon = key;
            state.touched.icon = true;
            updatePreviewCard();
            updateIcons();
          },
        },
        content
      );
    const tiles = [];
    if (state.source.kind === "preset" && state.source.preset.icon) {
      tiles.push(tile("preset", h("img", { src: ZenUI.fileURL(state.source.preset.icon), alt: "" }), state.source.preset.name));
    }
    state.icons.forEach((image, index) => {
      const label = image.small ? `${hostOf(image.src)} · ${t("wizard.icon.small")}` : hostOf(image.src);
      tiles.push(tile(index, h("img", { src: image.blobUrl, alt: "" }), label, image.small ? "small" : ""));
    });
    if (state.upload) {
      tiles.push(tile("upload", h("img", { src: state.upload.blobUrl, alt: "" }), t("wizard.icon.upload")));
    }
    const name = state.choice.name || state.preview?.app?.name || hostOf(state.source.url) || "?";
    tiles.push(tile("letter", letterIcon(name, state.choice.accent), t("wizard.icon.letter")));
    if (state.inspect === "loading" || state.inspect === "icons") {
      const progress = wavy();
      progress.hidden = false;
      tiles.push(h("div", { class: "icon-choice loading", "aria-hidden": "true" }, progress));
    }
    tiles.push(
      h(
        "button",
        { class: "icon-choice upload", type: "button", "aria-label": t("wizard.icon.upload"), title: t("wizard.icon.upload"), onclick: () => uploadIcon() },
        icon("upload")
      )
    );
    ui.icons.replaceChildren(...tiles);
  }

  function pickColor(hex) {
    const value = Site.hexColor(hex);
    if (!value) {
      return;
    }
    state.choice.accent = value;
    state.touched.accent = true;
    if (!state.colors.includes(value)) {
      state.colors = [...state.colors, value].slice(-6);
    }
    updateColors();
    updatePreviewCard();
    updateIcons();
    schedulePreview();
  }

  function updateColors() {
    const ui = state.ui;
    if (!ui.colors) {
      return;
    }
    const swatches = state.colors.map(hex =>
      h(
        "button",
        {
          class: "swatch",
          type: "button",
          role: "radio",
          "aria-checked": String(hex === state.choice.accent),
          "aria-label": hex,
          title: hex,
          style: `--swatch: ${hex}; --swatch-on: ${contrastText(hex)}`,
          onclick: () => pickColor(hex),
        },
        icon("check")
      )
    );
    const custom = h("input", { type: "color", "aria-label": t("wizard.color.custom"), title: t("wizard.color.custom") });
    custom.value = state.choice.accent;
    custom.addEventListener("change", () => pickColor(custom.value));
    ui.colors.replaceChildren(...swatches, h("label", { class: "swatch custom", title: t("wizard.color.custom") }, icon("colorize"), custom));
  }

  function updateScope() {
    const ui = state.ui;
    if (!ui.scope) {
      return;
    }
    if (state.source.kind === "preset") {
      ui.scope.replaceChildren(h("div", { class: "note" }, icon("verified"), h("span", {}, t("wizard.preset.scope"))));
      return;
    }
    const domain = state.scope.wholeDomain;
    if (!domain) {
      ui.scope.replaceChildren();
      return;
    }
    const base = domain.slice(2);
    const host = hostOf(state.source.url);
    const title = h("div", { class: "title" }, state.choice.wholeDomain ? t("wizard.scope.domain", { domain: base }) : t("wizard.scope.only", { host }));
    const toggle = listToggle(t("wizard.scope.domain", { domain: base }), state.choice.wholeDomain, value => {
      state.choice.wholeDomain = value;
      state.touched.scope = true;
      title.textContent = value ? t("wizard.scope.domain", { domain: base }) : t("wizard.scope.only", { host });
      row.classList.toggle("on", value);
      updateSignIns();
      schedulePreview();
    });
    const row = h(
      "div",
      { class: "row scope-row" + (state.choice.wholeDomain ? " on" : "") },
      h("div", { class: "lead" }, icon("domain")),
      h("div", { class: "text" }, title, h("div", { class: "body" }, t("wizard.scope.domain.desc"))),
      toggle
    );
    ui.scope.replaceChildren(h("div", { class: "field-label" }, t("wizard.scope")), row);
  }

  function updateSignIns() {
    const ui = state.ui;
    if (!ui.signIns) {
      return;
    }
    if (state.source.kind === "preset") {
      ui.signIns.replaceChildren();
      return;
    }
    const chips = Site.SIGN_IN_PROVIDERS.map(provider => {
      const on = state.choice.signIns.has(provider.key);
      return h(
        "button",
        {
          class: "filter-chip",
          type: "button",
          "aria-pressed": String(on),
          "data-provider": provider.key,
          onclick: () => {
            if (state.choice.signIns.has(provider.key)) {
              state.choice.signIns.delete(provider.key);
            } else {
              state.choice.signIns.add(provider.key);
            }
            state.touched.signIns = true;
            updateSignIns();
            schedulePreview();
          },
        },
        icon(on ? "check" : "add"),
        provider.label,
        state.detected.includes(provider.key) ? h("span", { class: "chip-note" }, t("wizard.signins.detected")) : null
      );
    });
    ui.signIns.replaceChildren(h("div", { class: "field-label" }, t("wizard.signins")), h("div", { class: "chips-select" }, chips), note(t("wizard.signins.desc")));
  }

  function updateLook() {
    const ui = state?.ui;
    if (!ui?.look) {
      return;
    }
    const preview = state.preview;
    let text;
    let iconName;
    if (preview) {
      // The styles of the app's own site (not of the sign-in pages it shows).
      const authHosts = new Set(preview.app.authHosts || []);
      const all = preview.styles || [];
      const own = all.filter(style => (style.hosts || []).some(host => !authHosts.has(host)));
      const styles = (own.length ? own : all).map(style => style.site.replace(/\.css$/, "").replace(/^[+-]/, ""));
      const auto = state.choice.appearance === "auto";
      if (preview.app.appearance === "transparent") {
        text = styles.length && auto ? t("wizard.look.transparent", { styles: styles.join(", ") }) : t("wizard.look.transparentPlain");
        iconName = "blur_on";
      } else {
        text = auto ? t("wizard.look.opaque") : t("wizard.look.opaquePlain");
        iconName = "rectangle";
      }
    } else if (state.previewPending) {
      text = t("wizard.look.checking");
      iconName = "hourglass_top";
    } else {
      text = t("wizard.look.unknown");
      iconName = "help";
    }
    ui.look.replaceChildren(icon(iconName), h("span", {}, text));
    ui.look.classList.toggle("pending", state.previewPending);

    // The check's (or the creation's) error, next to the Create button.
    const failure = state.createError || (state.previewPending ? null : state.previewError);
    if (ui.error) {
      const message = failure ? errorText(failure) : "";
      if (ui.error.textContent !== message) {
        ui.error.textContent = message;
      }
    }
    if (ui.create) {
      // While a check runs Create stays clickable: it waits for the check.
      ui.create.disabled = !state.previewPending && (!preview || !!state.previewError);
    }
    const taken = preview?.idTaken && !state.choice.id;
    if (ui.idNote) {
      ui.idNote.textContent = taken
        ? t("wizard.id.taken", { id: preview.suggestedId })
        : t("wizard.id.desc", { id: state.choice.id || preview?.app?.id || "…" });
    }
    if (ui.id && !state.choice.id) {
      ui.id.placeholder = (taken ? preview.suggestedId : preview?.app?.id) || "";
    }
    if (ui.takenNote) {
      const installed = state.source?.kind === "preset" && state.source.preset.installed?.length > 0;
      ui.takenNote.replaceChildren(
        taken
          ? h("div", { class: "note" }, icon("content_copy"), h("span", {}, t(installed ? "wizard.preset.again" : "wizard.id.taken", { id: preview.suggestedId })))
          : ""
      );
    }
    if (ui.name && !state.choice.name.trim()) {
      ui.name.placeholder = preview?.app?.name || "";
    }
  }

  function updateInspectNote() {
    const ui = state.ui;
    if (!ui.inspectNote) {
      return;
    }
    if (state.inspect === "loading") {
      const progress = wavy();
      progress.hidden = false;
      ui.inspectNote.replaceChildren(h("div", { class: "inspecting" }, h("span", { class: "body" }, t("wizard.inspecting")), progress));
    } else if (state.inspect === "failed") {
      ui.inspectNote.replaceChildren(
        h(
          "div",
          { class: "note" },
          icon("wifi_off"),
          h("div", { class: "note-text" }, h("span", {}, t("wizard.inspectFailed")), h("span", { class: "note-detail" }, errorText(state.inspectError)))
        )
      );
    } else {
      ui.inspectNote.replaceChildren();
    }
  }

  function updateDetails() {
    if (!layer || state.step !== "details" || !state.ui.name) {
      return;
    }
    if (state.ui.name.value !== state.choice.name && !state.touched.name) {
      state.ui.name.value = state.choice.name;
    }
    updateInspectNote();
    updatePreviewCard();
    updateIcons();
    updateColors();
    updateScope();
    updateSignIns();
    updateLook();
  }

  function creatingStep() {
    const progress = wavy();
    progress.hidden = false;
    return h(
      "div",
      { class: "creating-step" },
      h("div", { class: "wizard-shape spinning" }, shapeSvg("cookie12"), icon("apps")),
      h("h2", { class: "headline" }, t("wizard.creating", { app: state.creatingName || "" })),
      progress
    );
  }

  function doneStep() {
    const app = state.created?.app || {};
    return h(
      "div",
      { class: "done-step" },
      h("div", { class: "wizard-shape pop" }, shapeSvg("softBurst"), icon("check")),
      h("h2", { class: "done-title" }, t("wizard.done.title")),
      h("p", { class: "body" }, t("wizard.done.text", { app: app.name || "" })),
      h(
        "div",
        { class: "done-actions" },
        h("button", { class: "btn tonal medium", type: "button", onclick: () => restart() }, icon("add"), t("wizard.done.another")),
        h("button", { class: "btn filled medium", type: "button", onclick: () => close() }, t("wizard.done.list"), icon("arrow_forward"))
      )
    );
  }

  window.ZenAppWizard = Object.freeze({ open, close });
})();
