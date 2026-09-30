/* zen-app-runtime app mode: Webappok's link to the zen-app CLI.
 *
 * The manager never edits an app itself: every action is a `zen-app … --json`
 * call, so the CLI stays the single source of truth for definitions,
 * profiles and launchers. Calls run without a shell (an argument array,
 * values as --flag=value so they are never read as options) and time out.
 */

const lazy = {};
ChromeUtils.defineESModuleGetters(lazy, {
  Subprocess: "resource://gre/modules/Subprocess.sys.mjs",
  clearTimeout: "resource://gre/modules/Timer.sys.mjs",
  setTimeout: "resource://gre/modules/Timer.sys.mjs",
});

const CLI_TIMEOUT_MS = 120000;
/** This window's identity: the apps the CLI starts must not inherit it. */
const DROP_ENV = new Set([
  "MOZ_APP_REMOTINGNAME",
  "MOZ_DESKTOP_FILE_NAME",
  "MOZ_LAUNCHED_CHILD",
  "MOZ_NO_REMOTE",
  "ZEN_APP_ID",
  "PULSE_PROP_OVERRIDE",
  "DESKTOP_STARTUP_ID",
  "XDG_ACTIVATION_TOKEN",
]);
const PRESET_KEY = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;

/** A failed CLI call; `code` is the CLI's error code (or noCli / io). */
export class CliError extends Error {
  constructor(code, message) {
    super(message || code);
    this.code = code;
  }
}

function cliEnvironment() {
  const environment = lazy.Subprocess.getEnvironment();
  for (const key of Object.keys(environment)) {
    if (DROP_ENV.has(key) || key.startsWith("XRE_") || key.startsWith("MOZ_CRASHREPORTER")) {
      delete environment[key];
    }
  }
  return environment;
}

async function readAll(pipe) {
  let text = "";
  for (;;) {
    const chunk = await pipe.readString();
    if (!chunk) {
      return text;
    }
    text += chunk;
  }
}

/** Run `shim ARGS… --json`; the parsed output, or a CliError. */
export async function runCli(shim, args, { timeoutMs = CLI_TIMEOUT_MS } = {}) {
  if (!shim) {
    throw new CliError("noCli", "zen-app is not installed");
  }
  let process;
  try {
    process = await lazy.Subprocess.call({
      command: shim,
      arguments: [...args, "--json"],
      environment: cliEnvironment(),
      stderr: "pipe",
    });
  } catch (error) {
    throw new CliError("noCli", String(error?.message || error));
  }
  process.stdin.close().catch(() => {});
  const timer = lazy.setTimeout(() => process.kill(), timeoutMs);
  try {
    const [stdout, stderr, { exitCode }] = await Promise.all([readAll(process.stdout), readAll(process.stderr), process.wait()]);
    let data;
    try {
      data = JSON.parse(stdout);
    } catch {
      throw new CliError("io", (stderr.trim() || stdout.trim() || `zen-app exited with ${exitCode}`).slice(0, 600));
    }
    if (data && data.ok === false) {
      throw new CliError(data.code || "invalid", data.message || "");
    }
    return data;
  } finally {
    lazy.clearTimeout(timer);
  }
}

/** Arguments of `zen-app new` for the wizard's choices. */
export function newAppArguments({ source, id, name, hosts, authHosts, accent, icon, appearance }) {
  const text = String(source ?? "").trim();
  if (!/^https:\/\//i.test(text) && !PRESET_KEY.test(text)) {
    throw new CliError("badSource", "not an https:// address");
  }
  const args = ["new", text];
  const option = (flag, value) => {
    if (value !== undefined && value !== null && String(value) !== "") {
      args.push(`${flag}=${value}`);
    }
  };
  option("--id", id);
  option("--name", typeof name === "string" ? name.trim() : name);
  for (const host of hosts || []) {
    option("--host", host);
  }
  for (const host of authHosts || []) {
    option("--auth-host", host);
  }
  option("--accent", accent);
  option("--icon", icon);
  if (appearance === "transparent" || appearance === "opaque") {
    args.push(`--${appearance}`);
  }
  return args;
}

export class Manager {
  constructor(app) {
    this.app = app;
  }

  get shim() {
    return this.app.store?.config.launcher?.shim || null;
  }

  run(args) {
    return runCli(this.shim, args);
  }

  apps() {
    return this.run(["apps"]);
  }

  presets() {
    return this.run(["presets"]);
  }

  open(id) {
    return this.run(["run", String(id)]);
  }

  settings(id) {
    return this.run(["settings", String(id)]);
  }

  refresh(id) {
    return this.run(["refresh", String(id)]);
  }

  remove(id, { purge = false } = {}) {
    return this.run(["rm", String(id), "--yes", ...(purge ? ["--purge"] : [])]);
  }

  dryRun(choices) {
    return this.run([...newAppArguments(choices), "--dry-run"]);
  }

  create(choices) {
    return this.run(newAppArguments(choices));
  }
}
