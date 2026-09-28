# Build, validation, and upstream strategy

## Local deterministic build

The package uses only the Python 3.11+ standard library. `pyproject.toml` selects the in-tree PEP 517 backend, so building requires no network access or dependency installation. The backend normalizes archive ordering, timestamps, ownership metadata, permissions, gzip headers, and wheel records.

```sh
export PYTHONPATH="$PWD/src"
PYTHONDONTWRITEBYTECODE=1 python3.11 -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/zen-app-runtime-pycache \
  python3.11 -m compileall -q -f src tests
node --check src/zen_app_runtime/browser_adapter_bundle/chrome/content/shell.js
xmllint --noout \
  src/zen_app_runtime/browser_adapter_bundle/chrome/content/shell.xhtml
mkdir -p dist
python3.11 -c \
  'from zen_app_runtime.build_backend import build_wheel, build_sdist; print(build_wheel("dist")); print(build_sdist("dist"))'
sha256sum dist/*
```

The packaging test performs two independent wheel builds and two independent source builds and requires byte-identical pairs. The wheel contains all importable Python modules, including `app_shell_mediation.py`, `gecko_runtime_candidate.py`, and `chromium_runtime_candidate.py`; both canonical locks under `runtime_candidates/`; the exact `chromium_shell/CMakeLists.txt` and `chromium_shell/main.cc` sources; plus the exact eight-file `browser_adapter_bundle/` tree: `application.ini`, two chrome manifests, `shell.xhtml`, `shell.css`, `shell.js`, `discord-zen-internet.css`, and `defaults/preferences/prefs.js`. The sdist additionally contains README, documentation, and the complete test suite, including `tests/test_gecko_runtime_candidate.py`, `tests/test_real_gecko_candidate.py`, `tests/test_chromium_runtime_candidate.py`, `tests/test_real_chromium_candidate.py`, `tests/test_app_shell_mediation.py`, `tests/test_real_zen.py`, and `tests/test_real_app_shell.py`. Neither artifact contains a Zen/Firefox/CEF executable, browser runtime/source archive, Arch package or signature, Arch packaging checkout, extracted CEF SDK, compiler/build cache, assembled Gecko or Chromium candidate or descriptor, system `chrome-sandbox`, copied `omni.ja` module, extension package, user profile, operator runtime lock, capability file, or generated integration evidence.

The bundled adapter is closed code authority, not an arbitrary package-data directory. `browser_adapter.py` rejects missing and unknown entries, symlinks, non-regular files, executable files, multiple links, oversized data, and metadata/identity changes around hashing. Its browser-adapter-bundle v2 canonical tree SHA-256 is `3dbc0f9459b3aaf0daa1a1b32a00c531e70f9aa3e593f482c57ae23ed039e45a`; the retained directory and files are revalidated immediately before spawn.

## Test layers

The default deterministic suite covers:

