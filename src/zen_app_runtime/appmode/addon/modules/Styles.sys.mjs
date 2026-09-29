/* zen-app-runtime app mode: Zen Internet site styles.
 *
 * Uses the same community catalog as the Zen Internet extension
 * (sameerasw/my-internet styles.json, MIT) and the same site matching, so a
 * site looks the way it does in Zen. Styles are registered once as user
 * sheets scoped with @-moz-document to exactly the app's hosts (author sheets
 * from the style-sheet service cannot use @-moz-document).
 */

function normalize(hostname) {
  hostname = String(hostname || "").toLowerCase().replace(/\.$/, "");
  return hostname.startsWith("www.") ? hostname.slice(4) : hostname;
}

/** Zen Internet precedence: exact, +wildcard, -TLD wildcard, suffix, mapping. */
export function bestMatch(website, hostname, mapping = {}) {
  const host = normalize(hostname);
  let best = null;
  let bestLength = 0;
  for (const key of Object.keys(website)) {
    const site = key.endsWith(".css") ? key.slice(0, -4) : key;
    const normalized = normalize(site);
    if (host === normalized) {
      return key;
    }
    if (site.startsWith("+")) {
      const base = site.slice(1);
      if ((host === base || host.endsWith("." + base)) && base.length > bestLength) {
        best = key;
        bestLength = base.length;
      }
    } else if (site.startsWith("-")) {
      const domain = site.slice(1).split(".").slice(0, -1).join(".");
      const parts = host.split(".");
      const hostDomain = parts.length > 1 ? parts.slice(0, -1).join(".") : host;
      if (domain && hostDomain === domain && domain.length > bestLength) {
        best = key;
        bestLength = domain.length;
      }
    } else if (host !== normalized && host.endsWith("." + normalized) && normalized.length > bestLength) {
      best = key;
      bestLength = normalized.length;
    }
  }
  if (best) {
    return best;
  }
  for (const [source, targets] of Object.entries(mapping || {})) {
    if (Array.isArray(targets) && targets.includes(host) && website[source]) {
      return source;
    }
  }
  return null;
}

export const isTransparencyFeature = name => /transpar/i.test(name);

export function featureKey(site, feature) {
  return `${site}|${feature}`;
}

function cssString(value) {
  return JSON.stringify(String(value));
}

export class StyleCatalog {
  constructor(data, meta = {}) {
    this.website = data?.website || {};
    this.mapping = data?.mapping || {};
    this.meta = meta;
  }

  static async load(path, meta) {
    try {
      const data = JSON.parse(await IOUtils.readUTF8(path));
      return new StyleCatalog(data, meta);
    } catch (error) {
      console.error("zen-app: cannot read the Zen Internet catalog", path, error);
      return new StyleCatalog({}, meta);
    }
  }

  /**
   * [{site, hosts, features: [{name, transparency}]}] for the app's hosts.
   * Hosts in `exactOnly` (sign-in hops) only take a style written for exactly
   * that host, never a broader parent-domain style.
   */
  match(hosts, exactOnly = new Set()) {
    const bySite = new Map();
    for (let host of hosts) {
      const exact = exactOnly.has(host);
      if (host.startsWith("*.")) {
        host = host.slice(2);
      }
      let site = bestMatch(this.website, host, this.mapping);
      if (site && exact && normalize(site.replace(/\.css$/, "").replace(/^[+-]/, "")) !== normalize(host)) {
        site = null;
      }
      if (!site) {
        continue;
      }
      if (!bySite.has(site)) {
        bySite.set(site, []);
      }
      if (!bySite.get(site).includes(host)) {
        bySite.get(site).push(host);
      }
    }
    return [...bySite].map(([site, siteHosts]) => ({
      site,
      hosts: siteHosts,
      features: Object.keys(this.website[site] || {}).map(name => ({
        name,
        transparency: isTransparencyFeature(name),
      })),
    }));
  }

  /** One CSS sheet per matched site, honouring disabled features. */
  sheets(hosts, { translucent, disabled, exactOnly, aliases = {} }) {
    const off = new Set(disabled || []);
    const sheets = [];
    for (const { site, hosts: siteHosts } of this.match(hosts, exactOnly)) {
      const features = this.website[site] || {};
      const parts = [];
      for (const [name, css] of Object.entries(features)) {
        if (off.has(featureKey(site, name))) {
          continue;
        }
        if (isTransparencyFeature(name) && !translucent) {
          continue;
        }
        parts.push(`/* ${name.replace(/\*\//g, "")} */\n${css}`);
      }
      if (!parts.length) {
        continue;
      }
      const conditions = siteHosts.map(host => `domain(${cssString(host)})`);
      // A site that moved (messenger.com → facebook.com/messages) keeps its style.
      for (const [source, targets] of Object.entries(aliases || {})) {
        if (siteHosts.some(host => normalize(host) === source || normalize(host).endsWith("." + source))) {
          conditions.push(...targets.map(target => `url-prefix(${cssString(target)})`));
        }
      }
      sheets.push({ site, css: `/* Zen Internet: ${site} */\n@-moz-document ${[...new Set(conditions)].join(", ")} {\n${parts.join("\n")}\n}\n` });
    }
    return sheets;
  }
}

export function dataSheetURI(css) {
  return Services.io.newURI("data:text/css;charset=utf-8," + encodeURIComponent(css));
}
