# kumadeck

A personal fork of [scallsen/japanese-lookup](https://github.com/scallsen/japanese-lookup) (GPL-3.0) for a Steam
Deck LCD: read a game's dialogue in Game Mode, look a word up, and mine it into Anki. Planned changes, inspired by
[bee-san/Rubi_for_vita](https://github.com/bee-san/Rubi_for_vita):

- OCR picked by measurement on emulated games and the Yakuza series;
- Yomitan-style lookup (longest match from the cursor, deinflection) instead of MeCab lemmas;
- cards sent over Tailscale to the owner's Anki on a PC (AnkiConnect), with the line's audio;
- text hooks for emulators, checked against OCR.

No releases yet. Everything below is the upstream README.

---

<img width="1280" height="640" alt="gh-hero-image" src="https://github.com/user-attachments/assets/27b3df76-f70a-4cd4-b54a-835f617d3f9b" />

# Japanese Lookup for Decky

A [Decky Loader](https://decky.xyz/) plugin for reading Japanese visual novels (or any game with text boxes) without leaving gaming mode:

1. **Press a back button** (<picture><source media="(prefers-color-scheme: dark)" srcset="assets/icons/cutout/sd_l5.svg"><img height="16" alt="L5" src="assets/icons/dark/sd_l5.svg"></picture> by default) while a line of Japanese is on screen.
2. The plugin screenshots the text box, **reads the text on-device** (OCR — no internet needed), and opens the side menu with the line split into words.
3. **Move over a word** to see its dictionary definition.
4. Optionally **add the word to your Anki queue**, then later **scan a QR code** with your phone to import all queued cards into AnkiMobile or AnkiDroid.

Everything runs locally on the Deck. No Anki, browser, or account is needed on the Deck itself.

---

## Install 

> [!TIP]
> A [visual step-by-step guide](https://scallsen.github.io/japanese-lookup/) is available on the plugin website.

Every [release](https://github.com/scallsen/japanese-lookup/releases/latest) includes a ready-to-use `japanese-lookup-vX.Y.Z.zip` — no Node, pnpm, or SSH required. This uses Decky's own zip-sideloading, the same mechanism other unlisted plugins use since this one isn't on the Decky store.

1. **Install [Decky Loader](https://decky.xyz/)** first, if you haven't already — it's the plugin loader this plugin runs on top of. In Desktop Mode (hold <picture><source media="(prefers-color-scheme: dark)" srcset="assets/icons/cutout/sd_button_steam.svg"><img height="16" alt="Steam" src="assets/icons/dark/sd_button_steam.svg"></picture> → **Power** → **Switch to Desktop**), open a browser, go to [decky.xyz](https://decky.xyz/), download the installer, and run it (choose the *release* version). Switch back to Gaming Mode when it's done.
2. On the Deck, download the zip from the [latest release](https://github.com/scallsen/japanese-lookup/releases/latest) (Desktop Mode's browser is easiest, but Gaming Mode's works too).
3. Open Quick Access <picture><source media="(prefers-color-scheme: dark)" srcset="assets/icons/cutout/sd_button_aux.svg"><img height="16" alt="Quick Access" src="assets/icons/dark/sd_button_aux.svg"></picture> → the **Decky** tab (plug icon) → the gear icon → **General**, and turn on **Developer Mode** at the bottom.
4. A new **Developer** tab appears. Open it → **Install from zip** → browse to the file you downloaded (usually in `Downloads`) → select it.
5. Wait for it to finish, then check the **Decky** tab again — restart Decky Loader (`systemctl restart plugin_loader` in Konsole, or just reboot) if **Japanese Lookup** doesn't show up right away.

Then skip ahead to [First launch on the Deck](#first-launch-on-the-deck).

To update later, just repeat these steps with the newest release's zip — it overwrites the old install and keeps your settings, dictionary, and Anki queue.

---

<details>
<summary><h2>Install from source (Not required for general use)</h2></summary>

Building it yourself also works, and is the only option if you want to change the code. You build the plugin from this repository and a script copies it to your Deck over the network. The steps below walk you through it.

---

### Step 1 — Prepare the Deck (Desktop Mode)

This is the only time you need Desktop Mode. On the Deck, press the **Steam button → Power → Switch to Desktop**, then open **Konsole** (from the app launcher, under *System*).

1. **Set a password** for the `deck` user, if you've never done so. You'll need it again in Step 4.

   ```bash
   passwd
   ```

2. **Turn on SSH**, so your computer can copy files to the Deck:

   ```bash
   sudo systemctl enable --now sshd
   ```

3. **Install Decky Loader.** Open a browser on the Deck, go to [decky.xyz](https://decky.xyz/), download the installer, and run it (choose the *release* version). Or paste this into Konsole:

   ```bash
   curl -L https://github.com/SteamDeckHomebrew/decky-installer/releases/latest/download/install_release.sh | sh
   ```

4. **Note the Deck's address.** It's usually `steamdeck.local`. If that doesn't work later on, use its IP address instead: *Settings → Internet →* select your Wi-Fi network, and look for the IP address (e.g. `192.168.1.42`).

You can now switch back to gaming mode (the *Return to Gaming Mode* icon on the desktop).

### Step 2 — Connect your computer to the Deck

On your computer, open a terminal.

1. **Create an SSH key** (skip if `~/.ssh/id_ed25519` already exists; just press Enter at every prompt):

   ```bash
   ssh-keygen -t ed25519
   ```

2. **Copy it to the Deck.** It asks for the `deck` password from Step 1:

   ```bash
   ssh-copy-id deck@steamdeck.local
   ```

3. **Check it works.** This should log you in *without* asking for a password. Type `exit` to leave again.

   ```bash
   ssh deck@steamdeck.local
   ```

Replace `steamdeck.local` with the Deck's IP address everywhere if it doesn't connect.

### Step 3 — Get the code and build tools

You need [Node.js](https://nodejs.org/) 22 or newer, and `pnpm`:

```bash
npm install -g pnpm
```

Then download this repository and install its dependencies:

```bash
git clone https://github.com/scallsen/japanese-lookup.git
cd japanese-lookup
pnpm install
```

### Step 4 — Tell the deploy script about your Deck

Create your local settings file from the template:

```bash
cp .vscode/defsettings.json .vscode/settings.json
```

Open `.vscode/settings.json` in any text editor and fill in:

| Setting    | What to put there                                                     |
| ---------- | --------------------------------------------------------------------- |
| `deckip`   | `steamdeck.local`, or the Deck's IP address                           |
| `deckpass` | The `deck` password from Step 1 (needed to install the plugin as root) |
| `deckkey`  | Leave as is, unless your SSH key isn't `~/.ssh/id_ed25519`             |

Leave the other values alone. This file is ignored by git, so your password stays on your computer.

### Step 5 — Install the plugin on the Deck

```bash
./deploy.sh
```

This builds the plugin, copies it to the Deck, and restarts Decky. It should end with `==> Done.` Run the same command again any time you want to reinstall.

</details>

---

## First launch on the Deck

Back in gaming mode, press <picture><source media="(prefers-color-scheme: dark)" srcset="assets/icons/cutout/sd_button_aux.svg"><img height="16" alt="Quick Access" src="assets/icons/dark/sd_button_aux.svg"></picture> (Quick Access), open the **Decky** tab (the plug icon), and select **Japanese Lookup**.

The first time, the panel shows a few one-time download buttons. Make sure the Deck is on Wi-Fi, then press each one and wait for it to finish before moving on:

| Section | Button                                      | Size     |
| ------- | ------------------------------------------- | -------- |
| Lookup  | **Install lookup runtime**                  | ~60 MB   |
| Lookup  | **Download dictionary (Jitendex)**          | ~70 MB   |
| Setup   | **1. Install OCR runtime**                  | ~400 MB  |
| Setup   | **2. Download OCR models**                  | ~22 MB   |

---

## Everyday use

### 1. Capture a line

Start your game. When a line of Japanese text is on screen, **press L5**.

A brief outline flashes over the area being read, and the Quick Access menu opens on Japanese Lookup with the line shown as words.

### 2. Look up words

- **With the D-pad:** move over a word and pause — the definition appears below. Press **A** to jump into the definitions and scroll through them.
- **With the touchscreen:** tap a word.

Grammar words (particles etc.) are skipped by the D-pad but can still be tapped. Close the menu with **B** or `…` and keep playing.

### 3. Save words for Anki *(optional)*

1. Scroll down to the **Anki** section and turn on **Enable Anki integration**. 
2. When looking up a word, press **A** on a definition (or tap **+**). You'll see *✓ Added … to Anki queue*.

The details you see will be added to the card in Anki, along with the current sentence and name of the game.

Your queue is kept on the Deck until you clear it. Press the **eye icon** next to the card count to preview the cards and remove any you don't want.

### 4. Send your cards to your phone

1. Make sure your phone is on the **same Wi-Fi** as the Deck.
2. In the **Anki** section, press **Export via QR code**. 
3. **Scan the QR code** with your phone's camera and open the link.
4. When the download finishes, choose **Open in Anki** (AnkiMobile / AnkiDroid). Follow the app steps to import the new cards.
5. Once you've checked the cards arrived, press **Clear Anki queue** so they aren't exported twice.

The QR link only works for about 5 minutes. If it has expired, just export again.

### Adjusting the capture area

By default, **L5** reads the bottom third of the screen. You should change this for each game to capture the text area accurately.

1. **With the game running and its text box on screen**, open Japanese Lookup and press **Change area** in the **Capture area** section. The menu closes and a screenshot of the game appears with a blue box on top.
2. Drag the box over the text box, or use the button controls.
3. Press **A** to save.

Changes are saved **for the game that's currently running**.

You can optionally change the hotkey for capture to use the triggers (L1,R1), bumpers (L2, R2), pressing the joysticks (L3, R3), or the back buttons (L4, L5, R4, R5).

#### Create additional capture areas

For games that display text in different areas, create additional capture areas for them.

1. **With the game running and its text box on screen**, open Japanese Lookup and press **Add capture area** in the **Capture area** section.
2. Repeat the steps from the above section for the new capture area.
3. Change the hotkey by selecting the dropdown.

You can delete redundant capture areas by pressing **Delete**.

To reset every game back to the default area, use **Advanced settings → Delete all capture areas**.

---

## Updating

Follow the same steps needed to install the plugin. Decky will recognize the new version as an update, and replace the old plugin. The dependencies (OCR model, Dictionary, etc) will carry over to the new version.

---

## Freeing up space and uninstalling

**To free up space but keep the plugin**, open **Advanced settings → Delete downloaded data**. This removes the OCR engine, OCR models, and dictionary (about 800 MB). Your settings, capture areas, and Anki queue are kept, and the download buttons from [First launch](#first-launch-on-the-deck) come back so you can reinstall them later.

**To remove the plugin completely**, open Quick Access (**…**) → **Decky** tab → gear icon → **Plugins**, open the menu next to Japanese Lookup, and choose **Uninstall**. About 20 seconds later, everything the plugin downloaded or saved is deleted too: the OCR engine, models, dictionary, settings, Anki queue, and logs.


---

## Optional: Yomitan in Firefox

If you prefer [Yomitan](https://github.com/yomidevs/yomitan) (for example to use your own dictionaries), the plugin also shows every captured line on a local page at found by pressing **Show debug info** in the **Advanced settings** section. **You don't need any of this for the built-in lookup.**

---

## Credits & data sources

**Code.** Screen capture, the back-button monitor, and the in-game overlay are ported from [Decky-Translator](https://github.com/cat-in-a-box/Decky-Translator) by Alexander Timoshuk, licensed GPL-3.0. Because of that, this whole project is licensed **GPL-3.0-or-later** (see [LICENSE](LICENSE)) — anyone redistributing a modified build needs to keep it open under the same terms. Capture-area tuning is informed by the research at [rentry.co/deckmining](https://rentry.co/deckmining). Project scaffolding comes from [decky-plugin-template](https://github.com/SteamDeckHomebrew/decky-plugin-template).

**Dictionary data.** The plugin ships no dictionary — the built-in lookup downloads [Jitendex](https://jitendex.org/) (or any other Yomitan-format dictionary you supply) at runtime, into a local database on your Deck. Jitendex itself is built from, and credits:

- [JMdict/EDICT](https://www.edrdg.org/wiki/index.php/JMdict-EDICT_Dictionary_Project) by the [Electronic Dictionary Research and Development Group](https://www.edrdg.org/) at Monash University — used under EDRDG's [licence terms](https://www.edrdg.org/edrdg/licence.html).
- [Tatoeba](https://tatoeba.org/) example sentences (CC BY 2.0 FR).
- [JmdictFurigana](https://github.com/Doublevil/JmdictFurigana) furigana data (CC BY-SA).
- Jitendex's own compiled data is CC BY-SA 4.0 — see [jitendex.org/pages/legal](https://jitendex.org/pages/legal.html).

The dictionary format is [Yomitan](https://github.com/yomidevs/yomitan)'s.

**Other runtime dependencies**, downloaded on the Deck rather than bundled: [RapidOCR](https://github.com/RapidAI/RapidOCR) and [ONNX Runtime](https://github.com/microsoft/onnxruntime) (Apache-2.0 / MIT) for OCR; [fugashi](https://github.com/polm/fugashi) + [unidic-lite](https://github.com/polm/unidic-lite) for splitting sentences into words; [genanki](https://github.com/kerrickstaley/genanki) (MIT) for building the exported Anki deck.

---

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