- exact manifest validation and conservative legacy policy representation;
- descriptor-relative AppStore publication/read/list/removal and strict tombstones;
- byte-stable LaunchPlan v1 and plan-only Desktop preview rendering;
- strict runtime-lock v1 parsing, lexical paths, ownership/modes, symlink/hard-link/race rejection, opened-fd hashing, and RuntimeIdentity v1;
- authoritative managed-profile provisioning, exact sentinels/bindings, idempotency, rollback, rebinding defense, and durability errors;
- DoctorReport v1 platform/runtime/display states and persistent-filesystem read-only behavior;
- high-level app onboarding/install/status/inspect/launch/repair contracts, exact idempotency, ordering before mutation, partial-install preservation, and repair;
- WebAppOnboardingPlan/Result v1 legacy and v2 bundled-shell behavior, including network-free URL normalization and side-effect-free previews;
- WorkflowStatus v1 ready/blocked/unchecked distinctions and explicit Wayland/X11 resolution;
- AppShellPolicy, external AdapterCapability, AdapterProtocol, and Handshake v2 closed contracts plus AppShellMediationRequest/Decision v1 canonical privacy-bounded evidence;
- the exact bundled adapter tree, no-follow descriptor verification, pinned hashes, canonical tree digest, pre-spawn revalidation, branded pre-content onboarding without a speculative renderer, privileged one-shot permission/WebRTC/download mediation, system-picker and Wayland-portal boundaries, exact one-argument helper-app factory ownership/restore, explicit camera/microphone/shared-audio/speaker/source selectors, bounded memory-backed network/image/media caches without Fission/sandbox weakening, alpha-capable transparent-window markup without fractional root opacity or retained GPU blur surfaces, the exact-origin Discord Zen Internet-derived USER_SHEET without broad transition work, top-edge-revealable non-layout chrome, session-scoped callback-proven authentication-origin pinning, and active same-site account-enrollment transitions with exact-origin confirmation;
- bundled workflow/install/repair v3 and inspection v2, mutual exclusion from the external capability path, sealed policy memfd, URL removal from child argv, exact pre-content ACK, bad/missing ACK session drain, and unchanged legacy argv/pass-fd/ticket behavior;
- LaunchTicket v1 compatibility and DisplayLaunchTicket v2 backend-specific sanitized environments;
- retained root-owned runtime-fd or sealed user-owned runtime-memfd execution, fd-backed profile/HOME/XDG/TMP state, launch-session locking, signal propagation, and bounded cleanup;
- canonical GeckoRuntimeSourceLock/Candidate/Tree v1 parsing, offline deterministic dual assembly with mocked signature transport, malformed lock/signature/digest/source-metadata rejection, traversal/absolute/link/hard-link/special tar rejection, normalized output, closed privacy-bounded descriptors, retained-tree tamper/rebind detection, and exact package inclusion/exclusion;
- canonical ChromiumRuntimeSourceLock/ShellSourceTree/SdkTree/RuntimeTree/Candidate v1 parsing; exact package/signature, packaging-source, toolchain, SDK, shell-output, runtime-subset, and root-owned sandbox binding; malformed/noncanonical lock rejection; bounded GnuPG status handling; complete tar traversal/absolute/link/hard-link/special/duplicate rejection; sealed config validation; deterministic no-overwrite assembly; retained-tree tamper/hardlink/rebind detection; and exact wheel/sdist inclusion/exclusion;
- wheel/sdist reproducibility, normalized modes, and exact package-content assertions.

No default test discovers, downloads, or selects a browser. The native tests are explicitly opt-in and use only temporary managed definitions/profiles.

## Offline Firefox 154 candidate assembly

The checked-in source lock is `src/zen_app_runtime/runtime_candidates/gecko-firefox-154.0-linux-x86_64-en-US.lock.json` (SHA-256 `037cd82a1e840ab5734a5fa54263bf62c07bc53e5d058512b2211e1030f9db3b`). Place the five separately obtained Mozilla inputs in an operator cache, then run entirely offline:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3.11 -m zen_app_runtime.gecko_runtime_candidate assemble \
    --checksums "$PWD/build/gecko-firefox-154.0/inputs/SHA256SUMS" \
    --checksums-signature "$PWD/build/gecko-firefox-154.0/inputs/SHA256SUMS.asc" \
    --signing-key "$PWD/build/gecko-firefox-154.0/inputs/KEY" \
    --runtime-archive "$PWD/build/gecko-firefox-154.0/inputs/firefox-154.0.tar.xz" \
    --source-archive "$PWD/build/gecko-firefox-154.0/inputs/firefox-154.0.source.tar.xz" \
    --output "$PWD/build/gecko-firefox-154.0/candidate-a"
```

Run the same command with a fresh `candidate-b` output, verify both, and compare their canonical descriptor bytes and recursive `(relative path, mode, mtime, size, SHA-256)` inventories. Both must identify the 53-file, `329408396`-byte runtime tree SHA-256 `52c63263f1488d75ea0819138fad02c23edad360e1396751ee3c957fd960c2b0`. The qualified 2026-08-31 dual run produced identical descriptor SHA-256 `048603fbb24ddff30ffb3367a83def79a7541191a12078bd16ffe90286c597a2` and identical 67-entry normalized inventory SHA-256 `3c05102ab8fa9daf20011a20c84a75468cf2179de785f78a1a48d3e157c4d32c`. The output operation is no-overwrite; remove only disposable failed-test outputs explicitly rather than weakening publication.

The source archive is tied through `sourcestamp.txt` to BuildID `20260812182057` and `https://hg.mozilla.org/releases/mozilla-release/rev/9ce1ee6baeb9a3c326dbd180bdece65d8fc2eadc`. This is source provenance, not source-build evidence. The lock deliberately says `not-performed`/`not-tested`; those values may change only after an actual pinned offline source build and independent clean-builder comparison.

## Offline Chromium/CEF 151 candidate assembly

