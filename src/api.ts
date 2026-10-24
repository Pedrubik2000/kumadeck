import { callable } from "@decky/api";

export interface VnlEvent {
  stage: "capturing" | "ocr" | "done" | "error";
  message?: string;
  text?: string;
  raw?: string;
  confidence?: number;
  warning?: string | null;
  clients?: number;
  copy_to_clipboard?: boolean;
  region?: Region | null;
}

export interface PluginStatus {
  version: string;
  monitor: { running: boolean; initialized: boolean; device_path: string | null };
  runtime: { installed: boolean; lookup_installed: boolean; anki_installed: boolean; installing: boolean; step: string; error: string | null };
  models: { installed: boolean; downloading: boolean; progress: number; error: string | null; approx_size_mb: number };
  capture: { pipewire_source?: boolean; pngenc?: boolean; dims?: [number, number] | null; error?: string };
  delivery: { port: number; clients: number };
  anki_buffered: number;
  busy: boolean;
  last_result?: { text: string; raw: string; confidence: number } | null;
}

export interface Token {
  surface: string;
  dict_form: string;
  lemma: string;
  reading: string;
  pos: string;
  selectable: boolean;
}

export interface DictEntry {
  expression: string;
  reading: string;
  matched: string;
  glosses: string;
  word_type: string;
  tags: string;
  dicts: string[];
  frequency: string | null;
  pitch: number[] | null;
}

export interface LookupStatus {
  runtime_installed: boolean;
  runtime: { installing: boolean; step: string; error: string | null };
  dictionary: {
    dictionaries: string[];
    term_count: number;
    pending_zips: string[];
    importing: boolean;
    progress: number;
    step: string;
    error: string | null;
    ready: boolean;
  };
}

export interface Region { x: number; y: number; w: number; h: number }

// Steam Deck's screen (1280x800) — region x/y/w/h fractions and saved
// screenshots are both defined against this ratio; the region editor
// (RegionEditor.tsx) and the QAM thumbnail (Panel.tsx) must use the exact
// same ratio + `objectFit: "fill"`, or a region's box won't land on the
// same screen content it was drawn on.
export const SCREEN_ASPECT_RATIO = "16 / 10";

// Capture-area profile key for whatever's running when Steam can't report
// an appid (used both when no game is running and, in practice, never
// reached from a real capture — captures only fire while Router.MainRunningApp
// is set). Mirrors UNKNOWN_PROFILE_KEY in main.py.
export const UNKNOWN_APP_KEY = "unknown";

export const captureAndMine = callable<[button?: string, appid?: string], { ok: boolean; error?: string }>("capture_and_mine");
export const getButtonState = callable<[], { success: boolean; buttons: string[] }>("get_button_state");
export const getStatus = callable<[], PluginStatus>("get_status");
export const getAllSettings = callable<[], Record<string, any>>("get_all_settings");
export const setSetting = callable<[key: string, value: any], { ok: boolean }>("set_setting");
export const installRuntime = callable<[], { started: boolean }>("install_runtime");
export const downloadModels = callable<[], { started: boolean }>("download_models");
export const getDownloadedDataSize = callable<[], { bytes: number }>("get_downloaded_data_size");
export const deleteDownloadedData = callable<[], { ok: boolean; freed_bytes?: number; error?: string }>("delete_downloaded_data");

// visual region editor
export const getEditorFrame = callable<[], { ok: boolean; image?: string; error?: string }>("get_editor_frame");
export const captureEditorFrame = callable<[], { ok: boolean; image?: string; error?: string }>("capture_editor_frame");
export const detectRegion = callable<[], { ok: boolean; region?: Region; image?: string; error?: string }>("detect_region");

// per capture-area screenshot, persisted so the QAM thumbnail can preview
// the region against the real screen it was picked from
export const saveAreaScreenshot = callable<
  [imageB64: string, existingId?: string | null],
  { ok: boolean; id?: string; error?: string }
>("save_area_screenshot");
export const getAreaScreenshot = callable<[areaId: string], { ok: boolean; image?: string; error?: string }>("get_area_screenshot");
export const deleteAreaScreenshot = callable<[areaId: string], { ok: boolean }>("delete_area_screenshot");
export const deleteAllAreaScreenshots = callable<[], { ok: boolean }>("delete_all_area_screenshots");

// native lookup
export const tokenizeLine = callable<[text: string], { ok: boolean; tokens?: Token[]; error?: string }>("tokenize_line");
export const lookupWord = callable<[queries: string[]], { ok: boolean; entries: DictEntry[] }>("lookup_word");
export const getLookupStatus = callable<[], LookupStatus>("get_lookup_status");
export const installLookupRuntime = callable<[], { started: boolean }>("install_lookup_runtime");
export const importDictionaries = callable<[downloadJitendex: boolean], { started: boolean }>("import_dictionaries");
export const createAnkiCard = callable<
  [
    expression: string,
    reading: string,
    glosses: string,
    sentence: string,
    game: string,
    wordType: string,
  ],
  // kuma3: added straight into kuma3 on the Deck (deck = where); existing: that word was mined already
  { ok: boolean; buffered?: number; id?: string; error?: string; kuma3?: boolean; existing?: boolean; deck?: string }
>("create_anki_card");
export const clearAnkiBuffer = callable<[], { ok: boolean }>("clear_anki_buffer");
export const installAnkiExportRuntime = callable<[], { started: boolean }>("install_anki_export_runtime");
export const exportAnkiBuffer = callable<
  [],
  { ok: boolean; url?: string; count?: number; error?: string; needs_install?: boolean }
>("export_anki_buffer");

export interface BufferedCard {
  id: string;
  expression: string;
  reading: string;
  glosses: string;
  sentence: string;
  game: string;
  word_type: string;
}
export const getAnkiBuffer = callable<[], { cards: BufferedCard[] }>("get_anki_buffer");
export const removeAnkiBufferCard = callable<
  [cardId: string],
  { ok: boolean; buffered?: number }
>("remove_anki_buffer_card");
