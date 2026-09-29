/* zen-app-runtime app mode: the user's own CSS for an app.
 *
 * An uploaded file (plain CSS, a Stylus userstyle or a Vencord/Vesktop theme)
 * is kept as written in <library>/<id>.css. compileUserCss() turns it into a
 * "program": the file split into chunks, each with the @-moz-document
 * conditions it sits in. The ZenAppCss actor evaluates those conditions for
 * every page and loads the matching chunks as one AUTHOR sheet of that page,
 * the way Stylus and Vencord apply styles: the CSS cascades exactly as its
 * author wrote it, after the site's own styles. (Author sheets cannot use
 * @-moz-document, which is why the matching happens in JavaScript.)
 *
 * On the way:
 *  - Only the app's own pages get the CSS; plain rules apply to all of them.
 *  - A section for a site the app now reaches elsewhere (messenger.com →
 *    facebook.com/messages) also matches the new location.
 *  - https @imports are inlined from `imports` (fetched and cached by the
 *    runtime); the others are listed as pending. Relative url()s of imported
 *    files are made absolute.
 *  - The Caelestia colour block (between /* BEGIN CAELESTIA COLORS * / and
 *    /* END CAELESTIA COLORS * /, as used by Vencord/Vesktop themes) is filled
 *    with the current wallpaper colours.
 *  - The file is rebuilt block by block with balanced braces, so no chunk can
 *    leak into another.
 * Pure JavaScript without Gecko APIs, so node can unit-test it.
 */

export const MAX_CUSTOM_CSS_BYTES = 512 * 1024;
export const MAX_IMPORT_BYTES = 2 * 1024 * 1024;
export const MAX_IMPORT_DEPTH = 3;
/** sharedData key the parent publishes the compiled program under. */
export const CUSTOM_CSS_KEY = "zenapp:customCss";

const IMPORT_MARKER = /^\/\* zen-app: imported from "([^"\n]*)" \*\/\r?\n?/;
const DOCUMENT_RULES = new Set(["-moz-document", "document"]);
const GROUP_RULES = new Set(["media", "supports", "layer", "container", "scope", "starting-style"]);
const CLOSERS = { "{": "}", "(": ")", "[": "]" };
const LEADING_COMMENTS = /^\s*(?:\/\*[\s\S]*?\*\/\s*)*/;
const PALETTE_BLOCK = /(\/\*\s*BEGIN CAELESTIA COLOU?RS\s*\*\/)([\s\S]*?)(\/\*\s*END CAELESTIA COLOU?RS\s*\*\/)/gi;

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
      items.push({ kind: "stray", start, end: i + 1 }); // dropped: it would close someone else's block
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

/* ------------------------------------------------------ palette block */

/**
 * Fill the Caelestia colour blocks with `colors` ({primary: "#rrggbb", ...}).
 * Only `--name: #hex;` declarations inside the markers change.
 */
export function applyPalette(src, colors) {
  let blocks = 0;
  let applied = 0;
  const text = String(src).replace(PALETTE_BLOCK, (all, begin, body, end) => {
    blocks++;
    if (!colors) {
      return all;
    }
    const filled = body.replace(
      /(--([A-Za-z][\w-]*)\s*:\s*)#[0-9a-fA-F]{3,8}(?=\s*(?:!\s*important\s*)?[;}])/g,
      (declaration, head, name) => {
        const value = colors[name];
        if (typeof value !== "string" || !/^#[0-9a-f]{6}$/i.test(value)) {
          return declaration;
        }
        applied++;
        return head + value.toLowerCase();
      }
    );
    return begin + filled + end;
  });
  return { text, blocks, applied };
}

/* ------------------------------------------------------------ imports */

function parseImport(text) {
  const match =
    /^@import\s+(?:url\(\s*(?:"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)'|([^)'"\s]*))\s*\)|"((?:\\.|[^"\\])*)"|'((?:\\.|[^'\\])*)')\s*([\s\S]*?)\s*;?\s*$/i.exec(
      text.replace(LEADING_COMMENTS, "").trim()
    );
  if (!match) {
    return null;
  }
  const url = [match[1], match[2], match[3], match[4], match[5]].find(value => value !== undefined) ?? "";
  return { url: url.replace(/\\([\s\S])/g, "$1"), condition: match[6] || "" };
}

function resolveUrl(url, base) {
  try {
    return new URL(url, base || undefined).href;
  } catch {
    return null;
  }
}