The checked-in lock is `src/zen_app_runtime/runtime_candidates/chromium-cef-151.3.24-1-x86_64.lock.json`, canonical SHA-256 `fd802275a477c912f9c54cf8f967855008a11449c63ae667f622a60875f6007a`. It binds the signed Arch `cef-151.3.24-1-x86_64` package and detached signature, closed Arch packaging source inventory for tag `151.3.24-1`, extracted CEF SDK, pinned local tools, exact two-file project shell source tree, expected compiled shell, 13-file runtime subset, and installed sandbox. The qualified offline commands are:

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

The assembler uses the system pacman keyring at `/etc/pacman.d/gnupg`, accepts only the pinned package signature identity, scans the complete package tar and metadata, extracts a transient hash-pinned SDK/runtime subset, and runs the exact root-owned CMake/Clang/Ninja toolchain with `ninja -j1`. It accepts only compiled `zen-cef-shell` SHA-256 `0915e3b5fc3d7d439a4ab3469faabce5c88f204325c774db2ae14dd0dda49ebc` (`1280800` bytes), shell source-tree SHA-256 `b513041ef07d364379844b4862f8f057fd78753bcfa45d99b37b3c38cd31921e`, and final 13-file/`326364196`-byte runtime-tree SHA-256 `1549fcc1ff8759f64a25919840260cf23b6f7193b74f0d34ca81dec033e48c74`. Staging SDK/source/build/home/tmp trees are removed before no-replace publication.

The qualified 2026-08-31 dual run produced identical descriptor SHA-256 `56e998b67ab1023eefb58a1cf407f3fdb280f9f2b195c011dd06b2bf7200091c` and identical 16-entry normalized inventory SHA-256 `9f017a70c0b1df045117fa39390136da10aa1525dacd26a7a7696f8a8b22cd74`. The build is path-independent and hardened as PIE, RELRO/BIND_NOW, non-executable stack, stack protector, and `$ORIGIN`-only RUNPATH. Assembly and verification both fail unless `/usr/lib/cef/chrome-sandbox` is the exact hash-pinned, root-owned/root-group, single-link mode-4755 regular file. The Arch package/cache, detached signature, packaging checkout, transient SDK, compiled binary, and candidate outputs are never wheel/sdist content.

The lock deliberately fixes `cefSourceBuildStatus: not-performed` and `cefSourceReproducibilityStatus: not-tested`; package extraction plus a project-shell build is not a CEF/Chromium source build. CEF Wayland/PipeWire capture parity remains `blocked-upstream-cef-4053`, so no source-build reproducibility or screen-share parity PASS may be inferred from these identical candidate assemblies.

## Qualified native test layers

### Legacy isolated-browser smoke

`tests/test_real_zen.py` exercises the unchanged stock-chrome path through the complete confirmed `app onboard https://example.com/ --launch --yes` workflow. Cold and warm phases require exact profile reuse, content processes, fd-backed profile and HOME authority, backend-specific environment/window evidence, PSS/RSS and sandbox state from `/proc`, supervisor exit 143, and an empty original runtime session after termination.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
ZEN_REAL_RUNTIME=/opt/zen-browser-bin/zen-bin \
ZEN_REAL_BACKENDS=wayland,x11 \
  python3.11 -m unittest tests.test_real_zen -v
```

### Bundled privileged App Shell smoke

`tests/test_real_app_shell.py` has two layers. The current headless phase launches the exact package bundle with Gecko `-app`, requires the native no-activation/malformed-permission and invalid-download-context cancellation self-test plus mediation-readiness marker before consuming the strict v2 ACK, proves `MEDIATION_READY < ONBOARDING_READY < CONTENT_REQUESTED < CONTENT_COMMITTED`, verifies helper-app factory restoration on unload, and requires an empty runtime session:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
ZEN_REAL_RUNTIME=/opt/zen-browser-bin/zen-bin \
  python3.11 -m unittest \
    tests.test_real_app_shell.RealAppShellTests.test_headless_precontent_handshake_then_site_commit -v
```

