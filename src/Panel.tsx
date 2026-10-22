import {
  ButtonItem,
  ConfirmModal,
  DialogButton,
  Field,
  Focusable,
  PanelSection,
  PanelSectionRow,
  Router,
  showModal,
  TextField,
  ToggleField,
} from "@decky/ui";
import { addEventListener, removeEventListener } from "@decky/api";
import { FC, ReactNode, useEffect, useRef, useState } from "react";
import { FaClone, FaEye, FaGamepad } from "react-icons/fa";
import {
  clearAnkiBuffer,
  deleteAllAreaScreenshots,
  deleteAreaScreenshot,
  deleteDownloadedData,
  downloadModels,
  exportAnkiBuffer,
  getAllSettings,
  getAreaScreenshot,
  getDownloadedDataSize,
  getStatus,
  installAnkiExportRuntime,
  installRuntime,
  PluginStatus,
  Region,
  saveAreaScreenshot,
  SCREEN_ASPECT_RATIO,
  setSetting,
  UNKNOWN_APP_KEY,
  VnlEvent,
} from "./api";
import { AnkiBufferModal } from "./AnkiBufferModal";
import { LookupSection } from "./LookupPanel";
import { openRegionEditor } from "./RegionEditor";
import { QrCode } from "./QrCode";
import { openTriggerButtonMenu, TriggerButtonSelector } from "./TriggerButtonOptions";

interface CaptureArea {
  region: Region;
  button: string | null;
  // id of the persisted screenshot taken when this area's region was last
  // saved — absent until the area has been edited at least once
  screenshot_id?: string | null;
}

interface CaptureProfile {
  display_name: string;
  areas: CaptureArea[];
}

// shape for newly-added areas — the default area already covers the usual
// bottom-third text box, so a second one probably wants more of the screen
const NEW_AREA_REGION: Region = { x: 0.1, y: 0.08, w: 0.8, h: 0.84 };

const formatMb = (bytes: number) =>
  bytes < 1_000_000 ? "<1 MB" : `${Math.round(bytes / 1_000_000)} MB`;

// Cheap at-a-glance preview of where a region sits on screen — a grey box
// standing in for the display (or the screenshot taken when the region was
// last saved, if we have one), with a blue box for the region, positioned
// with the same x/y/w/h-as-percentage math as the visual region editor
// (RegionEditor.tsx), just without the drag handling. Must match the
// editor's SCREEN_ASPECT_RATIO + objectFit: "fill" exactly — the region's
// fractions were drawn against that mapping, so anything else misaligns
// the blue box against the screenshot.
const AreaThumbnail: FC<{ region: Region; screenshotSrc?: string }> = ({ region, screenshotSrc }) => (
  <div
    style={{
      position: "relative",
      width: "100%",
      aspectRatio: SCREEN_ASPECT_RATIO,
      background: "rgba(255,255,255,0.06)",
      border: "1px solid rgba(255,255,255,0.15)",
      borderRadius: 4,
      overflow: "hidden",
      marginBottom: 8,
    }}
  >
    {screenshotSrc ? (
      <img
        src={`data:image/png;base64,${screenshotSrc}`}
        style={{
          position: "absolute",
          inset: 0,
          width: "100%",
          height: "100%",
          objectFit: "fill",
          filter: "brightness(0.55)",
        }}
      />
    ) : null}
    <div
      style={{
        position: "absolute",
        left: `${region.x * 100}%`,
        top: `${region.y * 100}%`,
        width: `${region.w * 100}%`,
        height: `${region.h * 100}%`,
        background: "rgba(79,195,247,0.35)",
        border: "1px solid #4fc3f7",
        boxSizing: "border-box",
      }}
    />
  </div>
);

