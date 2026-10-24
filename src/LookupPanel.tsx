// Native lookup UI: the last captured line as focusable word chips;
// selecting a chip shows dictionary entries below and can create an Anki
// card directly. Runs entirely local — no Firefox/Yomitan involved.

import {
  ButtonItem,
  DialogButton,
  Focusable,
  PanelSection,
  PanelSectionRow,
  Router,
} from "@decky/ui";
import { FC, useEffect, useRef, useState } from "react";
import { FaMinus, FaPlus } from "react-icons/fa";
import {
  createAnkiCard,
  getWordStates,
  DictEntry,
  getLookupStatus,
  importDictionaries,
  installLookupRuntime,
  LookupStatus,
  lookupWord,
  removeAnkiBufferCard,
  Token,
  tokenizeLine,
} from "./api";
import { onFirstWordFocusRequest, takeFirstWordFocus } from "./firstWordFocus";

// POS classes that gamepad focus skips (still touch-tappable): particles,
// auxiliaries — you rarely look them up, and skipping them makes D-pad
// navigation step word-to-word instead of morpheme-to-morpheme.
const GRAMMAR_POS = new Set(["助詞", "助動詞"]);

// kumapie's word colours: green a card due today, blue mined and never reviewed, red no card of its own; studied
// words stay the normal colour. Only when kuma3 answers (else no colours at all).
const WORD_COLOURS = { d: "#7CC47F", b: "#6FA8FF", u: "#FF6B6B" } as const;

const isContentWord = (t: Token) =>
  t.selectable && !GRAMMAR_POS.has(t.pos);

// below this, the OCR read is shaky enough to flag — not a hard science,
// just a heads-up that the capture area/game text might need a look
const LOW_CONFIDENCE_THRESHOLD = 0.6;

// Yomitan-style scan: candidate lookup keys from the tapped token outward,
// longest first. For each window, try the raw surface and the surface with
// the last token in dictionary form (気になっ… → 気になる). The backend
// returns entries for the first key that hits, so compounds win when the
// dictionary knows them.
const candidatesAt = (tokens: Token[], i: number): string[] => {
  const cands: string[] = [];
  const maxW = Math.min(5, tokens.length - i);
  for (let w = maxW; w >= 2; w--) {
    const slice = tokens.slice(i, i + w);
    const surface = slice.map((t) => t.surface).join("");
    const withDictForm =
      slice.slice(0, -1).map((t) => t.surface).join("") +
      slice[slice.length - 1].dict_form;
    cands.push(surface);
    if (withDictForm !== surface) cands.push(withDictForm);
  }
  const t = tokens[i];
  cands.push(t.dict_form, t.lemma.split("-")[0], t.surface, t.reading);
  return [...new Set(cands.filter(Boolean))];
};