/** Make the relative url()s of a file fetched from `base` absolute. */
export function absolutizeUrls(text, base) {
  return String(text).replace(/\burl\(\s*(?:"([^"]*)"|'([^']*)'|([^)'"\s]*))\s*\)/gi, (all, dq, sq, bare) => {
    const value = dq ?? sq ?? bare ?? "";
    if (!value || value.startsWith("#") || /^[a-z][\w+.-]*:/i.test(value)) {
      return all;
    }
    const resolved = resolveUrl(value, base);
    return resolved ? `url(${cssString(resolved)})` : all;
  });
}

/* ---------------------------------------------------------- rebuild */

function flush(ctx) {
  // A chunk of nothing but comments and whitespace does not need a sheet.
  if (ctx.current.replace(/\/\*[\s\S]*?\*\//g, "").trim()) {
    ctx.chunks.push({ levels: ctx.levels.map(level => level.map(condition => ({ ...condition }))), css: ctx.current });
  }
  ctx.current = "";
}

/** A statement inside a rule or group body (imports only work at top level). */
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
      out += atName(text) ? statement(text, ctx) : text;
    }
  }
  return out;
}

/** Rules inside a group rule (@media, @supports, ...). */
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
    // Only possible at the top level or directly inside another one.
    ctx.warnings.add("nestedDocument");
    return "";
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

/** The top level of a file or the body of an @-moz-document rule. */
function sectionRules(src, from, to, ctx) {
  for (const item of scanItems(src, from, to)) {
    const text = src.slice(item.start, item.end);
    if (item.kind === "block") {
      const prelude = src.slice(item.start, item.open);
      const name = atName(prelude);
      if (name && DOCUMENT_RULES.has(name)) {
        documentSection(prelude, src, item.open + 1, item.close, ctx);
      } else {
        ctx.current += block(src, item, ctx, "rules");
      }
    } else if (item.kind === "statement") {
      const name = atName(text);
      if (name === "namespace") {
        ctx.namespaces.add(text.replace(LEADING_COMMENTS, "").trim());
      } else if (name === "import") {
        importRule(text, ctx);
      } else if (name !== "charset") {
        ctx.current += text;
      }
    } else if (item.kind === "tail") {
      ctx.current += text;
    }
  }
}

function importRule(text, ctx) {
  const parsed = parseImport(text);
  const url = parsed ? resolveUrl(parsed.url, ctx.base) : null;
  if (!url || !/^https:\/\//i.test(url) || ctx.depth >= MAX_IMPORT_DEPTH) {
    ctx.warnings.add("import");
    return;
  }
  const imported = ctx.imports[url];
  if (typeof imported !== "string") {
    ctx.pending.add(url);
    return;
  }
  if (!imported) {
    ctx.failed.add(url); // fetching it failed: continue without it
    return;
  }
  ctx.inlined.add(url);
  const source = terminate(absolutizeUrls(imported.replace(/^\uFEFF/, "").replace(/\u0000/g, "\uFFFD"), url));
  const saved = { base: ctx.base, depth: ctx.depth };
  ctx.base = url;
  ctx.depth++;
  const condition = parsed.condition.trim();
  if (condition && !/^(layer|supports)\b/i.test(condition)) {
    ctx.current += `@media ${condition} {${rules(source, 0, source.length, ctx)}}`;
  } else {
    sectionRules(source, 0, source.length, ctx);
  }
  ctx.base = saved.base;
  ctx.depth = saved.depth;
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

/** Static evaluation against the app's hosts, for the summary and warnings. */
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

function documentSection(prelude, src, open, close, ctx) {
  const parts = splitConditions(prelude.replace(LEADING_COMMENTS, "").replace(/^@[-\w]+/, ""));
  const conditions = [];
  const seen = new Set();
  const add = (fn, arg) => {
    const key = `${fn}\u0000${arg}`;
    if (!seen.has(key)) {
      seen.add(key);
      conditions.push({ fn, arg });
    }
  };
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
    if (part.fn && part.arg !== null) {
      add(part.fn, part.arg);
    }
    for (const [fn, value] of result.add) {
      add(fn, value);
    }
  }
  ctx.sections++;
  if (matches) {
    ctx.matching++;
  } else {
    elsewhere.forEach(host => ctx.elsewhere.add(host));
  }
  if (!conditions.length) {
    return;
  }
  flush(ctx);
  ctx.levels.push(conditions);
  ctx.section++;
  sectionRules(src, open, close, ctx);
  flush(ctx);
  ctx.section--;
  ctx.levels.pop();
}

