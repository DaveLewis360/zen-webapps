"""Render an app-mode profile: prefs, chrome CSS, the privileged add-on, config.

Everything here is deterministic for a given definition + package so repeated
launches rewrite nothing (and keep Gecko's startup caches warm).
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
from typing import Any
import zipfile

from .. import __version__ as PACKAGE_VERSION
from .definition import AppDefinition, definition_to_dict
from .desktop_look import glass_sources, inherited_render_prefs, resolve_glass
from .icons import IconChoice
from .library import write_private_file
from .palette import initial_roles, scheme_paths
from .paths import AppModePaths, desktop_id
from .presets import get_preset
from .zen import ZenRuntime
from .zen_internet import catalog_meta

ADDON_ID = "zenapp@zen-app-runtime"
ADDON_DIR = Path(__file__).resolve().parent / "addon"
USER_CHROME_TEMPLATE = Path(__file__).resolve().parent / "profile" / "userChrome.css"
# Keep in sync with MAX_CUSTOM_CSS_BYTES in addon/modules/UserCss.sys.mjs.
MAX_CUSTOM_CSS_BYTES = 512 * 1024
SETTINGS_BLOCK_BEGIN = "// >>> zen-app settings (managed; rewritten by the app)"
SETTINGS_BLOCK_END = "// <<< zen-app settings"

# App-mode extras (addon/themes): "glass" ones only in the translucent
# appearance, "always" ones in every appearance. Site transparency itself
# comes from the Zen Internet catalog.
EXTRAS: dict[str, dict[str, str]] = {
    "spotify-glass": {"file": "spotify-glass.css", "when": "glass"},
    "facebook-messages-app": {"file": "facebook-messages-app.css", "when": "always"},
    "facebook-messages-glass": {"file": "facebook-messages-glass.css", "when": "glass"},
}

# Site compatibility fixes a preset can ask for (Preset.compat). Each one is a
# page script of Firefox's own web compatibility interventions, taken from the
# installed Zen (so it follows Mozilla's updates) and run in the page before
# its scripts. Firefox itself ships them only in Nightly so far.
COMPAT_FIXES: dict[str, dict[str, Any]] = {
    # Messenger's encrypted calls use Chrome's legacy createEncodedStreams();
    # the shim maps it onto the standard RTCRtpScriptTransform.
    # https://bugzilla.mozilla.org/show_bug.cgi?id=1896361
    "encoded-streams": {
        "script": "resource://builtin-addons/webcompat/injections/js/bug1913599-shim-createencodedstreams.js",
        "hosts": ["www.facebook.com", "web.facebook.com", "facebook.com"],
        "bug": 1896361,
    },
}

# Page themes a preset can bring (Preset.theme; files in addon/page-themes):
# CSS loaded into the site's own pages as an author sheet, like a Vencord or
# Stylus theme, before the app's custom CSS (which can override it). A "dark"
# theme is coloured in the dark tones of the wallpaper scheme (also for a
# light wallpaper) and keeps the page in dark mode; a "glass" theme needs the
# translucent appearance. settings.disabledFeatures "theme|<id>" switches one off.
PAGE_THEMES: dict[str, dict[str, str]] = {
    "discord-midnight": {
        "file": "discord-midnight.css",
        "name": "Midnight",
        "author": "refact0r",
        "url": "https://github.com/refact0r/midnight-discord",
        "license": "MIT",
        "scheme": "dark",
        "when": "glass",
    },
}

# The language of the app's own words and of the pages it asks for. English
# only for now; British English keeps the 24-hour clock and day-month dates.
UI_LANGUAGE = "en"
PAGE_LANGUAGES = "en-GB, en"


def theme_feature_key(theme_id: str) -> str:
    return f"theme|{theme_id}"


def page_theme_ids(definition: AppDefinition) -> tuple[str, ...]:
    preset = get_preset(definition.preset) if definition.preset else None
    return tuple(key for key in ((preset.theme,) if preset and preset.theme else ()) if key in PAGE_THEMES)


def active_page_themes(definition: AppDefinition) -> list[str]:
    """The page themes that apply with the current settings (as the add-on decides)."""
    settings = definition.settings
    return [
        key
        for key in page_theme_ids(definition)
        if theme_feature_key(key) not in settings.disabled_features
        and (PAGE_THEMES[key]["when"] != "glass" or settings.appearance == "transparent")
        and (PAGE_THEMES[key]["scheme"] != "dark" or settings.color_scheme != "light")
    ]


def forces_dark_content(definition: AppDefinition) -> bool:
    """A dark page theme keeps a "system" colour scheme app's pages dark."""
    return definition.settings.color_scheme == "system" and any(
        PAGE_THEMES[key]["scheme"] == "dark" for key in active_page_themes(definition)
    )

