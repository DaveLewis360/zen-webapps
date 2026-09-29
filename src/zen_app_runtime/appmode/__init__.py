"""App mode: native-feeling site apps on the full, package-managed Zen runtime.

The frozen App Shell path launches Gecko with ``-app`` and therefore runs
without Zen's browser layer (no WebRTC actors, EME defaults, permission UI,
context menus or dialogs).  App mode keeps that layer intact and instead turns
one dedicated profile per app into a single-purpose application:

* generated preferences (``user.js``) and chrome/content stylesheets,
* a small privileged WebExtension experiment shipped with this package that
  removes browser behaviour (shortcuts, tabs, menus, link routing) and hosts the
  Material 3 Expressive onboarding/settings UI,
* a per-app window identity, desktop entry and icon.
"""

APP_MODE_VERSION = 1
