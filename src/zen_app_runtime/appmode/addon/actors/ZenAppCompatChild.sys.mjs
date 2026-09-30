/* zen-app-runtime app mode: site compatibility fixes.
 *
 * Some sites only work in Firefox with a small page script. The fixes are the
 * ones of Firefox's own web compatibility interventions, loaded from the
 * installed Zen (resource://builtin-addons/webcompat/…), and they run the way
 * the interventions run them: in the page itself, before any of its scripts
 * (a MAIN-world, document_start content script). The parent publishes which
 * fix applies to which hosts in sharedData ("zenapp:compat").
 */

export const COMPAT_KEY = "zenapp:compat";

/** url → { script, promise }: every fix is compiled once per content process. */
const compiled = new Map();

function compile(url) {
  let entry = compiled.get(url);
  if (!entry) {
    entry = { script: null, promise: ChromeUtils.compileScript(url, { hasReturnValue: false }) };
    entry.promise.then(
      script => {
        entry.script = script;
      },
      error => {
        compiled.delete(url);
        console.error(`zen-app: cannot load the compatibility fix ${url}`, error);
      }
    );
    compiled.set(url, entry);
  }
  return entry;
}

export class ZenAppCompatChild extends JSWindowActorChild {
  handleEvent(event) {
    if (event.type !== "DOMDocElementInserted") {
      return;
    }
    const fixes = Services.cpmm.sharedData.get(COMPAT_KEY);
    const doc = this.document;
    const win = this.contentWindow;
    const uri = doc?.documentURIObject;
    if (!fixes?.length || !win || !uri?.schemeIs("https")) {
      return;
    }
    let host;
    try {
      host = uri.host;
    } catch {
      return;
    }
    for (const fix of fixes) {
      if (!fix.hosts?.includes(host)) {
        continue;
      }
      const entry = compile(fix.script);
      const run = script => {
        try {
          script.executeInGlobal(win, { reportExceptions: true });
        } catch (error) {
          console.error(`zen-app: compatibility fix ${fix.id}`, error);
        }
      };
      if (entry.script) {
        run(entry.script);
      } else {
        // Hold the parser until the fix is compiled, so it still runs first.
        doc.blockParsing(entry.promise.then(run, () => {}), { blockScriptCreated: false });
      }
    }
  }
}
