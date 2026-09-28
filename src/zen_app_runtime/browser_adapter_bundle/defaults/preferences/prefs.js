// Zen App Runtime browser adapter v2 security and privacy baseline.
pref("toolkit.defaultChromeURI", "chrome://zenappshell/content/shell.xhtml");
pref("toolkit.defaultChromeFeatures", "chrome,dialog=no,resizable,centerscreen,width=1080,height=760");
pref("browser.dom.window.dump.enabled", true);
pref("browser.shell.checkDefaultBrowser", false);
pref("browser.startup.homepage_override.mstone", "ignore");
pref("browser.startup.blankWindow", false);

// Preserve upstream multiprocess isolation and the qualified Zen sandbox levels.
pref("browser.tabs.remote.autostart", true);
pref("fission.autostart", true);
pref("security.sandbox.content.level", 6);
pref("security.sandbox.content.write_path_whitelist", "");
pref("security.sandbox.content.read_path_whitelist", "");
pref("security.sandbox.content.syscall_whitelist", "");
pref("security.sandbox.socket.process.level", 2);

// Keep the single-app process model lean without weakening Fission or sandboxing.
// Cache limits preserve correctness by falling back to disk/redecode instead of
// merging security principals or removing required Gecko process types.
pref("dom.ipc.processPrelaunch.enabled", false);
pref("browser.cache.memory.capacity", 32768);
pref("browser.sessionhistory.max_total_viewers", 1);
pref("image.mem.surfacecache.max_size_kb", 131072);
pref("image.mem.surfacecache.min_expiration_ms", 30000);
pref("media.memory_caches_combined_limit_kb", 65536);

// Keep untrusted content inside the HTTPS, cookie, tracking, and mixed-content boundary.
pref("dom.security.https_only_mode", true);
pref("dom.security.https_only_mode_pbm", true);
pref("security.mixed_content.block_active_content", true);
pref("security.mixed_content.block_display_content", true);
pref("network.cookie.cookieBehavior", 5);
pref("network.cookie.cookieBehavior.pbmode", 5);
pref("privacy.trackingprotection.enabled", true);
pref("privacy.trackingprotection.pbmode.enabled", true);
pref("privacy.trackingprotection.cryptomining.enabled", true);
pref("privacy.trackingprotection.fingerprinting.enabled", true);
pref("privacy.trackingprotection.socialtracking.enabled", true);

// The privileged shell owns windows, fullscreen, downloads, and developer tooling.
pref("browser.link.open_newwindow", 1);
pref("browser.link.open_newwindow.restriction", 0);
pref("dom.disable_open_during_load", true);
pref("full-screen-api.enabled", false);
pref("browser.download.useDownloadDir", false);
pref("browser.download.always_ask_before_handling_new_types", true);
pref("browser.download.manager.addToRecentDocs", false);
pref("devtools.chrome.enabled", false);
pref("devtools.debugger.remote-enabled", false);

// Preserve real permission and capture plumbing. Parent chrome mediates every
// decision; fake devices and the test-only bypass remain disabled. On Wayland,
// Gecko's real capture backend must continue into xdg-desktop-portal/PipeWire.
pref("media.navigator.permission.disabled", false);
pref("media.navigator.streams.fake", false);
pref("media.getusermedia.screensharing.enabled", true);

// A pinned custom application must not self-update, run studies, or submit telemetry.
pref("app.update.enabled", false);
pref("app.update.auto", false);
pref("extensions.update.enabled", false);
pref("app.normandy.enabled", false);
pref("app.shield.optoutstudies.enabled", false);
pref("datareporting.policy.dataSubmissionEnabled", false);
pref("datareporting.healthreport.uploadEnabled", false);
pref("toolkit.telemetry.enabled", false);
pref("toolkit.telemetry.unified", false);