TILING_DESKTOPS = {
    "hyprland", "sway", "niri", "river", "i3", "bspwm", "qtile", "dwl", "herbstluftwm",
    "awesome", "xmonad", "dwm", "leftwm", "spectrwm", "mango", "mangowc",
}

# Preferences every app profile gets. They switch browser behaviour off; they
# never weaken the sandbox, site isolation or safe browsing.
BASELINE_PREFS: dict[str, Any] = {
    # The privileged app-mode add-on (unsigned, profile-scoped).
    "xpinstall.signatures.required": False,
    "extensions.experiments.enabled": True,
    "extensions.autoDisableScopes": 0,
    "extensions.startupScanScopes": 1,
    "extensions.enabledScopes": 5,
    "extensions.getAddons.cache.enabled": False,
    "extensions.htmlaboutaddons.recommendations.enabled": False,
    "extensions.pocket.enabled": False,
    "extensions.formautofill.addresses.enabled": False,
    "extensions.formautofill.creditCards.enabled": False,
    "toolkit.legacyUserProfileCustomizations.stylesheets": True,
    # Startup: no welcome pages, restore prompts or default-browser nags.
    "browser.startup.page": 0,
    "browser.startup.homepage": "about:blank",
    "browser.startup.homepage_override.mstone": "ignore",
    "startup.homepage_welcome_url": "",
    "startup.homepage_welcome_url.additional": "",
    "startup.homepage_override_url": "",
    "browser.newtabpage.enabled": False,
    "browser.aboutwelcome.enabled": False,
    "browser.shell.checkDefaultBrowser": False,
    "browser.shell.didSkipDefaultBrowserCheckOnFirstRun": True,
    "browser.sessionstore.resume_from_crash": False,
    "browser.sessionstore.max_tabs_undo": 0,
    "browser.sessionstore.max_windows_undo": 0,
    "browser.profiles.enabled": False,
    "browser.uitour.enabled": False,
    "browser.messaging-system.whatsNewPanel.enabled": False,
    "browser.newtabpage.activity-stream.asrouter.userprefs.cfr.addons": False,
    "browser.newtabpage.activity-stream.asrouter.userprefs.cfr.features": False,
    "messaging-system.rsexperimentloader.enabled": False,
    "app.normandy.enabled": False,
    "app.shield.optoutstudies.enabled": False,
    "browser.discovery.enabled": False,
    "datareporting.policy.dataSubmissionPolicyBypassNotification": True,
    "toolkit.telemetry.reportingpolicy.firstRun": False,
    "identity.fxaccounts.enabled": False,
    "browser.vpn_promo.enabled": False,
    "browser.promo.focus.enabled": False,
    "browser.promo.pin.enabled": False,
    # One window, one tab: no warnings, links routed by the runtime.
    "browser.tabs.warnOnClose": False,
    "browser.tabs.warnOnCloseOtherTabs": False,
    "browser.tabs.warnOnOpen": False,
    "browser.warnOnQuit": False,
    "browser.warnOnQuitShortcut": False,
    "browser.tabs.closeWindowWithLastTab": True,
    "browser.link.open_newwindow": 3,
    "browser.link.open_newwindow.restriction": 2,
    "browser.tabs.loadInBackground": True,
    "browser.tabs.loadDivertedInBackground": True,
    "browser.toolbars.bookmarks.visibility": "never",
    "browser.ctrlTab.sortByRecentlyUsed": False,
    # Native desktop behaviour.
    "widget.use-xdg-desktop-portal.file-picker": 1,
    "widget.gtk.native-context-menus": False,
    "widget.gtk.overlay-scrollbars.enabled": True,
    "ui.key.menuAccessKeyFocuses": False,
    "accessibility.typeaheadfind": False,
    "accessibility.typeaheadfind.manual": False,
    "accessibility.browsewithcaret_shortcut.enabled": False,
    "middlemouse.contentLoadURL": False,
    "general.autoScroll": False,
    "browser.download.useDownloadDir": False,
    "browser.download.alwaysOpenPanel": False,
    "browser.download.always_ask_before_handling_new_types": False,
    "browser.download.panel.shown": True,
    "privacy.popups.showBrowserMessage": False,
    "full-screen-api.warning.timeout": 0,
    "pointer-lock-api.warning.timeout": 0,
    "privacy.webrtc.hideGlobalIndicator": True,
    "media.webrtc.camera.allow-pipewire": True,
    "browser.eme.ui.enabled": False,
    "media.videocontrols.picture-in-picture.enabled": False,
    "media.videocontrols.picture-in-picture.video-toggle.enabled": False,
    # The app publishes its own media player (MPRIS) under its own name;
    # Gecko's would be called "Mozilla zen-app-<id>". Re-enabled at run time
    # if the app's own one cannot start.
    "media.hardwaremediakeys.enabled": False,
    # Browser assistants and prompts that have no place in an app.
    "browser.formfill.enable": False,
    "signon.rememberSignons": False,
    "signon.autofillForms": False,
    "signon.generation.enabled": False,
    "signon.firefoxRelay.feature": "disabled",
    "browser.translations.enable": False,
    "browser.translations.automaticallyPopup": False,
    "browser.ml.enable": False,
    "browser.ml.chat.enabled": False,
    "browser.ml.linkPreview.enabled": False,
    "permissions.default.geo": 2,
    "permissions.default.xr": 2,
    # English, like the app's own words: the browser's prompts and menus, and
    # the language pages are asked for (Accept-Language, navigator.language).
    "intl.locale.requested": "en-GB,en-US",
    "intl.accept_languages": PAGE_LANGUAGES,
    # Zen: no welcome, update toasts, workspaces gestures, glance or split view.
    "zen.welcome-screen.seen": True,
    "zen.updates.show-update-notification": False,
    "zen.watermark.enabled": False,
    "zen.urlbar.open-on-startup": False,
    "zen.urlbar.replace-newtab": False,
    "zen.window-sync.enabled": False,
    "zen.glance.enabled": False,
    "zen.splitView.enable-tab-drop": False,
    "zen.splitView.enable-tab-click-split": False,
    "zen.splitView.enable-drag-over-split": False,
    "zen.mediacontrols.enabled": False,
    "zen.view.grey-out-inactive-windows": False,
    "zen.view.compact.show-background-tab-toast": False,
    "zen.mods.auto-update": False,
    "zen.downloads.download-animation": False,
    "zen.workspaces.swipe-actions": False,
    "zen.boosts.enabled": False,
    "zen.theme.content-element-separation": 0,
    "zen.theme.border-radius": 0,
    "zen.tabs.close-window-with-empty": True,
}