The GUI phase exercises the complete supervising CLI path. Before synthetic Enter it requires a mapped `zen-app-shell` onboarding window and no site-content request. After confirmation it requires a committed remote browser content process with `Seccomp=2`, `NoNewPrivs=1`, `-sandboxReporter`, and `-chrootClient`; it also verifies backend selection, URL absence from child argv, policy/handshake environment descriptors, exit 143, and session cleanup.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
ZEN_REAL_RUNTIME=/opt/zen-browser-bin/zen-bin \
ZEN_REAL_APP_SHELL_GUI=1 \
ZEN_REAL_BACKENDS=wayland,x11 \
  python3.11 -m unittest \
    tests.test_real_app_shell.RealAppShellTests.test_supervised_wayland_and_x11_onboarding_window_and_sandbox -v
```

### Archive-exact Firefox candidate smoke

`tests/test_real_gecko_candidate.py` opens an assembled candidate through the closed descriptor/tree authority and executes the launcher from its retained distribution fd, preserving adjacent Firefox resources. The headless case repeats the mediation self-test, strict v2 ACK, onboarding-before-content ordering, commit, factory restoration, and empty-session checks without screenshots:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
ZEN_REAL_GECKO_CANDIDATE="$PWD/build/gecko-firefox-154.0/candidate-a" \
  python3.11 -m unittest \
    tests.test_real_gecko_candidate.RealGeckoCandidateTests.test_headless_retained_candidate_app_shell_handshake -v
```

The Wayland case starts a dedicated worker in a transient systemd user scope. The worker reopens/re-hashes the candidate, passes its distribution fd directly to Firefox, keeps Fission/remote-content/sandbox/portal/alpha/hardware-acceleration static invariants, waits for privileged onboarding, uses a synthetic Return activation, requires a later new `Seccomp=2` + `NoNewPrivs=1` process and positive aggregate PSS, then terminates the browser and requires the cgroup to disappear:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
ZEN_REAL_GECKO_CANDIDATE="$PWD/build/gecko-firefox-154.0/candidate-a" \
ZEN_REAL_GECKO_CANDIDATE_WAYLAND=1 \
  python3.11 -m unittest \
    tests.test_real_gecko_candidate.RealGeckoCandidateTests.test_wayland_cgroup_app_shell_sandbox_smoke -v
```

This smoke performs no Discord login, message access, media capture, portal selection, or screenshot. The qualified 2026-08-31 official candidate run passed the retained-fd headless case in `3.295 s` and the native-Wayland transient-cgroup case in `3.371 s`; the latter observed a post-onboarding secure process and complete scope removal. It is runnable-candidate evidence, not live Discord parity, a memory hard-gate result, general-Linux qualification, or a source-built artifact claim.

### Chromium/CEF candidate smoke

`tests/test_real_chromium_candidate.py` is an explicit screenshot-free native-Wayland fixture test. It opens and revalidates the assembled candidate, creates a fresh mode-0700 profile, serves only an explicit `127.0.0.1` HTTP fixture, transfers the closed six-line configuration through a fully sealed memfd, and launches the retained candidate executable in a transient systemd user scope. It requires CEF Views onboarding before any content request, synthetic keyboard activation, ordered browser creation/content commit markers, positive pre/post-content PSS, a new secure process, renderer and GPU processes, URL/profile absence from CEF child argv, no sandbox/GPU/remoting-weakening switch, normal stop, and complete cgroup/process/window cleanup:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
ZEN_REAL_CHROMIUM_CANDIDATE="$PWD/build/chromium-cef-151.3.24-1/candidate-a" \
ZEN_REAL_CHROMIUM_CANDIDATE_WAYLAND=1 \
  python3.11 -m unittest -v tests.test_real_chromium_candidate
```

The qualified 2026-08-31 run passed in `3.524 s`. Its renderer had `Seccomp=2` and `NoNewPrivs=1`; the GPU process was present with `Seccomp=0` and `NoNewPrivs=1`. The latter is an explicit limitation of this exact Arch/CEF build, not GPU sandbox parity and not permission to add `--no-sandbox`, `--disable-setuid-sandbox`, or `--disable-gpu`. The smoke performs no live Discord/account operation, actual permission or media request, portal selection, capture, screenshot, general-Linux matrix, memory acceptance gate, or soak test.

### Current-Zen optimization A/B harness

