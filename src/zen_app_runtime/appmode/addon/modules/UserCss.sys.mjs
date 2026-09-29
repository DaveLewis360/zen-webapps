/* zen-app-runtime app mode: the user's own CSS for an app.
 *
 * An uploaded file (plain CSS or a Stylus userstyle) is kept as written in
 * <library>/<id>.css and compiled at runtime into one user sheet:
 *  - The whole sheet is wrapped in @-moz-document for the app's own hosts.
 *    Style-sheet-service user sheets apply to every document, the app's
 *    settings pages and the browser chrome included, so nothing may leave
 *    that scope: the file is rebuilt block by block with our own balanced
 *    braces, and whatever it leaves open at the end is closed first.
 *  - @-moz-document sections of the file are kept, nested inside the scope
 *    (Gecko treats nested document rules as AND). A section for a site the
 *    app now reaches elsewhere (messenger.com → facebook.com/messages) also
 *    matches the new location.
 *  - Declarations become !important. Stylus injects author styles; in a user
 *    sheet a normal declaration loses to the site's own CSS.
 * Pure JavaScript without Gecko APIs, so node can unit-test it.
 */

export const MAX_CUSTOM_CSS_BYTES = 512 * 1024;

const IMPORT_MARKER = /^\/\* zen-app: imported from "([^"\n]*)" \*\/\r?\n?/;
const DOCUMENT_RULES = new Set(["-moz-document", "document"]);
const GROUP_RULES = new Set(["media", "supports", "layer", "container", "scope", "starting-style"]);
const CLOSERS = { "{": "}", "(": ")", "[": "]" };
const LEADING_COMMENTS = /^\s*(?:\/\*[\s\S]*?\*\/\s*)*/;

/* ------------------------------------------------------------- lexing */