# setting -> value -> prefs. Shared with the add-on (rendered into app.json).
SETTINGS_PREFS: dict[str, dict[str, dict[str, Any]]] = {
    "appearance": {
        "transparent": {
            "browser.tabs.allow_transparent_browser": True,
            "zen.widget.linux.transparency": True,
            "mozilla.widget.use-argb-visuals": True,
        },
        "opaque": {
            "browser.tabs.allow_transparent_browser": False,
            "zen.widget.linux.transparency": False,
            "mozilla.widget.use-argb-visuals": False,
        },
    },
    "windowChrome": {
        "none": {"browser.tabs.inTitlebar": 1},
        "titlebar": {"browser.tabs.inTitlebar": 0},
    },
    "colorScheme": {
        "system": {"zen.view.window.scheme": 2, "layout.css.prefers-color-scheme.content-override": 2},
        "dark": {"zen.view.window.scheme": 0, "layout.css.prefers-color-scheme.content-override": 0},
        "light": {"zen.view.window.scheme": 1, "layout.css.prefers-color-scheme.content-override": 1},
    },
    "notifications": {
        "true": {"permissions.default.desktop-notification": 1},
        "false": {"permissions.default.desktop-notification": 2},
    },
    "media": {
        "true": {"permissions.default.camera": 0, "permissions.default.microphone": 0},
        "false": {"permissions.default.camera": 2, "permissions.default.microphone": 2},
    },
    "autoplay": {
        "true": {"media.autoplay.default": 0},
        "false": {"media.autoplay.default": 1},
    },
    "drm": {
        "true": {"media.eme.enabled": True, "media.gmp-widevinecdm.enabled": True, "media.gmp-widevinecdm.visible": True},
        "false": {"media.eme.enabled": False, "media.gmp-widevinecdm.enabled": False},
    },
}


@dataclass
class RenderResult:
    profile: Path
    changed: list[str] = field(default_factory=list)
    addon_version: str = ""

    @property
    def addon_changed(self) -> bool:
        return any(name.startswith("extensions/") for name in self.changed)


def _render_pref(name: str, value: Any) -> str:
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, int):
        rendered = str(value)
    else:
        rendered = json.dumps(str(value))
    return f"user_pref({json.dumps(name)}, {rendered});"