`tests/test_real_app_shell_optimization.py` opts into the fixed native-Wayland controller in `native_app_shell_benchmark.py`. It runs three alternating baseline/candidate pairs with cold and warm phases (12 scopes total), samples pre-content onboarding, post-commit active PSS, and a quiescent 10-second idle CPU interval through `CgroupV2Reader`, verifies new Seccomp+NoNewPrivs PID incarnations without command inspection, and requires every transient scope to disappear. The worker accepts no arbitrary URL, script, selector, debugger, credential, or screenshot authority. Its one candidate is exact Gecko 154 `javascript.options.mem.gc_min_empty_chunk_count=0`; the upstream value `1` is the fallback.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
ZEN_REAL_RUNTIME=/opt/zen-browser-bin/zen-bin \
ZEN_REAL_APP_SHELL_OPTIMIZATION=1 \
  python3.11 -m unittest \
    tests.test_real_app_shell_optimization.RealAppShellOptimizationTests.test_exact_wayland_cold_warm_baseline_candidate_matrix -v
```

The equivalent direct command writes canonical aggregate evidence:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python3.11 -m zen_app_runtime.native_app_shell_benchmark \
    --runtime /opt/zen-browser-bin/zen-bin \
    --runtime-sha256 0f5e46fecd345615d0d8d9c7515fa2df0971eae4a4c82f4bd3170276de19c66f \
    --output "$PWD/docs/zen-runtime-optimization-evidence.json"
```

The qualified 12-run report SHA-256 is `c5786ac78b7f358a3614e501464fd599ea0467d434644270e21824df8df8d7b5`; its decision is `rejected` for onboarding PSS, idle CPU, idle CPU regression, and insufficient memory reduction. The candidate was temporary and is not part of `prefs.js`, so no App Shell version, file digest, or tree digest changed. Packaging assertions require `runtime_optimization.py` and `native_app_shell_benchmark.py` in the wheel and all three related test modules in the sdist. The generated host-specific JSON report remains deliberately outside wheel/sdist artifacts.

`ZEN_REAL_BACKENDS` defaults to `wayland,x11`. The qualified host uses native Wayland and X11 through XWayland; the GUI harness uses `hyprctl`, `wtype`/`ydotool`, and for X11 `xdotool`/`xprop`. Pure Xorg/Xvfb, Flatpak, AppImage, other distributions, other Zen builds, and broad compatibility are not claimed.

The qualified 2026-08-29/30 input is `/opt/zen-browser-bin/zen-bin` from Arch package `zen-browser-bin 1.21.15b-1`, SHA-256 `0f5e46fecd345615d0d8d9c7515fa2df0971eae4a4c82f4bd3170276de19c66f`, reporting Gecko 154.0. This is host-specific evidence, not a dependency or built-in trust anchor. `/usr/bin/zen-browser` is a shell wrapper and is not accepted as the pinned runtime authority.

Custom `-app` mode on this exact Zen build logs missing `resource:///modules/...` compatibility warnings for startup registrations that normally resolve from Zen's `browser/omni.ja`, including policy schema, BrowserContentHandler, TaskbarTabsCmd, DevToolsStartup, SelectableProfileService, and SessionStoreFunctions modules. The qualified tests prove those warnings did not prevent ACK, onboarding, remote content, or sandboxing on this build. The project deliberately does not copy or symlink unpinned Zen modules into the adapter.

## Release gates

A release candidate must satisfy all of these checks:

- full default unittest discovery passes, with only explicit native opt-in cases skipped when their environment variables are absent;
- Python compileall, Node syntax checking for `shell.js`, and XHTML parsing for `shell.xhtml` pass;
- exact-Zen headless pre-content ACK/marker ordering passes;
- the pinned Firefox candidate assembles twice offline from the five verified inputs with identical normalized inventories, verifies through retained tree authority, and passes both direct headless and native Wayland cgroup smokes;
- the pinned Chromium/CEF candidate assembles twice offline from the exact signed Arch package/signature and closed packaging source with identical descriptors/inventories, revalidates its external root-owned setuid sandbox, and passes the native Wayland local-fixture cgroup smoke without overstating GPU seccomp or capture parity;
- the combined real Wayland and X11/XWayland bundled-shell GUI test passes without weakening remote content, Fission, seccomp, NoNewPrivs, or the configured sandbox levels;
- the legacy native smoke remains available to detect regressions in the backward-compatible path;
- independently built wheel pairs and sdist pairs are byte-identical, and archive contents/modes match contract;
- `git diff --check` reports no whitespace errors;
- no project-local bytecode, probe directories, build scratch state, or surviving test Zen session remains after cleanup.

The authority gates also require:

