# Twitch Null – Endlosstreifen

Zwei Seiten auf demselben Server: **Live** (nur Stream) und **Bedienung** (Einstellungen). Zeigt zufällige Twitch-Livestreams mit genau **0 Zuschauer\*innen**.

Von zehn Streams sind sieben aus IRL-Kategorien (Just Chatting, ASMR, Musik, Art, Crafts, Food, Talk, Fitness, Co-working …). Drei sind Gaming.

## Starten

Voraussetzungen: `ffmpeg` (inkl. `ffprobe`) und das Projekt-`.venv` (Pillow). **Immer** `.venv/bin/python` starten — macOS/`python3` kann CLT 3.9 sein; dann scheitert Pillow (`_imaging`) und Screenshots/Streifen bleiben tot.

```bash
cd ~/Desktop/261027_CODE_NEU
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
# oder: screen -dmS twitch-null bash -lc 'cd ~/Desktop/261027_CODE_NEU && exec ./.venv/bin/python server.py'
```

### URLs

| Seite | URL | Inhalt |
|------|-----|--------|
| **Live** | [http://localhost:8787/](http://localhost:8787/) oder `/live` | Vollbild-Stream, kein UI. Taste **Enter** = nächster Stream |
| **Bedienung** | [http://localhost:8787/controls](http://localhost:8787/controls) oder `/admin` | Screenshots an/aus, Live-Druck an/aus, nächster Stream, Streifen eingebettet |
| **Streifen** | [http://localhost:8787/print](http://localhost:8787/print) | Endlosstreifen-Vorschau + Browser-Druck |

Dateien nicht direkt als `file://` öffnen — Twitch-Player und APIs brauchen den lokalen Server.

### Live-Seite

- Stream füllt den Viewport **1:1** (`100vw`×`100vh`, kein Overscan/Zoom). Twitch **Interactive Embed** (`controls: false` + `pointer-events: none`) — Chrome ohne Video-Zuschnitt.
- **Ton:** Player startet `muted: true`. Ein Klick oder **Enter** (`#unlock-gate`) setzt im gleichen Player `setMuted(false)` + `setVolume(1)` (Watchdog). **Kein** Player-Recreate beim Unlock (das setzte Volume dauerhaft auf 0). `setChannel` bei Streamwechsel; Unmute wird nachgezogen.
- **Hard-Limit:** Zero-Gesture-Unmute geht in Chrome/Safari nicht; **eine** Geste pro Page-Load. Danach bleibt `muted=false` / `vol=1` auch nach **Enter**.
- **Geschwindigkeit:** Server-Prefetch-Queue (~3) + `/api/random?prefetch=1` (ändert `CURRENT_STREAM` nicht). Live hält einen **zweiten, unsichtbaren** `Twitch.Player` mit dem nächsten Kanal — **Enter** → CSS-Swap. Preload gemuted/unsichtbar bis Enter. **Sichtbarer Stream wechselt nur bei Enter** (kein Auto-Skip bei Ads/Offline/Timeout, kein Sync von `/api/current`).
- Keine Buttons/HUD. **Enter** / Numpad-Enter: nächster Stream.

### Bedienungsseite (`/controls`)

- Nächster Stream (aktualisiert den Server-Zustand; die Live-Seite folgt mit)
- Screenshots an/aus
- Live-Druck an/aus (+ Drucker-/Job-Info)
- **Pool-Filter:** Kategorien (IRL/Preferred) und Sprachen einzeln an/aus, Gaming an/aus
- **Auf Standard zurücksetzen:** stellt 7:3 IRL/Gaming, alle Default-Kategorien und die volle Sprachenliste wieder her (`POST /api/filters/reset`, schreibt `pool_filters.json`)
- Eingebettete Streifen-/Druck-Oberfläche (`/print`)

API: `GET/POST /api/filters`, `POST /api/filters/reset`.

## Screenshots & Endlosstreifen

Solange ein Stream läuft und Screenshots an sind, speichert der Server **jede Minute**:

1. Ein Einzelbild in `Screenshots/` im **VORLAGE_DRUCK**-Layout:
   - Stream-Screenshot in der Mitte
   - **Username** senkrecht links (unten→oben), Schrift Akkurat
   - **Datum/Zeit** senkrecht rechts (`TT.MM.JJ HH:MM`), Schrift Akkurat
2. Mehrere Captures desselben Streams werden direkt untereinander gehängt **ohne** Titelbalken.
3. Wechselt der Stream (**Enter** auf Live oder Button unter Bedienung), kommt **danach** einmal der warmgraue **Titelbalken** mit dem Streamtitel — am Ende des Blocks.

### Wo liegt der Streifen?

| Pfad | Zweck |
|------|--------|
| `Screenshots/endless_strip.jpg` | Primäre Streifen-Datei (Web: `/Screenshots/endless_strip.jpg`) |
| `Screenshots/endless_strip_full_*.jpg` | Archiv, wenn die JPEG-Höhe ~65000 px erreicht (danach neuer Streifen) |
| `Streifen/endless_strip.jpg` | Kopie im Ordner **Streifen** |
| `Screenshots/*.jpg` | Einzel-Captures / Titelbalken pro Minute bzw. Wechsel |

## Live-Druck (Stück für Stück, ESC/POS)

Nach jedem erfolgreichen Capture (und nach jedem Titelbalken beim Streamwechsel) rastert der Server **nur dieses neue JPEG** auf die native Druckbreite und schickt es **direkt per ESC/POS** (ohne CUPS) an den Thermodrucker — **nicht** den ganzen `endless_strip.jpg`. Code: `escpos.py` (Python-Port von [Servers_Printer](https://github.com/ZHdK-Critical-Coding/Servers_Printer)).

- **Epson TM-T88V:** 180 × 180 dpi, 80-mm-Papier → **512 Punkte** Breite, quadratische Punkte. Ein Frame (1304×675) wird 512×265 Punkte ≈ 37 mm hoch.
- Rasterbild per `GS v 0` in Bändern à 256 Zeilen, Floyd-Steinberg-Dithering. Frames laufen nahtlos aneinander (kein Schnitt, kein Vorschub).
- USB über `pyusb` + libusb (`brew install libusb`). Läuft gerade ein CUPS-Job auf demselben Drucker, ist das USB-Interface belegt.

### Druckerwahl beim Start

Ohne `ESCPOS_DEVICE` sucht der Server beim Start nach direkt ansprechbaren Druckern (USB-Druckerklasse, `/dev/usb/lp*`). Epson-TM-Geräte (USB `04b8:0e..` oder „TM-“ im Namen) stehen oben.

- **Im Terminal:** Menü mit den gefundenen Druckern sowie `s` Simulation, `r` neu suchen, `n` kein Live-Druck oder eine direkte Eingabe (`tcp://…`, Pfad, `usb:…`). Enter oder 20 s ohne Eingabe (`ESCPOS_SELECT_TIMEOUT`, 0 = warten) übernimmt den Vorschlag: erster Epson-TM, sonst `n`.
- **Ohne Terminal** (z. B. `run-server.sh`, `screen -dm`): automatisch der erste Epson-TM, sonst Live-Druck aus.
- **`ESCPOS_DEVICE` gesetzt:** keine Suche, kein Menü.

| Variable | Standard | Bedeutung |
|----------|----------|-----------|
| `ESCPOS_DEVICE` | *(Auswahl)* | `usb` (erster USB-Drucker, Epson bevorzugt), `usb:04b8:0202`, `usb:04b8:0202@1-2.3` (Bus-Port), `tcp://host:9100`, `/dev/usb/lp0`, `sim:Ordner` (Trockenlauf: `.bin` + `.png`) |
| `ESCPOS_WIDTH` | `512` | Druckbreite in Punkten (576 für 203-dpi-Modelle wie TM-T20) |
| `ESCPOS_DITHER` | `fs` | `fs` oder `threshold` |
| `ESCPOS_THRESHOLD` | `128` | Schwelle für `threshold` |
| `ESCPOS_GAMMA` | `1.0` | > 1 hellt Mitteltöne auf |
| `ESCPOS_CUT` / `ESCPOS_FEED` | `0` / `0` | Schnitt bzw. Zeilenvorschub nach jedem Bild |
| `PORT` | `8787` | HTTP-Port (z. B. `8788`, um neben dem CUPS-Original zu laufen) |

```bash
ESCPOS_DEVICE=sim:escpos_sim .venv/bin/python escpos.py Screenshots/irgendein_frame.jpg   # Test ohne Drucker
ESCPOS_DEVICE=usb .venv/bin/python escpos.py Screenshots/irgendein_frame.jpg              # echter Testdruck
```

### Ein/Aus

| Weg | Beispiel |
|-----|----------|
| UI | `/controls` → **„Live-Druck an / aus“** |
| API | `GET/POST /api/print-live` mit `{"enabled": true\|false}` |
| Env | `PRINT_LIVE=1` (an, Standard) bzw. `PRINT_LIVE=0` (aus) |

```bash
ESCPOS_DEVICE=usb PRINT_LIVE=1 .venv/bin/python server.py
ESCPOS_DEVICE=tcp://192.168.1.50:9100 PORT=8788 .venv/bin/python server.py
```

API-Status: `GET /api/print-live`. Aktueller Stream: `GET /api/current`. Streifen: `GET /api/strip`.

## Schriftart

| Pfad | Bedeutung |
|------|-----------|
| `assets/PRINT_FONT.ttf` | **Aktive** Druckschrift (AkkuratStd-Mono) |
| `assets/AkkuratStd-Mono.ttf` | Kopie unter dem Originalnamen |
| `assets/PRINT_FONT.ttf.fallback` | Arimo-Fallback |
| System | macOS Arial / Linux DejaVu als letzter Fallback |

## Unverändert (Backend)

Stream-Pool, 1‑Minuten-Screenshots, Streifen-Anhang, Titelbalken beim Wechsel, Campusprint-Live-Druck, Dual-Stack-Bind (`localhost` IPv4/IPv6), IRL/Gaming 7:3.