def resolve_window_chrome_auto() -> str:
    desktops = os.environ.get("XDG_CURRENT_DESKTOP", "") + ":" + os.environ.get("XDG_SESSION_DESKTOP", "")
    names = {part.strip().lower() for part in desktops.split(":") if part.strip()}
    return "none" if names & TILING_DESKTOPS else "titlebar"


def settings_prefs(definition: AppDefinition, window_chrome_auto: str) -> dict[str, Any]:
    settings = definition.settings
    values = {
        "appearance": settings.appearance,
        "windowChrome": window_chrome_auto if settings.window_chrome == "auto" else settings.window_chrome,
        "colorScheme": settings.color_scheme,
        "notifications": "true" if settings.notifications else "false",
        "media": "true" if settings.media else "false",
        "autoplay": "true" if settings.autoplay else "false",
        "drm": "true" if settings.drm else "false",
    }
    prefs: dict[str, Any] = {}
    for key, value in values.items():
        prefs.update(SETTINGS_PREFS[key].get(value, {}))
    if forces_dark_content(definition):
        prefs["layout.css.prefers-color-scheme.content-override"] = 0
    return prefs


def render_user_js(
    definition: AppDefinition,
    window_chrome_auto: str,
    inherited: dict[str, Any] | None = None,
    inherited_from: Path | None = None,
) -> str:
    lines = [
        f"// Generated by zen-app-runtime app mode for {definition.id!r}.",
        "// Do not edit: `zen-app run` and the app itself rewrite this file.",
        _render_pref("zenapp.appId", definition.id),
    ]
    if inherited:
        lines.append(f"// Rendering prefs inherited from the main Zen profile ({inherited_from}),")
        lines.append("// so the app uses this GPU exactly like your browser does.")
        lines.extend(_render_pref(name, inherited[name]) for name in sorted(inherited))
    lines.extend(_render_pref(name, BASELINE_PREFS[name]) for name in BASELINE_PREFS)
    prefs = settings_prefs(definition, window_chrome_auto)
    lines.append(SETTINGS_BLOCK_BEGIN)
    lines.extend(_render_pref(name, prefs[name]) for name in sorted(prefs))
    lines.append(SETTINGS_BLOCK_END)
    return "\n".join(lines) + "\n"


def initial_tint(definition: AppDefinition, sources: dict[str, Any], resolved: str, surface: str) -> tuple[str, int]:
    if resolved != "custom" and resolved in sources:
        source = sources[resolved]
        return source["color"], int(round(source["alpha"] * 100))
    return surface, definition.settings.tint


def render_user_chrome(definition: AppDefinition, sources: dict[str, Any] | None = None, resolved: str = "custom") -> str:
    roles = initial_roles(definition.accent, definition.settings.palette, definition.settings.color_scheme)
    variables = "\n".join(f"  --zenapp-{name}: {value};" for name, value in sorted(roles.items()))
    tint_color, tint = initial_tint(definition, sources or {}, resolved, roles["surface"])
    header = (
        "/* First-paint defaults; the app runtime overrides them live. */\n"
        ":root {\n"
        f"  --zenapp-accent: {definition.accent};\n"
        f"  --zenapp-tint: {tint};\n"
        f"  --zenapp-tint-color: {tint_color};\n"
        f"{variables}\n"
        "}\n\n"
    )
    return header + USER_CHROME_TEMPLATE.read_text(encoding="utf-8")


def _addon_files() -> list[Path]:
    return sorted(path for path in ADDON_DIR.rglob("*") if path.is_file() and "__pycache__" not in path.parts)


def build_addon() -> tuple[bytes, str]:
    """Deterministic XPI of the privileged add-on and its version string."""
    digest = hashlib.sha256()
    contents: list[tuple[str, bytes]] = []
    for path in _addon_files():
        name = path.relative_to(ADDON_DIR).as_posix()
        data = path.read_bytes()
        digest.update(name.encode() + b"\0" + data + b"\0")
        contents.append((name, data))
    build = int(digest.hexdigest()[:7], 16)
    version = f"{PACKAGE_VERSION}.{build}"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in contents:
            if name == "manifest.json":
                data = data.replace(b"@VERSION@", version.encode())
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return buffer.getvalue(), version