function urlStart(src, i) {
  return (src[i] === "u" || src[i] === "U") && /^url\(/i.test(src.slice(i, i + 4)) && !/[\w-]/.test(src[i - 1] || "");
}

/** Index after the comment, string, escape or unquoted url() at i, else -1. */
function atomEnd(src, i, to) {
  const c = src[i];
  if (c === "/" && src[i + 1] === "*") {
    const end = src.indexOf("*/", i + 2);
    return end < 0 || end + 2 > to ? to : end + 2;
  }
  if (c === '"' || c === "'") {
    for (let j = i + 1; j < to; j++) {
      if (src[j] === "\\") {
        j++;
      } else if (src[j] === c || src[j] === "\n") {
        return j + 1;
      }
    }
    return to;
  }
  if (c === "\\") {
    return Math.min(i + 2, to);
  }
  if (urlStart(src, i)) {
    let j = i + 4;
    while (j < to && /\s/.test(src[j])) {
      j++;
    }
    if (src[j] === '"' || src[j] === "'") {
      return -1; // url("…") is an ordinary function
    }
    for (; j < to; j++) {
      if (src[j] === "\\") {
        j++;
      } else if (src[j] === ")") {
        return j + 1;
      }
    }
    return to;
  }
  return -1;
}

/**
 * Close a comment, string or url() the file leaves open at its end and drop a
 * dangling backslash: otherwise they would swallow the braces added around it.
 */
export function terminate(src) {
  const to = src.length;
  let i = 0;
  while (i < to) {
    const c = src[i];
    if (c === "/" && src[i + 1] === "*") {
      const end = src.indexOf("*/", i + 2);
      if (end < 0) {
        return src + " */";
      }
      i = end + 2;
    } else if (c === '"' || c === "'" || urlStart(src, i)) {
      const quoted = c === '"' || c === "'";
      let j = quoted ? i + 1 : i + 4;
      if (!quoted) {
        while (j < to && /\s/.test(src[j])) {
          j++;
        }
        if (src[j] === '"' || src[j] === "'") {
          i = j; // url("…"): the string is handled on the next turn
          continue;
        }
      }
      const closer = quoted ? c : ")";
      let closed = false;
      while (j < to) {
        if (src[j] === "\\") {
          if (j + 1 >= to) {
            return src.slice(0, -1) + closer;
          }
          j += 2;
        } else if (src[j] === closer || (quoted && src[j] === "\n")) {
          closed = true;
          break;
        } else {
          j++;
        }
      }
      if (!closed) {
        return src + closer;
      }
      i = j + 1;
    } else if (c === "\\") {
      if (i + 1 >= to) {
        return src.slice(0, -1);
      }
      i += 2;
    } else {
      i++;
    }
  }
  return src;
}

/** Index of the "}" closing the block opened at `open` (CSS matching), or `to`. */
function blockEnd(src, open, to) {
  const stack = [];
  for (let i = open; i < to; ) {
    const end = atomEnd(src, i, to);
    if (end >= 0) {
      i = end;
      continue;
    }
    const c = src[i];
    if (c in CLOSERS) {
      stack.push(CLOSERS[c]);
    } else if (c === stack[stack.length - 1]) {
      stack.pop();
      if (!stack.length) {
        return i;
      }
    }
    i++;
  }
  return to;
}

/** Statements (up to ";"), blocks and a trailing remainder of a rule list or body. */
function scanItems(src, from, to) {
  const items = [];
  const stack = [];
  let start = from;
  for (let i = from; i < to; ) {
    const end = atomEnd(src, i, to);
    if (end >= 0) {
      i = end;
      continue;
    }
    const c = src[i];
    if (stack.length) {
      if (c in CLOSERS) {
        stack.push(CLOSERS[c]);
      } else if (c === stack[stack.length - 1]) {
        stack.pop();
      }
    } else if (c === "(" || c === "[") {
      stack.push(CLOSERS[c]);
    } else if (c === ";") {
      items.push({ kind: "statement", start, end: i + 1 });
      start = i + 1;
    } else if (c === "{") {
      const close = blockEnd(src, i, to);
      items.push({ kind: "block", start, open: i, close, end: Math.min(close + 1, to) });
      i = Math.min(close + 1, to);
      start = i;
      continue;
    } else if (c === "}") {
      items.push({ kind: "stray", start, end: i + 1 }); // dropped: it would close our scope
      start = i + 1;
    }
    i++;
  }
  if (start < to) {
    items.push({ kind: "tail", start, end: to });
  }
  return items;
}

function atName(text) {
  const match = /^@([-\w]+)/.exec(text.replace(LEADING_COMMENTS, ""));
  return match ? match[1].toLowerCase() : null;
}

function cssString(value) {
  return `"${String(value).replace(/[\\"]/g, "\\$&").replace(/\n/g, "\\a ")}"`;
}

/* ------------------------------------------------------- declarations */

/** One declaration with !important added (unless present or not a declaration). */
function important(text) {
  const stack = [];
  let colon = -1;
  let last = -1;
  for (let i = 0; i < text.length; ) {
    const end = atomEnd(text, i, text.length);
    if (end >= 0) {
      if (!(text[i] === "/" && text[i + 1] === "*")) {
        last = end - 1;
      }
      i = end;
      continue;
    }
    const c = text[i];
    if (!stack.length && c === ";") {
      break;
    }
    if (c in CLOSERS) {
      stack.push(CLOSERS[c]);
    } else if (c === stack[stack.length - 1]) {
      stack.pop();
    } else if (!stack.length && c === ":" && colon < 0) {
      colon = i;
    }
    if (!/\s/.test(c)) {
      last = i;
    }
    i++;
  }
  if (colon < 0 || last <= colon) {
    return text;
  }
  const head = text.slice(0, last + 1);
  const name = text.slice(0, colon).replace(/\/\*[\s\S]*?\*\//g, "").trim();
  if (!name || name.startsWith("@") || /!\s*important$/i.test(head)) {
    return text;
  }
  return `${head} !important${text.slice(last + 1)}`;
}

/* ---------------------------------------------------------- rebuild */

function statement(text, ctx) {
  const name = atName(text);
  if (name === "import") {
    ctx.warnings.add("import");
    return "";
  }
  return name === "charset" || name === "namespace" ? "" : text;
}

function declarations(src, from, to, ctx) {
  let out = "";
  for (const item of scanItems(src, from, to)) {
    const text = src.slice(item.start, item.end);
    if (item.kind === "block") {
      out += block(src, item, ctx, "declarations");
    } else if (item.kind !== "stray") {
      out += atName(text) ? statement(text, ctx) : important(text);
    }
  }
  return out;
}

function rules(src, from, to, ctx) {
  let out = "";
  for (const item of scanItems(src, from, to)) {
    const text = src.slice(item.start, item.end);
    if (item.kind === "block") {
      out += block(src, item, ctx, "rules");
    } else if (item.kind === "statement") {
      out += statement(text, ctx);
    } else if (item.kind === "tail") {
      out += text;
    }
  }
  return out;
}

function block(src, item, ctx, context) {
  const prelude = src.slice(item.start, item.open);
  const name = atName(prelude);
  const open = item.open + 1;
  const close = item.close;
  if (name && DOCUMENT_RULES.has(name)) {
    return context === "rules" ? documentRule(prelude, src, open, close, ctx) : "";
  }
  if (!ctx.section) {
    ctx.plainBlocks++;
  }
  if (name && GROUP_RULES.has(name)) {
    const body = context === "rules" ? rules(src, open, close, ctx) : declarations(src, open, close, ctx);
    return `${prelude}{${body}}`;
  }
  if (name || (context === "declarations" && /^--[\w-]*\s*:/.test(prelude.replace(LEADING_COMMENTS, "")))) {
    // @keyframes, @font-face, … (descriptors) and {}-valued custom properties.
    return `${prelude}{${src.slice(open, close)}}`;
  }
  ctx.rules++;
  return `${prelude}{${declarations(src, open, close, ctx)}}`;
}

/* ------------------------------------------------ @-moz-document */

function splitConditions(text) {
  const parts = [];
  const stack = [];
  let start = 0;
  for (let i = 0; i < text.length; ) {
    const end = atomEnd(text, i, text.length);
    if (end >= 0) {
      i = end;
      continue;
    }
    const c = text[i];
    if (c in CLOSERS) {
      stack.push(CLOSERS[c]);
    } else if (c === stack[stack.length - 1]) {
      stack.pop();
    } else if (c === "," && !stack.length) {
      parts.push(text.slice(start, i));
      start = i + 1;
    }
    i++;
  }
  parts.push(text.slice(start));
  return parts
    .map(raw => {
      const plain = raw.replace(/\/\*[\s\S]*?\*\//g, " ").trim();
      const match = /^([-\w]+)\(\s*([\s\S]*?)\s*\)$/.exec(plain);
      let arg = match ? match[2] : null;
      if (arg && /^(["'])[\s\S]*\1$/.test(arg)) {
        arg = arg.slice(1, -1).replace(/\\([\s\S])/g, "$1");
      }
      return { text: raw.trim(), fn: match ? match[1].toLowerCase() : null, arg };
    })
    .filter(part => part.text);
}

function normalizeHost(host) {
  return String(host || "").toLowerCase().replace(/\.$/, "");
}

function hostInApp(host, ctx) {
  host = normalizeHost(host);
  return !!host && ctx.hosts.some(h => h === host || h.endsWith("." + host) || host.endsWith("." + h));
}

function aliasFor(host, ctx) {
  host = normalizeHost(host);
  for (const [source, targets] of Object.entries(ctx.aliases)) {
    if (host === source || host.endsWith("." + source)) {
      return { source, targets };
    }
  }
  return null;
}

function samples(ctx) {
  return [ctx.startUrl, ...ctx.hosts.map(host => `https://${host}/`), ...Object.values(ctx.aliases).flat().map(t => `${t}/`)].filter(Boolean);
}

function evaluate(part, ctx) {
  const { fn, arg } = part;
  if (arg === null) {
    return { matches: false, add: [] };
  }
  if (fn === "domain") {
    const alias = aliasFor(arg, ctx);
    return {
      matches: hostInApp(arg, ctx) || !!alias,
      alias,
      add: alias ? alias.targets.map(target => ["url-prefix", target]) : [],
      host: normalizeHost(arg),
    };
  }
  if (fn === "url" || fn === "url-prefix") {
    const url = /^(?:[a-z][\w+.-]*:)?\/\/([^/?#:]+)(?::\d+)?([\s\S]*)$/i.exec(arg);
    if (!url) {
      return { matches: fn === "url-prefix" && samples(ctx).some(sample => sample.startsWith(arg)), add: [] };
    }
    const alias = aliasFor(url[1], ctx);
    return {
      matches: hostInApp(url[1], ctx) || !!alias,
      alias,
      add: alias ? alias.targets.map(target => [fn, target + url[2]]) : [],
      host: normalizeHost(url[1]),
    };
  }
  if (fn === "regexp") {
    try {
      const pattern = new RegExp(`^(?:${arg})$`);
      return { matches: samples(ctx).some(sample => pattern.test(sample)), add: [] };
    } catch {
      return { matches: false, add: [] };
    }
  }
  return { matches: false, add: [] };
}

function documentRule(prelude, src, open, close, ctx) {
  const parts = splitConditions(prelude.replace(LEADING_COMMENTS, "").replace(/^@[-\w]+/, ""));
  const conditions = parts.map(part => part.text);
  let matches = false;
  const elsewhere = [];
  for (const part of parts) {
    const result = evaluate(part, ctx);
    matches ||= result.matches;
    if (!result.matches && result.host) {
      elsewhere.push(result.host);
    }
    if (result.alias) {
      const shown = result.alias.targets[0].replace(/^[a-z]+:\/\/(www\.)?/i, "");
      ctx.aliasesUsed.add(`${result.alias.source} → ${shown}`);
    }
    for (const [fn, value] of result.add) {
      const condition = `${fn}(${cssString(value)})`;
      if (!conditions.includes(condition)) {
        conditions.push(condition);
      }
    }
  }
  ctx.sections++;
  if (matches) {
    ctx.matching++;
  } else {
    elsewhere.forEach(host => ctx.elsewhere.add(host));
  }
  ctx.section++;
  const body = rules(src, open, close, ctx);
  ctx.section--;
  return conditions.length ? `@-moz-document ${conditions.join(", ")} {${body}}` : "";
}

/* ------------------------------------------------------------- API */

/** The ==UserStyle== header of a Stylus style, if any. */
export function userStyleMeta(src) {
  const match = /\/\*\s*==UserStyle==([\s\S]*?)==\/UserStyle==\s*\*\//.exec(src);
  if (!match) {
    return {};
  }
  const meta = {};
  for (const line of match[1].split(/\r?\n/)) {
    const field = /^\s*@([\w-]+)\s+(.*?)\s*$/.exec(line);
    if (!field) {
      continue;
    }
    const key = field[1].toLowerCase();
    if (key === "var" || key === "advanced") {
      meta.vars = true;
    } else if (!(key in meta)) {
      meta[key] = field[2].slice(0, 120);
    }
  }
  return { name: meta.name, version: meta.version, preprocessor: meta.preprocessor, vars: !!meta.vars };
}

/** File name recorded by importHeader(), if the file has one. */
export function importedFrom(src) {
  return IMPORT_MARKER.exec(src)?.[1] || null;
}

export function importHeader(fileName) {
  const safe = String(fileName || "").replace(/[\u0000-\u001f\u007f"*/\\]/g, "_").slice(0, 120);
  return safe ? `/* zen-app: imported from "${safe}" */\n` : "";
}

export function stripImportHeader(src) {
  return String(src).replace(IMPORT_MARKER, "");
}

/**
 * Compile an uploaded CSS for an app.
 * hosts: the app's hostnames; aliases: {oldDomain: [url prefixes it moved to]};
 * startUrl: used to evaluate regexp() sections.
 * Returns {css, info} where css is "" when there is nothing to apply.
 */
export function compileUserCss(input, { hosts = [], aliases = {}, startUrl = "" } = {}) {
  const ctx = {
    hosts: [...new Set(hosts.map(h => normalizeHost(String(h).replace(/^\*\./, ""))).filter(h => /^[a-z0-9.-]+$/.test(h)))],
    aliases: Object.fromEntries(
      Object.entries(aliases || {}).map(([source, targets]) => [
        normalizeHost(source),
        (targets || []).filter(t => /^https?:\/\/[^\s"'()\\]+$/i.test(t)).map(t => t.replace(/\/+$/, "")),
      ])
    ),
    startUrl: String(startUrl || ""),
    warnings: new Set(),
    elsewhere: new Set(),
    aliasesUsed: new Set(),
    sections: 0,
    matching: 0,
    rules: 0,
    plainBlocks: 0,
    section: 0,
  };
  const src = terminate(stripImportHeader(String(input ?? "")).replace(/^\uFEFF/, "").replace(/\u0000/g, "\uFFFD"));
  const meta = userStyleMeta(src);
  if (meta.preprocessor && !/^(default|uso)$/i.test(meta.preprocessor)) {
    ctx.warnings.add("preprocessor");
  }
  if (meta.vars) {
    ctx.warnings.add("vars");
  }
  const namespaces = [];
  let body = "";
  for (const item of scanItems(src, 0, src.length)) {
    const text = src.slice(item.start, item.end);
    if (item.kind === "block") {
      body += block(src, item, ctx, "rules");
    } else if (item.kind === "statement") {
      if (atName(text) === "namespace") {
        namespaces.push(text.replace(LEADING_COMMENTS, "").trim());
      } else {
        body += statement(text, ctx);
      }
    } else if (item.kind === "tail") {
      body += text;
    }
  }
  if (ctx.sections && !ctx.matching && !ctx.plainBlocks) {
    ctx.warnings.add("noMatch");
  }
  const info = {
    name: meta.name || null,
    version: meta.version || null,
    sections: ctx.sections,
    matching: ctx.matching,
    plain: ctx.plainBlocks > 0,
    rules: ctx.rules,
    warnings: [...ctx.warnings],
    elsewhere: [...ctx.elsewhere].slice(0, 6),
    aliases: [...ctx.aliasesUsed],
  };
  const scope = [
    ...ctx.hosts.map(host => `domain(${cssString(host)})`),
    ...Object.values(ctx.aliases).flat().map(target => `url-prefix(${cssString(target)})`),
  ];
  if (!scope.length || !(ctx.rules || ctx.plainBlocks || ctx.sections)) {
    return { css: "", info };
  }
  const css = [...namespaces, "/* zen-app: custom CSS */", `@-moz-document ${[...new Set(scope)].join(", ")} {`, body, "}", ""].join("\n");
  return { css, info };
}
