# Zen App Runtime

Zen App Runtime is a working Linux vertical slice for running a site-specific app with an explicitly pinned local Zen/Firefox-compatible executable and a dedicated managed profile. The trust boundary is intentionally narrow: strict versioned app manifests, descriptor-relative storage, deterministic plans and launch tickets, SHA-256 verification of an operator-supplied runtime lock, sentinel-marked profiles, explicit Wayland/X11 selection, and a synchronous supervising launcher.

The legacy and App Shell workflows below do **not** bundle, discover, download, or update Zen; access a default Firefox/Zen profile; or install extensions or desktop files. Legacy mode deliberately leaves stock browser chrome and does not enforce manifest navigation metadata. The opt-in bundled App Shell supplies a reviewed Gecko `-app` adapter with its own privileged onboarding, minimal chrome, first same-origin navigation/shortcut/window controls, and an explicit sandbox-preserving preference baseline. Native evidence below qualifies one local Arch Zen installation, not every Zen package format or version. [App mode](#app-mode) is the deliberate, separately rooted exception for daily use: it runs the system Zen package, sideloads its own add-on into its own profiles, and installs desktop entries.

## App mode

App mode turns a site into a desktop app that looks and behaves native: its own launcher, window class, profile, translucent glass background, and no visible browser. Each app is a complete Zen instance whose browser layer is hidden rather than removed, so WebRTC, screen sharing, notifications, Widevine/EME and permission prompts work exactly as in Zen.

```sh
python3.11 -m zen_app_runtime setup      # installs the ~/.local/bin/zen-app shim (re-run after moving the checkout)
zen-app presets                          # spotify, discord, messenger, youtube-music, whatsapp, telegram
zen-app new discord                      # create and launch; the first run shows the onboarding
zen-app new https://app.slack.com/client --id slack --no-launch
zen-app run discord [URL]                # a second launch focuses the running window
zen-app settings discord                 # or Ctrl+, inside the app (also the launcher's Settings action)
zen-app apps [--json]
zen-app refresh [ID]                     # re-render profiles after updating the checkout or Zen
zen-app styles show discord              # Zen Internet styles that apply to an app or a host
zen-app styles update                    # fetch the current Zen Internet catalog
zen-app glass                            # desktop glass sources and inherited rendering prefs
zen-app css messenger ~/messenger.user.css   # the app's own CSS; also --on, --off, --remove, or no argument to show it
zen-app rm slack --yes [--purge]         # --purge also deletes the profile, its logins and the app's CSS
```

Each app has a definition `~/.config/zen-apps/<id>.json` (schema `zen-app/1`), a profile `~/.local/share/zen-apps/<id>/profile`, and a launcher `~/.local/share/applications/zen-app-<id>.desktop`. The window's Wayland app_id / X11 class is `zen-app-<id>`, for compositor rules such as `hl.window_rule({ match = { class = "^(zen-app-spotify)$" }, workspace = "special:music" })`. `ZEN_APP_MODE_ROOT=/absolute/dir` keeps everything below one directory; `ZEN_APP_ZEN_BINARY` selects another Zen executable.

How it works:

- **Runtime.** The package-managed Zen (`/opt/zen-browser-bin/zen-bin` or `zen-browser` on `PATH`), not hash-pinned, so package updates apply. Launches pass `--name/--class zen-app-<id>` and `MOZ_APP_REMOTINGNAME`, so a second launch is forwarded to the running app and focused through `hyprctl` when available. Each app is started through `~/.local/share/zen-apps/<id>/zen-app-<id>`, a link to Zen, and its add-on keeps that name after in-app restarts: the process is called `zen-app-<id>` (15 characters kept by the kernel), so `killall zen-bin` stops only the browser and `killall zen-app-<id>` stops just that app.
- **Profile.** `user.js` starts with the GPU/rendering preferences of the main Zen profile (read-only, allowlisted prefixes such as `gfx.webrender.`, `widget.dmabuf`, `media.ffmpeg.`), so apps render and decode video like the browser, followed by the app baseline and a marked settings block. `chrome/userChrome.css` hides every Zen surface.
- **Add-on.** A privileged experiment add-on (`zenapp@zen-app-runtime`, built deterministically on render) provides the M3 Expressive onboarding and settings studio, scope routing (in-scope navigation stays in the app, everything else opens in the default browser), the shortcut and context-menu allowlists, site styles, glass and a status file `zen-app/status.json`.
- **Glass.** `auto` resolves to Ghostty, then Vesktop, then Zen; `ghostty`, `vesktop`, `zen` and `custom` can be chosen in Settings → Appearance. The window paints exactly the source's colour and alpha (for example Ghostty's `background` at `background-opacity`) and the page stays transparent; blur comes from the compositor, so it matches every other blurred window.
- **Site styles.** A pinned copy of the [Zen Internet](https://github.com/sameerasw/zen-internet) community catalog ([my-internet](https://github.com/sameerasw/my-internet), 666 sites, MIT © 2025 Transparent Zen, see `appmode/data/LICENSE-zen-internet.txt`) is matched with the extension's own rules (exact, `+` wildcard, `-` TLD wildcard, suffix, mapping) and registered as origin-scoped user sheets. Individual features can be switched off in Settings → Styles. Sign-in hops (for example `accounts.google.com`) only receive styles written for exactly that host. App-mode extras add Spotify glass surfaces and a standalone Facebook Messages layout. A preset can declare where a site moved (`messenger.com` → `facebook.com/messages`); styles written for the old domain then apply at the new place too.
- **Custom CSS.** The onboarding (appearance step) and Settings → Styles accept an uploaded `.css` file or pasted CSS: plain CSS, a Stylus userstyle or a Vencord/Vesktop theme. It is saved as `~/.config/zen-apps/<id>.css` and loaded into the app's own pages as an author style sheet, the way Stylus and Vencord do it, so it cascades exactly as written. `@-moz-document` sections are matched per page (and follow a moved site). `@import`s from `https://` are downloaded once a day and cached in the profile, so themes built on Midnight work unchanged. A Caelestia colour block (`/* BEGIN CAELESTIA COLORS */ … /* END CAELESTIA COLORS */`) is filled with the current wallpaper colours and follows a new wallpaper within about two seconds. Editing the file and switching back to the app reloads it.
- **Ad blocking and extensions.** The `adblock` setting (on by default; onboarding integration step and Settings → Extensions) installs [uBlock Origin](https://addons.mozilla.org/firefox/addon/ublock-origin/) from addons.mozilla.org into the app's profile at the next start and keeps it enabled or disabled accordingly; its filter lists open from the same place. Settings → Extensions also searches AMO (or takes a pasted AMO link), installs from a local `.xpi`, and switches, configures or removes (with undo) each extension. Extensions run in that app only.
- **Site compatibility fixes.** A preset can ask for one of Firefox's own web compatibility interventions, taken from the installed Zen and run in the page before its scripts. Messenger uses the `createEncodedStreams()` shim of [bug 1913599](https://bugzilla.mozilla.org/show_bug.cgi?id=1913599), which Firefox itself enables only in Nightly: without it, Messenger reports that voice calling is not supported in this browser, because its encrypted calls use that legacy Chrome API ([bug 1896361](https://bugzilla.mozilla.org/show_bug.cgi?id=1896361)). Settings → Integration → Encrypted calls (experimental) switches it (`webcompat`).
- **Shortcuts.** Only editing, reload (`Ctrl+R`, `F5`), zoom (`Ctrl` `+`/`-`/`0`), `Alt+←/→`, `F11`, `Ctrl+Q`, find (`Ctrl+F`, when enabled) and devtools (when developer mode is on) keep their browser meaning; `Ctrl+W` closes the window and `Ctrl+,` opens settings. All other browser keys are disabled and the context menu is reduced to page actions.

Security notes: the add-on is unsigned; `xpinstall.signatures.required=false` and `extensions.experiments.enabled=true` are set only inside app-mode profiles, which accept add-ons from the profile and the Zen installation only (no user- or system-wide extension directories). Because unsigned add-ons are accepted there, an extension downloaded from addons.mozilla.org is installed only if it has AMO's SHA-256 hash, is the add-on that was asked for, and carries Mozilla's signature; the download URL must be on addons.mozilla.org. A local `.xpi` may be unsigned (it is marked as such) but can never replace the runtime add-on. Profiles are private (`0700`) and separate from the main Zen profile, which app mode only reads for glass and rendering preferences. Zen Internet catalog updates are fetched over HTTPS, validated, size-limited and applied as CSS only.

Limitations: no tray icon or run-in-background on window close, no unread badges on the launcher, and media controls (MPRIS) are not renamed per app. Messenger uses `facebook.com/messages/` since messenger.com closed in April 2026; its encrypted calls depend on the experimental shim above (verified here only in a local audio/video loopback, not in a real Messenger call). Extension toolbar buttons and their context-menu items are not shown in apps; an extension's own settings page opens in a window of its own.

## Included now

- Strict manifest schema v1 with lowercase stable app IDs, `Opaque`/`Transparent` presentation requests, fixed conservative link-policy metadata, and metadata-only Zen Internet/Stylus selections. The privileged adapter separately carries one pinned, reviewed Discord CSS derivative; it does not install or execute the Zen Internet extension.
- Descriptor-relative AppStore operations with strict ownership/mode/link checks, durable unnamed-fd publication, explicit removal confirmation, and fail-closed tombstones.
- Deterministic inert launch-plan v1 and plan-only Desktop Entry preview renderers.
- Strict runtime-lock v1 verification of an explicit local executable and deterministic runtime-identity v1 output. Verification never executes or discovers a runtime.
- A checked-in GeckoRuntimeSourceLock v1 for Mozilla Firefox 154.0 (`linux-x86_64`, `en-US`) and a network-free offline assembler. It verifies the pinned Mozilla signing key and detached `SHA256SUMS` signature in an isolated GnuPG home, archive SHA-256/SHA-512 and sizes, the source archive's BuildID/revision metadata, tar path/type safety, and the exact 53-file canonical runtime tree before atomically publishing a normalized candidate. The runnable binary is archive-exact; the matching source archive is pinned, but no source-built artifact or clean-builder source reproducibility is claimed.
- A closed GeckoRuntimeCandidate v1 descriptor and retained-directory verifier. Direct candidate tests execute `/proc/self/fd/…/firefox` with the complete package-relative distribution available, re-hash before spawn, and reject tampering, links, special files, empty/unknown tree entries, descriptor mutation, and pathname rebinding. Effective-user-owned trees remain development authority; release-grade immutability is deferred to the release-integration step.
- A checked-in ChromiumRuntimeSourceLock v1 for Arch Linux CEF 151.3.24-1 (`CEF 151.3.24+g2384915`, Chromium 151.0.7922.174) and a network-free offline assembler. It binds the Arch package and detached signature, package metadata and complete tar inventory, the exact Arch packaging source tree, package-derived CEF SDK, root-owned pinned tools, project-authored C++ shell sources, exact single-job build output, 13-file runtime subset, and external root-owned setuid sandbox. The minimal CEF Views reference shell keeps onboarding before content, same-origin navigation, deny-all permission/media policy, system Save-picker downloads, a sealed config, and child-command-line privacy. It is a bakeoff candidate, not a Chromium source build or a complete Discord/capture implementation.
- Sentinel-marked per-app managed profiles bound to the normalized root, canonical manifest digest, and verified runtime identity. There is no default-profile adoption or access.
- Read-only `doctor` capability reporting and a high-level `app onboard/install/status/inspect/launch/repair` workflow layered over the same strict store/runtime/profile/launch authorities. `app onboard URL` provides a network-free terminal onboarding preview and can explicitly install/launch a stock-Zen legacy web app from one HTTPS link; operationally blocked reports are versioned JSON rather than generic failures.
- App Shell policy v2 plus a package-bundled privileged Gecko application adapter. It creates browser-chrome onboarding before site content, omits normal Zen sidebar/tab/address chrome by construction, keeps origin/security chrome available, uses a remote content browser, enforces same-origin navigation and shortcut/window boundaries, and owns permission, WebRTC, download, file-picker, and Wayland screen-cast mediation in parent chrome before its v2 readiness ACK.
- Read-only `launch inspect` and explicit `launch execute --yes` flows. Execution uses the retained verified fd through `/proc/self/fd/N`, holds a per-profile `flock`, creates profile-local HOME/XDG/TMP state, supervises the child process group, and propagates exit or signal status.
- Backward-compatible display selection: omitting `--display-backend` preserves the exact launch-ticket v1 contract; explicitly choosing `auto`, `wayland`, or `x11` produces display launch-ticket v2.
- Opt-in native smoke coverage for the locally installed Zen 1.21.15b on Wayland and X11 through XWayland: legacy cold/warm process and memory evidence plus App Shell policy/ACK ordering, real onboarding-before-content, `zen-app-shell` window identity, remote content, seccomp/NoNewPrivs, signal handling, and orphan cleanup.
- Pure BenchmarkScenario/BenchmarkResult/BenchmarkAcceptance v1 contracts for the approved Discord workload. They encode cold/warm startup, onboarding, active, idle, voice, video, screen-share, and 24-hour soak phases; exact hardware/kernel/compositor/portal/runtime evidence; conservative 95% confidence-bound hard gates; and explicit incomplete results when evidence has not been collected. Defining or evaluating the contract performs no live benchmark.
- A cgroup-v2 benchmark runner that launches an explicit tokenized command in a transient systemd user scope, retains no-follow descriptors to the isolated cgroup and `/proc`, treats stable summed `smaps_rollup` PSS as primary memory evidence, records `memory.current/peak/stat`, swap, tasks, CPU, seccomp, and NoNewPrivs as secondary evidence, and never reads process command lines, environments, URLs, or maps. Integer CPU basis points and deterministic bootstrap observations feed the benchmark contract without floating-point ambiguity.
- An engine-neutral Discord web-on-Linux conformance suite v1 with 21 synthetic platform cases and 15 live-Discord workflows. Semantic action/event IDs cover startup ordering, persistence, service workers, WebSocket resume, files, clipboard, notifications, audio, microphone, camera, WebRTC, portal-mediated screen share, rendering, auth/navigation, keyboard/accessibility, messaging, search, reconnect, settings, and idle recovery without DOM selectors, screenshots, credentials, message text, or free-form diagnostics. Synthetic-only evidence is explicitly incomplete and cannot satisfy the Discord parity gate.
- Runtime Driver protocol v2 and a current-Zen host adapter wrapper. The current adapter-protocol-v2-bound descriptor SHA-256 is `e03e62d451241bad291ded207a75961522e31f934643db5f7cb990c3557960fe`. A suite/vocabulary-bound descriptor, bounded canonical action/result messages, host-monotonic event timing, and fail-closed transcript construction connect the semantic suite to the transient cgroup-v2 lifecycle and its memory/CPU samples. Automation requires an explicitly injected local test channel; release plans carry `releaseDebugAuthority: none` and are rejected by the automation start API before process launch. The wrapper accepts only the confirmed bundled-App-Shell supervisor path and rejects generic remote-debugging or sandbox-weakening inputs. Fake-channel tests qualify this orchestration contract only; no live Discord transcript has passed it yet.
- Privacy-safe Gecko memory attribution v1. Exact cgroup-sample and Runtime Driver hashes bind a closed PID/start-time role-evidence document, while the persistable result exposes only fixed coarse role buckets and aggregate PSS/RSS/private/swap/security counts. Every scope process, including runtime supervisors, must be represented exactly once; unknown roles remain a visible `partial` bucket and total summed PSS must reconcile byte-for-byte with the authoritative cgroup sample. The aggregate contains no PID list, process name, command line, environment, map, origin, URL, message, or cgroup path. Unit evidence is synthetic; no native Gecko role inventory is claimed yet.
- A dependency-free Python 3.11+ build/test scaffold. Package version remains `0.1.0`.

## Offline Firefox 154 candidate

The package contains only the canonical source lock, never the Firefox archives or assembled tree. Obtain the five named inputs separately from the lock's Mozilla release URLs, then assemble with explicit absolute paths and no network access:

```sh
export PYTHONPATH="$PWD/src"
python3.11 -m zen_app_runtime.gecko_runtime_candidate assemble \
  --checksums "$PWD/build/gecko-firefox-154.0/inputs/SHA256SUMS" \
  --checksums-signature "$PWD/build/gecko-firefox-154.0/inputs/SHA256SUMS.asc" \
  --signing-key "$PWD/build/gecko-firefox-154.0/inputs/KEY" \
  --runtime-archive "$PWD/build/gecko-firefox-154.0/inputs/firefox-154.0.tar.xz" \
  --source-archive "$PWD/build/gecko-firefox-154.0/inputs/firefox-154.0.source.tar.xz" \
  --output "$PWD/build/gecko-firefox-154.0/candidate"
python3.11 -m zen_app_runtime.gecko_runtime_candidate verify \
  --candidate "$PWD/build/gecko-firefox-154.0/candidate"
```

The checked-in lock SHA-256 is `037cd82a1e840ab5734a5fa54263bf62c07bc53e5d058512b2211e1030f9db3b`; the accepted runtime tree SHA-256 is `52c63263f1488d75ea0819138fad02c23edad360e1396751ee3c957fd960c2b0`. Assembly is missing-output-only and rejects overwrite. It invokes only tokenized `/usr/bin/gpg`; it never invokes a downloader, shell, build tool, or browser. The descriptor deliberately contains no input/output path, user, host, PID, timestamp, profile, URL history, or benchmark data.

## Offline Chromium/CEF candidate

The package contains the canonical Chromium/CEF source lock, Python assembler/verifier, and the two project-authored shell source files. It never contains the Arch package or detached signature, packaging checkout, extracted SDK, build/cache directories, compiled shell, assembled candidate, system sandbox binary, profile, or fixture data. The qualified host used the exact signed Arch package inputs and packaging checkout below; assembly is offline and the output path must not already exist:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3.11 -m zen_app_runtime.chromium_runtime_candidate assemble \
    --package /var/cache/pacman/pkg/cef-151.3.24-1-x86_64.pkg.tar.zst \
    --package-signature /var/cache/pacman/pkg/cef-151.3.24-1-x86_64.pkg.tar.zst.sig \
    --packaging-source "$PWD/build/chromium-cef-151.3.24-1/arch-packaging" \
    --output "$PWD/build/chromium-cef-151.3.24-1/candidate-a"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3.11 -m zen_app_runtime.chromium_runtime_candidate assemble \
    --package /var/cache/pacman/pkg/cef-151.3.24-1-x86_64.pkg.tar.zst \
    --package-signature /var/cache/pacman/pkg/cef-151.3.24-1-x86_64.pkg.tar.zst.sig \
    --packaging-source "$PWD/build/chromium-cef-151.3.24-1/arch-packaging" \
    --output "$PWD/build/chromium-cef-151.3.24-1/candidate-b"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3.11 -m zen_app_runtime.chromium_runtime_candidate verify \
    --candidate "$PWD/build/chromium-cef-151.3.24-1/candidate-a"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3.11 -m zen_app_runtime.chromium_runtime_candidate verify \
    --candidate "$PWD/build/chromium-cef-151.3.24-1/candidate-b"
```

The canonical lock SHA-256 is `fd802275a477c912f9c54cf8f967855008a11449c63ae667f622a60875f6007a`. It binds shell source-tree SHA-256 `b513041ef07d364379844b4862f8f057fd78753bcfa45d99b37b3c38cd31921e`, `main.cc` SHA-256 `a02607787b51fc052a431cf314541f4b0c6fc26b34323373705d6f16328e8e67`, and exact built `zen-cef-shell` SHA-256 `0915e3b5fc3d7d439a4ab3469faabce5c88f204325c774db2ae14dd0dda49ebc` (`1280800` bytes). The accepted 13-file runtime is `326364196` bytes with ChromiumRuntimeTree v1 SHA-256 `1549fcc1ff8759f64a25919840260cf23b6f7193b74f0d34ca81dec033e48c74`.

The qualified 2026-08-31 dual assembly produced descriptor SHA-256 `56e998b67ab1023eefb58a1cf407f3fdb280f9f2b195c011dd06b2bf7200091c` and 16-entry normalized inventory SHA-256 `9f017a70c0b1df045117fa39390136da10aa1525dacd26a7a7696f8a8b22cd74` for both outputs. The shell is PIE with RELRO/BIND_NOW, non-executable stack, stack protector, and `$ORIGIN`-only RUNPATH. Runtime use additionally requires the exact root-owned `/usr/lib/cef/chrome-sandbox`, mode `4755`, hash-pinned and revalidated at open and immediately before execution; no user-owned sandbox copy or sandbox-weakening switch is accepted.

CEF/Chromium source compilation was not performed, so `cefSourceBuildStatus=not-performed` and `cefSourceReproducibilityStatus=not-tested` remain mandatory. Wayland/PipeWire screen-share parity is `blocked-upstream-cef-4053` ([CEF issue 4053](https://github.com/chromiumembedded/cef/issues/4053)); the reference branch therefore denies media permissions rather than bypassing portal consent. In the qualified screenshot-free local-fixture smoke, the renderer had `Seccomp=2` and `NoNewPrivs=1`, while the GPU process had `Seccomp=0` and `NoNewPrivs=1`; no `--no-sandbox`, `--disable-setuid-sandbox`, `--disable-gpu`, or remote-debugging switch was present. This is explicit host/build-specific evidence, not a claim of GPU seccomp parity, live Discord/account/media/portal coverage, general Linux qualification, or soak completion.

## Quick start

From the repository root:

### One HTTPS link to a launchable web app

Create an operator-supplied lock for the exact local Zen executable first. The digest below is the qualified local Zen 1.21.15b input; after any package update, independently establish the new executable digest instead of copying the old value.

```sh
export PYTHONPATH="$PWD/src"
umask 077
cat > "$PWD/runtime.lock.json" <<'JSON'
{
  "runtimeLockVersion": 1,
  "runtime": {
    "path": "/opt/zen-browser-bin/zen-bin",
    "sha256": "0f5e46fecd345615d0d8d9c7515fa2df0971eae4a4c82f4bd3170276de19c66f"
  }
}
JSON
```

Preview the complete onboarding plan. This command canonicalizes the URL, derives a stable local name and collision-resistant app ID, and performs no store/profile/runtime-lock read or mutation:

```sh
python3.11 -m zen_app_runtime app onboard \
  "https://example.com" \
  --runtime-lock "$PWD/runtime.lock.json" \
  --bundled-app-shell \
  --launch
```

Review the JSON, then explicitly install the definition/profile and launch it:

```sh
python3.11 -m zen_app_runtime app onboard \
  "https://example.com" \
  --runtime-lock "$PWD/runtime.lock.json" \
  --bundled-app-shell \
  --launch --yes
```

`--name "My App"` and `--app-id my-app` override the network-free defaults. `--profile-root` defaults directly to `$XDG_DATA_HOME/zen-app-runtime` or `~/.local/share/zen-app-runtime`, allowing the managed-profile authority to create that root without implicitly creating unverified intermediate directories; the definition store keeps its existing XDG config default. Repeating the exact confirmed command is idempotent. An explicit app-ID collision with different content fails closed and is never overwritten. The command prints the derived `appId`, which can subsequently be used with `app status`, `app inspect`, and `app launch` plus the same `--bundled-app-shell` option.

With `--bundled-app-shell`, the first mapped window is polished privileged onboarding and no site request occurs until **Open APP** is activated. Normal Zen sidebar, tab strip, and free-form address bar are absent. After launch, web content uses the complete window; the glass security chrome occupies no layout space, stays hidden by default, and is revealed from the top-edge handle or with `Ctrl+L`, then can be hidden explicitly. `--mode Transparent` requests an alpha-capable transparent top-level XUL surface without applying fractional opacity to the whole window. For the exact `discord` app at `https://discord.com`, the adapter also registers its pinned, origin-scoped Zen Internet-derived USER_SHEET so Discord's own opaque canvas exposes that alpha channel; other websites can still cover it with their own backgrounds. Omit the bundled flag to retain the original stock-Zen legacy isolated-browser path and normal browser chrome. The bundled and external `--app-shell-capability` modes are mutually exclusive.

### Full workflow and lower-level commands

```sh
export PYTHONPATH="$PWD/src"
python3 -m unittest discover -s tests -v

# Read-only host/session capability report. The runtime lock is optional.
python3 -m zen_app_runtime doctor \
  --runtime-lock "$PWD/runtime.lock.json" --display-backend auto

# High-level exact install: verify lock, create/reuse definition, provision profile.
python3 -m zen_app_runtime --store "$PWD/demo-apps" app install example-mail \
  --name "Example Mail" --url https://mail.example.com \
  --mode Opaque --with-stylus \
  --profile-root "$PWD/demo-profiles" \
  --runtime-lock "$PWD/runtime.lock.json"

python3 -m zen_app_runtime --store "$PWD/demo-apps" app status example-mail \
  --profile-root "$PWD/demo-profiles" \
  --runtime-lock "$PWD/runtime.lock.json" \
  --display-backend auto
python3 -m zen_app_runtime --store "$PWD/demo-apps" app inspect example-mail \
  --profile-root "$PWD/demo-profiles" \
  --runtime-lock "$PWD/runtime.lock.json" \
  --display-backend auto
python3 -m zen_app_runtime --store "$PWD/demo-apps" app launch example-mail \
  --profile-root "$PWD/demo-profiles" \
  --runtime-lock "$PWD/runtime.lock.json" \
  --display-backend auto --yes

# Safe after an interrupted/partial install; exact existing bindings are idempotent.
python3 -m zen_app_runtime --store "$PWD/demo-apps" app repair example-mail \
  --profile-root "$PWD/demo-profiles" \
  --runtime-lock "$PWD/runtime.lock.json"
```

The lower-level `create`, `list`, `show`, `runtime verify`, `profile provision`, `launch inspect`, and `launch execute` commands remain available and retain their existing contracts.

The runtime lock is supplied by the operator; this project never generates or downloads it. Its exact schema-v1 shape is:

```json
{
  "runtimeLockVersion": 1,
  "runtime": {
    "path": "/opt/zen/zen",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}
```

Replace the example digest with an independently established lowercase SHA-256 for the exact executable. Prefer the real executable rather than an unpinned shell wrapper. Provisioning is required before launch inspection or execution. `app launch` and low-level `launch execute` start a process only with `--yes`; without confirmation they fail before store access or launcher state creation.

## Terminal onboarding and app workflow v1

`app onboard URL` is a separately versioned URL-to-webapp convenience boundary: WebAppOnboardingPlan v1 and WebAppOnboardingResult v1. It accepts only an absolute credential-free HTTPS link, canonicalizes scheme/IDNA host/default port without fetching the page or following redirects, preserves path/query/fragment, and derives the display name locally. Its default app ID combines a bounded host slug with the first ten hexadecimal characters of SHA-256 over the complete canonical URL, so distinct paths on one host remain distinct. Explicit `--name` and `--app-id` values still pass the strict manifest validators.

Without `--yes`, onboarding renders a deterministic `confirmation: required` plan and does not read the supplied runtime lock or create a store/profile. With `--yes`, it delegates unchanged to authoritative runtime verification, exact definition installation, and profile provisioning. Omitting `--bundled-app-shell` preserves WebAppOnboardingPlan/Result v1, `legacy-isolated-browser`, and `inBrowserAppShell: false`. Selecting it emits plan/result v2, verifies the closed bundled adapter before mutation, reports `bundled-privileged-app-shell`, and makes optional `--launch` cross the policy-fd/handshake authority. A launch failure may leave an already-valid definition/profile, and rerunning the exact onboarding command safely repairs/reuses that state.

`doctor` is persistent-filesystem read-only. It reports `doctorReportVersion: 1`, `overallStatus: ready|blocked`, `/proc/self/fd` and sealed-memfd support, the requested/resolved display backend, and optional runtime-lock verification. A verified runtime declares `executionAuthority: sealed-snapshot|trusted-root-file`; omitting the lock reports `not-requested`. The anonymous memfd capability probe creates no persistent state.

`app status` is also read-only and emits `workflowStatusVersion: 1`: exact definition digest, runtime `verified|invalid`, profile `ready|blocked|unchecked`, display `legacy-v1|available|unavailable`, and aggregate `ready|blocked`. Omitting `--display-backend` intentionally preserves legacy launch-ticket v1 behavior. `app inspect` emits the exact existing launch-ticket v1/v2 document without an extra workflow envelope.

Exit codes distinguish readiness from malformed or unsafe input:

- `0`: valid ready doctor/status report, or successful workflow command;
- `1`: valid doctor/status JSON whose operational state is blocked;
- `2`: argument, input, safety, authority, or execution-precondition error.

`app install` validates the manifest and runtime lock before store mutation. It exclusively creates a missing definition or accepts only an exact existing definition, then delegates profile creation to the authoritative provisioner. Repeating the same install is idempotent; different existing content fails closed and is never overwritten. If profile provisioning fails after definition publication, that exact definition deliberately remains so `app repair` can complete the profile safely. `app repair` never changes the definition and accepts only its exact existing runtime/profile binding.

There is deliberately no high-level `app uninstall`: deleting only a definition would misleadingly leave profile data, while no reviewed profile-delete authority exists. The lower-level `remove --yes` removes only an app definition and must not be treated as profile cleanup. The workflow performs no runtime discovery, automatic trust, download, update, or lock generation, and never accesses a default Zen/Firefox profile.

## App Shell v2 boundary

App Shell v2 freezes the UX/security requirements that privileged browser code must enforce before loading site content:

- versioned custom onboarding before the first site commit, with no default-profile import;
- hidden Zen sidebar, tab strip, and free-form address bar;
- a revealable floating security surface whose exact origin, TLS/permission state, and security panel remain available without reserving page layout space;
- same-origin internal navigation decided at final top-level commit, including redirects;
- external-by-default popups, explicit custom-protocol confirmation, and deny-unless-pinned authentication callbacks; the bundled adapter can create a session-only exact-origin pin only after privileged confirmation when either a recognized callback parameter resolves to the app/already pinned authentication origin, or an active committed same-site authentication flow requests an explicit account-enrollment path;
- fixed shortcut allow/remap/block sets, including `Ctrl+L` → origin/security panel and `Ctrl+W` → close app; Discord's `Ctrl+K` and `Ctrl+Shift+N` remain content-visible, while normal browser tab/window/address/devtools/fullscreen shortcuts remain blocked;
- exact-origin/document/navigation-epoch-bound, 30-second, one-outstanding-request permission and download mediation in privileged parent chrome;
- session-only camera, microphone, speaker-selection, shared-audio, and screen-source decisions with no silent or persisted grants;
- a mandatory system Save picker for downloads, no hidden default destination, no automatic opening, app-specific window identity, and no additional normal browser windows.

Two mutually exclusive authorities are available. `--app-shell-capability ABSOLUTE_PATH` keeps the external-adapter contract: strict bounded JSON must identify `org.zen-app-runtime.app-shell`, capability/protocol v2, the exact ten-capability set, and the exact verified RuntimeIdentity. `--bundled-app-shell` instead opens `src/zen_app_runtime/browser_adapter_bundle/` through a no-follow descriptor walk, requires the exact eight-file set and pinned per-file hashes, checks canonical browser-adapter-bundle v2 tree SHA-256 `3dbc0f9459b3aaf0daa1a1b32a00c531e70f9aa3e593f482c57ae23ed039e45a`, retains the directory fd, and invokes Gecko with `-app /proc/self/fd/N/application.ini`. Both modes fail before store/profile mutation on authority mismatch; omitting both preserves workflow/install/repair v1 and LaunchTicket v1/v2 behavior.

Bundled status/install/repair documents use v3 and inspection uses v2 so they cannot be confused with legacy or an operator-claimed external adapter. During execution the site URL is absent from child argv. The canonical policy is placed in a bounded, read-only, `F_SEAL_WRITE|GROW|SHRINK|SEAL` memfd; only its descriptor number is inherited. A separate one-way pipe carries one exact AppShellHandshake v2. The adapter validates the closed policy and canonical bytes, registers the optional site theme and global/window handlers, installs and probes the parent mediation authorities, hashes the exact policy, writes `ready-before-site-content`, and only then enables its browser-chrome confirmation button. No remote browser exists during onboarding. After confirmation it creates one transparent remote browser and attaches popup and top-level progress enforcement before the first `loadURI`. Missing, malformed, trailing, mismatched, timed-out, or early-exit ACK drains the whole launch session and fails.

The bundled Gecko application supplies unmistakable pre-content onboarding and full-window site content, so normal Zen sidebar, tab strip, and address/search chrome are absent by construction. Its origin/security controls are a translucent overlay: hidden by default, revealable from the top edge or with `Ctrl+L`, explicitly hideable, and never reserve page layout space. It checks top-level request starts and committed locations against the canonical HTTPS origin. Ordinary cross-origin HTTPS remains outside the app and requires confirmation for the system browser. Authentication origins are exact, transient, privileged-confirmation pins; callback-free account enrollment is allowed only from an already committed pin to the same registrable domain and an explicit enrollment path. Returning to the app origin clears all pins. Custom protocols remain separately confirmed, extra normal windows stay blocked, and only the exact Gecko WebRTC indicator chrome URI is exempt while sharing is active.

Gecko's `fxrPermissionPrompt` hook distinguishes content permissions from WebRTC requests. Every content permission requires transient content activation plus a trusted shell click. Camera, microphone, speaker output, shared-audio, and non-Wayland screen/window sources use explicit selectors without exposing device labels in persisted evidence. A Wayland screen request may only continue to Gecko's xdg-desktop-portal path; the shell never selects a source silently, stores a restore token, persists a grant, or claims portal consent. Multiple/malformed portal sources, requested shared audio without a source, unsupported WebRTC combinations, navigation/reload/close, expiry, and replays fail closed. Downloads are intercepted through the exact Gecko 154 `nsIHelperAppLauncherDialog` factory contract (`createInstance(iid)`), source-origin-bound, filename-sanitized, confirmed in dedicated chrome, and delegated to `nsIFilePicker.modeSave`; cancel, navigation, shutdown, or factory errors never fall back to a default directory or auto-open. The original factory CID is restored on unload.

`Transparent` requests an alpha-capable XUL/browser surface and never reduces whole-window opacity. For the exact Transparent `discord` policy at `https://discord.com`, the adapter synchronously registers `discord-zen-internet.css` as a USER_SHEET before its ready ACK; the CSS has no imports, is additionally scoped with an exact HTTPS `@-moz-document` rule, and is adapted under MIT from Sameera's Zen Internet `my-internet` commit `f3a6453942894205c8644c5b0aefb502706a462e`. Other opaque website canvases may still cover the alpha channel. Explicit prefs retain remote/Fission content, Zen-qualified sandbox levels 6/2, mixed-content blocking, cookie behavior 5, tracking protection, real permission/capture plumbing, and disabled telemetry/update/devtools surfaces. App Shell 1.4.0 also retains the 1.3.0 security-preserving cache/process reductions: no speculative process prelaunch, bounded memory-backed network/image/media caches with disk/redecode fallback, and no persistent theme transition or GPU blur. This is an enforceable parent-chrome minimum, not proof of live Discord parity or every browser edge case.

Two release limitations remain explicit. First, a user-owned editable package tree can be modified by another same-UID process after final verification; descriptor retention closes pathname replacement but production-grade immutable authority requires a root-owned/non-user-writable installation or stronger staging. Second, Zen 1.21.15b custom `-app` maps `resource:///` to the custom application directory while several libxul startup components still probe modules normally found in Zen's `browser/omni.ja`. Known missing-resource warnings include `policies/schema.sys.mjs`, `BrowserContentHandler.sys.mjs`, `TaskbarTabsCmd.sys.mjs`, `DevToolsStartup.sys.mjs`, `SelectableProfileService.sys.mjs`, and `sessionstore/SessionStoreFunctions.sys.mjs`. They did not block policy validation, ACK, onboarding, remote content, or sandbox evidence in the qualified run, but other Zen versions/package layouts are unsupported until separately pinned and tested.

A capability authority has this exact shape, with the runtime identity replaced by the independently verified executable identity:

```json
{
  "adapterCapabilityVersion": 2,
  "adapterId": "org.zen-app-runtime.app-shell",
  "adapterProtocolVersion": 2,
  "capabilities": [
    "download-mediation-v2",
    "file-picker-boundary-v1",
    "minimal-security-chrome-v1",
    "navigation-policy-v1",
    "onboarding-v1",
    "permission-mediation-v2",
    "shortcut-policy-v2",
    "startup-handshake-v2",
    "wayland-screencast-portal-v1",
    "window-identity-v1"
  ],
  "runtimeIdentity": {
    "identityVersion": 1,
    "path": "/opt/zen/zen-bin",
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}
```

## Display backend and ticket contract

Display selection is session-specific and deliberately does not modify manifest v1, runtime-lock v1, runtime-identity v1, profile markers, launch-plan v1, or Desktop preview output.

- If `--display-backend` is omitted, the pre-existing launch-ticket v1 JSON and legacy child-environment contract remain byte-compatible.
- If the option is explicitly `auto`, `wayland`, or `x11`, inspection returns strict `launchTicketVersion: 2` JSON with exactly the additional declaration `"displayBackend": {"requested": "...", "resolved": "wayland|x11"}`.
- `auto` deterministically prefers a valid Wayland declaration, then a valid X11 declaration. A present but malformed Wayland declaration fails closed rather than silently changing transport.
- Wayland requires a safe basename in `WAYLAND_DISPLAY` and an absolute lexically normalized `XDG_RUNTIME_DIR`. The child gets launcher-authored `GDK_BACKEND=wayland` and `MOZ_ENABLE_WAYLAND=1`; `DISPLAY` and `XAUTHORITY` are removed.
- X11 requires `DISPLAY`. An optional `XAUTHORITY` must be absolute and lexically normalized. The child gets launcher-authored `GDK_BACKEND=x11`; `WAYLAND_DISPLAY` and inherited arbitrary `MOZ_*` forcing variables are removed.
- Missing or unsafe declarations fail before child launch. The environment is snapshotted once while constructing the ticket so inspection and execution do not re-resolve the backend from mutable ambient state.

`launch inspect` remains inert for both ticket versions. Tickets describe the verified launch but are not replayable authority; `launch execute` reopens and verifies the authoritative runtime/profile binding.

## Verified launch boundary

The lock, runtime, profile root, and store paths must be explicit absolute normalized paths where required. Security-sensitive traversal starts at `/`, follows no symlinks, and requires ancestors/files to be owned by root or the effective user and not group/world writable. Runtime locks are duplicate-free bounded UTF-8 JSON. The runtime must be a regular readable file executable by the effective user; it is hashed from its already-open descriptor, full identity is checked around hashing, and the lowercase lock digest is compared in constant time.

`launch execute` creates or validates only these effective-user-owned mode-0700 directories below the verified profile:

```text
PROFILE/.zen-app-runtime/home/
PROFILE/.zen-app-runtime/cache/
PROFILE/.zen-app-runtime/config/
PROFILE/.zen-app-runtime/data/
PROFILE/.zen-app-runtime/state/
PROFILE/.zen-app-runtime/tmp/
```

It opens `.zen-app-runtime-launch.lock` as a single-link mode-0600 regular file and takes nonblocking exclusive `flock`. Runtime authority has two execution-safe forms: a root-owned executable that is not writable by the effective user remains on its retained package fd so Zen can resolve adjacent installation resources, while an effective-user-owned executable is copied while hashing into a mode-0500 Linux memfd and sealed against write, grow, shrink, and seal changes. The retained authority is revalidated and re-hashed immediately before spawn. Path replacement cannot redirect either form; later source-file mutation cannot affect a sealed snapshot.

The public legacy logical argv remains `RUNTIME --no-remote --new-instance --profile PROFILE_PATH START_URL`. Execution is intentionally different but non-serializable: `Popen(executable=/proc/self/fd/RUNTIME_FD, ...)` receives the logical runtime as argv[0], substitutes the retained profile directory as `/proc/self/fd/PROFILE_FD`, and rewrites only profile-local HOME/XDG/TMP values to that same retained profile authority. Legacy execution passes exactly runtime and profile descriptors. External App Shell execution removes the site URL and adds sealed-policy-read plus handshake-write descriptors. Bundled mode additionally passes the verified adapter directory descriptor and inserts `-app /proc/self/fd/ADAPTER_FD/application.ini`; policy/handshake fd numbers are also authored into the sanitized child environment because privileged Gecko JS cannot consume unknown command-line switches directly. Tickets, policies, capabilities, and markers remain fd-free.

The supervisor starts a new session, keeps runtime/profile authority and the profile lock until that launch session has no live members, and forwards SIGHUP/SIGINT/SIGQUIT/SIGTERM with statuses 129/130/131/143 when the leader terminates by signal. If the direct leader exits while same-session processes remain, it performs bounded SIGTERM then SIGKILL draining before releasing the lock. This contains the normal Zen process tree; it is not a cgroup and does not claim to contain a deliberately malicious child that creates a separate session. The child otherwise receives fixed safe `PATH` plus only the audited GUI/session allowlist; host HOME/PATH, `LD_*`, `PYTHON*`, arbitrary `MOZ_*`, and unknown variables are omitted.

This is lifecycle and environment isolation, not a replacement for Firefox/Zen sandboxing, Fission, Safe Browsing, TLS validation, or permission UI.

## Qualified local Zen integration

The opt-in native test qualified this installation on 2026-08-29:

- Arch package: `zen-browser-bin 1.21.15b-1`
- verified executable: `/opt/zen-browser-bin/zen-bin` (root-owned mode 0755 x86-64 PIE ELF)
- SHA-256: `0f5e46fecd345615d0d8d9c7515fa2df0971eae4a4c82f4bd3170276de19c66f`
- reported version: `Mozilla Zen 1.21.15b`
- BuildID: `20260818101929`; Zen SourceStamp: `cee4147767801299dec330c81318c01e5a39e6ec`; Gecko: `154.0`

`/usr/bin/zen-browser` was not used as runtime authority because it is a Bash wrapper that execs `/opt/zen-browser-bin/zen-bin`. The test used only temporary managed definitions/profiles under the repository, never `~/.mozilla`, `profiles.ini`, or any existing Zen/Firefox profile. Each cold/warm phase invoked the complete `app onboard https://example.com/ --launch --yes` URL-to-webapp flow, so the first phase created the definition/profile and the second proved exact idempotent reuse before real GUI/network launch.

Each backend used one cold and one warm run on the same managed profile. Startup is measured until profile initialization and a content process are observed; memory is sampled three seconds later. PSS is the primary shared-memory-aware baseline; summed RSS is supplementary and double-counts shared pages.

| Backend / phase | Startup | Processes | Content | Seccomp-filtered content | NoNewPrivs content | PSS | Summed RSS | fd-backed profile | X11 window |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| Wayland cold | 1987 ms | 15 | 11 | 10 | 10 | 611791 KiB | 1779856 KiB | yes | no |
| Wayland warm | 2423 ms | 18 | 11 | 10 | 10 | 555661 KiB | 1744288 KiB | yes | no |
| X11/XWayland cold | 1772 ms | 15 | 11 | 10 | 10 | 610251 KiB | 1777968 KiB | yes | yes |
| X11/XWayland warm | 1661 ms | 18 | 11 | 10 | 10 | 570972 KiB | 1751724 KiB | yes | yes |

All four remediated legacy runs observed the retained `/proc/self/fd/N` profile contract in the Zen command/environment, handled SIGTERM as exit 143, and left no live member in the recorded runtime session when identified by session ID plus process start time. No `--no-sandbox` or `MOZ_DISABLE_CONTENT_SANDBOX` setting was present. These are single-host smoke snapshots, not statistically significant benchmarks, and they supersede the pre-remediation measurements.

`tests/test_real_app_shell.py` separately qualifies the bundled adapter against the same executable. The current App Shell 1.4.0 headless phase observed the privacy-bounded no-activation/malformed-permission and invalid-download-context self-test plus `MEDIATION_READY permission download xdg-desktop-portal` before consuming the exact v2 ACK. It then proved `MEDIATION_READY < ONBOARDING_READY < CONTENT_REQUESTED < CONTENT_COMMITTED`, successful unload restoration of the original helper-app factory, exit 0, and no remaining process in the launch session. Earlier native-Wayland and X11/XWayland GUI phases used the complete confirmed `app onboard URL --bundled-app-shell --launch --yes` supervisor path: before synthetic Enter, onboarding was mapped as class `zen-app-shell` and no content-request marker existed; afterward `https://example.com/` committed in a remote browser content process with `Seccomp=2`, `NoNewPrivs=1`, `-sandboxReporter`, and `-chrootClient`, then exit 143 and an empty original runtime session.

### Current-Zen optimization decision

`runtime_optimization.py` and `native_app_shell_benchmark.py` add a closed AppShellOptimizationReport v1 contract plus an opt-in native Wayland A/B harness. The harness fixes the public `https://example.com/` probe internally, launches each worker in the existing transient cgroup-v2 scope, samples pre-content onboarding, post-commit active memory, and two quiescent idle CPU/memory points, alternates baseline/candidate order across three cold/warm pairs, and requires at least one new Seccomp-filtered+NoNewPrivs PID incarnation after commit. It stores aggregate process/memory/security counts only—no PID, cgroup path, command line, environment, URL, account/message content, screenshot, or free-form diagnostic—and requires every trial scope to disappear.

The exact Gecko 154 `omni.ja` default source (`greprefs.js`, SHA-256 `f5359324dc1d863c72b9804b24a48bda198342f0eda5f6162fad1f60f1656ac5`) defines `javascript.options.mem.gc_min_empty_chunk_count=1`. Twelve native-Wayland runs compared that upstream fallback with the one-variable candidate `0`. The candidate stayed within startup and active-memory limits (cold/warm usable p95 1696/1295 ms; active PSS p95 317188 KiB), but failed the onboarding hard gate (208447 KiB p95 versus 122880 KiB), failed idle CPU (189 basis points p95 versus the strict `<100` limit), regressed median idle CPU from 19 to 42 basis points, and reduced median onboarding/active/idle PSS by only 995/3182/733 KiB—below the 4096 KiB acceptance threshold. All 12 scopes cleaned up and each observed two new secure process incarnations, so security/lifecycle evidence passed while the optimization decision was **rejected**.

Canonical evidence is `docs/zen-runtime-optimization-evidence.json`, SHA-256 `c5786ac78b7f358a3614e501464fd599ea0467d434644270e21824df8df8d7b5`. The candidate existed only in temporary profiles and was not promoted. App Shell therefore remains `1.4.0`; the eight pinned file hashes and bundle tree SHA-256 `3dbc0f9459b3aaf0daa1a1b32a00c531e70f9aa3e593f482c57ae23ed039e45a` remain unchanged. This controlled public-page slice does not populate the authenticated Discord, 10-minute active, voice/video/screen-share, general-Linux, or 24-hour soak gates.

Run the exact harness only in an isolated native Wayland session:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3.11 -m zen_app_runtime.native_app_shell_benchmark \
    --runtime /opt/zen-browser-bin/zen-bin \
    --runtime-sha256 0f5e46fecd345615d0d8d9c7515fa2df0971eae4a4c82f4bd3170276de19c66f \
    --output "$PWD/docs/zen-runtime-optimization-evidence.json"
```

The local session exposes `org.freedesktop.portal.Desktop`, ScreenCast interface version 6 with source types 7/cursor modes 3, and FileChooser interface version 4. This is availability evidence only. No current native camera, microphone, speaker, shared-audio, real download Save picker, WebRTC indicator, or screen-source consent workflow was exercised; no portal source was selected or captured, and those workflows remain **not exercised / incomplete**, not PASS. Live Discord conformance also remains unrun without an account/test fixture. The evidence validates the parent-chrome startup/fail-closed boundary without claiming Discord parity or portal end-to-end success.

The evidence covers native Wayland and X11 through XWayland in a Wayland session; it does **not** claim pure-Xorg/Xvfb, Flatpak, AppImage, or other Zen-version/package compatibility. RAM tuning that disables multiprocess isolation, content sandboxing, or other security protections is intentionally rejected.

Run the default suite (native test skips unless configured):

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3.11 -m unittest discover -s tests -v
```

Run the qualified native smoke explicitly:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
ZEN_REAL_RUNTIME=/opt/zen-browser-bin/zen-bin \
ZEN_REAL_BACKENDS=wayland,x11 \
  python3.11 -m unittest tests.test_real_zen -v
```

Run both qualified App Shell phases explicitly (the GUI phase uses `hyprctl` plus `wtype`/`ydotool`; X11 evidence also uses `xdotool`/`xprop`):

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
ZEN_REAL_RUNTIME=/opt/zen-browser-bin/zen-bin \
ZEN_REAL_APP_SHELL_GUI=1 \
ZEN_REAL_BACKENDS=wayland,x11 \
  python3.11 -m unittest tests.test_real_app_shell -v
```

`ZEN_REAL_BACKENDS` defaults to `wayland,x11`. The host must expose the requested display session; all opt-in tests create only temporary managed profiles and never read a default Zen/Firefox profile.

## Plan, desktop, and managed-profile limits

`plan` and `desktop render` are deterministic render-only commands. They do not check or execute a runtime, create profiles, access a network, or write live XDG state. Plans report presentation/profile/link-policy capabilities as `requested-not-enforced` and extensions as `unsupported`. Desktop output invokes the absolute launcher with `plan`, uses `Terminal=true`, a generic icon, and no StartupWMClass, D-Bus activation, or MIME claim.

Profiles are created only below an explicit root, with `.zen-app-runtime-root.json` and `.zen-app-runtime-profile.json` exact sentinels. Existing profiles are accepted only when the marker exactly binds root, app ID, manifest schema/digest, and runtime identity. There is no delete, adopt, force, migration, discovery, Firefox initialization, or access to default browser profiles.

## Reproducible package build

```sh
mkdir -p dist
PYTHONPATH=src python3 -c \
  'from zen_app_runtime.build_backend import build_wheel, build_sdist; print(build_wheel("dist")); print(build_sdist("dist"))'
sha256sum dist/*
```

The wheel contains Python package source plus the exact eight-file `browser_adapter_bundle/` resource tree, including the pinned Discord theme stylesheet, and the `appmode/` package: add-on sources (built into an XPI only when a profile is rendered), `userChrome.css`, and the vendored Zen Internet catalog with its MIT license. The sdist additionally contains documentation and tests, including both opt-in native harnesses; neither artifact contains Zen, extension archives, user profiles, runtime locks, or generated evidence.

See [architecture.md](docs/architecture.md) for detailed invariants and [build.md](docs/build.md) for release validation and future pinned-upstream strategy.
