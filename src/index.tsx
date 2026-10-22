import { Navigation, QuickAccessTab, Router, staticClasses } from "@decky/ui";
import {
  addEventListener,
  removeEventListener,
  definePlugin,
  routerHook,
  toaster,
} from "@decky/api";
import { FaBookOpen } from "react-icons/fa";

import { captureAndMine, getAllSettings, UNKNOWN_APP_KEY, VnlEvent } from "./api";
import { copyToClipboard } from "./clipboard";
import { requestFirstWordFocus } from "./firstWordFocus";
import { TriggerButton, TriggerWatcher } from "./input";
import { Panel } from "./Panel";
import { ScanOverlay } from "./ScanOverlay";

export default definePlugin(() => {
  let latestSettings: Record<string, any> = {};
  let lastAppId: string | undefined;

  const watcher = new TriggerWatcher((button) => {
    // The hidraw monitor sees the button even inside Steam menus/QAM, and
    // the capture grabs whatever gamescope composites — so only fire while
    // a game is actually running to avoid OCRing the Steam UI.
    const app = Router.MainRunningApp;
    if (!app) return;
    void captureAndMine(button, app.appid).catch((e) => {
      toaster.toast({ title: "kumadeck", body: `capture failed: ${e}` });
    });
  });

  // This game's areas if it has a saved profile, else the Default list —
  // mirrors the backend's Plugin._areas_for in main.py.
  const areasForApp = (appid?: string): any[] => {
    const profiles = latestSettings.capture_profiles || {};
    const profile = profiles[appid || UNKNOWN_APP_KEY];
    const areas = profile?.areas?.length ? profile.areas : latestSettings.capture_areas;
    return Array.isArray(areas) ? areas : [];
  };

  const reconfigureWatcher = () => {
    lastAppId = Router.MainRunningApp?.appid;
    const watched = areasForApp(lastAppId)
      .map((a) => a?.button)
      .filter(Boolean) as TriggerButton[];
    watcher.configure(watched);
  };

  const applySettings = (s: Record<string, any>) => {
    latestSettings = s;
    reconfigureWatcher();
  };

  // the backend may still be starting when the frontend loads — retry
  // until the first settings fetch succeeds so a configured trigger
  // button isn't silently replaced by the default
  const loadInitialSettings = async (attempt = 0): Promise<void> => {
    try {
      applySettings(await getAllSettings());
    } catch {
      if (attempt < 30) {
        setTimeout(() => void loadInitialSettings(attempt + 1), 1000);
      }
    }
  };
  void loadInitialSettings();
  watcher.start();

  // Steam doesn't expose a "running app changed" event here, so poll for
  // it cheaply — reconfigure only actually rebuilds state when it fires.
  const appPoll = setInterval(() => {
    if (Router.MainRunningApp?.appid !== lastAppId) reconfigureWatcher();
  }, 1500);

  const onEvent = (ev: VnlEvent) => {
    if (ev.stage === "done" && ev.text && ev.copy_to_clipboard) {
      copyToClipboard(ev.text);
    }
    if (ev.stage === "done") {
      if (ev.text) requestFirstWordFocus(ev.text);
      // select our plugin in Decky's QAM tab before opening it.
      // deckyState is TS-private but present at runtime; internal API, so
      // fail soft — worst case the QAM opens on the last-used view.
      try {
        (window as any).DeckyPluginLoader?.deckyState?.setActivePlugin?.("kumadeck");
      } catch {
        /* decky internals changed; QAM still opens */
      }
      Navigation.OpenQuickAccessMenu(QuickAccessTab.Decky);
    }
    // a failed capture has no result to show in the sidebar, so a toast is
    // the only feedback for it
    if (ev.stage === "error" && ev.message) {
      toaster.toast({ title: "kumadeck", body: ev.message });
    }
  };
  addEventListener<[VnlEvent]>("vnl_event", onEvent);

  addEventListener<[Record<string, any>]>("vnl_settings", applySettings);

  routerHook.addGlobalComponent("VnLookupScanOverlay", () => <ScanOverlay />);

  return {
    name: "kumadeck",
    titleView: <div className={staticClasses.Title}>kumadeck</div>,
    content: <Panel />,
    icon: <FaBookOpen />,
    alwaysRender: true,
    onDismount() {
      watcher.stop();
      clearInterval(appPoll);
      removeEventListener("vnl_event", onEvent);
      removeEventListener("vnl_settings", applySettings);
      routerHook.removeGlobalComponent("VnLookupScanOverlay");
    },
  };
});
