"""Direkter ESC/POS-Druck (Epson TM-T88V) ohne CUPS.

Portiert aus dem Rust-Printer-Server (src/bitmap.rs, src/thermal.rs, src/devices.rs),
aber mit nativer Auflösung: GS v 0 Rasterbild, quadratische Punkte, volle Breite.

TM-T88V: 180 × 180 dpi, 80-mm-Papier → 512 Punkte Druckbreite (72 mm).
(203 dpi / 576 Punkte wären TM-T20/T82 — dann ESCPOS_WIDTH=576 setzen.)

ESCPOS_DEVICE (ohne Angabe: Epson-TM automatisch, sonst Auswahl beim Start, siehe choose_device):
  usb                 erster USB-Drucker der Klasse 7 (Epson bevorzugt)
  usb:04b8:0202       bestimmter USB-Drucker (Vendor:Product, hex)
  usb:04b8:0202@1-2.3 dito an Bus 1, Port 2.3 (gleiche Drucker unterscheiden)
  tcp://host:9100     Netzwerkdrucker (Raw-Port)
  /dev/usb/lp0        Gerätedatei (Linux)
  sim:Ordner          Simulation: speichert .bin + .png-Vorschau in Ordner
"""

import os
import socket
import threading
import time
from pathlib import Path

ESC = 0x1B
GS = 0x1D

PRINTER_CLASS = 0x07
EPSON_VENDOR = 0x04B8
CHUNK_SIZE = 16 * 1024
USB_TIMEOUT_MS = 60_000
# GS v 0 in Bändern senden: hält die Befehle klein, der Drucker druckt nahtlos weiter.
BAND_ROWS = 256

ENV_DEVICE = (os.environ.get("ESCPOS_DEVICE") or "").strip()
DEVICE = ENV_DEVICE or "usb"
# Sekunden, bis die Startauswahl den Vorschlag übernimmt (0 = ewig warten).
SELECT_TIMEOUT = int(os.environ.get("ESCPOS_SELECT_TIMEOUT") or 20)
WIDTH = int(os.environ.get("ESCPOS_WIDTH") or 512)
# "fs" = Floyd-Steinberg (Fotos), "threshold" = harte Schwelle
DITHER = (os.environ.get("ESCPOS_DITHER") or "fs").strip().casefold()
THRESHOLD = int(os.environ.get("ESCPOS_THRESHOLD") or 128)
# Thermo druckt Mitteltöne zu dunkel: Gamma > 1 hellt auf.
GAMMA = float(os.environ.get("ESCPOS_GAMMA") or 1.0)
# Endlosstreifen: standardmäßig kein Schnitt und kein Vorschub zwischen den Frames.
CUT = os.environ.get("ESCPOS_CUT", "0").strip().casefold() in ("1", "true", "yes", "on", "an")
FEED_LINES = int(os.environ.get("ESCPOS_FEED") or 0)

_SEND_LOCK = threading.Lock()


def set_device(spec):
    global DEVICE
    DEVICE = spec


def describe():
    return f"ESC/POS {DEVICE} ({WIDTH} Punkte)"


def options():
    return {
        "device": DEVICE,
        "width": WIDTH,
        "dither": DITHER,
        "threshold": THRESHOLD,
        "gamma": GAMMA,
        "cut": CUT,
        "feed": FEED_LINES,
    }


# --- Bild → Punkte ---------------------------------------------------------


def to_bitmap(image_path, width=None):
    """JPEG → 1-Bit-Bild in nativer Druckbreite (Pillow-Modus "1", 0 = schwarz)."""
    from PIL import Image

    width = width or WIDTH
    with Image.open(image_path) as src:
        img = src.convert("RGBA")
    # Transparenz auf Weiß, dann Graustufen (wie decode_gray in bitmap.rs).
    white = Image.new("RGBA", img.size, (255, 255, 255, 255))
    gray = Image.alpha_composite(white, img).convert("L")
    # Quadratische Punkte (180 × 180 dpi): Seitenverhältnis 1:1 übernehmen.
    height = max(1, round(gray.height * width / gray.width))
    gray = gray.resize((width, height), Image.LANCZOS)
    if GAMMA != 1.0:
        gray = gray.point(lambda v: round(255 * (v / 255) ** (1 / GAMMA)))
    if DITHER == "threshold":
        return gray.point(lambda v: 255 if v >= THRESHOLD else 0).convert("1", dither=Image.Dither.NONE)
    return gray.convert("1", dither=Image.Dither.FLOYDSTEINBERG)