// Steam caches per-app icons locally (works offline in gaming mode) and
// exposes them via these undocumented store globals — not in @decky/ui's
// types, so read them off `window` directly.
const getAppIconUrl = (appid?: string | null): string | null => {
  if (!appid) return null;
  try {
    const w = window as any;
    const overview = w.appStore?.GetAppOverviewByAppID?.(Number(appid));
    return (overview && w.appStore?.GetIconURLForApp?.(overview)) || null;
  } catch {
    return null;
  }
};

// Small square game icon; falls back to `fallbackIcon` (a generic gamepad
// glyph by default) when Steam has no cached icon for the appid, there's no
// appid at all (the "Default" profile), or the caller never looked one up.
const AppThumbnail: FC<{ appid?: string | null; size?: number; fallbackIcon?: ReactNode }> = ({
  appid,
  size = 32,
  fallbackIcon,
}) => {
  const url = getAppIconUrl(appid);
  return (
    <div
      style={{
        width: size,
        height: size,
        borderRadius: 6,
        overflow: "hidden",
        flexShrink: 0,
        background: "rgba(255,255,255,0.08)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      {url ? (
        <img
          src={url}
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
        />
      ) : (
        fallbackIcon ?? <FaGamepad size={size * 0.55} style={{ opacity: 0.5 }} />
      )}
    </div>
  );
};

// Icon + name in a bordered box — reused for "which game these capture
// areas are for" (capture-area section) and "which game gets tagged on
// buffered cards" (Anki section).
const GameBox: FC<{
  appid?: string | null;
  displayName: string;
  fallbackIcon?: ReactNode;
  action?: ReactNode;
}> = ({ appid, displayName, fallbackIcon, action }) => (
  <div
    style={{
      display: "flex",
      gap: 10,
      alignItems: "center",
      padding: 6,
      borderRadius: 4,
      background: "rgba(255,255,255,0.06)",
      border: "1px solid rgba(255,255,255,0.1)",
    }}
  >
    <AppThumbnail appid={appid} fallbackIcon={fallbackIcon} />
    <div style={{ fontSize: 13, fontWeight: 600, flex: 1 }}>{displayName}</div>
    {action}
  </div>
);