/* ---------------------------------------------------------- selecting */

function conditionMatches({ fn, arg }, host, url) {
  if (typeof arg !== "string") {
    return false;
  }
  switch (fn) {
    case "domain": {
      const domain = normalizeHost(arg);
      return !!domain && (host === domain || host.endsWith("." + domain));
    }
    case "url-prefix":
      return url.startsWith(arg);
    case "url":
      return url === arg;
    case "regexp":
      try {
        return new RegExp(`^(?:${arg})$`).test(url);
      } catch {
        return false;
      }
    default:
      return false;
  }
}

function inScope(scope, host, url) {
  for (const pattern of scope?.hosts || []) {
    if (pattern.startsWith("*.") ? host === pattern.slice(2) || host.endsWith(pattern.slice(1)) : host === pattern) {
      return true;
    }
  }
  return (scope?.prefixes || []).some(prefix => url.startsWith(prefix));
}

/** The CSS a page gets from a program ("" if none). */
export function selectCss(program, url) {
  if (!program || !Array.isArray(program.chunks)) {
    return "";
  }
  let host;
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
      return "";
    }
    host = normalizeHost(parsed.hostname);
  } catch {
    return "";
  }
  if (!inScope(program.scope, host, url)) {
    return "";
  }
  const parts = program.chunks
    .filter(chunk => chunk.levels.every(level => level.some(condition => conditionMatches(condition, host, url))))
    .map(chunk => chunk.css);
  return parts.length ? [...(program.namespaces || []), ...parts].join("\n") : "";
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
 * hosts: the app's host patterns; aliases: {oldDomain: [url prefixes it moved
 * to]}; startUrl: used to judge regexp() sections; colors: the palette for
 * the Caelestia colour block; imports: {https url: fetched text ("" = failed)}.
 * Returns {program, info}; program is null when there is nothing to apply.
 */
export function compileUserCss(input, { hosts = [], aliases = {}, startUrl = "", colors = null, imports = {} } = {}) {
  const patterns = [...new Set(hosts.map(h => normalizeHost(h)).filter(h => /^(\*\.)?[a-z0-9.-]+$/.test(h)))];
  const ctx = {
    hosts: [...new Set(patterns.map(h => h.replace(/^\*\./, "")))],
    aliases: Object.fromEntries(
      Object.entries(aliases || {}).map(([source, targets]) => [
        normalizeHost(source),
        (targets || []).filter(t => /^https?:\/\/[^\s"'()\\]+$/i.test(t)).map(t => t.replace(/\/+$/, "")),
      ])
    ),
    startUrl: String(startUrl || ""),
    imports: imports || {},
    warnings: new Set(),
    elsewhere: new Set(),
    aliasesUsed: new Set(),
    pending: new Set(),
    inlined: new Set(),
    failed: new Set(),
    namespaces: new Set(),
    chunks: [],
    levels: [],
    current: "",
    base: null,
    depth: 0,
    sections: 0,
    matching: 0,
    rules: 0,
    plainBlocks: 0,
    section: 0,
  };
  const raw = terminate(stripImportHeader(String(input ?? "")).replace(/^\uFEFF/, "").replace(/\u0000/g, "\uFFFD"));
  const palette = applyPalette(raw, colors);
  const src = palette.text;
  const meta = userStyleMeta(src);
  if (meta.preprocessor && !/^(default|uso)$/i.test(meta.preprocessor)) {
    ctx.warnings.add("preprocessor");
  }
  if (meta.vars) {
    ctx.warnings.add("vars");
  }
  sectionRules(src, 0, src.length, ctx);
  flush(ctx);
  if (ctx.sections && !ctx.matching && !ctx.plainBlocks) {
    ctx.warnings.add("noMatch");
  }
  if (ctx.failed.size) {
    ctx.warnings.add("importFailed");
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
    imports: { pending: [...ctx.pending], inlined: [...ctx.inlined], failed: [...ctx.failed] },
    palette: { blocks: palette.blocks, applied: palette.applied },
  };
  const prefixes = [...new Set(Object.values(ctx.aliases).flat())];
  if (!(patterns.length || prefixes.length) || !ctx.chunks.length) {
    return { program: null, info };
  }
  return {
    program: { version: 1, scope: { hosts: patterns, prefixes }, namespaces: [...ctx.namespaces], chunks: ctx.chunks },
    info,
  };
}
