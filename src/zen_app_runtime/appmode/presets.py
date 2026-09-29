"""Curated presets for well-known web apps.

A preset only seeds a new app definition; after creation the definition in the
library is the single source of truth and can be edited independently.

Host lists are deliberately explicit.  ``scope`` hosts are "the app": top-level
navigation stays inside the window.  ``auth`` hosts are sign-in/consent hops
that must also stay inside the window so a login can return to the app.
Everything else opens in the system default browser.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Common identity providers used by "Continue with ..." flows.
_GOOGLE_AUTH = ("accounts.google.com", "accounts.youtube.com")
_APPLE_AUTH = ("appleid.apple.com", "idmsa.apple.com")
_FACEBOOK_AUTH = ("www.facebook.com", "m.facebook.com", "web.facebook.com")


@dataclass(frozen=True)
class Preset:
    key: str
    name: str
    start_url: str
    scope_hosts: tuple[str, ...]
    auth_hosts: tuple[str, ...] = ()
    scope_paths: tuple[str, ...] = ()
    extras: tuple[str, ...] = ()
    icon_names: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    accent: str = "#7b6cff"
    notifications: bool = False
    media: bool = False
    autoplay: bool = True
    drm: bool = False
    tagline: dict[str, str] = field(default_factory=dict)


PRESETS: dict[str, Preset] = {
    preset.key: preset
    for preset in (
        Preset(
            key="spotify",
            name="Spotify",
            start_url="https://open.spotify.com/",
            scope_hosts=("open.spotify.com",),
            auth_hosts=(
                "accounts.spotify.com",
                "challenge.spotify.com",
                "login.spotify.com",
                *_GOOGLE_AUTH,
                *_APPLE_AUTH,
                *_FACEBOOK_AUTH,
            ),
            extras=("spotify-glass",),
            icon_names=("spotify", "com.spotify.Client", "spotify-client"),
            categories=("AudioVideo", "Audio", "Music", "Player"),
            keywords=("music", "zene", "podcast", "player"),
            accent="#1ed760",
            notifications=False,
            media=False,
            autoplay=True,
            drm=True,
            tagline={
                "hu": "Zene és podcastok, natív ablakban.",
                "en": "Music and podcasts in a native window.",
            },
        ),
        Preset(
            key="discord",
            name="Discord",
            start_url="https://discord.com/app",
            scope_hosts=("discord.com", "ptb.discord.com", "canary.discord.com", "discord.gg", "discordapp.com"),
            auth_hosts=(),
            icon_names=("discord", "com.discordapp.Discord", "discord-canary"),
            categories=("Network", "InstantMessaging", "Chat"),
            keywords=("chat", "voice", "csevegés", "hang"),
            accent="#5865f2",
            notifications=True,
            media=True,
            autoplay=True,
            drm=False,
            tagline={
                "hu": "Csevegés, hang és képernyőmegosztás.",
                "en": "Chat, voice and screen sharing.",
            },
        ),
        Preset(
            key="messenger",
            name="Messenger",
            start_url="https://www.facebook.com/messages/",
            scope_hosts=("www.facebook.com", "facebook.com", "web.facebook.com", "www.messenger.com", "messenger.com"),
            scope_paths=(
                "/messages",
                "/messenger",
                "/login",
                "/checkpoint",
                "/two_step_verification",
                "/two_factor",
                "/auth_platform",
                "/recover",
                "/groupcall",
                "/videocall",
                "/privacy/consent",
                "/cookie",
                "/confirmemail",
                "/dialog",
            ),
            auth_hosts=("m.facebook.com", "accountscenter.facebook.com", "business.facebook.com"),
            extras=("facebook-messages-app", "facebook-messages-glass"),
            icon_names=("fbmessenger", "messenger", "facebook-messenger", "com.facebook.Messenger"),
            categories=("Network", "InstantMessaging", "Chat"),
            keywords=("chat", "facebook", "üzenet"),
            accent="#0a7cff",
            notifications=True,
            media=True,
            autoplay=True,
            drm=False,
            tagline={
                "hu": "A Messenger a Facebook webes üzenetküldőjén keresztül.",
                "en": "Messenger through Facebook's web messaging.",
            },
        ),
        Preset(
            key="youtube-music",
            name="YouTube Music",
            start_url="https://music.youtube.com/",
            scope_hosts=("music.youtube.com",),
            auth_hosts=(*_GOOGLE_AUTH, "consent.youtube.com", "consent.google.com", "www.youtube.com"),
            icon_names=("youtube-music", "com.github.th_ch.youtube_music", "youtube-music-desktop-app"),
            categories=("AudioVideo", "Audio", "Music", "Player"),
            keywords=("music", "zene", "youtube"),
            accent="#ff0033",
            notifications=False,
            media=False,
            autoplay=True,
            drm=True,
            tagline={"hu": "Zene a YouTube-ról.", "en": "Music from YouTube."},
        ),
        Preset(
            key="whatsapp",
            name="WhatsApp",
            start_url="https://web.whatsapp.com/",
            scope_hosts=("web.whatsapp.com",),
            icon_names=("whatsapp", "com.github.eneshecan.WhatsAppForLinux", "whatsapp-desktop", "zapzap"),
            categories=("Network", "InstantMessaging", "Chat"),
            keywords=("chat", "üzenet"),
            accent="#25d366",
            notifications=True,
            media=True,
            autoplay=True,
            drm=False,
            tagline={"hu": "Üzenetek a telefonod mellé.", "en": "Messages alongside your phone."},
        ),
        Preset(
            key="telegram",
            name="Telegram",
            start_url="https://web.telegram.org/a/",
            scope_hosts=("web.telegram.org",),
            icon_names=("telegram", "org.telegram.desktop", "telegram-desktop"),
            categories=("Network", "InstantMessaging", "Chat"),
            keywords=("chat", "üzenet"),
            accent="#2aabee",
            notifications=True,
            media=True,
            autoplay=True,
            drm=False,
            tagline={"hu": "Gyors, felhőalapú üzenetküldés.", "en": "Fast, cloud-based messaging."},
        ),
    )
}


def get_preset(key: str) -> Preset | None:
    return PRESETS.get(key)