export const Panel: FC = () => {
  const [status, setStatus] = useState<PluginStatus | null>(null);
  const [settings, setSettingsState] = useState<Record<string, any> | null>(null);
  const [busyMsg, setBusyMsg] = useState("");
  const [qrUrl, setQrUrl] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [showDebug, setShowDebug] = useState(false);
  const [deletingData, setDeletingData] = useState(false);
  const [dataMsg, setDataMsg] = useState("");
  // screenshot_id -> base64 PNG, fetched lazily and cached across renders
  const [thumbs, setThumbs] = useState<Record<string, string>>({});
  const alive = useRef(true);
  const lastLocalEdit = useRef(0);

  useEffect(() => {
    if (!qrUrl) return;
    const t = setTimeout(() => setQrUrl(null), 5 * 60 * 1000);
    return () => clearTimeout(t);
  }, [qrUrl]);

  const handleExport = async () => {
    setBusyMsg("");
    if (!status?.runtime?.anki_installed) {
      await installAnkiExportRuntime();
      setBusyMsg("Installing…");
      return;
    }
    setExporting(true);
    setQrUrl(null);
    try {
      const r = await exportAnkiBuffer();
      if (r.ok && r.url) {
        setQrUrl(r.url);
        setBusyMsg(`Exported ${r.count} card(s) — scan to import`);
      } else {
        setBusyMsg(r.error ?? "export failed");
      }
    } finally {
      setExporting(false);
    }
  };

  const handleDeleteData = async () => {
    setDataMsg("");
    const { bytes } = await getDownloadedDataSize();
    showModal(
      <ConfirmModal
        strTitle="Delete downloaded data?"
        strDescription={
          `Removes the OCR runtime, OCR models and dictionary (${formatMb(bytes)}). ` +
          "Your settings, capture areas and Anki queue are kept. " +
          "You'll need to download everything again to use the plugin."
        }
        strOKButtonText="Delete"
        bDestructiveWarning
        onOK={async () => {
          setDeletingData(true);
          try {
            const r = await deleteDownloadedData();
            setDataMsg(
              r.ok ? `Deleted ${formatMb(r.freed_bytes ?? 0)} of downloaded data` : r.error ?? "delete failed"
            );
            void refreshStatus();
          } finally {
            setDeletingData(false);
          }
        }}
      />
    );
  };

  const refreshStatus = async () => {
    try {
      const s = await getStatus();
      if (alive.current) setStatus(s);
      // pick up settings changed elsewhere (e.g. the visual region editor
      // modal saves directly) — but never clobber an in-progress slider drag
      if (Date.now() - lastLocalEdit.current > 3000) {
        const fresh = await getAllSettings();
        if (alive.current && Date.now() - lastLocalEdit.current > 3000) {
          setSettingsState(fresh);
        }
      }
    } catch {
      /* backend still starting */
    }
  };

  useEffect(() => {
    alive.current = true;
    void refreshStatus();
    void getAllSettings().then((s) => alive.current && setSettingsState(s));
    const t = setInterval(refreshStatus, 2500);
    // a finished scan shows up now, not on the next poll
    const onEvent = (ev: VnlEvent) => {
      if (ev.stage === "done" || ev.stage === "error") void refreshStatus();
    };
    addEventListener<[VnlEvent]>("vnl_event", onEvent);
    return () => {
      alive.current = false;
      clearInterval(t);
      removeEventListener("vnl_event", onEvent);
    };
  }, []);

  const update = (key: string, value: any) => {
    lastLocalEdit.current = Date.now();
    setSettingsState((prev) => (prev ? { ...prev, [key]: value } : prev));
    void setSetting(key, value);
  };

  const defaultAreas: CaptureArea[] =
    Array.isArray(settings?.capture_areas) && settings.capture_areas.length > 0
      ? settings.capture_areas
      : [{ region: { x: 0.03, y: 0.62, w: 0.94, h: 0.36 }, button: "L5" }];

  const profiles: Record<string, CaptureProfile> = settings?.capture_profiles ?? {};
  const runningApp = Router.MainRunningApp;
  // no appid (no game running, or Steam can't report one) shares one
  // generic bucket — there's no per-game identity to key a profile on
  const editingKey = runningApp?.appid || UNKNOWN_APP_KEY;
  const editingProfile = profiles[editingKey];
  const hasCustomProfile = !!editingProfile?.areas?.length;
  const areas: CaptureArea[] = hasCustomProfile ? editingProfile.areas : defaultAreas;

  // lazily fetch any area screenshot we haven't cached yet — cheap no-op
  // once every id currently in `areas` is already in `thumbs`
  useEffect(() => {
    for (const a of areas) {
      const id = a.screenshot_id;
      if (id && !(id in thumbs)) {
        void getAreaScreenshot(id).then((r) => {
          if (r.ok && r.image) {
            setThumbs((prev) => (id in prev ? prev : { ...prev, [id]: r.image! }));
          }
        });
      }
    }
  }, [areas, thumbs]);

  // editing with no saved profile yet writes one on the first change,
  // seeded from whatever Default showed — that's the "automatic save"
  const updateAreas = (next: CaptureArea[]) => {
    update("capture_profiles", {
      ...profiles,
      [editingKey]: {
        display_name: runningApp?.display_name ?? "Unknown game",
        areas: next,
      },
    });
  };

  const setAreaButton = (i: number, button: string) => {
    const next = areas.map((a, idx) => {
      if (idx === i) return { ...a, button: button === "off" ? null : button };
      // a physical button can only trigger one area — clear it elsewhere
      return a.button === button && button !== "off" ? { ...a, button: null } : a;
    });
    updateAreas(next);
  };

  const setAreaRegion = (i: number, region: Region, screenshotId?: string) => {
    updateAreas(
      areas.map((a, idx) =>
        idx === i ? { ...a, region, ...(screenshotId ? { screenshot_id: screenshotId } : {}) } : a
      )
    );
  };

  // persists the screenshot the editor was showing when the user saved
  // (skipped on cancel, since openRegionEditor's onSave only fires on save)
  const saveAreaEdit = async (i: number, region: Region, image: string | null) => {
    const area = areas[i];
    let screenshotId = area.screenshot_id ?? undefined;
    if (image) {
      const r = await saveAreaScreenshot(image, screenshotId ?? null);
      if (r.ok && r.id) {
        screenshotId = r.id;
        setThumbs((prev) => ({ ...prev, [r.id!]: image }));
      }
    }
    setAreaRegion(i, region, screenshotId);
  };

  const addArea = () => updateAreas([...areas, { region: NEW_AREA_REGION, button: "L4" }]);

  const deleteArea = (i: number) => {
    const id = areas[i].screenshot_id;
    updateAreas(areas.filter((_, idx) => idx !== i));
    if (id) {
      void deleteAreaScreenshot(id);
      setThumbs((prev) => {
        if (!(id in prev)) return prev;
        const rest = { ...prev };
        delete rest[id];
        return rest;
      });
    }
  };

  if (!settings) {
    return (
      <PanelSection title="kumadeck">
        <PanelSectionRow>Loading…</PanelSectionRow>
      </PanelSection>
    );
  }

  const runtime = status?.runtime;
  const models = status?.models;
  const setupDone = !!runtime?.installed && !!models?.installed;
  const buffered = status?.anki_buffered ?? 0;

  return (
    <>
      <LookupSection
        sentence={status?.last_result?.text ?? null}
        confidence={status?.last_result?.confidence ?? null}
        ankiEnabled={!!settings.anki_enabled}
      />

      {!setupDone && (
        <PanelSection title="Setup (one-time)">
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={runtime?.installing || runtime?.installed}
              onClick={() => void installRuntime()}
            >
              {runtime?.installed
                ? "✓ OCR runtime installed"
                : runtime?.installing
                ? `Installing… (${runtime.step})`
                : "1. Install OCR runtime (~400 MB)"}
            </ButtonItem>
          </PanelSectionRow>
          {runtime?.error ? (
            <PanelSectionRow>
              <div style={{ fontSize: 11, color: "#e74c3c" }}>{runtime.error}</div>
            </PanelSectionRow>
          ) : null}
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={models?.downloading || models?.installed}
              onClick={() => void downloadModels()}
            >
              {models?.installed
                ? "✓ OCR models downloaded"
                : models?.downloading
                ? `Downloading… ${Math.round((models.progress ?? 0) * 100)}%`
                : `2. Download OCR models (~${models?.approx_size_mb ?? 22} MB)`}
            </ButtonItem>
          </PanelSectionRow>
          {models?.error ? (
            <PanelSectionRow>
              <div style={{ fontSize: 11, color: "#e74c3c" }}>{models.error}</div>
            </PanelSectionRow>
          ) : null}
        </PanelSection>
      )}

      <PanelSection title="Capture area">
        <PanelSectionRow>
          <div style={{ marginBottom: 8 }}>
            <GameBox
              appid={runningApp?.appid}
              displayName={runningApp ? runningApp.display_name : "Game not detected"}
            />
          </div>
        </PanelSectionRow>
        {areas.map((area, i) => (
          <div
            key={i}
            style={{
              borderTop: i > 0 ? "1px solid rgba(255,255,255,0.1)" : "none",
              // areas after the second follow a padded "Delete" button, so
              // only the first divider needs its own margin
              marginTop: i === 1 ? 12 : 0,
              paddingTop: i > 0 ? 12 : 0,
            }}
          >
            <PanelSectionRow>
              <AreaThumbnail
                region={area.region}
                screenshotSrc={area.screenshot_id ? thumbs[area.screenshot_id] : undefined}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <Focusable
                style={{ display: "flex", gap: 6, alignItems: "stretch" }}
                flow-children="row"
              >
                <DialogButton
                  style={{
                    flex: "0 1 auto",
                    width: "fit-content",
                    whiteSpace: "nowrap",
                  }}
                  onClick={() =>
                    openRegionEditor(
                      "Select the area to capture",
                      area.region,
                      (r, image) => void saveAreaEdit(i, r, image)
                    )
                  }
                >
                  Change area
                </DialogButton>
                <TriggerButtonSelector
                  code={area.button}
                  onOpen={(parent, resetHighlight) =>
                    openTriggerButtonMenu(
                      parent,
                      area.button,
                      (b) => setAreaButton(i, b ?? "off"),
                      resetHighlight
                    )
                  }
                />
              </Focusable>
            </PanelSectionRow>
            {i > 0 && (
              <PanelSectionRow>
                <ButtonItem
                  layout="below"
                  bottomSeparator="none"
                  onClick={() => deleteArea(i)}
                >
                  Delete
                </ButtonItem>
              </PanelSectionRow>
            )}
          </div>
        ))}
        <div
          style={{
            borderTop: "1px solid rgba(255,255,255,0.1)",
            // same rule as the dividers above: no margin after a padded
            // "Delete" button
            marginTop: areas.length > 1 ? 0 : 12,
          }}
        >
          <PanelSectionRow>
            <ButtonItem layout="below" bottomSeparator="none" onClick={addArea}>
              Add capture area
            </ButtonItem>
          </PanelSectionRow>
        </div>
      </PanelSection>

      <PanelSection title="Anki">
        <PanelSectionRow>
          <ToggleField
            label="Enable Anki integration"
            checked={!!settings.anki_enabled}
            onChange={(v) => update("anki_enabled", v)}
            bottomSeparator={settings.anki_enabled ? "standard" : "none"}
          />
        </PanelSectionRow>
        {settings.anki_enabled && (
          <>
            <PanelSectionRow>
              <Field childrenLayout="below" bottomSeparator="standard">
                <TextField
                  label="Deck name"
                  value={settings.anki_deck}
                  onChange={(e) => update("anki_deck", e.target.value)}
                />
              </Field>
            </PanelSectionRow>

            <PanelSectionRow>
              <div style={{ marginTop: 12, marginBottom: 4 }}>
                <GameBox
                  appid={null}
                  displayName={`${buffered} card${buffered === 1 ? "" : "s"} in Anki queue`}
                  fallbackIcon={<FaClone size={18} style={{ opacity: 0.5 }} />}
                  action={
                    <DialogButton
                      style={{ width: "fit-content", minWidth: 0, padding: "8px 10px" }}
                      disabled={buffered === 0}
                      onClick={() => showModal(<AnkiBufferModal settings={settings} />)}
                    >
                      <div style={{ display: "flex", alignItems: "center", justifyContent: "center" }}>
                        <FaEye />
                      </div>
                    </DialogButton>
                  }
                />
              </div>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                bottomSeparator="none"
                disabled={buffered === 0 || !!runtime?.installing || exporting}
                onClick={handleExport}
              >
                {!runtime?.anki_installed
                  ? runtime?.installing
                    ? `Installing… (${runtime.step})`
                    : "Install Anki export runtime (~5 MB)"
                  : exporting
                  ? "Exporting…"
                  : "Export via QR code"}
              </ButtonItem>
            </PanelSectionRow>
            {runtime?.error ? (
              <PanelSectionRow>
                <div style={{ fontSize: 11, color: "#e74c3c" }}>{runtime.error}</div>
              </PanelSectionRow>
            ) : null}

            {qrUrl ? (
              <>
                <PanelSectionRow>
                  <div
                    style={{ display: "flex", justifyContent: "center", padding: "8px 0" }}
                  >
                    <QrCode value={qrUrl} />
                  </div>
                </PanelSectionRow>
                <PanelSectionRow>
                  <div style={{ fontSize: 11, wordBreak: "break-all", opacity: 0.7 }}>
                    {qrUrl}
                  </div>
                </PanelSectionRow>
                <PanelSectionRow>
                  <div style={{ fontSize: 11, opacity: 0.7 }}>
                    Scan, then "Open in Anki" on your phone. Link expires in a
                    few minutes.
                  </div>
                </PanelSectionRow>
              </>
            ) : null}

            <PanelSectionRow>
              <div style={{ marginTop: runtime?.error || qrUrl ? 0 : -8 }}>
                <ButtonItem
                  layout="below"
                  bottomSeparator="none"
                  disabled={buffered === 0}
                  onClick={async () => {
                    await clearAnkiBuffer();
                    setQrUrl(null);
                    setBusyMsg("Anki queue cleared");
                  }}
                >
                  Clear Anki queue
                </ButtonItem>
              </div>
            </PanelSectionRow>

            {busyMsg ? (
              <PanelSectionRow>
                <div style={{ fontSize: 12, color: "#dcae3c" }}>{busyMsg}</div>
              </PanelSectionRow>
            ) : null}
          </>
        )}
      </PanelSection>

      <PanelSection title="Advanced settings">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => {
              update("capture_profiles", {});
              void deleteAllAreaScreenshots();
              setThumbs({});
            }}
          >
            Delete all capture areas
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={
              deletingData || !!status?.runtime?.installing || !!status?.models?.downloading
            }
            onClick={() => void handleDeleteData()}
          >
            {deletingData ? "Deleting…" : "Delete downloaded data"}
          </ButtonItem>
        </PanelSectionRow>
        {dataMsg ? (
          <PanelSectionRow>
            <div style={{ fontSize: 12, color: "#dcae3c" }}>{dataMsg}</div>
          </PanelSectionRow>
        ) : null}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            bottomSeparator="none"
            onClick={() => setShowDebug((v) => !v)}
          >
            {showDebug ? "Hide debug info" : "Show debug info"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {showDebug && (
        <PanelSection title="DEBUG INFO">
          <PanelSectionRow>
            <div style={{ fontSize: 12, lineHeight: 1.6 }}>
              <div>
                Texthooker page:{" "}
                <b>http://localhost:{status?.delivery.port ?? 8766}/</b>
              </div>
              <div>
                Readers connected: <b>{status?.delivery.clients ?? "?"}</b>
              </div>
              <div>
                Controller:{" "}
                <b>{status?.monitor.initialized ? "hooked" : "not found"}</b>
                {" · "}PipeWire:{" "}
                <b>{status?.capture?.pipewire_source ? "ok" : "no source"}</b>
              </div>
              <div>
                Running app appid: <b>{runningApp?.appid ?? "(none reported)"}</b>
                {" · "}Capture-area profile key: <b>{editingKey}</b>
              </div>
            </div>
          </PanelSectionRow>
        </PanelSection>
      )}

      <PanelSection title="About / sources">
        <PanelSectionRow>
          <div style={{ fontSize: 13, lineHeight: 1.6, marginBottom: 10 }}>
            <div>Version {status?.version ?? "?"}</div>
            <div>kumadeck: fork of Japanese Lookup by Simon Callsen</div>
            <div>github.com/Pedrubik2000/kumadeck</div>
          </div>
          <div style={{ fontSize: 10, opacity: 0.6, lineHeight: 1.5 }}>
            GPL-3.0-or-later; capture, controller-hook, and
            overlay code are ported from Decky-Translator (cat-in-a-box).
            The built-in dictionary downloads Jitendex (jitendex.org, CC
            BY-SA 4.0), built from JMdict/EDICT by the Electronic Dictionary
            Research and Development Group (edrdg.org) and Tatoeba example
            sentences (CC BY 2.0 FR). Full credits and license in the
            project README/LICENSE on GitHub.
          </div>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};