def raster_bytes(bitmap):
    """GS v 0 (Rasterbild, Normaldichte) in Bändern; Bit 1 = schwarzer Punkt."""
    from PIL import Image, ImageOps

    width, height = bitmap.size
    width_bytes = (width + 7) // 8
    # Pillow "1": 1 = weiß. Invertieren, damit gesetzte Bits schwarz drucken.
    inverted = ImageOps.invert(bitmap.convert("L")).convert("1", dither=Image.Dither.NONE)
    data = inverted.tobytes()  # zeilenweise, MSB links, auf ganze Bytes aufgefüllt
    out = bytearray([ESC, 0x40])  # Initialisieren
    for top in range(0, height, BAND_ROWS):
        rows = min(BAND_ROWS, height - top)
        out += bytes([GS, 0x76, 0x30, 0, width_bytes & 0xFF, width_bytes >> 8, rows & 0xFF, rows >> 8])
        out += data[top * width_bytes : (top + rows) * width_bytes]
    if FEED_LINES:
        out += bytes([ESC, 0x64, min(FEED_LINES, 255)])
    if CUT:
        out += bytes([GS, 0x56, 0x41, 0])  # Vorschub bis Messer + Teilschnitt
    return bytes(out)


def image_job(image_path):
    bitmap = to_bitmap(image_path)
    return raster_bytes(bitmap), bitmap


# --- Transport ---------------------------------------------------------------


def _usb_backend():
    import usb.backend.libusb1

    # Homebrew-libusb liegt auf Apple Silicon nicht im Standard-Suchpfad.
    for candidate in (
        "/opt/homebrew/lib/libusb-1.0.dylib",
        "/usr/local/lib/libusb-1.0.dylib",
    ):
        if Path(candidate).is_file():
            backend = usb.backend.libusb1.get_backend(find_library=lambda _n, c=candidate: c)
            if backend:
                return backend
    return usb.backend.libusb1.get_backend()


def _is_printer(dev):
    try:
        cfg = dev.get_active_configuration()
    except Exception:
        try:
            cfg = dev[0]
        except Exception:
            return False
    return any(intf.bInterfaceClass == PRINTER_CLASS for intf in cfg)


def _usb_location(dev):
    ports = ".".join(str(p) for p in (dev.port_numbers or ()))
    return f"{dev.bus}-{ports}" if ports else str(dev.bus)


def _usb_printers():
    import usb.core

    backend = _usb_backend()
    if backend is None:
        raise RuntimeError("libusb nicht gefunden (brew install libusb)")
    printers = [d for d in usb.core.find(find_all=True, backend=backend) if _is_printer(d)]
    # Gleiche Reihenfolge bei jedem Scan, Epson zuerst.
    printers.sort(key=lambda d: (d.idVendor != EPSON_VENDOR, d.bus, tuple(d.port_numbers or ())))
    return printers


def _find_usb(spec):
    rest = spec[len("usb"):].lstrip(":")
    ids, _, location = rest.partition("@")
    printers = _usb_printers()
    if ids:
        vendor, product = (int(x, 16) for x in ids.split(":"))
        printers = [d for d in printers if d.idVendor == vendor and d.idProduct == product]
    if location:
        printers = [d for d in printers if _usb_location(d) == location]
    if not printers:
        raise RuntimeError(f"USB-Drucker {rest} nicht gefunden" if rest else "Kein USB-Drucker gefunden")
    return printers[0]


def _port_status(dev, interface):
    # GET_PORT_STATUS der Druckerklasse, wie port_status in devices.rs
    try:
        status = dev.ctrl_transfer(0xA1, 1, 0, interface << 8, 1, timeout=1000)[0]
    except Exception:
        return ""
    if status & 0x20:
        return " (kein Papier)"
    if not status & 0x10:
        return " (Drucker offline)"
    if not status & 0x08:
        return " (Druckerfehler: Papier/Deckel prüfen)"
    return ""