def render_app_config(
    definition: AppDefinition,
    paths: AppModePaths,
    zen: ZenRuntime,
    icon: IconChoice,
    definition_path: Path,
    addon_version: str,
    window_chrome_auto: str,
    sources: dict[str, Any] | None = None,
    inherited: dict[str, Any] | None = None,
    inherited_from: Path | None = None,
    *,
    identity: str | None = None,
    role: str = "app",
) -> dict[str, Any]:
    preset = get_preset(definition.preset) if definition.preset else None
    tagline = preset.tagline.get(UI_LANGUAGE, "") if preset and preset.tagline else ""
    return {
        "configVersion": 1,
        "role": role,
        "appId": definition.id,
        "desktopId": identity or desktop_id(definition.id),
        "definitionPath": str(definition_path),
        "definition": definition_to_dict(definition),
        "tagline": tagline,
        "icon": {"name": icon.name, "file": str(icon.file) if icon.file else None, "source": icon.source},
        "windowChromeAuto": window_chrome_auto,
        "settingsPrefs": SETTINGS_PREFS,
        "extras": {key: dict(value) for key, value in EXTRAS.items()},
        "zenInternet": catalog_meta(paths),
        "customCss": {"path": str(paths.custom_css_path(definition.id)), "maxBytes": MAX_CUSTOM_CSS_BYTES},
        "styleAliases": {key: list(value) for key, value in (preset.style_aliases if preset else {}).items()},
        "compat": {
            key: {"script": COMPAT_FIXES[key]["script"], "hosts": list(COMPAT_FIXES[key]["hosts"]), "bug": COMPAT_FIXES[key]["bug"]}
            for key in (preset.compat if preset else ())
            if key in COMPAT_FIXES
        },
        "pageThemes": [{"id": key, **PAGE_THEMES[key]} for key in page_theme_ids(definition)],
        "glass": {"sources": sources or {}, "resolved": resolve_glass(definition.settings.glass, sources or {})},
        "inheritedPrefs": {"from": str(inherited_from) if inherited_from else None, "prefs": inherited or {}},
        "schemePaths": scheme_paths(),
        "locale": UI_LANGUAGE,
        "runtime": {"binary": str(zen.binary), "version": zen.version, "gecko": zen.gecko},
        "package": {"version": PACKAGE_VERSION, "addonVersion": addon_version},
        "launcher": {"shim": str(paths.shim_path())},
    }


_DEFAULT_HANDLERS = {
    "defaultHandlersVersion": {},
    "isDownloadsImprovementsAlreadyMigrated": True,
    "mimeTypes": {},
    "schemes": {
        scheme: {"action": 4, "ask": False, "handlers": [None]}
        for scheme in ("mailto", "tel", "sms", "spotify", "discord", "fb-messenger", "whatsapp", "tg", "magnet", "zoommtg", "msteams")
    },
}


def render_profile(
    definition: AppDefinition,
    paths: AppModePaths,
    zen: ZenRuntime,
    icon: IconChoice,
    definition_path: Path,
    *,
    profile: Path | None = None,
    identity: str | None = None,
    role: str = "app",
) -> RenderResult:
    profile = profile or paths.profile_dir(definition.id)
    profile.mkdir(parents=True, exist_ok=True)
    os.chmod(profile.parent, 0o700)
    os.chmod(profile, 0o700)
    result = RenderResult(profile=profile)
    window_chrome_auto = resolve_window_chrome_auto()

    xpi, version = build_addon()
    result.addon_version = version
    sources = glass_sources()
    resolved = resolve_glass(definition.settings.glass, sources)
    inherited, inherited_from = inherited_render_prefs()
    outputs: dict[str, bytes] = {
        "user.js": render_user_js(definition, window_chrome_auto, inherited, inherited_from).encode("utf-8"),
        "chrome/userChrome.css": render_user_chrome(definition, sources, resolved).encode("utf-8"),
        f"extensions/{ADDON_ID}.xpi": xpi,
        "zen-app/app.json": (
            json.dumps(
                render_app_config(
                    definition, paths, zen, icon, definition_path, version, window_chrome_auto,
                    sources, inherited, inherited_from, identity=identity, role=role,
                ),
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8"),
    }
    for relative, data in outputs.items():
        if write_private_file(profile / relative, data):
            result.changed.append(relative)
    handlers = profile / "handlers.json"
    if not handlers.exists():
        write_private_file(handlers, (json.dumps(_DEFAULT_HANDLERS, indent=2) + "\n").encode("utf-8"))
        result.changed.append("handlers.json")
    if result.addon_changed:
        # Make Gecko re-read the updated add-on instead of its cached copy.
        shutil.rmtree(profile / "startupCache", ignore_errors=True)
        for stale in ("addonStartup.json.lz4",):
            try:
                (profile / stale).unlink()
            except FileNotFoundError:
                pass
    return result


def write_pending_action(profile: Path, action: str, url: str | None = None) -> None:
    data = {"action": action, "url": url}
    write_private_file(profile / "zen-app" / "pending.json", (json.dumps(data) + "\n").encode("utf-8"))
