#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gtop - красивый терминальный монитор системы (аналог btop / neohtop).

Только стандартная библиотека Python. Данные берутся напрямую из /proc и /sys.

Запуск:
    python3 gtop.py              обычный интерактивный режим
    python3 gtop.py --gray       серая (монохромная) тема; клавиша g переключает на лету
    python3 gtop.py --snapshot   один кадр в stdout (для отладки), можно WxH: --snapshot 120x40

Клавиши: q выход | ↑↓ PgUp PgDn Home End выбор | k kill | K SIGKILL | / фильтр
         s смена сортировки | c m p n сортировать по CPU/MEM/PID/имени | r реверс
         t дерево процессов | пробел пауза | +/- скорость обновления | ? помощь
Мышь: клик по строке, клик по заголовку колонки (сортировка), колесо.
"""

import curses
import locale
import math
import os
import pwd
import re
import signal
import sys
import time
from collections import defaultdict, deque

CURSES_ACTIVE = False
HZ = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
NCPU = os.cpu_count() or 1

# =====================================================================
#  Палитра
# =====================================================================
GRAD_N = 11
GRAD_COLOR = [46, 82, 118, 154, 190, 226, 220, 214, 208, 202, 196]  # зелёный -> красный
GRAD_COLOR8 = [2, 2, 2, 2, 3, 3, 3, 3, 1, 1, 1]
(P_BORDER, P_TITLE, P_DIM, P_HEAD, P_SEL, P_DOWN, P_UP, P_KEY, P_WARN, P_TEXT) = range(1, 11)
P_GRAD = 20
RICH = False

# Темы: (цвет в 256-цветном режиме, цвет в 8-цветном режиме); для head/sel/key: ((fg, bg), (fg, bg))
THEMES = {
    "color": {
        "grad": GRAD_COLOR, "grad8": GRAD_COLOR8,
        "border": (99, 5), "title": (141, 5), "dim": (240, 7), "down": (81, 6), "up": (213, 5), "warn": (203, 1),
        "head": ((16, 99), (0, 5)), "sel": ((255, 54), (7, 4)), "key": ((16, 141), (0, 5)),
    },
    "gray": {  # оттенки серого: чем светлее, тем выше нагрузка
        "grad": [245, 246, 247, 248, 249, 250, 251, 252, 253, 254, 255], "grad8": [7] * GRAD_N,
        "border": (240, 7), "title": (252, 7), "dim": (240, 7), "down": (252, 7), "up": (246, 7), "warn": (255, 7),
        "head": ((16, 248), (0, 7)), "sel": ((255, 238), (0, 7)), "key": ((16, 250), (0, 7)),
    },
}
THEME = "color"


def init_colors():
    global RICH
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    RICH = curses.COLORS >= 256
    t = THEMES[THEME]
    i = 0 if RICH else 1

    def one(key):
        return (t[key][i], -1)

    defs = {
        P_BORDER: one("border"), P_TITLE: one("title"), P_DIM: one("dim"),
        P_DOWN: one("down"), P_UP: one("up"), P_WARN: one("warn"),
        P_HEAD: t["head"][i], P_SEL: t["sel"][i], P_KEY: t["key"][i],
        P_TEXT: (-1, -1),
    }
    for pid, (fg, bg) in defs.items():
        curses.init_pair(pid, fg, bg)
    shades = t["grad"] if RICH else t["grad8"]
    for k in range(GRAD_N):
        curses.init_pair(P_GRAD + k, shades[k], -1)


def toggle_theme():
    global THEME
    THEME = "gray" if THEME == "color" else "color"
    if CURSES_ACTIVE:
        init_colors()


def pair(p, extra=0):
    if not CURSES_ACTIVE:
        return 0
    a = curses.color_pair(p) | extra
    if p == P_DIM and not RICH:
        a |= curses.A_DIM
    return a


def grad(v, extra=0):
    i = max(0, min(GRAD_N - 1, int(v * GRAD_N)))
    return pair(P_GRAD + i, extra)


BOLD = curses.A_BOLD

# =====================================================================
#  Форматирование
# =====================================================================
SPARK = "▁▂▃▄▅▆▇█"
PART = " ▏▎▍▌▋▊▉"


def fmt_bytes(n):
    n = float(n)
    for u in "BKMGTP":
        if n < 1024 or u == "P":
            if u == "B" or n >= 100:
                return f"{n:.0f}{u}"
            return f"{n:.1f}{u}"
        n /= 1024


def fmt_rate(n):
    return fmt_bytes(n) + "/s"


def fmt_pct(v):
    return f"{v:5.1f}" if v < 1000 else f"{v:5.0f}"


def fmt_uptime(sec):
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return (f"{d}d " if d else "") + f"{h:02}:{m:02}:{s:02}"


def fmt_time(j):
    s = j / HZ
    h = int(s // 3600)
    m = int(s % 3600 // 60)
    sec = s % 60
    return f"{h}:{m:02}:{int(sec):02}" if h else f"{m}:{sec:05.2f}"


def bar_parts(v, w):
    """Горизонтальная полоска с точностью 1/8 символа. Возвращает (заполненная_строка, свободных_клеток)."""
    v = max(0.0, min(1.0, v))
    eighths = int(round(v * w * 8))
    full, rem = divmod(eighths, 8)
    s = "█" * full
    used = full
    if rem and full < w:
        s += PART[rem]
        used += 1
    return s, w - used


# =====================================================================
#  Сбор данных (/proc, /sys)
# =====================================================================
CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # управляющие символы (в т.ч. \n и \0) ломают curses


def _read(path):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


class Sampler:
    def __init__(self):
        self.t_prev = None
        self.prev_cpu = None
        self.cpu_pct = {}
        self.cpu_hist = deque(maxlen=800)
        self.mem = {}
        self.net_prev = None
        self.down = self.up = 0.0
        self.rx_total = self.tx_total = 0
        self.down_hist = deque(maxlen=800)
        self.up_hist = deque(maxlen=800)
        self.io_prev = None
        self.io_r = self.io_w = 0.0
        self.disks = []
        self.disks_t = 0.0
        self.procs = []
        self.prev_jiff = {}
        self.cache = {}
        self.users = {}
        self.uptime = 0.0
        self.load = (0.0, 0.0, 0.0)
        self.host = os.uname().nodename
        self.kernel = os.uname().release
        self.model = self._model()
        self.temp = None
        self.freq = None
        self.ntasks = self.nrun = self.nthr = 0
        self.temp_path = self._find_temp()

    # ---- статические вещи ----
    def _model(self):
        m = re.search(r"^(?:model name|Hardware|Model)\s*:\s*(.+)$", _read("/proc/cpuinfo"), re.M)
        return re.sub(r"\s+", " ", m.group(1)).strip() if m else "CPU"

    def _find_temp(self):
        base = "/sys/class/hwmon"
        pref = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal", "acpitz")
        cands = []
        try:
            names = sorted(os.listdir(base))
        except OSError:
            names = []
        for d in names:
            n = _read(f"{base}/{d}/name").strip()
            p = f"{base}/{d}/temp1_input"
            if n in pref and os.path.exists(p):
                cands.append((pref.index(n), p))
        if cands:
            return min(cands)[1]
        p = "/sys/class/thermal/thermal_zone0/temp"
        return p if os.path.exists(p) else None

    # ---- читалки ----
    def _read_cpu(self):
        cur = {}
        for line in _read("/proc/stat").splitlines():
            if not line.startswith("cpu"):
                break
            parts = line.split()
            vals = [int(x) for x in parts[1:9]]
            cur[parts[0]] = (vals[3] + (vals[4] if len(vals) > 4 else 0), sum(vals))
        return cur

    def _read_mem(self):
        m = {}
        for line in _read("/proc/meminfo").splitlines():
            k, _, v = line.partition(":")
            parts = v.split()
            if parts:
                m[k] = int(parts[0]) * (1024 if len(parts) > 1 else 1)
        return m

    def _read_net(self):
        rx = tx = 0
        for line in _read("/proc/net/dev").splitlines()[2:]:
            name, _, data = line.partition(":")
            if name.strip() == "lo":
                continue
            f = data.split()
            if len(f) >= 9:
                rx += int(f[0])
                tx += int(f[8])
        return rx, tx

    def _read_io(self):
        r = w = 0
        for line in _read("/proc/diskstats").splitlines():
            p = line.split()
            if len(p) < 10:
                continue
            name = p[2]
            if name.startswith(("loop", "ram", "zram", "dm-", "md", "sr")):
                continue
            if not os.path.exists(f"/sys/block/{name}"):  # только целые диски, без разделов
                continue
            r += int(p[5]) * 512
            w += int(p[9]) * 512
        return r, w

    def _read_disks(self):
        out, seen = [], set()
        for line in _read("/proc/mounts").splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            dev, mp = parts[0], parts[1].replace("\\040", " ")
            if not dev.startswith("/dev/") or dev.startswith("/dev/loop") or dev in seen:
                continue
            seen.add(dev)
            try:
                st = os.statvfs(mp)
            except OSError:
                continue
            used = (st.f_blocks - st.f_bfree) * st.f_frsize
            total = used + st.f_bavail * st.f_frsize
            if total > 0:
                out.append((mp, used, total))
        if not out:
            try:
                st = os.statvfs("/")
                used = (st.f_blocks - st.f_bfree) * st.f_frsize
                total = used + st.f_bavail * st.f_frsize
                if total > 0:
                    out.append(("/", used, total))
            except OSError:
                pass
        return out

    def _read_procs(self, dtotal):
        procs, seen = [], {}
        mem_total = self.mem.get("MemTotal", 1) or 1
        nrun = nthr = 0
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{entry}/stat", "r", errors="replace") as f:
                    s = f.read()
                lp, rp = s.find("("), s.rfind(")")
                name = CTRL.sub(" ", s[lp + 1:rp])
                rest = s[rp + 2:].split()
                state = rest[0]
                ppid = int(rest[1])
                jiff = int(rest[11]) + int(rest[12])
                thr = int(rest[17])
                start = rest[19]
                rss = int(rest[21]) * PAGE
            except (OSError, ValueError, IndexError):
                continue
            c = self.cache.get(pid)
            if c is None or c[0] != start:
                cmd = CTRL.sub(" ", _read(f"/proc/{pid}/cmdline")).strip()
                try:
                    uid = os.stat(f"/proc/{pid}").st_uid
                except OSError:
                    uid = 0
                user = self.users.get(uid)
                if user is None:
                    try:
                        user = pwd.getpwuid(uid).pw_name
                    except KeyError:
                        user = str(uid)
                    self.users[uid] = user
                parts = cmd.split(" ", 1)
                args = parts[1] if len(parts) > 1 else ""
                c = (start, cmd, user, args)
                self.cache[pid] = c
            prev = self.prev_jiff.get(pid)
            cpu = 0.0
            if prev is not None and dtotal > 0:
                cpu = max(0.0, (jiff - prev) * NCPU / dtotal)  # 1.0 == одно ядро на 100%
            seen[pid] = jiff
            if state == "R":
                nrun += 1
            nthr += thr
            procs.append({
                "pid": pid, "ppid": ppid, "name": name, "cmd": c[1], "user": c[2], "args": c[3],
                "state": state, "thr": thr, "cpu": cpu, "rss": rss, "mem": rss / mem_total,
                "jiff": jiff, "tree": "",
            })
        self.prev_jiff = seen
        self.cache = {k: v for k, v in self.cache.items() if k in seen}
        self.procs = procs
        self.ntasks, self.nrun, self.nthr = len(procs), nrun, nthr

    # ---- главный шаг ----
    def sample(self):
        now = time.monotonic()
        dt = (now - self.t_prev) if self.t_prev else 0.0
        self.t_prev = now

        cur = self._read_cpu()
        dtotal = 0
        if self.prev_cpu:
            for name, (idle, total) in cur.items():
                p = self.prev_cpu.get(name)
                if p:
                    dtt, di = total - p[1], idle - p[0]
                    self.cpu_pct[name] = max(0.0, min(1.0, 1 - di / dtt)) if dtt > 0 else 0.0
            if "cpu" in cur and "cpu" in self.prev_cpu:
                dtotal = cur["cpu"][1] - self.prev_cpu["cpu"][1]
            self.cpu_hist.append(self.cpu_pct.get("cpu", 0.0))
        else:
            for name in cur:
                self.cpu_pct.setdefault(name, 0.0)
        self.prev_cpu = cur

        self.mem = self._read_mem()
        try:
            self.load = os.getloadavg()
        except OSError:
            pass
        try:
            self.uptime = float(_read("/proc/uptime").split()[0])
        except (IndexError, ValueError):
            pass
        if self.temp_path:
            try:
                self.temp = int(_read(self.temp_path).strip()) / 1000.0
            except ValueError:
                self.temp = None
        mhz = [float(x) for x in re.findall(r"^cpu MHz\s*:\s*([\d.]+)", _read("/proc/cpuinfo"), re.M)]
        self.freq = sum(mhz) / len(mhz) if mhz else None

        rx, tx = self._read_net()
        if self.net_prev and dt > 0:
            self.down = max(0.0, (rx - self.net_prev[0]) / dt)
            self.up = max(0.0, (tx - self.net_prev[1]) / dt)
            self.down_hist.append(self.down)
            self.up_hist.append(self.up)
        self.net_prev = (rx, tx)
        self.rx_total, self.tx_total = rx, tx

        r, w = self._read_io()
        if self.io_prev and dt > 0:
            self.io_r = max(0.0, (r - self.io_prev[0]) / dt)
            self.io_w = max(0.0, (w - self.io_prev[1]) / dt)
        self.io_prev = (r, w)

        if now - self.disks_t > 5:
            self.disks = self._read_disks()
            self.disks_t = now

        self._read_procs(dtotal)


# =====================================================================
#  Рисование: Braille-холст (кольца и графики)
# =====================================================================
# BR[dx][dy] - бит точки в клетке Braille (2 колонки x 4 строки точек)
BR = [[0x01, 0x02, 0x04, 0x40], [0x08, 0x10, 0x20, 0x80]]


def gauge_cells(cw, ch, value):
    """Кольцо-спидометр на 270 градусов, раскрытое вниз. Возвращает (залито, трек, позиции)."""
    W, H = cw * 2, ch * 4
    cx, cy = (W - 1) / 2, (H - 1) / 2
    R = min(W, H) / 2 - 0.3
    r_in = R - 3.6
    fill, track, acc = {}, {}, {}
    for py in range(H):
        for px in range(W):
            dx, dy = px - cx, py - cy
            d = math.hypot(dx, dy)
            if d < r_in or d > R:
                continue
            theta = math.degrees(math.atan2(dx, -dy)) % 360  # по часовой от 12 часов
            rel = (theta - 225) % 360                          # старт снизу-слева
            if rel > 270:
                continue
            pos = rel / 270
            key = (py // 4, px // 2)
            bit = BR[px % 2][py % 4]
            if value > 0 and pos <= value:
                fill[key] = fill.get(key, 0) | bit
                a = acc.get(key, (0.0, 0))
                acc[key] = (a[0] + pos, a[1] + 1)
            else:
                track[key] = track.get(key, 0) | bit
    return fill, track, acc


def graph_cells(cw, ch, data, vmax):
    """Площадной график: по 2 отсчёта на клетку по ширине, 4 точки на клетку по высоте."""
    n = cw * 2
    s = list(data)[-n:]
    s = [0.0] * (n - len(s)) + s
    cells = {}
    if vmax <= 0:
        return cells
    hs = []
    for v in s:
        h = int(round(min(max(v / vmax, 0.0), 1.0) * ch * 4))
        if v > 0 and h == 0:
            h = 1
        hs.append(h)
    for c in range(cw):
        for r in range(ch):
            base = (ch - 1 - r) * 4
            mask, val = 0, 0.0
            for sub in (0, 1):
                fillrows = max(0, min(4, hs[2 * c + sub] - base))
                for k in range(fillrows):
                    mask |= BR[sub][3 - k]
                val = max(val, s[2 * c + sub] / vmax)
            if mask:
                cells[(r, c)] = (mask, min(1.0, val))
    return cells


# =====================================================================
#  Экран (обёртка над curses + фейковый экран для --snapshot)
# =====================================================================
class Screen:
    def __init__(self, win):
        self.win = win

    def size(self):
        return self.win.getmaxyx()

    def erase(self):
        self.win.erase()

    def put(self, y, x, s, attr=0):
        h, w = self.win.getmaxyx()
        if y < 0 or y >= h or x >= w or not s:
            return
        if x < 0:
            s = s[-x:]
            x = 0
        s = s[:w - x]
        if not s:
            return
        try:
            self.win.addstr(y, x, s, attr)
        except curses.error:
            pass

    def flush(self):
        self.win.refresh()


class FakeScreen:
    def __init__(self, h, w):
        self.h, self.w = h, w
        self.erase()

    def size(self):
        return self.h, self.w

    def erase(self):
        self.g = [[" "] * self.w for _ in range(self.h)]

    def put(self, y, x, s, attr=0):
        if y < 0 or y >= self.h:
            return
        for i, c in enumerate(s):
            xx = x + i
            if 0 <= xx < self.w:
                self.g[y][xx] = c

    def flush(self):
        pass

    def dump(self):
        return "\n".join("".join(r).rstrip() for r in self.g)


def box(scr, y, x, h, w, title="", right=""):
    if h < 2 or w < 6:
        return
    b = pair(P_BORDER)
    scr.put(y, x, "╭" + "─" * (w - 2) + "╮", b)
    for i in range(1, h - 1):
        scr.put(y + i, x, "│", b)
        scr.put(y + i, x + w - 1, "│", b)
    scr.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", b)
    end_title = x + 2
    if title:
        t = f" {title} "
        scr.put(y, x + 2, "┤", b)
        scr.put(y, x + 3, t, pair(P_TITLE, BOLD))
        scr.put(y, x + 3 + len(t), "├", b)
        end_title = x + 4 + len(t)
    if right:
        t = f" {right} "
        start = x + w - 4 - len(t)
        if start > end_title:
            scr.put(y, start, "┤", b)
            scr.put(y, start + 1, t, pair(P_TEXT))
            scr.put(y, start + 1 + len(t), "├", b)


def draw_gauge(scr, y, x, cw, ch, value, label, sub="", text=None, off=False):
    value = max(0.0, min(1.0, value))
    fill, track, acc = gauge_cells(cw, ch, 0 if off else value)
    dim = pair(P_DIM)
    for (r, c), mask in track.items():
        if (r, c) not in fill:
            scr.put(y + r, x + c, chr(0x2800 + mask), dim)
    for (r, c), mask in fill.items():
        s, n = acc[(r, c)]
        scr.put(y + r, x + c, chr(0x2800 + mask), grad(s / n))
    t = text if text is not None else f"{value * 100:.0f}%"
    scr.put(y + ch // 2 - 1, x + (cw - len(t)) // 2, t, dim if off else grad(value, BOLD))
    if sub:
        scr.put(y + ch // 2, x + (cw - len(sub)) // 2, sub, dim)
    scr.put(y + ch - 1, x + (cw - len(label)) // 2, label, pair(P_TITLE, BOLD))


def draw_graph(scr, y, x, cw, ch, data, vmax, color=None):
    for (r, c), (mask, val) in graph_cells(cw, ch, data, vmax).items():
        scr.put(y + r, x + c, chr(0x2800 + mask), pair(color) if color else grad(val))


def draw_bar(scr, y, x, v, w, attr=None, rest_char="░"):
    s, rest = bar_parts(v, w)
    scr.put(y, x, s, attr if attr is not None else grad(v))
    if rest > 0 and rest_char:
        scr.put(y, x + w - rest, rest_char * rest, pair(P_DIM))


# =====================================================================
#  Колонки таблицы процессов
# =====================================================================
# key, заголовок, ширина, выравнивание, мин. ширина терминала для показа
COLS = [
    ("pid", "PID", 7, ">", 0),
    ("user", "USER", 9, "<", 72),
    ("state", "S", 1, "<", 0),
    ("thr", "THR", 3, ">", 104),
    ("cpu", "CPU%", 5, ">", 0),
    ("cpubar", "", 6, "<", 92),
    ("mem", "MEM%", 5, ">", 0),
    ("rss", "RES", 6, ">", 80),
    ("time", "TIME+", 9, ">", 96),
    ("cmd", "COMMAND", 0, "<", 0),
]
SORTKEY = {"pid": "pid", "user": "user", "thr": "thr", "cpu": "cpu", "cpubar": "cpu",
           "mem": "mem", "rss": "mem", "time": "time", "cmd": "name", "state": None}
ARROW_COL = {"pid": "pid", "user": "user", "thr": "thr", "cpu": "cpu", "mem": "mem",
             "time": "time", "cmd": "name"}
SORT_ORDER = ["cpu", "mem", "time", "pid", "name", "user"]
DESC_BY_DEFAULT = ("cpu", "mem", "time", "thr")

SIGS = [
    (int(signal.SIGTERM), "SIGTERM", "попросить завершиться"),
    (int(signal.SIGKILL), "SIGKILL", "убить принудительно"),
    (int(signal.SIGINT), "SIGINT", "прерывание (как Ctrl+C)"),
    (int(signal.SIGHUP), "SIGHUP", "перечитать конфиг / отбой"),
    (int(signal.SIGSTOP), "SIGSTOP", "поставить на паузу"),
    (int(signal.SIGCONT), "SIGCONT", "продолжить работу"),
]

HELP = [
    ("↑ ↓  PgUp PgDn  Home End", "выбор процесса"),
    ("k / Del / F9", "послать сигнал (меню)"),
    ("K", "сразу меню с выбранным SIGKILL"),
    ("/", "фильтр: имя / команда / юзер / PID"),
    ("s", "сменить колонку сортировки"),
    ("c  m  p  n", "сорт. по CPU / MEM / PID / имени"),
    ("r", "обратный порядок"),
    ("t", "дерево процессов"),
    ("g", "серая / цветная тема"),
    ("пробел", "пауза обновления"),
    ("+  -", "чаще / реже обновлять"),
    ("мышь", "клик / заголовок (сорт.) / колесо"),
    ("q", "выход"),
]


def tree_order(procs, keyf, rev):
    pids = {p["pid"] for p in procs}
    kids = defaultdict(list)
    for p in procs:
        parent = p["ppid"] if (p["ppid"] in pids and p["ppid"] != p["pid"]) else 0
        kids[parent].append(p)
    out = []

    def walk(parent, prefix, is_root):
        lst = sorted(kids.get(parent, []), key=keyf, reverse=rev)
        for i, p in enumerate(lst):
            last = i == len(lst) - 1
            p["tree"] = "" if is_root else prefix + ("└─ " if last else "├─ ")
            out.append(p)
            walk(p["pid"], "" if is_root else prefix + ("   " if last else "│  "), False)

    walk(0, "", True)
    return out


# =====================================================================
#  Приложение (состояние + отрисовка + ввод)
# =====================================================================
class App:
    def __init__(self, st):
        self.st = st
        self.sort = "cpu"
        self.rev = True
        self.tree = False
        self.filter = ""
        self.mode = "normal"       # normal | filter | kill | help
        self.sel = None
        self.last_idx = 0
        self.scroll = 0
        self.paused = False
        self.interval = 1.0
        self.next_sample = 0.0
        self.msg = ""
        self.msg_t = 0.0
        self.kill_i = 0
        self.kill_target = None
        self.rows = []
        self.hit_cols = []
        self.geom = None
        self.vh = 10

    # ---------- представление списка процессов ----------
    def view(self):
        procs = self.st.procs
        q = self.filter.lower()
        if q:
            procs = [p for p in procs
                     if q in p["name"].lower() or q in p["cmd"].lower()
                     or q in p["user"].lower() or q == str(p["pid"])]
        keys = {
            "cpu": lambda p: (p["cpu"], p["rss"]),
            "mem": lambda p: (p["rss"], p["cpu"]),
            "pid": lambda p: p["pid"],
            "name": lambda p: p["name"].lower(),
            "user": lambda p: p["user"].lower(),
            "time": lambda p: p["jiff"],
            "thr": lambda p: p["thr"],
        }
        keyf = keys[self.sort]
        if self.tree and not q:
            return tree_order(procs, keyf, self.rev)
        for p in procs:
            p["tree"] = ""
        return sorted(procs, key=keyf, reverse=self.rev)

    def refresh_rows(self):
        self.rows = self.view()

    def cur_idx(self):
        for i, p in enumerate(self.rows):
            if p["pid"] == self.sel:
                return i
        return min(self.last_idx, max(0, len(self.rows) - 1))

    def sync_sel(self, vh):
        rows = self.rows
        if not rows:
            self.sel, self.scroll = None, 0
            return 0
        idx = None
        if self.sel is not None:
            for i, p in enumerate(rows):
                if p["pid"] == self.sel:
                    idx = i
                    break
        if idx is None:
            idx = min(self.last_idx, len(rows) - 1)
            self.sel = rows[idx]["pid"]
        self.last_idx = idx
        if idx < self.scroll:
            self.scroll = idx
        if idx >= self.scroll + vh:
            self.scroll = idx - vh + 1
        self.scroll = max(0, min(self.scroll, max(0, len(rows) - vh)))
        return idx

    def move(self, delta):
        if not self.rows:
            return
        idx = max(0, min(len(self.rows) - 1, self.cur_idx() + delta))
        self.sel = self.rows[idx]["pid"]

    def set_sort(self, key):
        if key == self.sort:
            self.rev = not self.rev
        else:
            self.sort = key
            self.rev = key in DESC_BY_DEFAULT

    def say(self, text):
        self.msg, self.msg_t = text, time.monotonic()

    # ---------- общая раскладка ----------
    def draw(self, scr):
        h, w = scr.size()
        scr.erase()
        if h < 12 or w < 60:
            scr.put(0, 0, f"gtop: окно мало, нужно 60x12", pair(P_WARN))
            scr.flush()
            return
        self.draw_header(scr, w)
        y = 1
        avail = h - 2
        top_h, net_h, min_proc = 8, 8, 7
        show_top = avail >= top_h + min_proc
        show_net = show_top and avail >= top_h + net_h + min_proc + 3
        half = w // 2
        if show_top:
            self.panel_cpu(scr, y, 0, top_h, half)
            self.panel_mem(scr, y, half, top_h, w - half)
            y += top_h
        if show_net:
            self.panel_net(scr, y, 0, net_h, half)
            self.panel_disk(scr, y, half, net_h, w - half)
            y += net_h
        self.panel_proc(scr, y, 0, h - 1 - y, w)
        self.draw_status(scr, h - 1, w)
        if self.mode == "kill":
            self.draw_kill(scr, h, w)
        elif self.mode == "help":
            self.draw_help(scr, h, w)
        scr.flush()

    def draw_header(self, scr, w):
        st = self.st
        scr.put(0, 0, " " * w, pair(P_HEAD))
        left = " ◉ gtop "
        scr.put(0, 0, left, pair(P_HEAD, BOLD))
        scr.put(0, len(left), f" {st.host} · {st.kernel} ", pair(P_HEAD))
        right = f"up {fmt_uptime(st.uptime)}   {time.strftime('%H:%M:%S')} "
        if self.paused:
            right = "⏸ PAUSED   " + right
        scr.put(0, max(len(left) + 2, w - len(right)), right, pair(P_HEAD, BOLD))

    # ---------- CPU ----------
    def panel_cpu(self, scr, y, x, h, w):
        st = self.st
        total = st.cpu_pct.get("cpu", 0.0)
        box(scr, y, x, h, w, "CPU", f"{NCPU} core" + ("" if NCPU == 1 else "s"))
        sub = f"{st.temp:.0f}°C" if st.temp is not None else f"{st.load[0]:.2f}"
        draw_gauge(scr, y + 1, x + 2, 12, 6, total, "CPU", sub)
        rx, rw = x + 16, w - 17
        if rw < 12:
            return
        scr.put(y + 1, rx, st.model[:rw], pair(P_DIM))
        draw_graph(scr, y + 2, rx, rw, 3, list(st.cpu_hist), 1.0)
        names = sorted((n for n in st.cpu_pct if n != "cpu"), key=lambda n: int(n[3:]))
        label = "cores "
        scr.put(y + 5, rx, label, pair(P_DIM))
        cx = rx + len(label)
        for n in names:
            if cx >= rx + rw:
                break
            v = st.cpu_pct[n]
            scr.put(y + 5, cx, SPARK[min(7, int(v * 8))], grad(v))
            cx += 1
        line = f"load {st.load[0]:.2f} {st.load[1]:.2f} {st.load[2]:.2f}"
        if st.temp is not None:
            line += f"   {st.temp:.0f}°C"
        if st.freq:
            line += f"   {st.freq / 1000:.1f}GHz"
        scr.put(y + 6, rx, line[:rw], pair(P_TEXT))

    # ---------- MEM ----------
    def panel_mem(self, scr, y, x, h, w):
        mem = self.st.mem
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        used = max(0, total - avail)
        frac = used / total if total else 0.0
        swt = mem.get("SwapTotal", 0)
        swu = swt - mem.get("SwapFree", 0)
        sfrac = swu / swt if swt else 0.0
        box(scr, y, x, h, w, "MEM", fmt_bytes(total))
        draw_gauge(scr, y + 1, x + 2, 12, 6, frac, "RAM", fmt_bytes(used))
        two = w - 2 >= 26
        if two:
            draw_gauge(scr, y + 1, x + 15, 12, 6, sfrac, "SWAP",
                       fmt_bytes(swu) if swt else "", text=None if swt else "off", off=not swt)
        tx = x + (29 if two else 16)
        tw = (x + w - 2) - tx + 1
        if tw < 13:
            return
        lines = [
            ("Total", fmt_bytes(total)),
            ("Used", fmt_bytes(used)),
            ("Avail", fmt_bytes(avail)),
            ("Cache", fmt_bytes(mem.get("Cached", 0))),
            ("Buffers", fmt_bytes(mem.get("Buffers", 0))),
            ("Swap", f"{fmt_bytes(swu)}/{fmt_bytes(swt)}" if swt else "off"),
        ]
        for i, (lab, val) in enumerate(lines):
            scr.put(y + 1 + i, tx, lab, pair(P_DIM))
            scr.put(y + 1 + i, tx + tw - len(val), val, pair(P_TEXT))

    # ---------- NET ----------
    def panel_net(self, scr, y, x, h, w):
        st = self.st
        box(scr, y, x, h, w, "NET", f"{fmt_bytes(st.rx_total)}▼ {fmt_bytes(st.tx_total)}▲")
        iw, gx = w - 4, x + 2
        dn, up = list(st.down_hist), list(st.up_hist)
        n = iw * 2
        vd = max([50 * 1024] + dn[-n:])
        vu = max([50 * 1024] + up[-n:])
        scr.put(y + 1, gx, "▼ ", pair(P_DOWN, BOLD))
        scr.put(y + 1, gx + 2, fmt_rate(st.down), pair(P_TEXT, BOLD))
        pk = f"peak {fmt_rate(max([0.0] + dn[-n:]))}"
        scr.put(y + 1, gx + iw - len(pk), pk, pair(P_DIM))
        draw_graph(scr, y + 2, gx, iw, 2, dn, vd, color=P_DOWN)
        scr.put(y + 4, gx, "▲ ", pair(P_UP, BOLD))
        scr.put(y + 4, gx + 2, fmt_rate(st.up), pair(P_TEXT, BOLD))
        pk = f"peak {fmt_rate(max([0.0] + up[-n:]))}"
        scr.put(y + 4, gx + iw - len(pk), pk, pair(P_DIM))
        draw_graph(scr, y + 5, gx, iw, 2, up, vu, color=P_UP)

    # ---------- DISK ----------
    def panel_disk(self, scr, y, x, h, w):
        st = self.st
        box(scr, y, x, h, w, "DISK")
        iw, gx = w - 4, x + 2
        for i, (mp, used, total) in enumerate(st.disks[:h - 3]):
            frac = used / total
            label = mp if len(mp) <= 10 else "…" + mp[-9:]
            right = f" {frac * 100:3.0f}% {fmt_bytes(used)}/{fmt_bytes(total)}"
            bw = iw - 11 - len(right)
            if bw < 4:
                right = f" {frac * 100:3.0f}%"
                bw = iw - 11 - len(right)
            if bw < 2:
                continue
            row = y + 1 + i
            scr.put(row, gx, label.ljust(10), pair(P_TEXT, BOLD))
            draw_bar(scr, row, gx + 11, frac, bw)
            scr.put(row, gx + 11 + bw, right, pair(P_TEXT))
        row = y + h - 2
        scr.put(row, gx, "IO ", pair(P_DIM))
        scr.put(row, gx + 3, "R ", pair(P_DOWN, BOLD))
        r = fmt_rate(st.io_r)
        scr.put(row, gx + 5, r, pair(P_TEXT))
        scr.put(row, gx + 6 + len(r) + 1, "W ", pair(P_UP, BOLD))
        scr.put(row, gx + 6 + len(r) + 3, fmt_rate(st.io_w), pair(P_TEXT))

    # ---------- Процессы ----------
    def panel_proc(self, scr, y, x, h, w):
        st = self.st
        self.refresh_rows()
        rows = self.rows
        arrow = "▼" if self.rev else "▲"
        right = f"{st.ntasks} tasks · {st.nrun} run · {st.nthr} thr"
        title = "Processes" + (" · tree" if self.tree else "")
        if self.filter:
            title += f" · /{self.filter}"
        box(scr, y, x, h, w, title, right)
        if h < 4:
            return
        iw, ix = w - 2, x + 1
        self.vh = max(1, h - 3)
        cols = [c for c in COLS if w >= c[4]]

        hy = y + 1
        scr.put(hy, ix, " " * iw, pair(P_HEAD))
        cx = ix
        self.hit_cols = []
        for key, ttl, cw, al, _ in cols:
            if key == "cmd":
                cw = max(0, iw - (cx - ix))
            if cw <= 0:
                break
            t = ttl
            if ARROW_COL.get(key) == self.sort:
                t = (ttl[:max(0, cw - 1)] + arrow) if len(ttl) + 1 > cw else ttl + arrow
            txt = t.rjust(cw) if al == ">" else t.ljust(cw)
            scr.put(hy, cx, txt[:cw], pair(P_HEAD, BOLD))
            self.hit_cols.append((cx, cx + cw, SORTKEY[key]))
            cx += cw + 1

        idx = self.sync_sel(self.vh)
        ry0 = y + 2
        self.geom = (ry0, ix, iw)
        for r in range(self.vh):
            i = self.scroll + r
            if i >= len(rows):
                break
            p = rows[i]
            sel = i == idx
            yy = ry0 + r
            base = pair(P_SEL) if sel else pair(P_TEXT)
            if sel:
                scr.put(yy, ix, " " * iw, pair(P_SEL))
            cx = ix
            for key, ttl, cw, al, _ in cols:
                if key == "cmd":
                    cw = max(0, iw - (cx - ix))
                if cw <= 0:
                    break
                attr = base
                if key == "pid":
                    txt = str(p["pid"]).rjust(cw)
                    attr = base if sel else pair(P_DIM)
                elif key == "user":
                    u = p["user"]
                    txt = (u if len(u) <= cw else u[:cw - 1] + "+").ljust(cw)
                elif key == "state":
                    txt = p["state"][:1]
                    if not sel:
                        attr = grad(0.0) if txt == "R" else (pair(P_WARN) if txt in "ZD" else pair(P_DIM))
                elif key == "thr":
                    txt = str(p["thr"]).rjust(cw)
                    attr = base if sel else pair(P_DIM)
                elif key == "cpu":
                    txt = fmt_pct(p["cpu"] * 100).rjust(cw)
                    if not sel:
                        attr = grad(min(1.0, p["cpu"]))
                elif key == "cpubar":
                    s, _rest = bar_parts(min(1.0, p["cpu"]), cw)
                    scr.put(yy, cx, s, base if sel else grad(min(1.0, p["cpu"])))
                    cx += cw + 1
                    continue
                elif key == "mem":
                    txt = fmt_pct(p["mem"] * 100).rjust(cw)
                    if not sel:
                        attr = grad(min(1.0, p["mem"] * 4))
                elif key == "rss":
                    txt = fmt_bytes(p["rss"]).rjust(cw)
                elif key == "time":
                    txt = fmt_time(p["jiff"]).rjust(cw)
                    attr = base if sel else pair(P_DIM)
                else:  # cmd
                    pre, nm, rest = p["tree"], p["name"], p["args"]
                    scr.put(yy, cx, pre[:cw], base if sel else pair(P_DIM))
                    room = cw - len(pre)
                    if room > 0:
                        scr.put(yy, cx + len(pre), nm[:room], base if sel else pair(P_TEXT, BOLD))
                        room2 = room - len(nm) - 1
                        if room2 > 0 and rest:
                            scr.put(yy, cx + len(pre) + len(nm) + 1, rest[:room2], base if sel else pair(P_DIM))
                    break
                scr.put(yy, cx, txt, attr)
                cx += cw + 1

        # скроллбар на правой рамке
        if len(rows) > self.vh:
            thumb = max(1, self.vh * self.vh // len(rows))
            pos = self.scroll * (self.vh - thumb) // max(1, len(rows) - self.vh)
            for r in range(self.vh):
                on = pos <= r < pos + thumb
                scr.put(ry0 + r, x + w - 1, "┃" if on else "│", pair(P_TITLE) if on else pair(P_BORDER))

    # ---------- Статус-строка ----------
    def draw_status(self, scr, y, w):
        scr.put(y, 0, " " * (w - 1), pair(P_TEXT))
        if self.mode == "filter":
            scr.put(y, 0, " / ", pair(P_KEY, BOLD))
            scr.put(y, 4, f"фильтр: {self.filter}▌", pair(P_TEXT, BOLD))
            hint = "Enter - оставить   Esc - сбросить"
            scr.put(y, max(30, w - len(hint) - 2), hint, pair(P_DIM))
            return
        hints = [("q", "выход"), ("↑↓", "выбор"), ("k", "kill"), ("/", "поиск"),
                 ("s", "сорт"), ("t", "дерево"), ("␣", "пауза"), ("?", "помощь")]
        x = 0
        for k, d in hints:
            need = len(k) + len(d) + 4
            if x + need > w - 18:
                break
            scr.put(y, x, f" {k} ", pair(P_KEY, BOLD))
            scr.put(y, x + len(k) + 2, f" {d} ", pair(P_DIM))
            x += need
        if self.msg:
            t = f" {self.msg} "
            scr.put(y, max(x, w - len(t) - 1), t, pair(P_WARN, BOLD))
        else:
            t = f"обновление {self.interval:.2f}s"
            scr.put(y, max(x, w - len(t) - 2), t, pair(P_DIM))

    # ---------- Попапы ----------
    def draw_kill(self, scr, h, w):
        n = len(SIGS)
        bw, bh = 48, n + 6
        y0, x0 = max(0, (h - bh) // 2), max(0, (w - bw) // 2)
        for r in range(bh):
            scr.put(y0 + r, x0, " " * bw, pair(P_TEXT))
        box(scr, y0, x0, bh, bw, "Послать сигнал")
        pid, name = self.kill_target if self.kill_target else (0, "?")
        scr.put(y0 + 1, x0 + 2, f"Цель: {pid}  {name}"[:bw - 4], pair(P_TEXT, BOLD))
        for i, (num, sname, desc) in enumerate(SIGS):
            line = f" {num:>2}  {sname:<8} {desc}".ljust(bw - 4)[:bw - 4]
            scr.put(y0 + 3 + i, x0 + 2, line, pair(P_SEL, BOLD) if i == self.kill_i else pair(P_TEXT))
        scr.put(y0 + bh - 2, x0 + 2, "↑↓ выбор   Enter отправить   Esc отмена", pair(P_DIM))

    def draw_help(self, scr, h, w):
        bw, bh = 62, len(HELP) + 4
        y0, x0 = max(0, (h - bh) // 2), max(0, (w - bw) // 2)
        for r in range(bh):
            scr.put(y0 + r, x0, " " * bw, pair(P_TEXT))
        box(scr, y0, x0, bh, bw, "Клавиши")
        for i, (k, d) in enumerate(HELP):
            scr.put(y0 + 1 + i, x0 + 2, k.ljust(26), pair(P_TITLE, BOLD))
            scr.put(y0 + 1 + i, x0 + 28, d[:bw - 30], pair(P_TEXT))
        scr.put(y0 + bh - 2, x0 + 2, "любая клавиша - закрыть", pair(P_DIM))

    # ---------- Ввод ----------
    def open_kill(self, kill_idx=0):
        if not self.rows:
            self.say("нет выбранного процесса")
            return
        p = self.rows[self.cur_idx()]
        self.kill_target = (p["pid"], p["name"])
        self.kill_i = kill_idx
        self.mode = "kill"

    def send(self):
        pid, name = self.kill_target
        num, sname, _ = SIGS[self.kill_i]
        self.mode = "normal"
        if pid == os.getpid():
            self.say("себя убивать не буду :)")
        elif pid == 1 and num in (int(signal.SIGKILL), int(signal.SIGSTOP)):
            self.say("PID 1 трогать не буду")
        else:
            try:
                os.kill(pid, num)
                self.say(f"{sname} → {pid} ({name})")
            except ProcessLookupError:
                self.say(f"процесс {pid} уже завершился")
            except PermissionError:
                self.say(f"нет прав на {pid} ({name}) - нужен root")
            except OSError as e:
                self.say(str(e))
        self.next_sample = 0.0  # сразу обновим список

    def key_kill(self, ch):
        if ch == curses.KEY_UP:
            self.kill_i = (self.kill_i - 1) % len(SIGS)
        elif ch == curses.KEY_DOWN:
            self.kill_i = (self.kill_i + 1) % len(SIGS)
        elif ch in ("\n", "\r", curses.KEY_ENTER):
            self.send()
        elif ch in ("\x1b", "q", "Q"):
            self.mode = "normal"

    def key_filter(self, ch):
        if ch in ("\n", "\r", curses.KEY_ENTER):
            self.mode = "normal"
        elif ch == "\x1b":
            self.filter = ""
            self.mode = "normal"
        elif ch in ("\x7f", "\b", curses.KEY_BACKSPACE):
            self.filter = self.filter[:-1]
        elif isinstance(ch, str) and ch.isprintable():
            self.filter += ch
        self.sel = None
        self.scroll = 0

    def mouse(self):
        try:
            _, mx, my, _, b = curses.getmouse()
        except curses.error:
            return
        up = getattr(curses, "BUTTON4_PRESSED", 0)
        down = getattr(curses, "BUTTON5_PRESSED", 0)
        if up and b & up:
            self.move(-3)
        elif down and b & down:
            self.move(3)
        elif b & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED):
            if not self.geom:
                return
            ry0 = self.geom[0]
            if my == ry0 - 1:
                for x0, x1, key in self.hit_cols:
                    if x0 <= mx < x1 and key:
                        self.set_sort(key)
                        break
            elif 0 <= my - ry0 < self.vh:
                i = self.scroll + (my - ry0)
                if i < len(self.rows):
                    self.sel = self.rows[i]["pid"]

    def handle_key(self, ch):
        self.refresh_rows()
        if self.mode == "filter":
            return self.key_filter(ch)
        if self.mode == "kill":
            return self.key_kill(ch)
        if self.mode == "help":
            self.mode = "normal"
            return None
        if ch == curses.KEY_MOUSE:
            return self.mouse()
        if ch in ("q", "Q"):
            return "quit"
        if ch == curses.KEY_UP:
            self.move(-1)
        elif ch == curses.KEY_DOWN:
            self.move(1)
        elif ch == curses.KEY_PPAGE:
            self.move(-self.vh)
        elif ch == curses.KEY_NPAGE:
            self.move(self.vh)
        elif ch == curses.KEY_HOME:
            self.move(-10 ** 9)
        elif ch == curses.KEY_END:
            self.move(10 ** 9)
        elif ch in ("k", curses.KEY_DC, curses.KEY_F9):
            self.open_kill(0)
        elif ch == "K":
            self.open_kill(1)
        elif ch in ("/", "f"):
            self.mode = "filter"
        elif ch == "\x1b" and self.filter:
            self.filter = ""
        elif ch == "s":
            self.set_sort(SORT_ORDER[(SORT_ORDER.index(self.sort) + 1) % len(SORT_ORDER)]
                          if self.sort in SORT_ORDER else "cpu")
        elif ch == "c":
            self.set_sort("cpu")
        elif ch == "m":
            self.set_sort("mem")
        elif ch == "p":
            self.set_sort("pid")
        elif ch == "n":
            self.set_sort("name")
        elif ch == "r":
            self.rev = not self.rev
        elif ch == "t":
            self.tree = not self.tree
        elif ch == "g":
            toggle_theme()
        elif ch == " ":
            self.paused = not self.paused
        elif ch in ("+", "="):
            self.interval = max(0.25, self.interval - 0.25)
        elif ch in ("-", "_"):
            self.interval = min(5.0, self.interval + 0.25)
        elif ch in ("?", "h", curses.KEY_F1):
            self.mode = "help"
        return None


# =====================================================================
#  Запуск
# =====================================================================
def run(stdscr):
    global CURSES_ACTIVE
    CURSES_ACTIVE = True
    init_colors()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    stdscr.timeout(60)
    try:
        curses.mouseinterval(0)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
    except curses.error:
        pass

    st = Sampler()
    st.sample()
    app = App(st)
    scr = Screen(stdscr)
    app.next_sample = time.monotonic() + 0.35  # быстрый второй замер, чтобы CPU% не были нулевыми
    app.draw(scr)

    while True:
        now = time.monotonic()
        need = False
        if now >= app.next_sample:
            if not app.paused:
                st.sample()
                need = True
            app.next_sample = now + app.interval
        try:
            ch = stdscr.get_wch()
        except curses.error:
            ch = None
        if ch is not None:
            if ch == curses.KEY_RESIZE:
                try:
                    curses.update_lines_cols()
                except AttributeError:
                    pass
            elif app.handle_key(ch) == "quit":
                break
            need = True
        if app.msg and now - app.msg_t > 4:
            app.msg = ""
            need = True
        if need:
            app.draw(scr)


def snapshot(arg):
    W, H = 120, 40
    m = re.match(r"(\d+)x(\d+)", arg or "")
    if m:
        W, H = int(m.group(1)), int(m.group(2))
    st = Sampler()
    st.sample()
    time.sleep(0.5)
    st.sample()
    time.sleep(0.5)
    st.sample()
    app = App(st)
    scr = FakeScreen(H, W)
    app.draw(scr)
    safe_print(scr.dump())


def safe_print(text):
    """print, который не падает, если вывод обрезан (gtop --help | head)."""
    try:
        print(text)
        sys.stdout.flush()
    except BrokenPipeError:
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass


def main():
    args = sys.argv[1:]
    if "-h" in args or "--help" in args:
        safe_print(__doc__)
        return 0
    global THEME
    if "--gray" in args or "--grey" in args:
        THEME = "gray"
    locale.setlocale(locale.LC_ALL, "")
    if "--snapshot" in args:
        i = args.index("--snapshot")
        snapshot(args[i + 1] if i + 1 < len(args) else "")
        return 0
    if "UTF" not in locale.getpreferredencoding().upper():
        print("gtop нужна UTF-8 локаль (кольца рисуются символами Брайля).\n"
              "Попробуй: LANG=en_US.UTF-8 python3 gtop.py   (на Gentoo: eselect locale list)")
        return 1
    if not sys.stdout.isatty():
        print("gtop нужно запускать в терминале (или используй --snapshot).")
        return 1
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(run)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