- omitting App Shell options preserves historical workflow/ticket/argv/pass-fd behavior;
- external `--app-shell-capability` and `--bundled-app-shell` remain mutually exclusive and fail before mutation on authority mismatch;
- bundled launch keeps the URL out of child argv, transfers canonical policy through a write/grow/shrink/seal-locked read-only memfd, and accepts only the exact pre-content ACK over a separate pipe;
- root-owned non-user-writable runtime files retain their verified fd while user-owned runtimes execute only from a hash-coupled sealed snapshot;
- actual profile argv and HOME/XDG/TMP values remain fd-backed while serialized tickets remain deterministic and fd-free;
- confirmed launch stays synchronous, shell-free, environment-sanitized, profile-locked, signal-forwarding, and session-draining;
- runtime discovery/download/update, default-profile access, extension installation, live Desktop/XDG mutation, and high-level unsafe uninstall remain absent.

Retaining the adapter directory fd closes pathname rebinding but cannot prevent a same-UID process from changing an editable user-owned bundle entry after the final hash. A source checkout or user-site wheel is development authority. Release-grade deployment must place the package bundle in root-owned, non-user-writable storage or introduce stronger immutable staging; the tree digest alone is not claimed to solve this TOCTOU boundary.

## Qualified runtime lock example

The operator-supplied lock for the qualified executable is:

```json
{
  "runtimeLockVersion": 1,
  "runtime": {
    "path": "/opt/zen-browser-bin/zen-bin",
    "sha256": "0f5e46fecd345615d0d8d9c7515fa2df0971eae4a4c82f4bd3170276de19c66f"
  }
}
```

A package update changes the executable and requires independently establishing its version/hash and provisioning a profile binding for the new RuntimeIdentity. The project never rewrites a lock automatically.

## Reproducible pinned-upstream plan

The bundled Gecko adapter and project-authored CEF shell still import no Firefox/Chromium source or executable code into the Python package. Step #8 adds one reviewed upstream *provenance* lock for the separate Firefox 154.0 bakeoff candidate: official runtime/source archives, signed checksum authority, immutable `mozilla-release` revision, and a future-build toolchain snapshot. The runtime artifact is reproduced by safe extraction of exact signed archive bytes; no patch is applied. The source archive is verified but not built, so source-build and clean-builder reproducibility remain explicitly incomplete.

Step #9 separately locks Arch Linux CEF 151.3.24-1: package/signature hashes and signer, package metadata and full archive inventory, signed packaging tag metadata and exact checked-out source files, CEF/Chromium source identifiers and build flags, a package-derived SDK, exact root-owned tools, and the two project shell sources/output. Its candidate is reproduced from the signed binary package plus a deterministic shell build; CEF/Chromium itself is not rebuilt. Consequently `not-performed`/`not-tested`, CEF issue 4053 capture blocking, and the observed GPU `Seccomp=0` limitation remain release-visible rather than being promoted to PASS.

If future work imports browser or third-party code, or turns this candidate into a source-built release artifact, the reviewed lock must additionally record and enforce:

- canonical HTTPS source repository;
- immutable Zen and Firefox commit IDs, not only floating tags/branches;
- release/tag signatures and signer identity where available;
- source archive SHA-256 values;
- toolchain/container image digests and target triplets;
- ordered local patch IDs/hashes;
- pinned Rust, Python, Node, and system build inputs;
- extension artifact version, source commit, permissions review, and digest;
- deterministic build flags and accepted platform variance.

Fetch into a separate ignored cache, verify before unpacking, build offline from the lock, emit SBOM/provenance, and compare clean-builder outputs. Updating a pin is a reviewed change with security notes, patch-rebase results, and complete sandbox/process-isolation tests. None of those later source-build claims may be inferred from the current archive-exact candidate lock.

## Deferred updater and broader browser gates

An updater remains intentionally absent. A future design needs offline root keys, threshold-signed expiring metadata, artifact hash/size/version and rollback counters, verification before atomic staging, known-good rollback, key rotation/revocation, freeze/rollback defense, and partial-download recovery.

Before broadening the current minimum App Shell claim, add privileged automation for redirect chains, popup-created OAuth, multi-provider/federated authentication, protocol, permission, download, spoofing, shortcut-priority, and accessibility behavior; pin any imported companion resources; qualify pure Xorg and additional package formats/versions; and collect repeatable controlled startup/memory/page-load measurements. No performance change may disable Fission, multiprocess isolation, content/socket sandboxing, seccomp, NoNewPrivs, or exploit mitigations.
