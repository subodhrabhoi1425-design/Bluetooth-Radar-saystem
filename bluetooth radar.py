"""
BLUETOOTH RADAR v3  -  smooth 60 FPS pygame radar for nearby BLE devices.

Install:  pip install bleak pygame
Run:      python bluetooth_radar_v3.py        (press H inside the app for all controls)

IMPORTANT HONESTY NOTE
  * Distance is ESTIMATED from signal strength (RSSI): approximate, +/- a few metres.
  * A normal Bluetooth adapter has ONE antenna, so it CANNOT measure direction.
    Compass bearings (marked with *) are fixed PLACEHOLDERS that just spread
    devices around the screen. Real direction needs Bluetooth 5.1 direction
    finding hardware (antenna array) or several receivers.
  * No Bluetooth / no bleak?  Press D for DEMO mode with simulated devices.
"""

import asyncio
import csv
import hashlib
import math
import random
import struct
import threading
import time
from datetime import datetime

import pygame

try:
    from bleak import BleakScanner
    BLEAK_OK = True
except Exception:  # bleak not installed
    BLEAK_OK = False

# ============================== CONSTANTS ==============================
TX_POWER = -59          # expected RSSI at 1 m
PATH_LOSS_N = 2.5       # 2 = open space, 3-4 = indoors
STALE_AFTER = 10.0      # seconds before a real device disappears
SIDEBAR_W = 370
RANGES = [5, 10, 20, 30, 50]                    # metres
SWEEP_PERIODS = [8.0, 6.0, 4.0, 3.0, 2.0, 1.4]  # seconds per rotation
SORT_NAMES = ["Distance", "Signal", "Name"]
RED = (255, 70, 70)

# name, main colour, background colour
THEMES = [
    ("Matrix", (57, 255, 20), (0, 20, 10)),
    ("Cyan", (0, 229, 255), (0, 14, 24)),
    ("Amber", (255, 176, 0), (24, 14, 0)),
    ("Crimson", (255, 60, 90), (24, 0, 6)),
    ("Ice", (190, 225, 255), (6, 10, 22)),
]

COMPASS8 = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


class Cfg:
    theme = 0
    range_idx = 1
    sweep_idx = 2
    labels = True
    trail = True
    demo = False
    sound = True
    metric = True
    min_rssi = -100
    hide_unnamed = False
    alert = 2.0
    sort = 0
    paused = False
    recording = False
    show_help = False


# ============================== SHARED STATE ==============================
devices = {}
lock = threading.Lock()
scan_status = "Starting..."
stop_event = threading.Event()


# ============================== HELPERS ==============================
def rssi_to_distance(rssi):
    return 10 ** ((TX_POWER - rssi) / (10 * PATH_LOSS_N))


def address_to_angle(addr):
    h = int(hashlib.md5(addr.encode()).hexdigest()[:8], 16)
    return (h % 3600) / 3600 * 2 * math.pi


