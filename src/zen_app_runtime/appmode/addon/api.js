/* This Source Code is part of zen-app-runtime (app mode). */
/* global ExtensionAPI, Services, ChromeUtils, Cc, Ci */
"use strict";

// Experiment entry point. It only wires the privileged resources
// (resource://zenapp/ and chrome://zenapp/) and hands over to ZenApp.sys.mjs.
this.zenapp = class extends ExtensionAPI {
  onStartup() {
    const { extension } = this;
    const resource = Services.io
      .getProtocolHandler("resource")
      .QueryInterface(Ci.nsIResProtocolHandler);
    resource.setSubstitution("zenapp", extension.rootURI);

    const startup = Cc["@mozilla.org/addons/addon-manager-startup;1"].getService(
      Ci.amIAddonManagerStartup
    );
    const manifestURI = Services.io.newURI("manifest.json", null, extension.rootURI);
    this.chromeHandle = startup.registerChrome(manifestURI, [
      ["content", "zenapp", "content/"],
    ]);

    const { ZenApp } = ChromeUtils.importESModule(
      "resource://zenapp/modules/ZenApp.sys.mjs"
    );
    this.zenApp = ZenApp;
    ZenApp.startup({ version: extension.version }).catch(error => {
      console.error("zen-app: startup failed", error);
    });
  }

  onShutdown(isAppShutdown) {
    if (isAppShutdown) {
      return;
    }
    try {
      this.zenApp?.shutdown();
    } finally {
      this.chromeHandle?.destruct();
      this.chromeHandle = null;
      Services.io
        .getProtocolHandler("resource")
        .QueryInterface(Ci.nsIResProtocolHandler)
        .setSubstitution("zenapp", null);
      Services.obs.notifyObservers(null, "startupcache-invalidate");
    }
  }

  getAPI() {
    return { zenapp: {} };
  }
};