def _write_usb(spec, data):
    import usb.core
    import usb.util

    dev = _find_usb(spec)
    cfg = dev.get_active_configuration()
    intf = next(i for i in cfg if i.bInterfaceClass == PRINTER_CLASS)
    endpoint = usb.util.find_descriptor(
        intf,
        custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
        and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK,
    )
    if endpoint is None:
        raise RuntimeError("Drucker hat keinen USB-Out-Endpoint")
    number = intf.bInterfaceNumber
    try:
        if dev.is_kernel_driver_active(number):
            dev.detach_kernel_driver(number)
    except (NotImplementedError, usb.core.USBError):
        pass
    try:
        usb.util.claim_interface(dev, number)
    except usb.core.USBError as exc:
        raise RuntimeError(f"USB-Drucker belegt ({exc}) — läuft ein CUPS-Job?") from exc
    total = 0
    retried = False
    try:
        for start in range(0, len(data), CHUNK_SIZE):
            chunk = data[start : start + CHUNK_SIZE]
            sent = 0
            while sent < len(chunk):
                try:
                    n = endpoint.write(chunk[sent:], timeout=USB_TIMEOUT_MS)
                except usb.core.USBError as exc:
                    # Hängender Endpoint eines abgebrochenen Jobs: einmal zurücksetzen,
                    # aber nur bevor Daten durch sind (sonst doppelter Druck).
                    if total == 0 and not retried:
                        retried = True
                        try:
                            dev.clear_halt(endpoint)
                        except usb.core.USBError:
                            pass
                        continue
                    raise RuntimeError(f"USB-Schreibfehler: {exc}{_port_status(dev, number)}") from exc
                sent += n
                total += n
    finally:
        usb.util.release_interface(dev, number)
        usb.util.dispose_resources(dev)
    return f"USB {dev.idVendor:04x}:{dev.idProduct:04x}, {total} Bytes"


def _write_tcp(spec, data):
    hostport = spec[len("tcp://") :]
    host, _, port = hostport.partition(":")
    with socket.create_connection((host, int(port or 9100)), timeout=30) as sock:
        sock.sendall(data)
    return f"{hostport}, {len(data)} Bytes"


def _write_file(path, data):
    with open(path, "wb", buffering=0) as handle:
        for start in range(0, len(data), CHUNK_SIZE):
            handle.write(data[start : start + CHUNK_SIZE])
    return f"{path}, {len(data)} Bytes"


def _simulate(folder, data, bitmap, title):
    folder = Path(folder or "escpos_sim")
    if not folder.is_absolute():
        folder = Path(__file__).resolve().parent / folder
    folder.mkdir(parents=True, exist_ok=True)
    base = folder / f"{time.strftime('%Y-%m-%d_%H-%M-%S')}_{title}"
    base.with_suffix(".bin").write_bytes(data)
    bitmap.save(base.with_suffix(".png"))
    return f"sim {base.with_suffix('.png').name}, {len(data)} Bytes"


def send(data, bitmap=None, title="job"):
    """Schickt ESC/POS-Bytes an ESCPOS_DEVICE. Blockiert bis geschrieben."""
    with _SEND_LOCK:
        if DEVICE == "usb" or DEVICE.startswith("usb:"):
            return _write_usb(DEVICE, data)
        if DEVICE.startswith("tcp://"):
            return _write_tcp(DEVICE, data)
        if DEVICE.startswith("sim"):
            return _simulate(DEVICE.partition(":")[2], data, bitmap, title)
        return _write_file(DEVICE, data)


# --- Suche & Startauswahl -------------------------------------------------------


def _usb_name(dev):
    try:
        name = f"{dev.manufacturer or ''} {dev.product or ''}".strip()
    except Exception:
        name = ""
    return name or f"USB-Drucker {dev.idVendor:04x}:{dev.idProduct:04x}"


def is_epson_tm(entry):
    # Wie default_mode in devices.rs: Epson-TM hat USB-IDs 04b8:0e.. bzw. "TM-" im Namen.
    vendor, product = entry.get("vendor"), entry.get("product")
    if vendor == EPSON_VENDOR and product is not None and product >> 8 == 0x0E:
        return True
    return "TM-" in entry["name"]


def discover():
    """Findet direkt ansprechbare Drucker: USB (Klasse 7) und Gerätedateien."""
    found = []
    try:
        for dev in _usb_printers():
            location = _usb_location(dev)
            found.append(
                {
                    "spec": f"usb:{dev.idVendor:04x}:{dev.idProduct:04x}@{location}",
                    "name": _usb_name(dev),
                    "detail": f"USB {dev.idVendor:04x}:{dev.idProduct:04x}, Bus/Port {location}",
                    "vendor": dev.idVendor,
                    "product": dev.idProduct,
                }
            )
    except Exception as exc:
        print(f"USB-Suche fehlgeschlagen: {exc}", flush=True)
    for folder in ("/dev/usb", "/dev"):
        try:
            names = sorted(n for n in os.listdir(folder) if n.startswith("lp") and n[2:].isdigit())
        except OSError:
            continue
        for name in names:
            path = f"{folder}/{name}"
            found.append({"spec": path, "name": f"Gerätedatei {path}", "detail": "Linux-Druckertreiber"})
    found.sort(key=lambda e: not is_epson_tm(e))
    return found