export const LookupSection: FC<{
  sentence: string | null;
  confidence: number | null;
  ankiEnabled: boolean;
}> = ({ sentence, confidence, ankiEnabled }) => {
  const [status, setStatus] = useState<LookupStatus | null>(null);
  const [tokens, setTokens] = useState<Token[]>([]);
  const [wordStates, setWordStates] = useState<Record<string, string> | null>(null);
  const refreshStates = (ts: Token[] = tokens) =>
    void getWordStates([...new Set(ts.flatMap((t) => [t.dict_form, t.surface]))])
      .then((r) => alive.current && setWordStates(r.ok ? r.states : null));
  const colourOf = (t: Token): string | undefined => {
    if (!wordStates) return undefined;
    const s = wordStates[t.dict_form] ?? wordStates[t.surface] ?? "u";
    return WORD_COLOURS[s as keyof typeof WORD_COLOURS];
  };
  const [sel, setSel] = useState<[number, number] | null>(null); // token index range
  const [focused, setFocused] = useState<number | null>(null);
  const [focusedEntry, setFocusedEntry] = useState<number | null>(null);
  const [entries, setEntries] = useState<DictEntry[] | null>(null);
  const [message, setMessage] = useState("");
  // expression|reading -> buffered card id, for words added this session
  // (lets the +/- icon reflect current queue membership without refetching
  // the whole buffer)
  const [queued, setQueued] = useState<Record<string, string>>({});
  const alive = useRef(true);
  const tokenizedFor = useRef<string | null>(null);
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const entriesRef = useRef<HTMLDivElement | null>(null);
  const wantFocusMove = useRef(false);
  const sectionRef = useRef<HTMLDivElement | null>(null);
  // the sentence `tokens` currently belong to (tokenizedFor is set as soon
  // as a tokenize call starts, this only once its result is rendered)
  const tokensFor = useRef<string | null>(null);
  // bumped when a scan requests first-word focus, so a rescan of the same
  // line (no new tokens) still re-runs the focus effect
  const [scanSeq, setScanSeq] = useState(0);
  const firstWordRef = useRef<HTMLDivElement | null>(null);
  // the word whose definition is showing (or loading)
  const lookedUp = useRef<number | null>(null);

  const refresh = async () => {
    try {
      const s = await getLookupStatus();
      if (alive.current) setStatus(s);
    } catch {
      /* backend still starting */
    }
  };

  useEffect(() => {
    alive.current = true;
    void refresh();
    const t = setInterval(refresh, 2000);
    return () => {
      alive.current = false;
      clearInterval(t);
      if (hoverTimer.current) clearTimeout(hoverTimer.current);
    };
  }, []);

  // tokenize whenever a new sentence arrives and the runtime is ready
  useEffect(() => {
    if (!sentence || !status?.runtime_installed) return;
    if (tokenizedFor.current === sentence) return;
    tokenizedFor.current = sentence;
    setSel(null);
    setEntries(null);
    lookedUp.current = null;
    void tokenizeLine(sentence).then((r) => {
      if (!alive.current || !r.ok || !r.tokens) return;
      tokensFor.current = sentence;
      setTokens(r.tokens);
      refreshStates(r.tokens);
    });
  }, [sentence, status?.runtime_installed]);

  useEffect(() => onFirstWordFocusRequest(() => setScanSeq((n) => n + 1)), []);

  // once the fresh sentence's words actually render: scroll back to the top
  // of the Lookup section (title included) and, if a scan just opened the
  // QAM for it, focus its first word so its definition loads right away.
  // Doing this off the capture event itself raced the async tokenize call
  // above, scrolling before the new words (or the QAM) had rendered.
  useEffect(() => {
    if (tokens.length === 0) return;
    sectionRef.current?.scrollIntoView({ block: "start" });
    if (!takeFirstWordFocus(tokensFor.current)) return;
    const first = tokens.findIndex(isContentWord);
    if (first < 0) return;
    // a beat for the QAM to finish opening, or it takes focus back
    const t = setTimeout(() => {
      firstWordRef.current?.focus();
      sectionRef.current?.scrollIntoView({ block: "start" });
      void doLookupRef.current(first);
    }, 150);
    return () => clearTimeout(t);
  }, [tokens, scanSeq]);

  // D-pad rests on a word for a beat → look it up without pressing A.
  // Debounced so scrolling across the sentence doesn't fire per word.
  const onWordFocus = (i: number) => {
    setFocused(i);
    // already showing this word (e.g. back up from its definition)
    if (lookedUp.current === i) return;
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
    hoverTimer.current = setTimeout(() => void doLookup(i), 350);
  };

  const onWordBlur = (i: number) => {
    setFocused((cur) => (cur === i ? null : cur));
  };

  // A-press/tap moves focus into the definition once it renders (Steam's
  // gamepad focus follows DOM focus) and scrolls it into view — hover-only
  // lookups must not steal focus or jump the scroll while just browsing.
  useEffect(() => {
    if (!wantFocusMove.current || !entries?.length) return;
    wantFocusMove.current = false;
    const t = setTimeout(() => {
      entriesRef.current
        ?.querySelector<HTMLElement>("[tabindex], button")
        ?.focus();
      entriesRef.current?.scrollIntoView({ block: "start" });
    }, 50);
    return () => clearTimeout(t);
  }, [entries]);

  const doLookup = async (i: number, moveFocus = false) => {
    if (hoverTimer.current) clearTimeout(hoverTimer.current);
    wantFocusMove.current = moveFocus;
    lookedUp.current = i;
    setSel([i, i]);
    setEntries(null);
    setMessage("");
    const t = tokens[i];
    const r = await lookupWord(candidatesAt(tokens, i));
    if (!alive.current) return;
    let all = r.entries ?? [];

    // highlight the tokens the match actually covered
    const matched = all[0]?.matched ?? t.surface;
    let acc = 0;
    let j = i;
    while (j < tokens.length && acc < matched.length) {
      acc += tokens[j].surface.length;
      j++;
    }
    setSel([i, Math.max(i, j - 1)]);

    // compound matched: also offer the tapped word's own entries below it
    if (matched.length > t.surface.length) {
      const base = await lookupWord([
        t.dict_form, t.lemma.split("-")[0], t.surface, t.reading,
      ]);
      if (!alive.current) return;
      const seen = new Set(all.map((e) => e.expression + "|" + e.reading));
      all = all.concat(
        (base.entries ?? []).filter(
          (e) => !seen.has(e.expression + "|" + e.reading)));
    }
    setEntries(all);
  };
  // latest doLookup (it closes over this render's tokens), for the timer above
  const doLookupRef = useRef(doLookup);
  doLookupRef.current = doLookup;

  const entryKey = (e: DictEntry) => `${e.expression}|${e.reading}`;

  // same action the +/- icon performs — the entry Focusable's onActivate
  // calls this directly so a controller can add/remove without ever
  // targeting the icon itself.
  const toggleCard = async (e: DictEntry) => {
    const key = entryKey(e);
    const bufferedId = queued[key];
    if (bufferedId) {
      setMessage("Removing…");
      const r = await removeAnkiBufferCard(bufferedId);
      if (r.ok) {
        setQueued((prev) => {
          const next = { ...prev };
          delete next[key];
          return next;
        });
      }
      const from = bufferedId.startsWith("kuma3:") ? "kuma3" : "Anki queue";
      if (r.ok && from === "kuma3") refreshStates();
      setMessage(r.ok ? `✓ Removed ${e.expression} from ${from}` : "✗ failed to remove");
      return;
    }
    setMessage("Adding…");
    const game = Router.MainRunningApp?.display_name ?? "";
    // the word as it stands in the line (行こう for 行く), bolded in the card's sentence
    const surface = sel ? tokens.slice(sel[0], sel[1] + 1).map((t) => t.surface).join("") : "";
    const r = await createAnkiCard(
      e.expression, e.reading, e.glosses, sentence ?? "", game, e.word_type, surface);
    if (r.ok && r.id && !r.existing) setQueued((prev) => ({ ...prev, [key]: r.id! }));
    if (r.ok && r.kuma3) refreshStates();
    setMessage(
      !r.ok ? `✗ ${r.error}`
        : r.existing ? `✓ ${e.expression} is already in ${r.deck}`
        : r.kuma3 ? `✓ Added ${e.expression} to ${r.deck}` + (r.sync_error ? ` · ${r.sync_error}` : "")
        : `✓ Added ${e.expression} to Anki queue`);
  };

  // ---- setup states ------------------------------------------------------

  if (status && !status.runtime_installed) {
    return (
      <PanelSection title="Lookup">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={status.runtime.installing}
            onClick={() => void installLookupRuntime().then(refresh)}
          >
            {status.runtime.installing
              ? `Installing… (${status.runtime.step})`
              : "Install lookup runtime (~60 MB)"}
          </ButtonItem>
        </PanelSectionRow>
        {status.runtime.error ? (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: "#e74c3c" }}>
              {status.runtime.error}
            </div>
          </PanelSectionRow>
        ) : null}
      </PanelSection>
    );
  }

  if (status && !status.dictionary.ready) {
    const d = status.dictionary;
    return (
      <PanelSection title="Lookup">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={d.importing}
            onClick={() => void importDictionaries(true).then(refresh)}
          >
            {d.importing
              ? `${d.step}… ${Math.round(d.progress * 100)}%`
              : "Download dictionary (Jitendex, ~70 MB)"}
          </ButtonItem>
        </PanelSectionRow>
        {d.pending_zips.length > 0 && !d.importing ? (
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={() => void importDictionaries(false).then(refresh)}
            >
              Import {d.pending_zips.length} zip(s) from dicts folder
            </ButtonItem>
          </PanelSectionRow>
        ) : null}
        {d.error ? (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: "#e74c3c" }}>{d.error}</div>
          </PanelSectionRow>
        ) : null}
        <PanelSectionRow>
          <div style={{ fontSize: 10, opacity: 0.5 }}>
            Dictionary data: Jitendex (jitendex.org, CC BY-SA 4.0), built from
            JMdict/EDICT (edrdg.org) and Tatoeba. Full credits under "About /
            sources" in the plugin's settings panel.
          </div>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  // ---- main lookup UI ----------------------------------------------------

  const firstWordIdx = tokens.findIndex(isContentWord);

  return (
    <div ref={sectionRef}>
      <PanelSection title="Lookup">
        {sentence === null ? (
          <PanelSectionRow>
            <div style={{ fontSize: 12, opacity: 0.7 }}>
              Capture a sentence first, then analyze here.
            </div>
          </PanelSectionRow>
        ) : sentence === "" ? (
          <PanelSectionRow>
            <div style={{ fontSize: 12, color: "#e0a04f" }}>
              No text found – check capture area, or image quality may be too
              low to scan
            </div>
          </PanelSectionRow>
        ) : (
          <>
            <PanelSectionRow>
              <Focusable
                style={{
                  display: "flex",
                  flexWrap: "wrap",
                  alignItems: "baseline",
                  fontSize: 19,
                  lineHeight: 1.7,
                  fontFamily: '"Noto Sans CJK JP", "Hiragino Sans", sans-serif',
                  padding: entries !== null ? "2px 0 8px" : "2px 0",
                  borderBottom: entries !== null
                    ? "1px solid rgba(255,255,255,0.1)"
                    : "none",
                }}
              >
                {tokens.map((t, i) => {
                  const inSel = !!sel && i >= sel[0] && i <= sel[1];
                  const isFocused = focused === i;
                  // DialogButton = native gamepad focus. The width/minWidth
                  // overrides beat its default width:100% class so words sit
                  // side by side and wrap like a sentence. The transparent
                  // background also kills Steam's own focus highlight, so we
                  // paint our own from onGamepadFocus state.
                  return isContentWord(t) ? (
                    <DialogButton
                      key={i}
                      ref={i === firstWordIdx ? firstWordRef : undefined}
                      style={{
                        width: "fit-content",
                        minWidth: 0,
                        margin: 0,
                        padding: "0 2px",
                        fontSize: 19,
                        lineHeight: 1.7,
                        background: isFocused
                          ? "rgba(255,255,255,0.9)"
                          : inSel
                          ? "rgba(26,159,255,0.35)"
                          : "transparent",
                        color: isFocused ? "#111" : colourOf(t) ?? "#e8e8e8",
                        borderRadius: 3,
                        borderBottom: isFocused
                          ? "2px solid transparent"
                          : "2px dotted rgba(255,255,255,0.3)",
                        boxShadow: "none",
                        transition: "background 0.1s, color 0.1s",
                      }}
                      onClick={() => void doLookup(i, true)}
                      onGamepadFocus={() => onWordFocus(i)}
                      onGamepadBlur={() => onWordBlur(i)}
                      onOKActionDescription="Look up"
                    >
                      {t.surface}
                    </DialogButton>
                  ) : (
                    <span
                      key={i}
                      style={{
                        opacity: t.selectable ? 0.75 : 0.55,
                        background: inSel ? "rgba(26,159,255,0.35)" : undefined,
                        borderRadius: 3,
                        padding: "0 1px",
                      }}
                      onClick={t.selectable ? () => void doLookup(i, true) : undefined}
                    >
                      {t.surface}
                    </span>
                  );
                })}
              </Focusable>
            </PanelSectionRow>

            {typeof confidence === "number" && confidence < LOW_CONFIDENCE_THRESHOLD && (
              <PanelSectionRow>
                <div style={{ fontSize: 12, color: "#e0a04f" }}>
                  Accuracy low – check capture area
                </div>
              </PanelSectionRow>
            )}

            {message ? (
              <PanelSectionRow>
                <div
                  style={{
                    fontSize: 12,
                    color: message.startsWith("✓")
                      ? "#4caf50"
                      : message.startsWith("✗")
                      ? "#e74c3c"
                      : "#dcae3c",
                  }}
                >
                  {message}
                </div>
              </PanelSectionRow>
            ) : null}

            {entries !== null && entries.length === 0 && (
              <PanelSectionRow>
                <div style={{ fontSize: 12, color: "#e0a04f" }}>
                  No dictionary hits — the OCR may have misread a character
                  (the full line is also on the texthooker page in Firefox).
                </div>
              </PanelSectionRow>
            )}

            <div ref={entriesRef}>
            {(entries ?? []).map((e, i) => {
              const isEntryFocused = focusedEntry === i;
              const isQueued = ankiEnabled && !!queued[entryKey(e)];
              return (
              <PanelSectionRow key={i}>
                <Focusable
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: 4,
                    padding: "6px 2px",
                    borderTop: i > 0 ? "1px solid rgba(255,255,255,0.1)" : "none",
                    background: isEntryFocused
                      ? "rgba(26,159,255,0.12)"
                      : "transparent",
                    transition: "background 0.1s",
                  }}
                  tabIndex={0}
                  // landing on an entry and D-pad-down to the next one both
                  // work whether or not Anki is enabled — when it's off,
                  // A still "activates" the row (a no-op) rather than doing
                  // nothing, so browsing the list behaves consistently and
                  // still gets Steam's own button-press feedback.
                  onActivate={ankiEnabled ? () => void toggleCard(e) : () => {}}
                  onOKActionDescription={ankiEnabled
                    ? (isQueued ? "Remove from Anki queue" : "Add to Anki queue")
                    : undefined}
                  onGamepadFocus={() => setFocusedEntry(i)}
                  onGamepadBlur={() => setFocusedEntry((cur) => (cur === i ? null : cur))}
                >
                  {/* the entry itself is the gamepad-focus/scroll-into-view
                      target (tabIndex here, not just on the +/-Anki icon) so
                      landing on an entry and D-pad-down to the next one both
                      work even when Anki is disabled and no icon renders.
                      The icon below is the touch tap-target and also lights
                      up with the row so it's clear A toggles the card. */}
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "space-between",
                      gap: 6,
                    }}
                  >
                    <div style={{ fontSize: 18, minWidth: 0 }}>
                      <b style={{ color: "#fff" }}>{e.expression}</b>
                      {e.reading && e.reading !== e.expression ? (
                        <span style={{ opacity: 0.8 }}>【{e.reading}】</span>
                      ) : null}
                      {e.pitch ? (
                        <span style={{ fontSize: 12, opacity: 0.7 }}>
                          {" "}[{e.pitch.join(",")}]
                        </span>
                      ) : null}
                      {e.frequency ? (
                        <span
                          style={{
                            fontSize: 11,
                            marginLeft: 6,
                            padding: "1px 5px",
                            borderRadius: 4,
                            background: "rgba(79,195,247,0.2)",
                            color: "#9fdcf9",
                          }}
                        >
                          {e.frequency}
                        </span>
                      ) : null}
                    </div>
                    {ankiEnabled && (
                      <DialogButton
                        style={{
                          width: 32,
                          height: 32,
                          minWidth: 0,
                          padding: 0,
                          flexShrink: 0,
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          background: isEntryFocused
                            ? "rgba(26,159,255,0.9)"
                            : undefined,
                        }}
                        focusable={false}
                        onClick={() => void toggleCard(e)}
                      >
                        {isQueued ? <FaMinus size={12} /> : <FaPlus size={12} />}
                      </DialogButton>
                    )}
                  </div>
                  {e.word_type && (
                    <div style={{ fontSize: 11, opacity: 0.55, marginBottom: 2 }}>
                      {e.word_type}
                    </div>
                  )}
                  <div
                    style={{
                      fontSize: 13,
                      whiteSpace: "pre-wrap",
                      opacity: 0.92,
                      maxHeight: 240,
                      overflowY: "auto",
                    }}
                  >
                    {e.glosses}
                  </div>
                  {e.dicts.length > 0 && (
                    <div style={{ fontSize: 10, opacity: 0.45 }}>
                      {e.dicts.join(" · ")}
                    </div>
                  )}
                </Focusable>
              </PanelSectionRow>
              );
            })}
            </div>
          </>
        )}
      </PanelSection>
    </div>
  );
};
