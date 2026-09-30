/* zen-app-runtime app mode: puts the app's page themes and custom CSS into its web pages.
 *
 * Runs in the content process for every document. The parent publishes the
 * compiled CSS in sharedData ("zenapp:customCss", see UserCss.sys.mjs) with
 * the palette colours it links to ("zenapp:customCssColors"), and re-sends
 * them when they change. Matching sections are loaded as one AUTHOR sheet of
 * this document: that is how Stylus and Vencord apply styles, so a theme
 * cascades exactly as its author wrote it (after the page's own CSS, without
 * turning every declaration into !important). The colours are a second,
 * small sheet of registered <color> properties: a new wallpaper only replaces
 * that one, and the page animates to the new colours the way the Caelestia
 * shell does. (A CSS transition would not do: site styles set `transition`
 * on <html> with user-sheet !important, which no page sheet can override.)
 */

import {
  CSS_COLORS_KEY,
  CUSTOM_CSS_KEY,
  PALETTE_TRANSITION,
  paletteSheet,
  selectCss,
} from "resource://zenapp/modules/UserCss.sys.mjs";

function sheetURL(css) {
  return css ? "data:text/css;charset=utf-8," + encodeURIComponent(css) : null;
}

export class ZenAppCssChild extends JSWindowActorChild {
  #program = null;
  #colors = {};
  #url = null;
  #colorsUrl = null;
  #animation = null;

  handleEvent(event) {
    if (event.type === "DOMDocElementInserted") {
      this.#program = Services.cpmm.sharedData.get(CUSTOM_CSS_KEY) || null;
      this.#colors = Services.cpmm.sharedData.get(CSS_COLORS_KEY) || {};
      this.#apply({ animate: false });
    }
  }

  receiveMessage(message) {
    if (message.name === "ZenAppCss:update") {
      this.#program = message.data?.program || null;
      this.#colors = message.data?.colors || {};
      this.#apply({ animate: false });
    } else if (message.name === "ZenAppCss:colors") {
      this.#colors = message.data?.colors || {};
      this.#apply({ animate: true, since: message.data?.since ?? null });
    }
    return null;
  }

  #apply({ animate, since = null }) {
    const win = this.contentWindow;
    const doc = this.document;
    if (!win || !doc?.documentElement) {
      return;
    }
    let css = "";
    try {
      css = this.#program ? selectCss(this.#program, doc.documentURI) : "";
    } catch (error) {
      console.error("zen-app: custom CSS", error);
    }
    const colors = css ? this.#colors : {};
    // What the page shows now (mid-animation too), to start the next one from.
    const before = animate && this.#colorsUrl ? this.#current(win, doc, Object.keys(colors)) : null;
    const utils = win.windowUtils;
    // Both sheets change in one go (no style flush in between).
    this.#url = this.#swap(utils, this.#url, sheetURL(css));
    this.#colorsUrl = this.#swap(utils, this.#colorsUrl, sheetURL(paletteSheet(colors)));
    this.#animation?.cancel();
    this.#animation = null;
    if (before) {
      this.#animate(win, doc, before, colors, since);
    }
  }

  #current(win, doc, properties) {
    const style = win.getComputedStyle(doc.documentElement);
    return Object.fromEntries(properties.map(property => [property, style.getPropertyValue(property).trim()]));
  }

  /**
   * From the old colours to the new ones with the shell's curve (unless
   * motion is reduced). `since`: when the shell started its change (the new
   * scheme file); the page then takes a little less time, to finish with it.
   */
  #animate(win, doc, before, colors, since) {
    if (win.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      return;
    }
    const from = {};
    const to = {};
    for (const [property, value] of Object.entries(colors)) {
      if (before[property]) {
        from[property] = before[property];
        to[property] = value;
      }
    }
    if (!Object.keys(from).length) {
      return;
    }
    const late = Number.isFinite(since) ? Math.max(0, Date.now() - since) : 0;
    const duration = Math.round(Math.min(PALETTE_TRANSITION.duration, Math.max(PALETTE_TRANSITION.duration * 0.4, PALETTE_TRANSITION.duration - late)));
    try {
      this.#animation = doc.documentElement.animate(Cu.cloneInto([from, to], win), {
        duration,
        easing: PALETTE_TRANSITION.easing,
      });
    } catch (error) {
      console.warn("zen-app: the colours change without animation", error);
    }
  }

  #swap(utils, current, next) {
    if (current === next) {
      return current;
    }
    if (current) {
      try {
        utils.removeSheetUsingURIString(current, utils.AUTHOR_SHEET);
      } catch {
        // the document is going away
      }
    }
    if (next) {
      utils.loadSheetUsingURIString(next, utils.AUTHOR_SHEET);
    }
    return next;
  }
}