def _terminal():
    """Ausgabe fürs Menü: stdout, oder direkt das Terminal, wenn stdout ins Log geht
    (run-server.sh leitet stdout/stderr nach /tmp/twitch-null-server.log um)."""
    import sys

    if sys.stdout.isatty():
        return sys.stdout
    try:
        return open("/dev/tty", "w")
    except OSError:
        return sys.stdout


def _ask(prompt, timeout, out):
    """input() mit Zeitlimit; None bei Zeitablauf oder EOF."""
    import select
    import sys

    print(prompt, end="", file=out, flush=True)
    if timeout:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        if not ready:
            print("", file=out, flush=True)
            return None
    line = sys.stdin.readline()
    return line.strip() if line else None


def choose_device(interactive=None):
    """Wählt beim Serverstart das ESC/POS-Gerät.

    ESCPOS_DEVICE gesetzt → wird übernommen. Sonst wird gesucht: ein Epson-TM wird
    automatisch genommen; nur wenn keiner da ist, kommt im Terminal ein Auswahlmenü
    (Enter bzw. Zeitablauf = kein Live-Druck), ohne Terminal ist Live-Druck dann aus.
    Rückgabe: Gerätespez. oder None (= Live-Druck aus).
    """
    import sys

    if ENV_DEVICE:
        return ENV_DEVICE
    if interactive is None:
        interactive = sys.stdin.isatty()
    while True:
        found = discover()
        if found and is_epson_tm(found[0]):
            spec = found[0]["spec"]
            print(f"ESC/POS: automatisch {found[0]['name']} ({spec})", flush=True)
            set_device(spec)
            return spec
        if not interactive:
            print("ESC/POS: kein Epson-TM gefunden — Live-Druck aus", flush=True)
            return None

        out = _terminal()
        print("\nKein Epson-TM gefunden. ESC/POS-Drucker wählen:", file=out, flush=True)
        for i, entry in enumerate(found, 1):
            print(f"  {i}) {entry['name']}  [{entry['detail']}]", file=out, flush=True)
        if not found:
            print("  (kein USB-Drucker / keine Gerätedatei gefunden)", file=out, flush=True)
        print("  s) Simulation (escpos_sim/)", file=out, flush=True)
        print("  r) neu suchen (Drucker einstecken, dann r)", file=out, flush=True)
        print("  n) kein Live-Druck", file=out, flush=True)
        print("  oder direkt eingeben: tcp://host:9100, /dev/usb/lp0, usb:04b8:0202", file=out, flush=True)
        default = "n"
        wait = f", {SELECT_TIMEOUT} s" if SELECT_TIMEOUT else ""
        answer = _ask(f"Auswahl [{default}{wait}]: ", SELECT_TIMEOUT, out)
        answer = (answer or default).strip()
        key = answer.casefold()
        if key == "r":
            continue
        if key == "n":
            print("ESC/POS: Live-Druck aus", flush=True)
            return None
        if key == "s":
            spec = "sim:escpos_sim"
        elif answer.isdigit() and 1 <= int(answer) <= len(found):
            spec = found[int(answer) - 1]["spec"]
        elif answer.startswith(("usb", "tcp://", "sim", "/")):
            spec = answer
        else:
            print(f"Unbekannte Auswahl: {answer}", file=out, flush=True)
            continue
        set_device(spec)
        print(f"ESC/POS: {spec}", flush=True)
        return spec


def print_image(image_path, title="job"):
    data, bitmap = image_job(image_path)
    return send(data, bitmap, title)


if __name__ == "__main__":
    # Test: python escpos.py bild.jpg  (mit ESCPOS_DEVICE=sim:escpos_sim zum Trockenlauf)
    import sys

    if len(sys.argv) < 2:
        raise SystemExit("Aufruf: python escpos.py bild.jpg [...]")
    for arg in sys.argv[1:]:
        print(print_image(arg, title=Path(arg).stem))
