"""
ContextMind v2 - workflow context tracker (Windows 8.1+, Python 3.8+)
Optional: Pillow (for real blur on the lock screen). Without it a frosted
placeholder is shown instead.
Data folder: %APPDATA%\\ContextMind
"""
import os
import sys
import csv
import json
import time
import random
import sqlite3
import hashlib
import secrets
import threading
import ctypes
from ctypes import wintypes as wt
from collections import deque
from datetime import datetime, date
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

try:
    from PIL import Image, ImageFilter, ImageGrab, ImageTk
    HAS_PIL = True
except Exception:
    HAS_PIL = False

IS_WIN = sys.platform.startswith("win")
APP_NAME = "ContextMind"
DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
DB_PATH = os.path.join(DATA_DIR, "contextmind.db")

BG = "#f3eefc"
ACCENT = "#6c3fc5"
DARK = "#2e1a5c"
LIGHT = "#e4d9f7"


# ----------------------------------------------------------------- Windows API
if IS_WIN:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.GetTickCount.restype = wt.DWORD

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]

    def get_foreground():
        """Returns (exe_name, window_title, pid) of the active window."""
        try:
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            exe = "unknown"
            h = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFO
            if h:
                pbuf = ctypes.create_unicode_buffer(1024)
                size = wt.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
                    exe = os.path.basename(pbuf.value)
                kernel32.CloseHandle(h)
            return exe, title, pid.value
        except Exception:
            return None

    def idle_seconds():
        try:
            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if user32.GetLastInputInfo(ctypes.byref(info)):
                return ((kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
        except Exception:
            pass
        return 0.0
else:  # only so the file can be tested on non-Windows
    def get_foreground():
        return "demo.exe", "Demo window", 0

    def idle_seconds():
        return 0.0


def fmt_secs(s):
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    if h:
        return "%dh %02dm" % (h, m)
    if m:
        return "%dm %02ds" % (m, sec)
    return "%ds" % sec


def ago(iso):
    try:
        s = int((datetime.now() - datetime.fromisoformat(iso)).total_seconds())
    except Exception:
        return ""
    if s < 90:
        return "just now"
    if s < 3600:
        return "%d min ago" % (s // 60)
    if s < 86400:
        return "%dh %02dm ago" % (s // 3600, (s % 3600) // 60)
    return "%d days ago" % (s // 86400)


# -------------------------------------------------------------------- Database
class DB:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.lock = threading.Lock()
        self.con = sqlite3.connect(path, check_same_thread=False)
        with self.lock:
            self.con.executescript("""
            CREATE TABLE IF NOT EXISTS usage(
                day TEXT, app TEXT, seconds REAL, PRIMARY KEY(day, app));
            CREATE TABLE IF NOT EXISTS checkpoints(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, project TEXT,
                note TEXT, next_step TEXT, context TEXT);
            CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
            """)
            self.con.commit()

    def q(self, sql, args=()):
        with self.lock:
            return self.con.execute(sql, args).fetchall()

    def x(self, sql, args=()):
        with self.lock:
            self.con.execute(sql, args)
            self.con.commit()

    def get(self, key, default=None):
        r = self.q("SELECT v FROM settings WHERE k=?", (key,))
        return r[0][0] if r else default

    def set(self, key, value):
        self.x("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (key, str(value)))

    def add_usage(self, day, app, secs):
        self.x("INSERT INTO usage(day,app,seconds) VALUES(?,?,?) "
               "ON CONFLICT(day,app) DO UPDATE SET seconds=seconds+excluded.seconds",
               (day, app, secs))

    def backup_to(self, dest):
        with self.lock:
            out = sqlite3.connect(dest)
            self.con.backup(out)
            out.close()

    # password
    def has_password(self):
        return bool(self.get("pw_hash"))

    def set_password(self, pw):
        if not pw:
            self.x("DELETE FROM settings WHERE k IN ('pw_hash','pw_salt')")
            return
        salt = secrets.token_hex(16)
        self.set("pw_salt", salt)
        self.set("pw_hash", self._hash(pw, salt))

    def check_password(self, pw):
        salt = self.get("pw_salt", "")
        return secrets.compare_digest(self._hash(pw, salt), self.get("pw_hash", ""))

    @staticmethod
    def _hash(pw, salt):
        return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                                   salt.encode("utf-8"), 100000).hex()


# --------------------------------------------------------------------- Tracker
class Tracker(threading.Thread):
    INTERVAL = 2.0

    def __init__(self, db):
        super().__init__(daemon=True)
        self.db = db
        self.paused = db.get("paused", "0") == "1"
        self.idle_limit = int(db.get("idle_minutes", "3")) * 60
        self.current = ("-", "-")
        self.is_idle = False
        self.last_active_ts = None
        self.recent = deque(maxlen=60)  # (app, title)
        self.own_pid = os.getpid()
        self._since_save = 0.0
        self._halt = threading.Event()

    def stop(self):
        self._halt.set()

    def run(self):
        last = time.time()
        while not self._halt.is_set():
            time.sleep(self.INTERVAL)
            now = time.time()
            dt = min(now - last, self.INTERVAL * 2.5)
            last = now
            if self.paused:
                continue
            try:
                self.is_idle = idle_seconds() > self.idle_limit
                if self.is_idle:
                    continue
                fg = get_foreground()
                if not fg:
                    continue
                app, title, pid = fg
                if pid == self.own_pid:  # ignore ContextMind's own windows / lock screen
                    continue
                item = (app, title)
                self.current = item
                if not self.recent or self.recent[-1] != item:
                    self.recent.append(item)
                self.last_active_ts = now
                self.db.add_usage(date.today().isoformat(), app, dt)
                self._since_save += dt
                if self._since_save >= 10:
                    self._since_save = 0.0
                    self.save_last_active()
            except Exception:
                pass

    def recent_context(self, n=6):
        seen, out = set(), []
        for app, title in reversed(self.recent):
            if app in seen:
                continue
            seen.add(app)
            out.append({"app": app, "title": title})
            if len(out) >= n:
                break
        return out

    def snapshot(self):
        if self.last_active_ts is None:
            return None
        return {"ts": datetime.fromtimestamp(self.last_active_ts).isoformat(timespec="seconds"),
                "apps": self.recent_context(5)}

    def save_last_active(self):
        snap = self.snapshot()
        if snap:
            self.db.set("last_active", json.dumps(snap))


# ------------------------------------------------------------- password dialog
def ask_password(root, title, prompt):
    """Modal password box. Works even when root is hidden. Returns str or None."""
    result = {"v": None}
    win = tk.Toplevel(root)
    win.title(title)
    win.configure(bg=BG)
    win.resizable(False, False)
    tk.Label(win, text=prompt, bg=BG, fg=DARK, font=("Segoe UI", 10)).pack(padx=20, pady=(16, 6))
    e = tk.Entry(win, show="*", width=28, font=("Segoe UI", 11))
    e.pack(padx=20, pady=4)

    def ok(_=None):
        result["v"] = e.get()
        win.destroy()

    def cancel(_=None):
        win.destroy()

    row = tk.Frame(win, bg=BG)
    row.pack(pady=12)
    tk.Button(row, text="OK", width=9, bg=ACCENT, fg="white", command=ok).pack(side="left", padx=5)
    tk.Button(row, text="Cancel", width=9, command=cancel).pack(side="left", padx=5)
    win.bind("<Return>", ok)
    win.bind("<Escape>", cancel)
    win.protocol("WM_DELETE_WINDOW", cancel)
    win.update_idletasks()
    x = (win.winfo_screenwidth() - win.winfo_width()) // 2
    y = (win.winfo_screenheight() - win.winfo_height()) // 2
    win.geometry("+%d+%d" % (x, y))
    win.attributes("-topmost", True)
    win.grab_set()
    e.focus_force()
    root.wait_window(win)
    return result["v"]


# ----------------------------------------------------------------- Lock screen
class LockScreen(tk.Toplevel):
    """Full-screen blurred overlay. Asks password, then shows last activity."""

    SUBTITLES = {
        "idle": "No activity detected - screen locked",
        "startup": "Welcome back - ContextMind is locked",
        "manual": "Locked",
    }

    def __init__(self, app, reason, snapshot, on_close):
        super().__init__(app)
        self.withdraw()  # stay hidden until the screenshot is taken
        self.app = app
        self.db = app.db
        self.reason = reason
        self.snapshot = snapshot
        self.on_close = on_close
        self.entry = None
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self._bg = self._make_bg(sw, sh)

        self.configure(bg=DARK)
        self.protocol("WM_DELETE_WINDOW", lambda: None)  # ignore Alt+F4
        self.canvas = tk.Canvas(self, highlightthickness=0, bg=DARK)
        self.canvas.pack(fill="both", expand=True)
        if self._bg is not None:
            self.canvas.create_image(0, 0, anchor="nw", image=self._bg)
        else:
            self._placeholder(sw, sh)
        self.card = tk.Frame(self.canvas, bg=DARK, padx=36, pady=28,
                             highlightthickness=2, highlightbackground=ACCENT)
        self.canvas.create_window(sw // 2, sh // 2, window=self.card)
        self.show_login()

        self.attributes("-fullscreen", True)
        self.attributes("-topmost", True)
        self.deiconify()
        self.lift()
        self.after(250, self._focus)

    # background
    def _make_bg(self, sw, sh):
        if not HAS_PIL:
            return None
        try:
            img = ImageGrab.grab().convert("RGB")
            small = img.resize((max(1, sw // 8), max(1, sh // 8)))
            small = small.filter(ImageFilter.GaussianBlur(2.5))
            res = getattr(Image, "Resampling", Image)
            big = small.resize((sw, sh), res.BILINEAR)
            tint = Image.new("RGB", (sw, sh), (46, 26, 92))
            return ImageTk.PhotoImage(Image.blend(big, tint, 0.5), master=self)
        except Exception:
            return None

    def _placeholder(self, sw, sh):
        rnd = random.Random(7)
        colors = ["#6c3fc5", "#8b63d9", "#4a2a94", "#a98be6"]
        for _ in range(16):
            w, h = rnd.randint(180, 460), rnd.randint(40, 150)
            x, y = rnd.randint(0, max(1, sw - w)), rnd.randint(0, max(1, sh - h))
            self.canvas.create_rectangle(x, y, x + w, y + h, fill=rnd.choice(colors),
                                         outline="", stipple="gray50")

    def _focus(self):
        try:
            self.focus_force()
            if self.entry is not None:
                self.entry.focus_force()
            self.grab_set()
        except Exception:
            pass

    def _clear(self):
        for w in self.card.winfo_children():
            w.destroy()

    # views
    def show_login(self):
        self._clear()
        c = self.card
        tk.Label(c, text="ContextMind", bg=DARK, fg="white",
                 font=("Segoe UI", 24, "bold")).pack()
        tk.Label(c, text=self.SUBTITLES.get(self.reason, "Locked"), bg=DARK, fg=LIGHT,
                 font=("Segoe UI", 11)).pack(pady=(2, 14))
        tk.Label(c, text="Enter password to see your last activity", bg=DARK, fg="white",
                 font=("Segoe UI", 11)).pack(pady=(0, 6))
        self.entry = tk.Entry(c, show="*", width=26, font=("Segoe UI", 14), justify="center")
        self.entry.pack(pady=4)
        self.msg = tk.Label(c, text="", bg=DARK, fg="#ff9aa2", font=("Segoe UI", 10))
        self.msg.pack(pady=2)
        tk.Button(c, text="Unlock", width=14, bg=ACCENT, fg="white", relief="flat",
                  font=("Segoe UI", 11, "bold"), command=self.try_unlock).pack(pady=(8, 0))
        self.entry.bind("<Return>", lambda e: self.try_unlock())
        self.entry.focus_set()

    def try_unlock(self):
        pw = self.entry.get()
        if self.db.check_password(pw):
            self.show_details()
        else:
            self.msg.config(text="Wrong password")
            self.entry.delete(0, "end")

    def show_details(self):
        self._clear()
        c = self.card
        tk.Label(c, text="Your last activity", bg=DARK, fg="white",
                 font=("Segoe UI", 20, "bold")).pack(pady=(0, 10))
        lines = []
        snap = self.snapshot
        if snap and snap.get("apps"):
            lines.append("Last active: %s  (%s)" % (snap.get("ts", "").replace("T", " "),
                                                   ago(snap.get("ts", ""))))
            lines.append("")
            lines.append("You were using:")
            for a in snap["apps"]:
                lines.append("   %s  -  %s" % (a.get("app"), (a.get("title") or "")[:70]))
        else:
            lines.append("No earlier activity recorded yet.")
        cp = self.db.q("SELECT ts,project,note,next_step FROM checkpoints ORDER BY id DESC LIMIT 1")
        if cp:
            ts, project, note, nxt = cp[0]
            lines += ["", "Last checkpoint (%s)" % ts,
                      "   Project   : %s" % (project or "-"),
                      "   Note      : %s" % ((note or "-").replace("\n", " ")[:120]),
                      "   Next step : %s" % ((nxt or "-").replace("\n", " ")[:120])]
        tot = self.db.q("SELECT SUM(seconds) FROM usage WHERE day=?", (date.today().isoformat(),))
        lines += ["", "Tracked today: %s" % fmt_secs(tot[0][0] or 0)]
        tk.Label(c, text="\n".join(lines), bg=DARK, fg="white", justify="left", anchor="w",
                 font=("Consolas", 11), wraplength=620).pack(anchor="w")
        tk.Button(c, text="Continue", width=16, bg=ACCENT, fg="white", relief="flat",
                  font=("Segoe UI", 11, "bold"), command=self.close).pack(pady=(16, 0))

    def close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        self.on_close()


# ------------------------------------------------------------------------- App
class App(tk.Tk):
    def __init__(self, minimized=False):
        super().__init__()
        self.title("ContextMind")
        self.geometry("920x680")
        self.minsize(820, 600)
        self.configure(bg=BG)
        self.minimized = minimized
        self.db = DB()
        self.prev_last = self._load_json(self.db.get("last_active"))  # before this session
        self.tracker = Tracker(self.db)
        self.tracker.start()
        self.lock_win = None
        self._n = 0

        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.iconify)  # X = minimize, tracking continues
        self.refresh_dashboard()

        if self.db.has_password():
            self.withdraw()
            self.after(150, lambda: self.show_lock("startup"))
        elif minimized:
            self.iconify()
        else:
            self.after(900, self.first_run_prompt)
        self.after(2000, self._tick)

    @staticmethod
    def _load_json(s):
        try:
            return json.loads(s) if s else None
        except ValueError:
            return None

    # ---- styling
    def _style(self):
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", padding=(16, 7), font=("Segoe UI", 10, "bold"),
                    background=LIGHT, foreground=DARK)
        s.map("TNotebook.Tab", background=[("selected", ACCENT)],
              foreground=[("selected", "white")])
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=DARK, font=("Segoe UI", 10))
        s.configure("H.TLabel", font=("Segoe UI", 12, "bold"))
        s.configure("TButton", font=("Segoe UI", 10), padding=6)
        s.configure("Accent.TButton", background=ACCENT, foreground="white")
        s.map("Accent.TButton", background=[("active", DARK)])
        s.configure("TCheckbutton", background=BG, foreground=DARK)
        s.configure("Treeview", rowheight=26, font=("Segoe UI", 10))
        s.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"),
                    background=ACCENT, foreground="white")
        s.map("Treeview", background=[("selected", ACCENT)])

    # ---- UI
    def _build(self):
        top = tk.Frame(self, bg=ACCENT)
        top.pack(fill="x")
        tk.Label(top, text="  ContextMind", bg=ACCENT, fg="white",
                 font=("Segoe UI", 16, "bold")).pack(side="left", pady=10)
        self.status_lbl = tk.Label(top, text="", bg=ACCENT, fg="white", font=("Segoe UI", 10))
        self.status_lbl.pack(side="right", padx=14)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        self.tab_dash = ttk.Frame(nb)
        self.tab_cp = ttk.Frame(nb)
        self.tab_hist = ttk.Frame(nb)
        self.tab_set = ttk.Frame(nb)
        nb.add(self.tab_dash, text="Dashboard")
        nb.add(self.tab_cp, text="Checkpoints")
        nb.add(self.tab_hist, text="Usage History")
        nb.add(self.tab_set, text="Settings")
        self._build_dash()
        self._build_cp()
        self._build_hist()
        self._build_settings()

    # Dashboard
    def _build_dash(self):
        f = self.tab_dash
        cards = ttk.Frame(f)
        cards.pack(fill="x", padx=10, pady=10)
        self.lbl_total = self._card(cards, "Tracked today")
        self.lbl_apps = self._card(cards, "Apps used today")
        self.lbl_now = self._card(cards, "Last active app", small=True)

        ttk.Label(f, text="Top apps today", style="H.TLabel").pack(anchor="w", padx=12)
        self.canvas = tk.Canvas(f, bg="white", height=230, highlightthickness=1,
                                highlightbackground=LIGHT)
        self.canvas.pack(fill="x", padx=12, pady=6)

        ttk.Label(f, text="Recent checkpoints", style="H.TLabel").pack(anchor="w", padx=12, pady=(6, 0))
        self.dash_cp = ttk.Treeview(f, columns=("time", "project", "note"), show="headings", height=4)
        for c, w in (("time", 140), ("project", 160), ("note", 500)):
            self.dash_cp.heading(c, text=c.title())
            self.dash_cp.column(c, width=w, anchor="w")
        self.dash_cp.pack(fill="x", padx=12, pady=6)

    def _card(self, parent, title, small=False):
        box = tk.Frame(parent, bg="white", highlightthickness=1, highlightbackground=LIGHT)
        box.pack(side="left", expand=True, fill="both", padx=6)
        tk.Label(box, text=title, bg="white", fg=DARK, font=("Segoe UI", 10)).pack(pady=(10, 0))
        lbl = tk.Label(box, text="-", bg="white", fg=ACCENT,
                       font=("Segoe UI", 11 if small else 20, "bold"), wraplength=240)
        lbl.pack(pady=(2, 10))
        return lbl

    def refresh_dashboard(self):
        today = date.today().isoformat()
        rows = self.db.q("SELECT app, seconds FROM usage WHERE day=? ORDER BY seconds DESC", (today,))
        total = sum(r[1] for r in rows)
        self.lbl_total.config(text=fmt_secs(total))
        self.lbl_apps.config(text=str(len(rows)))
        t = self.tracker
        if t.paused:
            now = "Tracking paused"
        elif t.is_idle:
            now = "Idle"
        else:
            now = t.current[0]
        self.lbl_now.config(text=now)
        self.status_lbl.config(text="Tracking: " + ("PAUSED" if t.paused else "ON"))
        if hasattr(self, "btn_track"):
            self.btn_track.config(text="Start tracking" if t.paused else "Pause tracking")

        c = self.canvas
        c.delete("all")
        top = rows[:8]
        if not top:
            c.create_text(20, 20, anchor="nw", fill=DARK, font=("Segoe UI", 10),
                          text="No data yet - use some apps, it will appear here.")
        else:
            mx = max(r[1] for r in top) or 1
            for i, (app, secs) in enumerate(top):
                y = 10 + i * 27
                c.create_text(10, y + 11, anchor="w", text=app, fill=DARK, font=("Segoe UI", 9))
                w = max(4, int((secs / mx) * 430))
                c.create_rectangle(170, y, 170 + w, y + 22, fill=ACCENT, outline="")
                c.create_text(178 + w, y + 11, anchor="w", text=fmt_secs(secs),
                              fill=DARK, font=("Segoe UI", 9))

        for i in self.dash_cp.get_children():
            self.dash_cp.delete(i)
        for r in self.db.q("SELECT ts, project, note FROM checkpoints ORDER BY id DESC LIMIT 4"):
            self.dash_cp.insert("", "end", values=(r[0], r[1], r[2].replace("\n", " ")[:90]))

    # Checkpoints
    def _build_cp(self):
        f = self.tab_cp
        form = ttk.Frame(f)
        form.pack(fill="x", padx=12, pady=10)
        ttk.Label(form, text="Project / Work name").grid(row=0, column=0, sticky="w")
        self.cp_project = ttk.Entry(form, width=50)
        self.cp_project.grid(row=1, column=0, sticky="we", pady=(0, 6))
        ttk.Label(form, text="Note (what you were doing)").grid(row=2, column=0, sticky="w")
        self.cp_note = tk.Text(form, height=3, width=60, font=("Segoe UI", 10))
        self.cp_note.grid(row=3, column=0, sticky="we", pady=(0, 6))
        ttk.Label(form, text="Next step (to resume later)").grid(row=4, column=0, sticky="w")
        self.cp_next = tk.Text(form, height=2, width=60, font=("Segoe UI", 10))
        self.cp_next.grid(row=5, column=0, sticky="we", pady=(0, 6))
        form.columnconfigure(0, weight=1)
        ttk.Button(form, text="Save checkpoint", style="Accent.TButton",
                   command=self.save_checkpoint).grid(row=6, column=0, sticky="w")

        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=12)
        ttk.Label(bar, text="Search:").pack(side="left")
        self.cp_search = tk.StringVar()
        self.cp_search.trace_add("write", lambda *a: self.load_checkpoints())
        ttk.Entry(bar, textvariable=self.cp_search, width=35).pack(side="left", padx=6)
        ttk.Button(bar, text="Open / Resume", command=self.open_checkpoint).pack(side="right")
        ttk.Button(bar, text="Delete", command=self.delete_checkpoint).pack(side="right", padx=6)

        self.cp_tree = ttk.Treeview(f, columns=("id", "time", "project", "note"),
                                    show="headings", height=9)
        for c, w in (("id", 40), ("time", 140), ("project", 160), ("note", 480)):
            self.cp_tree.heading(c, text=c.title())
            self.cp_tree.column(c, width=w, anchor="w")
        self.cp_tree.pack(fill="both", expand=True, padx=12, pady=8)
        self.cp_tree.bind("<Double-1>", lambda e: self.open_checkpoint())
        self.load_checkpoints()

    def save_checkpoint(self):
        project = self.cp_project.get().strip()
        note = self.cp_note.get("1.0", "end").strip()
        nxt = self.cp_next.get("1.0", "end").strip()
        if not project and not note:
            messagebox.showwarning("ContextMind", "Work name or note enter pannunga.")
            return
        ctx = json.dumps(self.tracker.recent_context())
        self.db.x("INSERT INTO checkpoints(ts,project,note,next_step,context) VALUES(?,?,?,?,?)",
                  (datetime.now().strftime("%Y-%m-%d %H:%M"), project, note, nxt, ctx))
        self.cp_project.delete(0, "end")
        self.cp_note.delete("1.0", "end")
        self.cp_next.delete("1.0", "end")
        self.load_checkpoints()
        self.refresh_dashboard()
        messagebox.showinfo("ContextMind", "Checkpoint saved.")

    def load_checkpoints(self):
        for i in self.cp_tree.get_children():
            self.cp_tree.delete(i)
        s = "%" + self.cp_search.get().strip() + "%"
        rows = self.db.q(
            "SELECT id,ts,project,note FROM checkpoints WHERE project LIKE ? OR note LIKE ? "
            "OR next_step LIKE ? OR context LIKE ? OR ts LIKE ? ORDER BY id DESC",
            (s, s, s, s, s))
        for r in rows:
            self.cp_tree.insert("", "end", values=(r[0], r[1], r[2], r[3].replace("\n", " ")[:100]))

    def _selected_cp(self):
        sel = self.cp_tree.selection()
        if not sel:
            messagebox.showinfo("ContextMind", "Oru checkpoint select pannunga.")
            return None
        return self.cp_tree.item(sel[0])["values"][0]

    def open_checkpoint(self):
        cid = self._selected_cp()
        if cid is None:
            return
        r = self.db.q("SELECT ts,project,note,next_step,context FROM checkpoints WHERE id=?", (cid,))
        if not r:
            return
        ts, project, note, nxt, ctx = r[0]
        try:
            apps = json.loads(ctx or "[]")
        except ValueError:
            apps = []
        win = tk.Toplevel(self)
        win.title("Resume - " + (project or "checkpoint"))
        win.configure(bg=BG)
        win.geometry("560x460")
        txt = tk.Text(win, wrap="word", font=("Segoe UI", 10), bg="white", padx=10, pady=10)
        txt.pack(fill="both", expand=True, padx=10, pady=10)
        body = "Project : %s\nSaved   : %s\n\nNOTE\n%s\n\nNEXT STEP\n%s\n\nAPPS OPEN AT THAT TIME\n" % (
            project, ts, note or "-", nxt or "-")
        for a in apps:
            body += "  - %s  |  %s\n" % (a.get("app"), a.get("title"))
        if not apps:
            body += "  (none recorded)\n"
        txt.insert("1.0", body)
        txt.config(state="disabled")

    def delete_checkpoint(self):
        cid = self._selected_cp()
        if cid is None:
            return
        if messagebox.askyesno("ContextMind", "Delete this checkpoint?"):
            self.db.x("DELETE FROM checkpoints WHERE id=?", (cid,))
            self.load_checkpoints()
            self.refresh_dashboard()

    # Usage history
    def _build_hist(self):
        f = self.tab_hist
        bar = ttk.Frame(f)
        bar.pack(fill="x", padx=12, pady=10)
        today = date.today().isoformat()
        self.h_from = tk.StringVar(value=today)
        self.h_to = tk.StringVar(value=today)
        self.h_app = tk.StringVar()
        ttk.Label(bar, text="From (YYYY-MM-DD)").grid(row=0, column=0, sticky="w")
        ttk.Label(bar, text="To").grid(row=0, column=1, sticky="w", padx=8)
        ttk.Label(bar, text="App contains").grid(row=0, column=2, sticky="w", padx=8)
        ttk.Entry(bar, textvariable=self.h_from, width=14).grid(row=1, column=0)
        ttk.Entry(bar, textvariable=self.h_to, width=14).grid(row=1, column=1, padx=8)
        ttk.Entry(bar, textvariable=self.h_app, width=20).grid(row=1, column=2, padx=8)
        ttk.Button(bar, text="Show", style="Accent.TButton",
                   command=self.load_history).grid(row=1, column=3, padx=8)
        self.h_total = ttk.Label(f, text="", style="H.TLabel")
        self.h_total.pack(anchor="w", padx=12)
        self.h_tree = ttk.Treeview(f, columns=("day", "app", "time"), show="headings")
        for c, w in (("day", 140), ("app", 360), ("time", 160)):
            self.h_tree.heading(c, text=c.title())
            self.h_tree.column(c, width=w, anchor="w")
        self.h_tree.pack(fill="both", expand=True, padx=12, pady=8)
        self.load_history()

    def load_history(self):
        for i in self.h_tree.get_children():
            self.h_tree.delete(i)
        rows = self.db.q(
            "SELECT day, app, seconds FROM usage WHERE day>=? AND day<=? AND app LIKE ? "
            "ORDER BY day DESC, seconds DESC",
            (self.h_from.get().strip(), self.h_to.get().strip(), "%" + self.h_app.get().strip() + "%"))
        for d, a, s in rows:
            self.h_tree.insert("", "end", values=(d, a, fmt_secs(s)))
        self.h_total.config(text="Total: " + fmt_secs(sum(r[2] for r in rows)))

    # Settings
    def _build_settings(self):
        f = self.tab_set
        pad = {"padx": 14, "pady": 4, "anchor": "w"}

        ttk.Label(f, text="Tracking", style="H.TLabel").pack(**pad)
        row = ttk.Frame(f)
        row.pack(**pad)
        self.btn_track = ttk.Button(row, text="Pause tracking", style="Accent.TButton",
                                    command=self.toggle_tracking)
        self.btn_track.pack(side="left")
        ttk.Label(row, text="   Stop counting after idle (minutes):").pack(side="left")
        self.idle_var = tk.IntVar(value=int(self.db.get("idle_minutes", "3")))
        ttk.Spinbox(row, from_=1, to=60, width=4, textvariable=self.idle_var,
                    command=self.set_idle).pack(side="left", padx=4)
        self.idle_var.trace_add("write", lambda *a: self.set_idle())

        self.auto_var = tk.BooleanVar(value=self.db.get("autostart", "0") == "1")
        ttk.Checkbutton(f, text="Start ContextMind when Windows starts",
                        variable=self.auto_var, command=self.toggle_autostart).pack(**pad)

        ttk.Label(f, text="Password & lock screen", style="H.TLabel").pack(**pad)
        row = ttk.Frame(f)
        row.pack(**pad)
        ttk.Button(row, text="Set / Change password", command=self.change_password).pack(side="left")
        ttk.Button(row, text="Remove password", command=self.remove_password).pack(side="left", padx=6)
        ttk.Button(row, text="Lock now", command=lambda: self.show_lock("manual")).pack(side="left")

        row = ttk.Frame(f)
        row.pack(**pad)
        self.lock_en = tk.BooleanVar(value=self.db.get("lock_idle", "1") == "1")
        ttk.Checkbutton(row, text="Lock screen when no mouse/keyboard activity for",
                        variable=self.lock_en, command=self.save_lock_settings).pack(side="left")
        self.lock_min = tk.IntVar(value=int(self.db.get("lock_idle_minutes", "2")))
        ttk.Spinbox(row, from_=1, to=120, width=4, textvariable=self.lock_min,
                    command=self.save_lock_settings).pack(side="left", padx=4)
        ttk.Label(row, text="minutes").pack(side="left")
        self.lock_min.trace_add("write", lambda *a: self.save_lock_settings())

        ttk.Label(f, text="Export / Backup", style="H.TLabel").pack(**pad)
        row = ttk.Frame(f)
        row.pack(**pad)
        ttk.Button(row, text="Export usage (CSV)", command=self.export_usage).pack(side="left")
        ttk.Button(row, text="Export checkpoints (CSV)", command=self.export_cp).pack(side="left", padx=6)
        ttk.Button(row, text="Export all (JSON)", command=self.export_json).pack(side="left")
        ttk.Button(row, text="Backup database", command=self.backup_db).pack(side="left", padx=6)

        ttk.Label(f, text="Window X button minimizes the app (tracking continues).",
                  foreground="#7a6a9a").pack(**pad)
        ttk.Button(f, text="Exit ContextMind completely", command=self.quit_app).pack(**pad)
        ttk.Label(f, text="Data folder: " + DATA_DIR, foreground="#7a6a9a").pack(**pad)

    def toggle_tracking(self):
        self.tracker.paused = not self.tracker.paused
        self.db.set("paused", "1" if self.tracker.paused else "0")
        self.refresh_dashboard()

    def set_idle(self):
        try:
            m = max(1, int(self.idle_var.get()))
        except (ValueError, tk.TclError):
            return
        self.db.set("idle_minutes", m)
        self.tracker.idle_limit = m * 60

    def save_lock_settings(self):
        try:
            m = max(1, int(self.lock_min.get()))
        except (ValueError, tk.TclError):
            return
        self.db.set("lock_idle_minutes", m)
        self.db.set("lock_idle", "1" if self.lock_en.get() else "0")

    def toggle_autostart(self):
        on = self.auto_var.get()
        if not IS_WIN:
            messagebox.showinfo("ContextMind", "Windows-la mattum work aagum.")
            self.auto_var.set(False)
            return
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                 winreg.KEY_SET_VALUE)
            if on:
                if getattr(sys, "frozen", False):
                    cmd = '"%s" --minimized' % sys.executable
                else:
                    pyw = sys.executable.replace("python.exe", "pythonw.exe")
                    cmd = '"%s" "%s" --minimized' % (pyw, os.path.abspath(__file__))
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
            self.db.set("autostart", "1" if on else "0")
        except Exception as ex:
            messagebox.showerror("ContextMind", "Startup setting failed: %s" % ex)
            self.auto_var.set(not on)

    # password + lock flows
    def first_run_prompt(self):
        if self.db.get("asked_pw") == "1" or self.db.has_password():
            return
        self.db.set("asked_pw", "1")
        if messagebox.askyesno("ContextMind",
                               "Lock screen use panna password venum.\nIppove password set pannalaamaa?"):
            self.change_password()

    def change_password(self):
        if self.db.has_password():
            old = ask_password(self, "ContextMind", "Current password:")
            if old is None or not self.db.check_password(old):
                messagebox.showerror("ContextMind", "Wrong password.")
                return
        p1 = ask_password(self, "ContextMind", "New password:")
        if not p1:
            return
        p2 = ask_password(self, "ContextMind", "Confirm new password:")
        if p1 != p2:
            messagebox.showerror("ContextMind", "Passwords match aagala.")
            return
        self.db.set_password(p1)
        messagebox.showinfo("ContextMind", "Password set.")

    def remove_password(self):
        if not self.db.has_password():
            return
        old = ask_password(self, "ContextMind", "Current password:")
        if old is not None and self.db.check_password(old):
            self.db.set_password("")
            messagebox.showinfo("ContextMind", "Password removed.")
        else:
            messagebox.showerror("ContextMind", "Wrong password.")

    def show_lock(self, reason):
        if self.lock_win is not None:
            return
        if not self.db.has_password():
            if reason == "manual":
                messagebox.showinfo("ContextMind", "Mudhalla password set pannunga.")
            return
        if reason == "startup":
            snap = self.prev_last
        else:
            snap = self.tracker.snapshot() or self.prev_last

        def done():
            self.lock_win = None
            if reason == "startup":
                self.deiconify()
                if self.minimized:
                    self.iconify()

        try:
            self.lock_win = LockScreen(self, reason, snap, done)
        except Exception:
            self.lock_win = None
            if reason == "startup":  # never leave the user without a window
                self.deiconify()

    # export / backup
    def _save_dialog(self, name, ext):
        return filedialog.asksaveasfilename(defaultextension=ext, initialfile=name,
                                            filetypes=[(ext.upper(), "*" + ext)])

    def export_usage(self):
        p = self._save_dialog("contextmind_usage.csv", ".csv")
        if not p:
            return
        with open(p, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["day", "app", "seconds", "time"])
            for d, a, s in self.db.q("SELECT day,app,seconds FROM usage ORDER BY day,app"):
                w.writerow([d, a, int(s), fmt_secs(s)])
        messagebox.showinfo("ContextMind", "Exported.")

    def export_cp(self):
        p = self._save_dialog("contextmind_checkpoints.csv", ".csv")
        if not p:
            return
        with open(p, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "time", "project", "note", "next_step", "context"])
            for r in self.db.q("SELECT id,ts,project,note,next_step,context FROM checkpoints"):
                w.writerow(r)
        messagebox.showinfo("ContextMind", "Exported.")

    def export_json(self):
        p = self._save_dialog("contextmind_all.json", ".json")
        if not p:
            return
        data = {
            "usage": [dict(day=d, app=a, seconds=s) for d, a, s in
                      self.db.q("SELECT day,app,seconds FROM usage")],
            "checkpoints": [dict(id=r[0], time=r[1], project=r[2], note=r[3],
                                 next_step=r[4], context=r[5]) for r in
                            self.db.q("SELECT id,ts,project,note,next_step,context FROM checkpoints")],
        }
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        messagebox.showinfo("ContextMind", "Exported.")

    def backup_db(self):
        p = self._save_dialog("contextmind_backup_%s.db" % date.today().isoformat(), ".db")
        if not p:
            return
        self.db.backup_to(p)
        messagebox.showinfo("ContextMind", "Backup saved.")

    # loop / quit
    def _tick(self):
        try:
            self._n += 1
            if (self.lock_win is None and self.db.get("lock_idle", "1") == "1"
                    and self.db.has_password()):
                limit = int(self.db.get("lock_idle_minutes", "2")) * 60
                if idle_seconds() > limit:
                    self.show_lock("idle")
            if self._n % 3 == 0 and self.lock_win is None and self.state() == "normal":
                self.refresh_dashboard()
        except Exception:
            pass
        finally:
            self.after(2000, self._tick)

    def quit_app(self):
        if messagebox.askyesno("ContextMind", "Exit? Tracking nillu pogum."):
            self.tracker.save_last_active()
            self.tracker.stop()
            self.destroy()


if __name__ == "__main__":
    App(minimized="--minimized" in sys.argv).mainloop()
