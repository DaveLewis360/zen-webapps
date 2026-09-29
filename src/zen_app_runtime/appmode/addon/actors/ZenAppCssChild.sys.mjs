/* zen-app-runtime app mode: puts the app's custom CSS into its web pages.
 *
 * Runs in the content process for every document. The parent publishes the
 * compiled custom CSS in sharedData ("zenapp:customCss", see UserCss.sys.mjs)
 * and re-sends it when it changes. Matching sections are loaded as one AUTHOR
 * sheet of this document: that is how Stylus and Vencord apply styles, so a
 * theme cascades exactly as its author wrote it (after the page's own CSS,
 * without turning every declaration into !important).
 */

import { CUSTOM_CSS_KEY, selectCss } from "resource://zenapp/modules/UserCss.sys.mjs";

export class ZenAppCssChild extends JSWindowActorChild {
  #url = null;

  handleEvent(event) {
    if (event.type === "DOMDocElementInserted") {
      this.#apply(Services.cpmm.sharedData.get(CUSTOM_CSS_KEY));
    }
  }

  receiveMessage(message) {
    if (message.name === "ZenAppCss:update") {
      this.#apply(message.data);
    }
    return null;
  }

  #apply(program) {
    const win = this.contentWindow;
    const doc = this.document;
    if (!win || !doc) {
      return;
    }
    let css = "";
    try {
      css = program ? selectCss(program, doc.documentURI) : "";
    } catch (error) {
      console.error("zen-app: custom CSS", error);
    }
    const url = css ? "data:text/css;charset=utf-8," + encodeURIComponent(css) : null;
    if (url === this.#url) {
      return;
    }
    const utils = win.windowUtils;
    if (this.#url) {
      try {
        utils.removeSheetUsingURIString(this.#url, utils.AUTHOR_SHEET);
      } catch {
        // the document is going away
      }
      this.#url = null;
    }
    if (url) {
      utils.loadSheetUsingURIString(url, utils.AUTHOR_SHEET);
      this.#url = url;
    }
  }
}