def compass_name(angle):
    deg = math.degrees(angle) % 360
    return COMPASS8[int((deg + 22.5) // 45) % 8]


def mix(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def fmt_dist(d, metric):
    return f"{d:.1f} m" if metric else f"{d * 3.281:.0f} ft"


def signal_bars(rssi):
    for n, limit in ((5, -50), (4, -60), (3, -70), (2, -80)):
        if rssi > limit:
            return n
    return 1


def make_beep(freq=880, ms=140, vol=0.35):
    rate = 22050
    n = int(rate * ms / 1000)
    buf = bytearray()
    for i in range(n):
        env = 1 - i / n
        buf += struct.pack("<h", int(32767 * vol * env * math.sin(2 * math.pi * freq * i / rate)))
    return pygame.mixer.Sound(buffer=bytes(buf))


# ============================== BLUETOOTH ==============================
def on_detect(device, adv):
    now = time.time()
    name = device.name or adv.local_name or "Unknown"
    with lock:
        d = devices.get(device.address)
        if d is None:
            devices[device.address] = dict(
                addr=device.address, name=name, rssi=adv.rssi, last_seen=now,
                first_seen=now, angle=address_to_angle(device.address),
                dist=rssi_to_distance(adv.rssi), demo=False)
        else:
            d["rssi"] += 0.3 * (adv.rssi - d["rssi"])
            d["last_seen"] = now
            if name != "Unknown":
                d["name"] = name


async def scan_forever():
    global scan_status
    try:
        async with BleakScanner(detection_callback=on_detect):
            scan_status = "Scanning Bluetooth..."
            while not stop_event.is_set():
                await asyncio.sleep(0.4)
    except Exception as e:  # no adapter, permissions, etc.
        scan_status = f"Bluetooth error: {str(e)[:60]}"


def start_scanner():
    asyncio.run(scan_forever())


# ============================== DEMO MODE ==============================
DEMO_NAMES = ["Demo Phone", "Demo Earbuds", "Demo Watch", "Demo Speaker",
              "Demo Tag", "Demo Laptop", "Demo Car Key"]


def add_demo_devices():
    now = time.time()
    with lock:
        for i, name in enumerate(DEMO_NAMES):
            addr = f"DEMO:{i}"
            devices[addr] = dict(
                addr=addr, name=name, rssi=-60, last_seen=now, first_seen=now,
                angle=random.uniform(0, 2 * math.pi), dist=random.uniform(1, 9),
                demo=True, tdist=random.uniform(1, 9),
                vd=random.uniform(-0.7, 0.7), va=random.uniform(-0.35, 0.35))


def remove_demo_devices():
    with lock:
        for k in [k for k, d in devices.items() if d["demo"]]:
            del devices[k]


def update_devices(dt, now):
    k = 1 - math.exp(-dt * 5)  # frame-rate independent easing
    with lock:
        for addr in list(devices):
            d = devices[addr]
            if d["demo"]:
                d["tdist"] += d["vd"] * dt
                if d["tdist"] < 0.7 or d["tdist"] > 12:
                    d["vd"] = -d["vd"]
                d["vd"] += random.uniform(-0.3, 0.3) * dt
                d["vd"] = max(-1.2, min(1.2, d["vd"]))
                d["angle"] = (d["angle"] + d["va"] * dt) % (2 * math.pi)
                target = TX_POWER - 10 * PATH_LOSS_N * math.log10(max(d["tdist"], 0.1))
                d["rssi"] += 0.2 * (target + random.uniform(-2, 2) - d["rssi"])
                d["last_seen"] = now
            elif now - d["last_seen"] > STALE_AFTER:
                del devices[addr]
                continue
            d["dist"] += (rssi_to_distance(d["rssi"]) - d["dist"]) * k


def snapshot():
    with lock:
        return [dict(d) for d in devices.values()]


# ============================== APP ==============================
class App:
    def __init__(self):
        pygame.mixer.pre_init(22050, -16, 1, 512)
        pygame.init()
        pygame.display.set_caption("Bluetooth Radar v3")
        self.windowed_size = (1200, 760)
        self.fullscreen = False
        self.screen = pygame.display.set_mode(self.windowed_size, pygame.RESIZABLE)
        self.clock = pygame.time.Clock()

        def font(size, bold=False):
            return pygame.font.SysFont("consolas,dejavusansmono,menlo,couriernew", size, bold=bold)

        self.f_s, self.f_m = font(13), font(15)
        self.f_b, self.f_xl = font(15, True), font(24, True)

        try:
            self.beep = make_beep()
        except Exception:
            self.beep = None

        self.cfg = Cfg()
        self.sweep = 0.0
        self.prev_sweep = 0.0
        self.ui = {}                 # addr -> {"ping": time, "alerted": bool}
        self.selected = None
        self.scroll = 0
        self.rows = []               # (rect, addr) for click detection
        self.blip_pos = {}           # addr -> (x, y)
        self.toast_text, self.toast_until = "", 0.0
        self.static_key, self.static = None, None
        self.trail_key, self.trail_surf = None, None
        self.overlay, self.overlay_size = None, None
        self.log_file, self.log_writer, self.last_log = None, None, 0.0

    # ---------- small utilities ----------
    def theme(self):
        name, main, bg = THEMES[self.cfg.theme]
        return name, main, bg

    def toast(self, msg):
        self.toast_text, self.toast_until = msg, time.time() + 1.8

    def text(self, surf, font, txt, pos, color, alpha=255, anchor="topleft"):
        img = font.render(txt, True, color)
        if alpha < 255:
            img.set_alpha(alpha)
        rect = img.get_rect(**{anchor: pos})
        surf.blit(img, rect)
        return rect

    # ---------- options ----------
    def toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        if self.fullscreen:
            self.windowed_size = self.screen.get_size()
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            self.screen = pygame.display.set_mode(self.windowed_size, pygame.RESIZABLE)

    def toggle_recording(self):
        c = self.cfg
        if c.recording:
            self.log_file.close()
            c.recording = False
            self.toast("Recording stopped")
        else:
            fname = datetime.now().strftime("radar_log_%Y%m%d_%H%M%S.csv")
            self.log_file = open(fname, "w", newline="", encoding="utf-8")
            self.log_writer = csv.writer(self.log_file)
            self.log_writer.writerow(["time", "address", "name", "rssi_dbm", "distance_m"])
            c.recording = True
            self.toast(f"Recording to {fname}")

    def on_key(self, k):
        c = self.cfg
        if k == pygame.K_SPACE:
            c.paused = not c.paused
            self.toast("Paused" if c.paused else "Resumed")
        elif k == pygame.K_t:
            c.theme = (c.theme + 1) % len(THEMES)
            self.toast(f"Theme: {THEMES[c.theme][0]}")
        elif k in (pygame.K_EQUALS, pygame.K_PLUS, pygame.K_KP_PLUS):
            c.range_idx = min(c.range_idx + 1, len(RANGES) - 1)
            self.toast(f"Range: {RANGES[c.range_idx]} m")
        elif k in (pygame.K_MINUS, pygame.K_KP_MINUS):
            c.range_idx = max(c.range_idx - 1, 0)
            self.toast(f"Range: {RANGES[c.range_idx]} m")
        elif k == pygame.K_RIGHTBRACKET:
            c.sweep_idx = min(c.sweep_idx + 1, len(SWEEP_PERIODS) - 1)
            self.toast(f"Sweep: {SWEEP_PERIODS[c.sweep_idx]}s per turn")
        elif k == pygame.K_LEFTBRACKET:
            c.sweep_idx = max(c.sweep_idx - 1, 0)
            self.toast(f"Sweep: {SWEEP_PERIODS[c.sweep_idx]}s per turn")
        elif k == pygame.K_l:
            c.labels = not c.labels
            self.toast(f"Labels {'ON' if c.labels else 'OFF'}")
        elif k == pygame.K_g:
            c.trail = not c.trail
            self.toast(f"Sweep trail {'ON' if c.trail else 'OFF'}")
        elif k == pygame.K_d:
            c.demo = not c.demo
            add_demo_devices() if c.demo else remove_demo_devices()
            self.toast(f"Demo mode {'ON' if c.demo else 'OFF'}")
        elif k == pygame.K_m:
            c.sound = not c.sound
            self.toast(f"Alert sound {'ON' if c.sound else 'OFF'}")
        elif k == pygame.K_u:
            c.metric = not c.metric
            self.toast("Units: " + ("metres" if c.metric else "feet"))
        elif k in (pygame.K_f, pygame.K_F11):
            self.toggle_fullscreen()
        elif k == pygame.K_s:
            fname = datetime.now().strftime("radar_%Y%m%d_%H%M%S.png")
            pygame.image.save(self.screen, fname)
            self.toast(f"Saved {fname}")
        elif k == pygame.K_r:
            self.toggle_recording()
        elif k == pygame.K_UP:
            c.min_rssi = min(c.min_rssi + 5, -40)
            self.toast(f"Min signal: {c.min_rssi} dBm")
        elif k == pygame.K_DOWN:
            c.min_rssi = max(c.min_rssi - 5, -100)
            self.toast(f"Min signal: {c.min_rssi} dBm")
        elif k == pygame.K_n:
            c.hide_unnamed = not c.hide_unnamed
            self.toast("Hiding unnamed devices" if c.hide_unnamed else "Showing all devices")
        elif k == pygame.K_PERIOD:
            c.alert = min(c.alert + 0.5, 30)
            self.toast(f"Alert distance: {c.alert:.1f} m")
        elif k == pygame.K_COMMA:
            c.alert = max(c.alert - 0.5, 0.5)
            self.toast(f"Alert distance: {c.alert:.1f} m")
        elif k == pygame.K_c:
            with lock:
                for key in [k2 for k2, d in devices.items() if not d["demo"]]:
                    del devices[key]
            self.toast("Cleared real devices")
        elif k == pygame.K_TAB:
            c.sort = (c.sort + 1) % len(SORT_NAMES)
            self.toast(f"Sort by: {SORT_NAMES[c.sort]}")
        elif k == pygame.K_h:
            c.show_help = not c.show_help
        elif k == pygame.K_ESCAPE:
            if c.show_help:
                c.show_help = False
            elif self.selected:
                self.selected = None
            else:
                return False
        elif k == pygame.K_q:
            return False
        return True

    # ---------- cached layers ----------
    def layout(self):
        w, h = self.screen.get_size()
        area_w = max(300, w - SIDEBAR_W)
        R = max(80, min(area_w, h) // 2 - 62)
        return w, h, area_w, R, (area_w // 2, h // 2)

    def build_static(self, area_w, h, R, cx, cy):
        _, main, bg = self.theme()
        range_m = RANGES[self.cfg.range_idx]
        surf = pygame.Surface((area_w, h))
        surf.fill(bg)
        # soft radial glow
        for r in range(R, 0, -6):
            pygame.draw.circle(surf, mix(bg, main, 0.10 * (1 - r / R) ** 1.5), (cx, cy), r)
        dim = mix(bg, main, 0.30)
        faint = mix(bg, main, 0.14)
        for deg in range(0, 180, 45):  # spokes
            a = math.radians(deg)
            dx, dy = R * math.sin(a), -R * math.cos(a)
            pygame.draw.line(surf, faint, (cx - dx, cy - dy), (cx + dx, cy + dy), 1)
        for i in range(1, 5):          # range rings
            r = R * i // 4
            pygame.draw.circle(surf, dim, (cx, cy), r, 1)
            val = range_m * i / 4
            lbl = f"{val:g} m" if self.cfg.metric else f"{val * 3.281:.0f} ft"
            self.text(surf, self.f_s, lbl, (cx + 5, cy - r - 15), mix(bg, main, 0.65))
        pygame.draw.circle(surf, main, (cx, cy), R, 2)
        for deg in range(0, 360, 5):   # tick marks
            ln = 14 if deg % 45 == 0 else 9 if deg % 15 == 0 else 5
            a = math.radians(deg)
            p1 = (cx + R * math.sin(a), cy - R * math.cos(a))
            p2 = (cx + (R + ln) * math.sin(a), cy - (R + ln) * math.cos(a))
            pygame.draw.line(surf, main if deg % 45 == 0 else dim, p1, p2, 1)
        for i, name in enumerate(COMPASS8):  # compass letters
            a = math.radians(i * 45)
            pos = (cx + (R + 34) * math.sin(a), cy - (R + 34) * math.cos(a))
            font = self.f_xl if i % 2 == 0 else self.f_b
            col = RED if name == "N" else main
            self.text(surf, font, name, pos, col, anchor="center")
        for deg in range(30, 360, 30):       # degree numbers
            if deg % 45 == 0:
                continue
            a = math.radians(deg)
            pos = (cx + (R + 30) * math.sin(a), cy - (R + 30) * math.cos(a))
            self.text(surf, self.f_s, str(deg), pos, dim, anchor="center")
        pygame.draw.circle(surf, main, (cx, cy), 4)
        return surf

    def build_trail(self, R, color, arc=math.radians(80), steps=50):
        surf = pygame.Surface((2 * R, 2 * R), pygame.SRCALPHA)
        for i in range(steps):
            t = (i + 1) / steps
            a0 = -arc * (1 - i / steps)
            a1 = -arc * (1 - (i + 1) / steps) + arc / steps * 0.6  # overlap = no seams
            pts = [(R, R)]
            for j in range(5):
                a = a0 + (a1 - a0) * j / 4
                pts.append((R + R * math.sin(a), R - R * math.cos(a)))
            pygame.draw.polygon(surf, (*color, int(115 * t ** 2.2)), pts)
        return surf

    # ---------- visible device list ----------
    def visible(self):
        c = self.cfg
        out = [d for d in snapshot()
               if d["rssi"] >= c.min_rssi and not (c.hide_unnamed and d["name"] == "Unknown")]
        keys = [lambda d: d["dist"], lambda d: -d["rssi"], lambda d: d["name"].lower()]
        out.sort(key=keys[c.sort])
        return out

    # ---------- radar drawing ----------
    def draw_radar(self, devs, now, w, h, area_w, R, cx, cy):
        c = self.cfg
        _, main, bg = self.theme()
        range_m = RANGES[c.range_idx]

        key = (area_w, h, R, c.theme, c.range_idx, c.metric)
        if key != self.static_key:
            self.static, self.static_key = self.build_static(area_w, h, R, cx, cy), key
        self.screen.blit(self.static, (0, 0))

        tkey = (R, c.theme)
        if tkey != self.trail_key:
            self.trail_surf, self.trail_key = self.build_trail(R, main), tkey
        if self.overlay_size != (area_w, h):
            self.overlay, self.overlay_size = pygame.Surface((area_w, h), pygame.SRCALPHA), (area_w, h)
        ov = self.overlay
        ov.fill((0, 0, 0, 0))

        # sweep trail (pre-rendered wedge, rotated = very cheap and smooth)
        if c.trail:
            rot = pygame.transform.rotate(self.trail_surf, -math.degrees(self.sweep))
            self.screen.blit(rot, rot.get_rect(center=(cx, cy)))

        # alert zone
        if c.alert < range_m:
            pulse = 70 + int(50 * math.sin(now * 3))
            pygame.draw.circle(ov, (*RED, pulse), (cx, cy), int(R * c.alert / range_m), 1)

        # sweep line with glow
        ex, ey = cx + R * math.sin(self.sweep), cy - R * math.cos(self.sweep)
        pygame.draw.line(ov, (*main, 35), (cx, cy), (ex, ey), 9)
        pygame.draw.line(ov, (*main, 90), (cx, cy), (ex, ey), 5)
        pygame.draw.line(ov, (*main, 255), (cx, cy), (ex, ey), 2)

        # blips
        step = (self.sweep - self.prev_sweep) % (2 * math.pi)
        self.blip_pos = {}
        labels = []
        for d in devs:
            addr = d["addr"]
            st = self.ui.setdefault(addr, {"ping": -10.0, "alerted": False})
            ang, dist = d["angle"], d["dist"]
            out_of_range = dist > range_m
            r_px = min(dist / range_m, 1.0) * R * 0.985
            x, y = cx + r_px * math.sin(ang), cy - r_px * math.cos(ang)
            self.blip_pos[addr] = (x, y)

            # swept this frame? -> ping + brighten
            if not c.paused and step < math.pi and ((ang - self.prev_sweep) % (2 * math.pi)) <= step:
                st["ping"] = now
            since = (self.sweep - ang) % (2 * math.pi) / (2 * math.pi)
            bright = 0.30 + 0.70 * (1 - since) ** 2
            age = now - d["last_seen"]
            fade = 1.0 if (d["demo"] or age < STALE_AFTER * 0.5) else max(0.2, 1 - (age - STALE_AFTER * 0.5) / (STALE_AFTER * 0.5))
            a = bright * fade

            # alert logic
            near = dist <= c.alert
            if near and not st["alerted"]:
                st["alerted"] = True
                if c.sound and self.beep:
                    self.beep.play()
            elif not near and dist > c.alert * 1.25:
                st["alerted"] = False
            col = RED if near else main

            pygame.draw.circle(ov, (*col, int(55 * a)), (x, y), 14)
            if out_of_range:
                pygame.draw.circle(ov, (*col, int(255 * a)), (x, y), 5, 1)
            else:
                pygame.draw.circle(ov, (*col, int(255 * a)), (x, y), 5)
            p = (now - st["ping"]) / 0.9
            if 0 <= p < 1:  # expanding ping ring
                pygame.draw.circle(ov, (*col, int(210 * (1 - p))), (x, y), int(6 + 28 * p), 2)

            if d["addr"] == self.selected:
                pygame.draw.line(ov, (*col, 70), (cx, cy), (x, y), 1)
                pygame.draw.circle(ov, (*col, 255), (x, y), 15, 1)
                for k in range(4):
                    t = now * 1.5 + k * math.pi / 2
                    pygame.draw.circle(ov, (*col, 255), (x + 19 * math.cos(t), y + 19 * math.sin(t)), 2)
            if c.labels or d["addr"] == self.selected:
                star = "" if d["demo"] else "*"
                labels.append((d, x, y, a, col, f"{compass_name(ang)}{star} {fmt_dist(dist, c.metric)}"))

        self.screen.blit(ov, (0, 0))
        for d, x, y, a, col, line2 in labels:
            r1 = self.text(self.screen, self.f_s, d["name"][:18], (x + 11, y - 17), (255, 255, 255), int(70 + 185 * a))
            self.text(self.screen, self.f_s, line2, (x + 11, r1.bottom), col, int(70 + 185 * a))

        # status chips
        chips = [f"THEME {THEMES[c.theme][0].upper()}", f"RANGE {range_m} m",
                 f"SWEEP {SWEEP_PERIODS[c.sweep_idx]}s", f"MIN {c.min_rssi} dBm",
                 f"ALERT {c.alert:.1f} m"]
        if c.paused: chips.append("PAUSED")
        if c.demo: chips.append("DEMO")
        if c.recording: chips.append("REC")
        x0, y0 = 14, 12
        for chip in chips:
            img = self.f_s.render(chip, True, RED if chip in ("REC", "PAUSED") else main)
            rect = pygame.Rect(x0, y0, img.get_width() + 14, img.get_height() + 8)
            if rect.right > area_w - 10:
                x0, y0 = 14, y0 + 28
                rect.topleft = (x0, y0)
            pygame.draw.rect(self.screen, mix(bg, main, 0.12), rect, border_radius=8)
            pygame.draw.rect(self.screen, mix(bg, main, 0.5), rect, 1, border_radius=8)
            self.screen.blit(img, (rect.x + 7, rect.y + 4))
            x0 = rect.right + 8

        # toast
        if now < self.toast_until:
            t_a = int(255 * min(1, (self.toast_until - now) / 0.4))
            self.text(self.screen, self.f_b, self.toast_text, (area_w // 2, h - 24), (255, 255, 255), t_a, "center")

        self.prev_sweep = self.sweep

    # ---------- sidebar ----------
    def draw_sidebar(self, devs, total, now, w, h, area_w):
        c = self.cfg
        _, main, bg = self.theme()
        panel = pygame.Rect(area_w, 0, w - area_w, h)
        pygame.draw.rect(self.screen, mix(bg, (255, 255, 255), 0.05), panel)
        pygame.draw.line(self.screen, mix(bg, main, 0.5), (area_w, 0), (area_w, h), 1)
        x, pw = area_w + 14, w - area_w - 28

        self.text(self.screen, self.f_xl, "BT RADAR", (x, 12), main)
        self.text(self.screen, self.f_s, scan_status[:46], (x, 46), mix(bg, main, 0.8))
        self.text(self.screen, self.f_s,
                  f"{len(devs)} shown / {total} total   sort: {SORT_NAMES[c.sort]}",
                  (x, 64), (200, 200, 200))

        # bottom: details + hint
        det_h = 118
        list_top, list_bottom = 92, h - det_h - 34
        clip = pygame.Rect(area_w + 1, list_top, w - area_w - 1, list_bottom - list_top)
        self.screen.set_clip(clip)
        self.rows = []
        row_h = 46
        max_scroll = max(0, len(devs) * row_h - clip.height)
        self.scroll = max(0, min(self.scroll, max_scroll))
        mouse = pygame.mouse.get_pos()
        for i, d in enumerate(devs):
            ry = list_top + i * row_h - self.scroll
            if ry + row_h < list_top or ry > list_bottom:
                continue
            rect = pygame.Rect(x - 6, ry, pw + 12, row_h - 4)
            self.rows.append((rect, d["addr"]))
            near = d["dist"] <= c.alert
            col = RED if near else main
            if d["addr"] == self.selected:
                pygame.draw.rect(self.screen, mix(bg, col, 0.22), rect, border_radius=8)
                pygame.draw.rect(self.screen, col, rect, 1, border_radius=8)
            elif rect.collidepoint(mouse):
                pygame.draw.rect(self.screen, mix(bg, col, 0.12), rect, border_radius=8)
            pygame.draw.circle(self.screen, col, (rect.x + 14, rect.centery), 5)
            self.text(self.screen, self.f_m, d["name"][:20], (rect.x + 28, rect.y + 4), (255, 255, 255))
            star = "" if d["demo"] else "*"
            self.text(self.screen, self.f_s,
                      f"{compass_name(d['angle'])}{star}  {fmt_dist(d['dist'], c.metric)}  {d['rssi']:.0f} dBm",
                      (rect.x + 28, rect.y + 24), (170, 170, 170))
            bars = signal_bars(d["rssi"])
            for b in range(5):
                bh = 5 + b * 4
                bx = rect.right - 38 + b * 7
                pygame.draw.rect(self.screen, col if b < bars else mix(bg, main, 0.2),
                                 (bx, rect.bottom - 8 - bh, 5, bh))
        self.screen.set_clip(None)
        if not devs:
            self.text(self.screen, self.f_s, "No devices yet - press D for demo mode",
                      (x, list_top + 8), (150, 150, 150))

        # details panel
        dy = h - det_h - 28
        pygame.draw.line(self.screen, mix(bg, main, 0.4), (area_w + 10, dy), (w - 10, dy), 1)
        sel = next((d for d in devs if d["addr"] == self.selected), None)
        if sel:
            lines = [
                (sel["name"][:26], self.f_b, main),
                (f"ID: {sel['addr'][-17:]}", self.f_s, (170, 170, 170)),
                (f"Distance: {fmt_dist(sel['dist'], c.metric)}   Signal: {sel['rssi']:.0f} dBm", self.f_s, (230, 230, 230)),
                (f"Bearing: {compass_name(sel['angle'])}"
                 + ("" if sel["demo"] else "* (placeholder)"), self.f_s, (230, 230, 230)),
                (f"Seen for {now - sel['first_seen']:.0f}s | last ping {now - sel['last_seen']:.1f}s ago",
                 self.f_s, (170, 170, 170)),
            ]
            for i, (t, f, col) in enumerate(lines):
                self.text(self.screen, f, t, (x, dy + 8 + i * 20), col)
        else:
            self.text(self.screen, self.f_s, "Click a device to track it", (x, dy + 10), (150, 150, 150))
        self.text(self.screen, self.f_s, "H = help    * bearing is a placeholder",
                  (x, h - 22), (140, 140, 140))

    def draw_help(self, w, h):
        _, main, bg = self.theme()
        keys = [
            ("SPACE", "pause / resume"), ("T", "change theme"), ("+ / -", "radar range"),
            ("[ / ]", "sweep speed"), ("L", "device labels"), ("G", "sweep trail"),
            ("D", "demo mode (fake devices)"), ("M", "alert sound"), ("U", "metres / feet"),
            ("UP / DOWN", "min signal filter"), ("N", "hide unnamed devices"),
            (", / .", "alert distance"), ("TAB", "sort list"), ("C", "clear devices"),
            ("R", "record to CSV"), ("S", "screenshot"), ("F / F11", "fullscreen"),
            ("Click", "select / track device"), ("Wheel", "scroll list"),
            ("H", "close help"), ("Q / ESC", "quit"),
        ]
        bw, bh = 520, 60 + len(keys) * 22
        rect = pygame.Rect((w - bw) // 2, (h - bh) // 2, bw, bh)
        shade = pygame.Surface((w, h), pygame.SRCALPHA)
        shade.fill((0, 0, 0, 170))
        self.screen.blit(shade, (0, 0))
        pygame.draw.rect(self.screen, mix(bg, (255, 255, 255), 0.06), rect, border_radius=14)
        pygame.draw.rect(self.screen, main, rect, 2, border_radius=14)
        self.text(self.screen, self.f_xl, "CONTROLS", (rect.centerx, rect.y + 28), main, anchor="center")
        for i, (k, desc) in enumerate(keys):
            yy = rect.y + 56 + i * 22
            self.text(self.screen, self.f_b, k, (rect.x + 30, yy), main)
            self.text(self.screen, self.f_m, desc, (rect.x + 190, yy), (225, 225, 225))

    # ---------- mouse ----------
    def on_click(self, pos):
        for rect, addr in self.rows:
            if rect.collidepoint(pos):
                self.selected = None if self.selected == addr else addr
                return
        best, best_d = None, 18
        for addr, (x, y) in self.blip_pos.items():
            dd = math.hypot(pos[0] - x, pos[1] - y)
            if dd < best_d:
                best, best_d = addr, dd
        self.selected = best if best != self.selected else None

    # ---------- main loop ----------
    def run(self):
        running = True
        last = time.time()
        while running:
            now = time.time()
            dt = min(now - last, 0.1)
            last = now

            for e in pygame.event.get():
                if e.type == pygame.QUIT:
                    running = False
                elif e.type == pygame.KEYDOWN:
                    if self.on_key(e.key) is False:
                        running = False
                elif e.type == pygame.MOUSEBUTTONDOWN and e.button == 1:
                    self.on_click(e.pos)
                elif e.type == pygame.MOUSEWHEEL:
                    self.scroll -= e.y * 40

            if not self.cfg.paused:
                self.sweep = (self.sweep + dt * 2 * math.pi / SWEEP_PERIODS[self.cfg.sweep_idx]) % (2 * math.pi)
                update_devices(dt, now)

            w, h, area_w, R, (cx, cy) = self.layout()
            devs = self.visible()
            total = len(devs) if len(devs) else 0
            with lock:
                total = len(devices)

            self.draw_radar(devs, now, w, h, area_w, R, cx, cy)
            self.draw_sidebar(devs, total, now, w, h, area_w)
            if self.cfg.show_help:
                self.draw_help(w, h)

            if self.cfg.recording and now - self.last_log >= 1.0:
                self.last_log = now
                stamp = datetime.now().isoformat(timespec="seconds")
                for d in devs:
                    self.log_writer.writerow([stamp, d["addr"], d["name"],
                                              f"{d['rssi']:.0f}", f"{d['dist']:.2f}"])
                self.log_file.flush()

            pygame.display.flip()
            self.clock.tick(60)

        stop_event.set()
        if self.cfg.recording:
            self.log_file.close()
        pygame.quit()


def main():
    global scan_status
    app = App()
    if BLEAK_OK:
        threading.Thread(target=start_scanner, daemon=True).start()
    else:
        scan_status = "bleak not installed - demo mode"
        app.cfg.demo = True
        add_demo_devices()
    app.run()


if __name__ == "__main__":
    main()
