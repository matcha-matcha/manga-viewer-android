#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
まんがビューワー サーバー (標準ライブラリのみで動作 / Pillow があれば画像を軽量化)
 - ZIP/CBZ はサーバー側で「展開せず」ページ単位で直接読み出し
 - RAR/CBR/7Z は 7-Zip (または Windows 標準 tar.exe) で一時展開し、展開中でも先頭ページから表示
"""
import os, sys, json, re, io, time, threading, hashlib, hmac, secrets, shutil, subprocess, zipfile, socket, unicodedata
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from collections import OrderedDict

try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 300_000_000
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

VERSION = "1.4"
FROZEN = bool(getattr(sys, "frozen", False))      # exe(PyInstaller)で動作中
if FROZEN:
    BASE = os.path.dirname(os.path.abspath(sys.executable))
    STATIC = os.path.join(getattr(sys, "_MEIPASS", BASE), "static")
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    STATIC = os.path.join(BASE, "static")
DATA = os.path.join(BASE, "data")
SETTINGS_FILE = os.path.join(DATA, "settings.json")
PROGRESS_FILE = os.path.join(DATA, "progress.json")
INDEX_FILE = os.path.join(DATA, "index.json")
BOOKMARK_FILE = os.path.join(DATA, "bookmarks.json")
os.makedirs(DATA, exist_ok=True)

IS_WIN = os.name == "nt"
NOWIN = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}
ZIP_EXT = {".zip", ".cbz"}
EXT_EXT = {".rar", ".cbr", ".7z", ".cb7"}
PDF_EXT = {".pdf"}
ARC_EXT = ZIP_EXT | EXT_EXT | PDF_EXT
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
        ".gif": "image/gif", ".bmp": "image/bmp", ".avif": "image/avif"}
STATIC_MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
               ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
               ".ico": "image/x-icon", ".json": "application/json; charset=utf-8",
               ".webmanifest": "application/manifest+json"}

DEFAULTS = {
    "library_path": "",          # 例: \\\\NAS\\manga  /  Z:\\manga
    "setup_done": False,         # 初回セットアップ(ウィザード)完了フラグ
    "host": "0.0.0.0",
    "port": 8765,
    "password": "",
    "secret": "",
    "cache_dir": os.path.join(BASE, "cache"),
    "cache_max_gb": 5.0,
    "sevenzip_path": "",
    "resize_enabled": True,      # 端末の画面幅に合わせて縮小して配信(転送量・表示を軽く)
    "resize_quality": 82,
    "resize_max_width": 2400,
    "extract_parallel": 2,
    "book_cache_size": 12,
}

S = dict(DEFAULTS)
S_LOCK = threading.Lock()


def load_settings():
    global S
    d = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            j = json.load(f)
        if "setup_done" not in j and j.get("library_path"):
            j["setup_done"] = True
        d.update(j)
    except FileNotFoundError:
        pass
    except Exception as e:
        print("settings.json 読み込み失敗:", e)
    if not d.get("secret"):
        d["secret"] = secrets.token_hex(24)
    if not d.get("server_id"):
        d["server_id"] = secrets.token_hex(8)   # アプリが「同じサーバー」を見つけ直すための識別子
    S = d
    save_settings()


def save_settings():
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(S, f, ensure_ascii=False, indent=2)
    os.replace(tmp, SETTINGS_FILE)


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


def natkey(s):
    s = unicodedata.normalize("NFKC", s).lower()
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def sha(s):
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()


def cache_root():
    d = S["cache_dir"] or DEFAULTS["cache_dir"]
    os.makedirs(d, exist_ok=True)
    return d


def cache_sub(name):
    d = os.path.join(cache_root(), name)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- パス解決
def resolve(rel):
    root = S["library_path"]
    if not root:
        raise ApiError(400, "ライブラリのパスが未設定です。設定画面で入力してください。")
    rel = (rel or "").replace("\\", "/").strip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    for p in parts:
        if p == ".." or ":" in p or "\x00" in p:
            raise ApiError(403, "不正なパスです")
    return os.path.join(root, *parts) if parts else root


# ---------------------------------------------------------------- 外部ツール
_TOOLS = {"t": 0, "v": None}


def find_tools():
    now = time.time()
    if _TOOLS["v"] and now - _TOOLS["t"] < 30:
        return _TOOLS["v"]
    sz = None
    cands = []
    if S.get("sevenzip_path"):
        cands.append(S["sevenzip_path"])
    if FROZEN:      # exe同梱の 7-Zip
        cands.append(os.path.join(BASE, "tools", "7z.exe"))
    for n in ("7z", "7zz", "7za"):
        w = shutil.which(n)
        if w:
            cands.append(w)
    if IS_WIN:
        for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            b = os.environ.get(env)
            if b:
                cands.append(os.path.join(b, "7-Zip", "7z.exe"))
    for c in cands:
        if c and os.path.isfile(c):
            sz = c
            break
    tar = None
    tc = []
    w = shutil.which("bsdtar")
    if w:
        tc.append(w)
    if IS_WIN:
        tc.append(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe"))
    else:
        w = shutil.which("tar")
        if w:
            tc.append(w)
    for c in tc:
        try:
            if os.path.isfile(c):
                out = subprocess.run([c, "--version"], capture_output=True, timeout=10, creationflags=NOWIN).stdout.decode("utf-8", "replace")
                if "bsdtar" in out or "libarchive" in out:
                    tar = c
                    break
        except Exception:
            pass
    _TOOLS["v"] = {"sevenzip": sz, "tar": tar}
    _TOOLS["t"] = now
    return _TOOLS["v"]


# ---------------------------------------------------------------- ブック
class Book:
    kind = ""
    def __init__(self, rel, full):
        self.rel = rel
        self.full = full
        st = os.stat(full)
        self.key = sha(f"{full}|{st.st_mtime_ns}|{st.st_size}")[:20]
        self.n = 0
        self.err = None
        self.lock = threading.Lock()

    def read(self, i):  # -> (bytes, ext)
        raise NotImplementedError

    def ready(self, i):
        return True

    def close(self):
        pass


def fix_zipname(info):
    name = info.filename
    if not (info.flag_bits & 0x800):
        try:
            name = name.encode("cp437").decode("cp932")
        except Exception:
            pass
    return name.replace("\\", "/")


def is_junk(name):
    base = name.rsplit("/", 1)[-1]
    return base.startswith(".") or "__MACOSX" in name.split("/") or base.lower() == "thumbs.db"


class ZipBook(Book):
    kind = "zip"
    def __init__(self, rel, full):
        super().__init__(rel, full)
        self.zf = zipfile.ZipFile(full)
        items = []
        for info in self.zf.infolist():
            if info.is_dir():
                continue
            name = fix_zipname(info)
            if is_junk(name):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in IMG_EXT:
                items.append((natkey(name), info, ext))
        items.sort(key=lambda t: t[0])
        self.items = [(i, e) for _, i, e in items]
        self.n = len(self.items)

    def read(self, i):
        info, ext = self.items[i]
        with self.lock:
            return self.zf.read(info), ext

    def close(self):
        with self.lock:
            try:
                self.zf.close()
            except Exception:
                pass


class DirBook(Book):
    kind = "dir"
    def __init__(self, rel, full):
        super().__init__(rel, full)
        files = []
        with os.scandir(full) as it:
            for e in it:
                ext = os.path.splitext(e.name)[1].lower()
                if ext in IMG_EXT and not e.name.startswith("."):
                    files.append(e.name)
        files.sort(key=natkey)
        self.files = files
        self.n = len(files)

    def read(self, i):
        name = self.files[i]
        with open(os.path.join(self.full, name), "rb") as f:
            return f.read(), os.path.splitext(name)[1].lower()


class PdfBook(Book):
    kind = "pdf"
    def __init__(self, rel, full):
        super().__init__(rel, full)
        try:
            try:
                import pymupdf as fz
            except ImportError:
                import fitz as fz
        except ImportError:
            raise ApiError(501, "PDFの表示にはPyMuPDFが必要です (pip install pymupdf)")
        self.fz = fz
        self.doc = fz.open(full)
        self.n = self.doc.page_count

    def read(self, i):
        ck = sha(f"pdf|{self.key}|{i}")
        cp = os.path.join(cache_sub("res"), ck[:2], ck + ".pdfp")
        if os.path.exists(cp):
            with open(cp, "rb") as f:
                return f.read(), ".jpg"
        with self.lock:
            page = self.doc[i]
            if page.rotation == 0 and not page.get_text("text").strip():
                imgs = page.get_images(full=True)
                if len(imgs) == 1:
                    try:
                        info = self.doc.extract_image(imgs[0][0])
                        e = (info or {}).get("ext", "")
                        if e in ("jpeg", "jpg", "png"):
                            return info["image"], ".jpg" if e in ("jpeg", "jpg") else ".png"
                    except Exception:
                        pass
            sc = 1800.0 / max(1.0, page.rect.width)
            pix = page.get_pixmap(matrix=self.fz.Matrix(sc, sc), alpha=False)
            try:
                data = pix.tobytes("jpg", jpg_quality=88)
            except TypeError:
                data = pix.tobytes("jpeg")
        try:
            os.makedirs(os.path.dirname(cp), exist_ok=True)
            with open(cp + ".tmp", "wb") as f:
                f.write(data)
            os.replace(cp + ".tmp", cp)
        except OSError:
            pass
        return data, ".jpg"

    def close(self):
        with self.lock:
            try:
                self.doc.close()
            except Exception:
                pass


ACTIVE = set()
ACTIVE_LOCK = threading.Lock()
EXTRACT_SEM = None


def parse_7z_list(text):
    entries = []
    sep = text.find("----------")
    if sep < 0:
        return entries
    for block in re.split(r"\r?\n\r?\n", text[sep + 10:]):
        d = {}
        for line in block.splitlines():
            if " = " in line:
                k, v = line.split(" = ", 1)
                d[k.strip()] = v
        p = d.get("Path")
        if not p:
            continue
        if d.get("Folder") == "+" or d.get("Attributes", "").startswith("D"):
            continue
        entries.append(p)
    return entries


def list_7z(exe, archive):
    p = subprocess.run([exe, "l", "-slt", "-sccUTF-8", archive], capture_output=True, timeout=300, creationflags=NOWIN)
    out = p.stdout.decode("utf-8", "replace")
    if p.returncode not in (0, 1) and "Path = " not in out:
        raise ApiError(500, "アーカイブを開けません: " + p.stderr.decode("utf-8", "replace")[:200])
    return parse_7z_list(out)


class ExtBook(Book):
    kind = "ext"
    def __init__(self, rel, full):
        super().__init__(rel, full)
        tools = find_tools()
        if not tools["sevenzip"] and not tools["tar"]:
            raise ApiError(501, "RAR/7Zの展開ツールが見つかりません。7-Zipをインストールするか、設定で7z.exeのパスを指定してください。")
        self.dest = os.path.join(cache_sub("ext"), self.key)
        self.marker = os.path.join(self.dest, ".done")
        self.listf = os.path.join(self.dest, ".pages.json")
        self.done = threading.Event()
        self.pages = []   # 表示順 (正規化パス)
        self.order = {}   # 正規化パス -> 展開順序
        self.seq = []
        if os.path.exists(self.marker) and os.path.exists(self.listf):
            try:
                with open(self.listf, "r", encoding="utf-8") as f:
                    j = json.load(f)
                self.pages, self.seq = j["pages"], j["seq"]
                self.order = {p: k for k, p in enumerate(self.seq)}
                self.n = len(self.pages)
                self.done.set()
                os.utime(self.dest, None)
                return
            except Exception:
                pass
        shutil.rmtree(self.dest, ignore_errors=True)
        os.makedirs(self.dest, exist_ok=True)
        with ACTIVE_LOCK:
            ACTIVE.add(self.key)
        if tools["sevenzip"]:
            raw = list_7z(tools["sevenzip"], full)
            imgs = []
            for p in raw:
                np_ = p.replace("\\", "/")
                if os.path.splitext(np_)[1].lower() in IMG_EXT and not is_junk(np_):
                    imgs.append(np_)
            self.seq = imgs[:]
            self.pages = sorted(imgs, key=natkey)
            self.order = {p: k for k, p in enumerate(self.seq)}
            self.n = len(self.pages)
            threading.Thread(target=self._extract, args=(tools,), daemon=True).start()
        else:
            self._extract(tools)  # tar は同期展開(順次表示不可)

    def _extract(self, tools):
        global EXTRACT_SEM
        try:
            with EXTRACT_SEM:
                if tools["sevenzip"]:
                    cmd = [tools["sevenzip"], "x", "-y", "-bd", "-bso0", "-bsp0", "-o" + self.dest, self.full]
                else:
                    cmd = [tools["tar"], "-xf", self.full, "-C", self.dest]
                p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=NOWIN)
                if p.returncode not in (0, 1):
                    msg = p.stderr.decode("utf-8", "replace")[:300]
                    if not any(os.scandir(self.dest)):
                        self.err = "展開に失敗しました: " + msg
                if not tools["sevenzip"]:
                    imgs = []
                    for root, _, fs in os.walk(self.dest):
                        for fn in fs:
                            if os.path.splitext(fn)[1].lower() in IMG_EXT and not fn.startswith("."):
                                rp = os.path.relpath(os.path.join(root, fn), self.dest).replace("\\", "/")
                                if not is_junk(rp):
                                    imgs.append(rp)
                    self.seq = sorted(imgs, key=natkey)
                    self.pages = self.seq[:]
                    self.order = {p: k for k, p in enumerate(self.seq)}
                    self.n = len(self.pages)
                if not self.err:
                    with open(self.listf, "w", encoding="utf-8") as f:
                        json.dump({"pages": self.pages, "seq": self.seq}, f, ensure_ascii=False)
                    open(self.marker, "w").close()
        except Exception as e:
            self.err = "展開エラー: " + str(e)
        finally:
            self.done.set()
            with ACTIVE_LOCK:
                ACTIVE.discard(self.key)
            try:
                trim_cache()
            except Exception:
                pass

    def _path(self, i):
        return os.path.join(self.dest, *self.pages[i].split("/"))

    def ready(self, i):
        if self.done.is_set():
            return True
        p = self._path(i)
        if not os.path.exists(p):
            return False
        k = self.order.get(self.pages[i], -1)
        if k + 1 >= len(self.seq):
            return False
        return os.path.exists(os.path.join(self.dest, *self.seq[k + 1].split("/")))

    def read(self, i):
        t0 = time.time()
        while not self.ready(i):
            if self.err:
                raise ApiError(500, self.err)
            if time.time() - t0 > 180:
                raise ApiError(504, "展開待ちでタイムアウトしました")
            time.sleep(0.04)
        if self.err and not os.path.exists(self._path(i)):
            raise ApiError(500, self.err)
        p = self._path(i)
        if not os.path.exists(p):
            raise ApiError(404, "ページが見つかりません")
        with open(p, "rb") as f:
            return f.read(), os.path.splitext(p)[1].lower()


BOOKS = OrderedDict()
BOOKS_LOCK = threading.Lock()
CREATING = {}


def make_book(rel, full):
    if os.path.isdir(full):
        return DirBook(rel, full)
    ext = os.path.splitext(full)[1].lower()
    if ext in PDF_EXT:
        return PdfBook(rel, full)
    if ext in ZIP_EXT:
        try:
            return ZipBook(rel, full)
        except zipfile.BadZipFile:
            return ExtBook(rel, full)  # 拡張子がzipでも中身がrarの場合
    if ext in EXT_EXT:
        return ExtBook(rel, full)
    raise ApiError(415, "対応していない形式です")


def get_book(rel):
    full = resolve(rel)
    try:
        st = os.stat(full)
    except OSError:
        raise ApiError(404, "見つかりません(ネットワークドライブに接続できているか確認してください)")
    k = (full, st.st_mtime_ns, st.st_size)
    with BOOKS_LOCK:
        b = BOOKS.get(k)
        if b:
            BOOKS.move_to_end(k)
            return b
        lk = CREATING.setdefault(k, threading.Lock())
    with lk:
        with BOOKS_LOCK:
            b = BOOKS.get(k)
        if b:
            return b
        try:
            b = make_book(rel, full)
        except ApiError:
            raise
        except Exception as e:
            raise ApiError(500, "開けませんでした: %s" % e)
        finally:
            with BOOKS_LOCK:
                CREATING.pop(k, None)
        with BOOKS_LOCK:
            BOOKS[k] = b
            while len(BOOKS) > max(3, int(S.get("book_cache_size", 12))):
                _, old = BOOKS.popitem(last=False)
                old.close()
        return b


# ---------------------------------------------------------------- キャッシュ管理
TRIM_LOCK = threading.Lock()


def dirsize(p):
    t = 0
    for root, _, fs in os.walk(p):
        for f in fs:
            try:
                t += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return t


def trim_cache():
    cap = int(float(S["cache_max_gb"]) * 1e9)
    if not TRIM_LOCK.acquire(blocking=False):
        return
    try:
        with BOOKS_LOCK:
            keep = {b.key for b in BOOKS.values() if isinstance(b, ExtBook)}
        with ACTIVE_LOCK:
            keep |= set(ACTIVE)
        items = []
        extd = cache_sub("ext")
        for n in os.listdir(extd):
            p = os.path.join(extd, n)
            if os.path.isdir(p):
                items.append((os.path.getmtime(p), dirsize(p), p, True, n))
        resd = cache_sub("res")
        for root, _, fs in os.walk(resd):
            for f in fs:
                p = os.path.join(root, f)
                try:
                    items.append((os.path.getmtime(p), os.path.getsize(p), p, False, ""))
                except OSError:
                    pass
        total = sum(i[1] for i in items)
        if total <= cap:
            return
        items.sort()
        for mt, sz, p, isdir, n in items:
            if total <= cap * 0.9:
                break
            if isdir:
                if n in keep:
                    continue
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)
                except OSError:
                    continue
            total -= sz
    finally:
        TRIM_LOCK.release()


# ---------------------------------------------------------------- 画像処理
RESIZE_SEM = threading.Semaphore(max(2, min(4, (os.cpu_count() or 2))))
COVER_SEM = threading.Semaphore(2)
_resize_count = [0]


def resize_image(data, w, quality):
    """元画像が w*1.15 より大きいときだけ縮小したJPEGを返す。それ以外はNone。"""
    if not HAVE_PIL:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        if getattr(im, "is_animated", False):
            return None
        if im.width <= w * 1.15:
            return None
        nh = max(1, int(im.height * w / im.width))
        if im.format == "JPEG":
            im.draft("L" if im.mode == "L" else "RGB", (w, nh))
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode == "1":
            im = im.convert("L")
        elif im.mode not in ("L", "RGB"):
            im = im.convert("RGB")
        im = im.resize((w, nh), Image.LANCZOS, reducing_gap=3.0)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality)
        return out.getvalue()
    except Exception:
        return None


def get_page_data(book, i, w):
    data, ext = book.read(i)
    ctype = MIME.get(ext, "application/octet-stream")
    if w and S.get("resize_enabled") and HAVE_PIL and ext != ".gif":
        w = max(200, min(int(w), int(S["resize_max_width"])))
        q = int(S["resize_quality"])
        ck = sha(f"{book.key}|{i}|{w}|{q}")
        cp = os.path.join(cache_sub("res"), ck[:2], ck + ".jpg")
        if os.path.exists(cp):
            with open(cp, "rb") as f:
                return f.read(), "image/jpeg"
        with RESIZE_SEM:
            r = resize_image(data, w, q)
        if r is not None and len(r) < len(data):
            try:
                os.makedirs(os.path.dirname(cp), exist_ok=True)
                with open(cp + ".tmp", "wb") as f:
                    f.write(r)
                os.replace(cp + ".tmp", cp)
                _resize_count[0] += 1
                if _resize_count[0] % 60 == 0:
                    threading.Thread(target=trim_cache, daemon=True).start()
            except OSError:
                pass
            return r, "image/jpeg"
    return data, ctype


def first_page_for_cover(rel, full, depth=0):
    if os.path.isdir(full):
        imgs, arcs, subs = [], [], []
        with os.scandir(full) as it:
            for e in it:
                if e.name.startswith(".") or e.name.startswith("$"):
                    continue
                x = os.path.splitext(e.name)[1].lower()
                if e.is_dir():
                    subs.append(e.name)
                elif x in IMG_EXT:
                    imgs.append(e.name)
                elif x in ARC_EXT:
                    arcs.append(e.name)
        if imgs:
            imgs.sort(key=natkey)
            with open(os.path.join(full, imgs[0]), "rb") as f:
                return f.read(), os.path.splitext(imgs[0])[1].lower()
        if depth < 2:
            for n in sorted(arcs, key=natkey)[:1] + sorted(subs, key=natkey)[:1]:
                r = first_page_for_cover("", os.path.join(full, n), depth + 1)
                if r:
                    return r
        return None
    ext = os.path.splitext(full)[1].lower()
    if ext in PDF_EXT:
        b = PdfBook(rel, full)
        try:
            return b.read(0) if b.n else None
        finally:
            b.close()
    if ext in ZIP_EXT:
        try:
            b = ZipBook(rel, full)
            try:
                return b.read(0) if b.n else None
            finally:
                b.close()
        except zipfile.BadZipFile:
            pass
    tools = find_tools()
    if not tools["sevenzip"]:
        return None
    # 展開済みキャッシュがあればそれを使う
    st = os.stat(full)
    key = sha(f"{full}|{st.st_mtime_ns}|{st.st_size}")[:20]
    dest = os.path.join(cache_sub("ext"), key)
    lf = os.path.join(dest, ".pages.json")
    if os.path.exists(os.path.join(dest, ".done")) and os.path.exists(lf):
        with open(lf, "r", encoding="utf-8") as f:
            j = json.load(f)
        if j["pages"]:
            p = os.path.join(dest, *j["pages"][0].split("/"))
            with open(p, "rb") as f:
                return f.read(), os.path.splitext(p)[1].lower()
    raw = list_7z(tools["sevenzip"], full)
    imgs = [p for p in raw if os.path.splitext(p)[1].lower() in IMG_EXT and not is_junk(p.replace("\\", "/"))]
    if not imgs:
        return None
    first = sorted(imgs, key=lambda p: natkey(p.replace("\\", "/")))[0]
    tmp = os.path.join(cache_sub("tmp"), sha(first + key))
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    try:
        subprocess.run([tools["sevenzip"], "x", "-y", "-bd", "-bso0", "-bsp0", "-o" + tmp, full, first],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300, creationflags=NOWIN)
        p = os.path.join(tmp, *first.replace("\\", "/").split("/"))
        if os.path.exists(p):
            with open(p, "rb") as f:
                return f.read(), os.path.splitext(p)[1].lower()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return None


def get_cover(rel):
    full = resolve(rel)
    try:
        st = os.stat(full)
    except OSError:
        raise ApiError(404, "not found")
    key = sha(f"cover|{full}|{st.st_mtime_ns}|{st.st_size}")
    tp = os.path.join(cache_sub("thumb"), key[:2], key + ".jpg")
    if os.path.exists(tp):
        with open(tp, "rb") as f:
            return f.read(), "image/jpeg"
    with COVER_SEM:
        if os.path.exists(tp):
            with open(tp, "rb") as f:
                return f.read(), "image/jpeg"
        r = first_page_for_cover(rel, full)
        if not r:
            raise ApiError(404, "no cover")
        data, ext = r
        if HAVE_PIL:
            t = resize_image(data, 360, 80)
            if t is None:
                try:
                    im = Image.open(io.BytesIO(data)).convert("RGB")
                    out = io.BytesIO()
                    im.save(out, "JPEG", quality=80)
                    t = out.getvalue()
                except Exception:
                    t = None
            if t:
                os.makedirs(os.path.dirname(tp), exist_ok=True)
                with open(tp, "wb") as f:
                    f.write(t)
                return t, "image/jpeg"
        return data, MIME.get(ext, "image/jpeg")


# ---------------------------------------------------------------- 読書進捗
PROGRESS = {}
PROG_LOCK = threading.Lock()
PROG_DIRTY = [False]


def load_progress():
    global PROGRESS
    try:
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            PROGRESS = json.load(f)
    except Exception:
        PROGRESS = {}


def flush_loop():
    while True:
        time.sleep(3)
        if PROG_DIRTY[0]:
            with PROG_LOCK:
                PROG_DIRTY[0] = False
                snap = json.dumps(PROGRESS, ensure_ascii=False)
            try:
                with open(PROGRESS_FILE + ".tmp", "w", encoding="utf-8") as f:
                    f.write(snap)
                os.replace(PROGRESS_FILE + ".tmp", PROGRESS_FILE)
            except OSError:
                pass


# ---------------------------------------------------------------- しおり(1冊に複数)
BOOKMARKS = {}
BM_LOCK = threading.Lock()


def load_bookmarks():
    global BOOKMARKS
    try:
        with open(BOOKMARK_FILE, "r", encoding="utf-8") as f:
            BOOKMARKS = json.load(f)
    except Exception:
        BOOKMARKS = {}


def save_bookmarks():
    tmp = BOOKMARK_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(BOOKMARKS, f, ensure_ascii=False)
    os.replace(tmp, BOOKMARK_FILE)


def normrel(b):
    return (b or "").replace("\\", "/").strip("/")


def api_bookmarks(q, body=None):
    b = normrel(q.get("b", [""])[0])
    with BM_LOCK:
        items = sorted(BOOKMARKS.get(b, []), key=lambda x: x["p"])
    return {"items": items}


def api_bookmark_set(q, body):
    b = normrel(body.get("b"))
    page = int(body.get("page", 0))
    if not b:
        raise ApiError(400, "bad request")
    with BM_LOCK:
        lst = BOOKMARKS.get(b, [])
        lst = [x for x in lst if x["p"] != page]
        if body.get("on", True):
            lst.append({"p": page, "ts": time.time()})
        if lst:
            BOOKMARKS[b] = lst
        else:
            BOOKMARKS.pop(b, None)
        save_bookmarks()
        items = sorted(BOOKMARKS.get(b, []), key=lambda x: x["p"])
    return {"items": items}


# ---------------------------------------------------------------- 検索インデックス
INDEX = []   # [rel, 'd'|'b']
SCAN = {"running": False, "count": 0, "ts": 0}


def load_index():
    global INDEX
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            j = json.load(f)
        INDEX = j.get("items", [])
        SCAN["ts"] = j.get("ts", 0)
    except Exception:
        INDEX = []


def scan_library():
    if SCAN["running"] or not S["library_path"]:
        return
    SCAN.update(running=True, count=0)

    def run():
        items = []
        root = S["library_path"]
        stack = [("", root)]
        try:
            while stack:
                rel, full = stack.pop()
                try:
                    with os.scandir(full) as it:
                        for e in it:
                            n = e.name
                            if n.startswith(".") or n.startswith("$") or n == "System Volume Information":
                                continue
                            r = (rel + "/" + n) if rel else n
                            try:
                                if e.is_dir():
                                    items.append([r, "d"])
                                    stack.append((r, e.path))
                                elif os.path.splitext(n)[1].lower() in ARC_EXT:
                                    items.append([r, "b"])
                            except OSError:
                                pass
                            SCAN["count"] = len(items)
                except OSError:
                    pass
            global INDEX
            INDEX = items
            SCAN["ts"] = time.time()
            with open(INDEX_FILE + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"ts": SCAN["ts"], "items": items}, f, ensure_ascii=False)
            os.replace(INDEX_FILE + ".tmp", INDEX_FILE)
        finally:
            SCAN["running"] = False

    threading.Thread(target=run, daemon=True).start()


def nname(s):
    return unicodedata.normalize("NFKC", s).lower()


# ---------------------------------------------------------------- 認証
def make_token():
    return hmac.new(S["secret"].encode(), b"manga-viewer", hashlib.sha256).hexdigest()


def authed(handler):
    if not S.get("password"):
        return True
    ck = handler.headers.get("Cookie", "")
    m = re.search(r"(?:^|;\s*)mv=([0-9a-f]+)", ck)
    return bool(m and hmac.compare_digest(m.group(1), make_token()))


# ---------------------------------------------------------------- API
def strip_ext(n):
    return os.path.splitext(n)[0]


def api_list(q, body=None):
    rel = (q.get("path", [""])[0]).replace("\\", "/").strip("/")
    full = resolve(rel)
    t0 = time.time()
    if not os.path.isdir(full):
        raise ApiError(404, "フォルダにアクセスできません。ネットワーク設定を確認してください。")
    dirs, books, nimg, dmt = [], [], 0, {}
    try:
        with os.scandir(full) as it:
            for e in it:
                n = e.name
                if n.startswith(".") or n.startswith("$") or n in ("System Volume Information", "Thumbs.db"):
                    continue
                try:
                    if e.is_dir():
                        dirs.append(n)
                        try:
                            dmt[n] = e.stat().st_mtime
                        except OSError:
                            pass
                    else:
                        ext = os.path.splitext(n)[1].lower()
                        if ext in ARC_EXT:
                            books.append((n, e.stat().st_size, e.stat().st_mtime))
                        elif ext in IMG_EXT:
                            nimg += 1
                except OSError:
                    pass
    except OSError as e:
        raise ApiError(502, "読み込みエラー: %s" % e)
    dirs.sort(key=natkey)
    books.sort(key=lambda b: natkey(b[0]))
    pre = (rel + "/") if rel else ""
    out_books = []
    with PROG_LOCK, BM_LOCK:
        for n, sz, mt in books:
            pg = PROGRESS.get(pre + n)
            out_books.append({"name": n, "size": sz, "mtime": mt, "p": pg["p"] if pg else None,
                              "l": (pg.get("l", pg["p"]) if pg else None), "t": pg["t"] if pg else None,
                              "ts": pg.get("ts") if pg else None, "bm": len(BOOKMARKS.get(pre + n, []))})
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return {"path": rel, "parent": parent if rel else None, "dirs": dirs, "dmt": dmt, "books": out_books,
            "images": nimg, "ms": int((time.time() - t0) * 1000)}


def api_book(q, body=None):
    rel = q.get("b", [""])[0]
    b = get_book(rel)
    if b.n == 0 and getattr(b, "done", None) is not None and b.done.is_set() and b.err:
        raise ApiError(500, b.err)
    with PROG_LOCK:
        pg = PROGRESS.get(rel.replace("\\", "/").strip("/"))
    return {"title": strip_ext(os.path.basename(rel.rstrip("/"))), "total": b.n, "kind": b.kind,
            "p": pg["p"] if pg else 0, "key": b.key}


def api_neighbors(q, body=None):
    rel = q.get("b", [""])[0].replace("\\", "/").strip("/")
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    full = resolve(parent)
    names = []
    with os.scandir(full) as it:
        for e in it:
            if os.path.splitext(e.name)[1].lower() in ARC_EXT and not e.name.startswith("."):
                names.append(e.name)
    names.sort(key=natkey)
    me = rel.rsplit("/", 1)[-1]
    pre = (parent + "/") if parent else ""
    try:
        k = names.index(me)
    except ValueError:
        return {"prev": None, "next": None, "parent": parent}
    return {"prev": pre + names[k - 1] if k > 0 else None,
            "next": pre + names[k + 1] if k + 1 < len(names) else None, "parent": parent}


def api_progress(q, body):
    b = (body.get("b") or "").replace("\\", "/").strip("/")
    if not b:
        raise ApiError(400, "bad request")
    with PROG_LOCK:
        pg = int(body.get("page", 0))
        PROGRESS[b] = {"p": pg, "l": int(body.get("last", pg)), "t": int(body.get("total", 0)), "ts": time.time()}
        PROG_DIRTY[0] = True
    return {"ok": True}


def api_recent(q, body=None):
    with PROG_LOCK:
        items = sorted(PROGRESS.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:40]
    out = []
    for k, v in items:
        if v["t"] and v.get("l", v["p"]) >= v["t"] - 1:
            continue  # 読了
        out.append({"path": k, "name": strip_ext(k.rsplit("/", 1)[-1]), "p": v["p"], "l": v.get("l", v["p"]), "t": v["t"], "ts": v.get("ts")})
        if len(out) >= 20:
            break
    return {"items": out}


def api_search(q, body=None):
    qs = nname(q.get("q", [""])[0]).split()
    if not qs:
        return {"items": [], "scanning": SCAN["running"], "count": SCAN["count"]}
    res = []
    for rel, t in INDEX:
        n = nname(rel)
        if all(s in n for s in qs):
            res.append({"path": rel, "type": t})
            if len(res) >= 300:
                break
    return {"items": res, "scanning": SCAN["running"], "count": SCAN["count"], "indexed": len(INDEX)}


# ---------------------------------------------------------------- ネットワーク探索 / フォルダ選択 (初回セットアップ)
def lan_prefixes():
    pref = []
    for ip in local_ips():
        p = ip.split(".")
        if len(p) == 4 and not ip.startswith("169.254") and not ip.startswith("127."):
            x = ".".join(p[:3])
            if x not in pref:
                pref.append(x)
    return pref


def probe(ip, port, t=0.5):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(t)
    try:
        return s.connect_ex((ip, port)) == 0
    except Exception:
        return False
    finally:
        s.close()


def run_hidden(cmd, timeout=20):
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NOWIN)
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")


def net_view_names():
    """Windows: net view で見えるコンピューター名 -> IP"""
    if not IS_WIN:
        return {}
    out = {}
    try:
        _, txt = run_hidden(["cmd", "/c", "chcp 65001>nul & net view"], 15)
        for m in re.finditer(r"^\\\\(\S+)", txt, re.M):
            n = m.group(1)
            try:
                out[socket.gethostbyname(n)] = n
            except Exception:
                pass
    except Exception:
        pass
    return out


def rev_name(ip):
    try:
        return socket.gethostbyaddr(ip)[0].split(".")[0]
    except Exception:
        return ""


def api_net_scan(q, body=None):
    own = set(local_ips())
    ips = ["%s.%d" % (p, i) for p in lan_prefixes() for i in range(1, 255)]
    found = []
    with ThreadPoolExecutor(128) as ex:
        nv = ex.submit(net_view_names)
        for ip, ok in zip(ips, ex.map(lambda x: probe(x, 445), ips)):
            if ok:
                found.append(ip)
        names = nv.result(timeout=20)
        futs = {ip: ex.submit(rev_name, ip) for ip in found if ip not in names}
        for ip, f in futs.items():
            try:
                names[ip] = f.result(timeout=3)
            except Exception:
                names[ip] = ""
    hosts = [{"ip": ip, "name": names.get(ip) or "", "self": ip in own} for ip in found]
    for ip, n in names.items():          # 445が閉じていてもnet viewに出るもの
        if n and ip not in found:
            hosts.append({"ip": ip, "name": n, "self": ip in own})
    return {"hosts": hosts, "own": socket.gethostname(), "subnets": lan_prefixes()}


def list_drives():
    out = []
    if IS_WIN:
        for c in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            if os.path.exists(c + ":\\"):
                out.append(c + ":\\")
    else:
        out = ["/"] + [os.path.join("/mnt", d) for d in (os.listdir("/mnt") if os.path.isdir("/mnt") else [])]
        out.append(os.path.expanduser("~"))
    return out


HOST_RE = re.compile(r"^[\w.\-]+$")


def api_net_shares(q, body=None):
    host = q.get("host", [""])[0].strip().lstrip("\\")
    if not HOST_RE.match(host):
        raise ApiError(400, "ホスト名が不正です")
    if not IS_WIN:
        return {"shares": [], "error": "この環境では共有の一覧取得に対応していません。パスを直接入力してください。", "unsupported": True}
    code, txt = run_hidden(["cmd", "/c", "chcp 65001>nul & net view \\\\%s /all" % host], 25)
    shares = []
    sep = txt.find("-----")
    if sep >= 0 and "Disk" in txt:
        for line in txt[sep:].splitlines()[1:]:
            m = re.match(r"^(\S.*?)\s{2,}Disk\b", line)
            if m and not m.group(1).endswith("$"):
                shares.append(m.group(1).strip())
    if shares:
        return {"shares": shares, "host": host}
    low = txt
    need = bool(re.search(r"\b(5|1219|1326|86)\b", low))
    return {"shares": [], "host": host, "need_login": need, "error": txt.strip().replace("chcp", "")[:300] or "共有を取得できませんでした"}


def api_net_login(q, body):
    host = str(body.get("host", "")).strip().lstrip("\\")
    user, pw = str(body.get("user", "")), str(body.get("password", ""))
    if not HOST_RE.match(host) or not user:
        raise ApiError(400, "入力が不正です")
    if not IS_WIN:
        raise ApiError(501, "Windowsのみ対応")
    run_hidden(["cmdkey", "/add:" + host, "/user:" + user, "/pass:" + pw], 15)
    code, txt = run_hidden(["net", "use", "\\\\" + host + "\\IPC$", pw, "/user:" + user], 25)
    return {"ok": code == 0, "error": "" if code == 0 else txt.strip()[:300]}


def api_fs_list(q, body=None):
    p = q.get("path", [""])[0].strip().strip('"')
    if not p:
        return {"path": "", "parent": None, "dirs": list_drives(), "full": True, "folders": 0, "archives": 0, "images": 0}
    if not os.path.isdir(p):
        raise ApiError(404, "フォルダを開けません: " + p)
    dirs, arc, img = [], 0, 0
    try:
        with os.scandir(p) as it:
            for e in it:
                n = e.name
                if n.startswith(".") or n.startswith("$") or n == "System Volume Information":
                    continue
                try:
                    if e.is_dir():
                        dirs.append(n)
                    else:
                        x = os.path.splitext(n)[1].lower()
                        if x in ARC_EXT:
                            arc += 1
                        elif x in IMG_EXT:
                            img += 1
                except OSError:
                    pass
    except OSError as e:
        raise ApiError(403, "アクセスできません: %s" % e)
    dirs.sort(key=natkey)
    cur = p.rstrip("\\/") or p
    par = os.path.dirname(cur)
    if not par or par == cur or par == p:
        par = None
    return {"path": p, "parent": par, "dirs": dirs, "folders": len(dirs), "archives": arc, "images": img}


def find_first_book(root, depth=4):
    """root配下で最初に見つかったマンガ(アーカイブ/PDF/画像フォルダ)の相対パスを返す"""
    budget = [300]

    def walk(full, rel, d):
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        try:
            ents = sorted(os.scandir(full), key=lambda e: natkey(e.name))
        except OSError:
            return None
        subs = []
        for e in ents:
            if e.name.startswith(".") or e.name.startswith("$"):
                continue
            try:
                if e.is_dir():
                    subs.append(e)
                elif os.path.splitext(e.name)[1].lower() in ARC_EXT:
                    return (rel + "/" if rel else "") + e.name
            except OSError:
                pass
        if rel and any(os.path.splitext(e.name)[1].lower() in IMG_EXT for e in ents if not e.is_dir()):
            return rel
        if d > 0:
            for e in subs:
                r = walk(e.path, (rel + "/" if rel else "") + e.name, d - 1)
                if r:
                    return r
        return None
    return walk(root, "", depth)


def api_net_verify(q, body):
    path = str(body.get("path", "")).strip().strip('"')
    if not os.path.isdir(path):
        raise ApiError(404, "フォルダにアクセスできません")
    sample = find_first_book(path)
    if not sample:
        return {"ok": False, "error": "このフォルダの中にマンガ(zip/rar/pdf/画像)が見つかりませんでした。別のフォルダを選んでください。"}
    with S_LOCK:
        S["library_path"] = path
        save_settings()
    with BOOKS_LOCK:
        for b in BOOKS.values():
            b.close()
        BOOKS.clear()
    return {"ok": True, "sample": sample}


def api_qr(q):
    u = q.get("u", [""])[0]
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        raise ApiError(404, "qrcode not installed")
    img = qrcode.make(u, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue()


def access_urls():
    urls = []
    for ip in local_ips():
        urls.append("http://%s:%d/" % (ip, int(S["port"])))
    try:
        urls.append("http://%s:%d/" % (socket.gethostname(), int(S["port"])))
    except Exception:
        pass
    return urls


def settings_public():
    d = {k: v for k, v in S.items() if k not in ("password", "secret", "server_id")}
    d["has_password"] = bool(S.get("password"))
    d["urls"] = access_urls()
    t = find_tools()
    d["tools"] = {"sevenzip": t["sevenzip"], "tar": t["tar"], "pillow": HAVE_PIL, "python": sys.version.split()[0],
                  "platform": sys.platform, "version": VERSION}
    d["scan"] = {"running": SCAN["running"], "count": SCAN["count"], "indexed": len(INDEX), "ts": SCAN["ts"]}
    return d


def api_settings_get(q, body=None):
    return settings_public()


def api_settings_set(q, body):
    global EXTRACT_SEM
    restart = False
    with S_LOCK:
        old_lib = S["library_path"]
        if "setup_done" in body:
            S["setup_done"] = bool(body["setup_done"])
        for k in ("library_path", "host", "cache_dir", "sevenzip_path"):
            if k in body and isinstance(body[k], str):
                v = body[k].strip().strip('"')
                if k == "host":
                    v = v or "0.0.0.0"
                if k == "host" and v != S[k]:
                    restart = True
                S[k] = v
        if "port" in body:
            p = int(body["port"])
            if not (1 <= p <= 65535):
                raise ApiError(400, "ポート番号が不正です")
            if p != S["port"]:
                restart = True
            S["port"] = p
        for k, lo, hi, typ in (("cache_max_gb", 0.2, 2000, float), ("resize_quality", 40, 98, int),
                               ("resize_max_width", 600, 6000, int), ("extract_parallel", 1, 6, int),
                               ("book_cache_size", 3, 50, int)):
            if k in body:
                v = typ(body[k])
                S[k] = max(lo, min(hi, v))
        if "resize_enabled" in body:
            S["resize_enabled"] = bool(body["resize_enabled"])
        if body.get("password") is not None and "password" in body:
            S["password"] = str(body["password"])
        if body.get("clear_password"):
            S["password"] = ""
        if S["library_path"] != old_lib and S["library_path"]:
            S["setup_done"] = True
        save_settings()
        _TOOLS["v"] = None
    EXTRACT_SEM = threading.Semaphore(int(S["extract_parallel"]))
    with BOOKS_LOCK:
        for b in BOOKS.values():
            b.close()
        BOOKS.clear()
    if S["library_path"] != old_lib:
        global INDEX
        INDEX = []
        scan_library()
    r = settings_public()
    r["restart_required"] = restart
    return r


def api_test_path(q, body):
    p = (body.get("path") or "").strip().strip('"')
    t0 = time.time()
    res = {"path": p, "exists": False, "is_dir": False, "entries": 0, "archives": 0, "folders": 0}
    try:
        res["exists"] = os.path.exists(p)
        res["is_dir"] = os.path.isdir(p)
        if res["is_dir"]:
            with os.scandir(p) as it:
                for k, e in enumerate(it):
                    if k > 5000:
                        break
                    res["entries"] += 1
                    if e.is_dir():
                        res["folders"] += 1
                    elif os.path.splitext(e.name)[1].lower() in ARC_EXT:
                        res["archives"] += 1
    except OSError as e:
        res["error"] = str(e)
    res["ms"] = int((time.time() - t0) * 1000)
    return res


def api_rescan(q, body):
    scan_library()
    return {"ok": True}


def api_clear_cache(q, body):
    with BOOKS_LOCK:
        for b in BOOKS.values():
            b.close()
        BOOKS.clear()
    for sub in ("ext", "res", "thumb", "tmp"):
        shutil.rmtree(os.path.join(cache_root(), sub), ignore_errors=True)
    return {"ok": True}


def respawn():
    exe = sys.executable
    if IS_WIN and exe.lower().endswith("python.exe"):
        pw = exe[:-10] + "pythonw.exe"
        if os.path.exists(pw):
            exe = pw
    flags = (0x00000008 | 0x00000200 | 0x08000000) if IS_WIN else 0
    cmd = [exe, "--server"] if FROZEN else [exe, os.path.join(BASE, "server.py")]
    subprocess.Popen(cmd, cwd=BASE, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def api_restart(q, body):
    def go():
        time.sleep(0.6)
        save_settings()
        try:
            SRV[0].server_close()
        except Exception:
            pass
        tray_stop()
        respawn()
        os._exit(0)
    threading.Thread(target=go, daemon=True).start()
    return {"ok": True}


def api_quit(q, body):
    def go():
        time.sleep(0.5)
        tray_stop()
        os._exit(0)
    threading.Thread(target=go, daemon=True).start()
    return {"ok": True}


def api_login(q, body):
    if not S.get("password"):
        return {"ok": True}
    pw = str(body.get("password", ""))
    if hmac.compare_digest(pw.encode(), S["password"].encode()):
        return {"ok": True, "_cookie": make_token()}
    time.sleep(0.6)
    raise ApiError(403, "パスワードが違います")


# ---------------------------------------------------------------- アプリの更新 (GitHub Releases)
GH_REPO = "matcha-matcha/manga-viewer-android"
try:
    from _build import BUILD          # exe作成時にCIが埋め込む通し番号
except Exception:
    BUILD = 0
TRAY = [None]


def tray_stop():
    """終了前にタスクトレイのアイコンを消す(残像防止)"""
    try:
        if TRAY[0]:
            TRAY[0].stop()
            time.sleep(0.25)
    except Exception:
        pass


UPD = {"checked": 0, "latest": 0, "url": None, "apk": 0, "error": None, "busy": False}


def update_check():
    import urllib.request
    UPD["error"] = None
    try:
        req = urllib.request.Request("https://api.github.com/repos/%s/releases?per_page=30" % GH_REPO,
                                     headers={"User-Agent": "MangaViewerServer", "Accept": "application/vnd.github+json"})
        rels = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        best, url, apk = 0, None, 0
        for r in rels:
            tag = r.get("tag_name", "")
            m = re.match(r"^(server|build)-(\d+)$", tag)
            if not m:
                continue
            n = int(m.group(2))
            if m.group(1) == "build":
                apk = max(apk, n)
                continue
            for a in r.get("assets", []):
                if a.get("name", "").lower().endswith(".zip") and n > best:
                    best, url = n, a.get("browser_download_url")
        UPD.update({"latest": best, "url": url, "apk": apk, "checked": time.time()})
    except Exception as e:
        msg = str(e)
        if "404" in msg:
            msg += " (GitHubのリポジトリが非公開だと確認できません)"
        UPD["error"] = msg


def update_info():
    return {"version": VERSION, "build": BUILD, "latest": UPD["latest"], "apk": UPD["apk"], "error": UPD["error"],
            "available": bool(UPD["url"] and UPD["latest"] > BUILD), "can_apply": FROZEN and IS_WIN,
            "auto": bool(S.get("auto_update", True)), "checked": UPD["checked"]}


def api_update_check(q, body=None):
    update_check()
    return update_info()


def update_apply():
    """新しいzipを取得 → 展開 → 終了後に差し替えて再起動するbatを実行"""
    import urllib.request
    if not (FROZEN and IS_WIN):
        raise ApiError(400, "exe版のみ自動更新できます")
    if not UPD["url"] or UPD["latest"] <= BUILD:
        raise ApiError(400, "更新はありません")
    if UPD["busy"]:
        raise ApiError(409, "更新中です")
    UPD["busy"] = True
    try:
        zp = os.path.join(BASE, "update.zip")
        tmp = os.path.join(BASE, "update_tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        req = urllib.request.Request(UPD["url"], headers={"User-Agent": "MangaViewerServer"})
        with urllib.request.urlopen(req, timeout=60) as r, open(zp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        with zipfile.ZipFile(zp) as z:
            z.extractall(tmp)
        src = tmp
        subs = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
        if len(subs) == 1 and not [f for f in os.listdir(tmp) if f.lower().endswith(".exe")]:
            src = os.path.join(tmp, subs[0])
        exe = os.path.basename(sys.executable)
        bat = os.path.join(BASE, "update.bat")
        with open(bat, "w", encoding="ascii", errors="replace") as f:
            f.write('@echo off\r\ntimeout /t 3 /nobreak >nul\r\n'
                    'xcopy /e /y /q "%s\\*" "%s\\" >nul\r\n'
                    'rmdir /s /q "%s"\r\ndel "%s"\r\n'
                    'start "" "%s\\%s" --server\r\n(goto) 2>nul & del "%%~f0"\r\n' % (src, BASE, tmp, zp, BASE, exe))
        subprocess.Popen(["cmd", "/c", bat], cwd=BASE, creationflags=0x00000008 | 0x00000200 | NOWIN, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def bye():
            time.sleep(0.8)
            tray_stop()
            os._exit(0)
        threading.Thread(target=bye, daemon=True).start()
    except Exception:
        UPD["busy"] = False
        raise


def api_update_apply(q, body):
    update_check()
    update_apply()
    return {"ok": True}


def api_update_auto(q, body):
    S["auto_update"] = bool(body.get("on", True))
    save_settings()
    return update_info()


def auto_update_thread():
    time.sleep(15)
    update_check()
    if S.get("auto_update", True) and FROZEN and IS_WIN and UPD["url"] and UPD["latest"] > BUILD:
        try:
            update_apply()
        except Exception as e:
            print("自動更新失敗:", e)
    while True:                      # その後も6時間ごとに確認(バナー表示用)
        time.sleep(6 * 3600)
        update_check()


def api_status(q, body=None):
    return {"needs_login": bool(S.get("password")), "configured": bool(S["library_path"] and S.get("setup_done")), "version": VERSION, "build": BUILD, "update": bool(UPD["url"] and UPD["latest"] > BUILD and getattr(CTX, "local", False)), "local": bool(getattr(CTX, "local", False)), "name": socket.gethostname(), "id": S.get("server_id", "")}


GET_ROUTES = {
    "/api/list": api_list, "/api/book": api_book, "/api/neighbors": api_neighbors, "/api/recent": api_recent,
    "/api/search": api_search, "/api/settings": api_settings_get,
    "/api/bookmarks": api_bookmarks, "/api/update/check": api_update_check, "/api/net/scan": api_net_scan, "/api/net/shares": api_net_shares, "/api/fs/list": api_fs_list,
}
POST_ROUTES = {
    "/api/progress": api_progress, "/api/settings": api_settings_set, "/api/test_path": api_test_path,
    "/api/bookmark": api_bookmark_set, "/api/update/check": api_update_check, "/api/update/apply": api_update_apply, "/api/update/auto": api_update_auto, "/api/rescan": api_rescan, "/api/quit": api_quit, "/api/net/login": api_net_login, "/api/net/verify": api_net_verify, "/api/clear_cache": api_clear_cache, "/api/restart": api_restart,
}


# ---------------------------------------------------------------- HTTP
CTX = threading.local()
ADMIN_GET = {"/api/settings", "/api/net/scan", "/api/net/shares", "/api/fs/list", "/api/update/check"}
ADMIN_POST = {"/api/settings", "/api/test_path", "/api/rescan", "/api/quit", "/api/restart", "/api/clear_cache",
              "/api/update/check", "/api/update/apply", "/api/update/auto", "/api/net/login", "/api/net/verify"}


def is_local_client(h):
    return h.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MangaViewer/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, data, ctype, extra=None):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _json(self, obj, code=200, extra=None):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8",
                   dict({"Cache-Control": "no-store"}, **(extra or {})))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        q = parse_qs(u.query)
        CTX.local = is_local_client(self)
        try:
            if path == "/api/status":
                return self._json(api_status(q))
            if path.startswith("/api/"):
                if not authed(self):
                    return self._json({"error": "login required", "login": True}, 401)
                if path in ADMIN_GET and not CTX.local:
                    raise ApiError(403, "サーバーのPCでのみ操作できます")
                if path == "/api/page":
                    return self.serve_page(q)
                if path == "/api/qr":
                    return self._send(200, api_qr(q), "image/svg+xml", {"Cache-Control": "private, max-age=3600"})
                if path == "/api/cover":
                    data, ct = get_cover(q.get("b", [""])[0])
                    return self._send(200, data, ct, {"Cache-Control": "private, max-age=2592000"})
                fn = GET_ROUTES.get(path)
                if not fn:
                    raise ApiError(404, "not found")
                return self._json(fn(q))
            return self.serve_static(path)
        except ApiError as e:
            self._json({"error": e.msg}, e.code)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as e:
            try:
                self._json({"error": "サーバーエラー: %s" % e}, 500)
            except Exception:
                pass

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path
        CTX.local = is_local_client(self)
        try:
            body = self._body()
            if path == "/api/login":
                r = api_login({}, body)
                ck = r.pop("_cookie", None)
                extra = {"Set-Cookie": "mv=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000" % ck} if ck else None
                return self._json(r, 200, extra)
            if not authed(self):
                return self._json({"error": "login required", "login": True}, 401)
            if path in ADMIN_POST and not CTX.local:
                raise ApiError(403, "サーバーのPCでのみ操作できます")
            fn = POST_ROUTES.get(path)
            if not fn:
                raise ApiError(404, "not found")
            return self._json(fn({}, body))
        except ApiError as e:
            self._json({"error": e.msg}, e.code)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as e:
            try:
                self._json({"error": "サーバーエラー: %s" % e}, 500)
            except Exception:
                pass

    def serve_page(self, q):
        rel = q.get("b", [""])[0]
        try:
            i = int(q.get("i", ["0"])[0])
            w = int(q.get("w", ["0"])[0] or 0)
        except ValueError:
            raise ApiError(400, "bad request")
        book = get_book(rel)
        if i < 0 or i >= book.n:
            raise ApiError(404, "ページ範囲外")
        etag = 'W/"%s-%d-%d"' % (book.key, i, w)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data, ct = get_page_data(book, i, w)
        self._send(200, data, ct, {"Cache-Control": "private, max-age=604800", "ETag": etag})

    def serve_static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        rel = os.path.normpath(path.lstrip("/"))
        if rel.startswith("..") or os.path.isabs(rel):
            raise ApiError(403, "forbidden")
        fp = os.path.join(STATIC, rel)
        if not os.path.isfile(fp):
            raise ApiError(404, "not found")
        with open(fp, "rb") as f:
            data = f.read()
        ct = STATIC_MIME.get(os.path.splitext(fp)[1].lower(), "application/octet-stream")
        self._send(200, data, ct, {"Cache-Control": "no-cache"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    allow_reuse_address = True


def local_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips


SRV = [None]


def init():
    global EXTRACT_SEM
    if sys.stdout is None:   # pythonw (コンソールなし)
        sys.stdout = sys.stderr = open(os.path.join(DATA, "server.log"), "a", encoding="utf-8")
    load_settings()
    load_progress()
    load_bookmarks()
    load_index()
    EXTRACT_SEM = threading.Semaphore(int(S["extract_parallel"]))
    cache_root()
    threading.Thread(target=flush_loop, daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(5), trim_cache()), daemon=True).start()
    if S["library_path"] and not INDEX:
        scan_library()
    srv = Server((S["host"], int(S["port"])), Handler)
    SRV[0] = srv
    return srv


def open_settings_page(configured=True):
    import webbrowser
    webbrowser.open("http://localhost:%d/%s" % (S["port"], "#/s" if configured else ""))


def _make_link(path, target, args, desc):
    ps = ("$w=New-Object -ComObject WScript.Shell;$l=$w.CreateShortcut($args[0]);$l.TargetPath=$args[1];"
          "$l.Arguments=$args[2];$l.WorkingDirectory=$args[3];$l.Description=$args[4];$l.Save()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps, path, target, args, BASE, desc], check=False, creationflags=NOWIN)


def make_shortcuts():
    """exe 初回起動: デスクトップのショートカットとログイン時の自動起動を作る"""
    try:
        desk = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
                              capture_output=True, timeout=20, creationflags=NOWIN).stdout.decode("utf-8", "replace").strip()
        if desk:
            _make_link(os.path.join(desk, "まんがビューワー サーバー.lnk"), sys.executable, "", "まんがビューワー サーバー(設定を開く)")
        st = os.path.join(os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        _make_link(os.path.join(st, "まんがビューワー サーバー.lnk"), sys.executable, "", "まんがビューワー サーバー")
    except Exception as e:
        print("ショートカット作成失敗:", e)


def _msgbox(text, title="まんがビューワー サーバー", yesno=False):
    import ctypes
    flags = (0x24 if yesno else 0x40) | 0x40000      # はい/いいえ+?アイコン / 情報アイコン、最前面
    return ctypes.windll.user32.MessageBoxW(0, text, title, flags)


def run_tray(srv):
    """タスクトレイに常駐。右クリック: 設定 / 更新を確認してインストール / 終了"""
    import pystray
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), 12, fill=(43, 98, 168, 255))
    d.rectangle((14, 14, 50, 50), fill=(255, 255, 255, 255))
    d.rectangle((14, 14, 32, 50), fill=(220, 232, 247, 255))
    d.line((32, 14, 32, 50), fill=(43, 98, 168, 255), width=3)

    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def on_settings(icon=None, item=None):
        open_settings_page(bool(S.get("library_path") and S.get("setup_done")))

    def on_update(icon=None, item=None):
        def work():
            update_check()
            if UPD["error"]:
                _msgbox("更新を確認できません。\n" + UPD["error"])
            elif not (UPD["url"] and UPD["latest"] > BUILD):
                _msgbox("最新版です (build %d)" % BUILD)
            elif _msgbox("新しい版 (build %d) があります。今すぐインストールしますか?\n(自動で再起動します)" % UPD["latest"], yesno=True) == 6:
                try:
                    update_apply()
                except Exception as e:
                    _msgbox("更新に失敗しました: %s" % e)
        threading.Thread(target=work, daemon=True).start()

    def on_quit(icon, item):
        try:
            save_settings()
        except Exception:
            pass
        icon.stop()
        os._exit(0)

    def on_restart(icon, item):
        api_restart(None, {})

    menu = pystray.Menu(
        pystray.MenuItem("設定", on_settings, default=True),
        pystray.MenuItem("更新を確認してインストール", on_update),
        pystray.MenuItem("再起動", on_restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("終了", on_quit))
    icon = pystray.Icon("MangaViewerServer", img, "まんがビューワー サーバー", menu)
    TRAY[0] = icon

    def setup(ic):
        ic.visible = True
        try:
            ic.notify("起動しました。右クリックで設定を開けます", "まんがビューワー サーバー")
        except Exception:
            pass
    icon.run(setup)


def main():
    if FROZEN and IS_WIN:
        os.makedirs(DATA, exist_ok=True)
        load_settings()
        try:                                            # すでに起動済みなら設定を開くだけ(二重起動しない)
            __import__("urllib.request").request.urlopen("http://localhost:%d/api/status" % S["port"], timeout=1.5).read()
            if "--server" not in sys.argv:
                open_settings_page()
            return
        except Exception:
            pass
    srv = init()
    first = not (S.get("library_path") and S.get("setup_done"))
    if FROZEN and IS_WIN:
        if not S.get("shortcuts_made"):
            S["shortcuts_made"] = True
            save_settings()
            threading.Thread(target=make_shortcuts, daemon=True).start()
        if first:                                       # 初回だけ設定画面を開く
            threading.Timer(1.5, open_settings_page, args=(False,)).start()
    threading.Thread(target=auto_update_thread, daemon=True).start()
    t = find_tools()
    print("=" * 56)
    print(" まんがビューワー v%s" % VERSION)
    print(" ライブラリ :", S["library_path"] or "(未設定: 設定画面で入力)")
    print(" 7-Zip      :", t["sevenzip"] or "なし", "/ tar:", t["tar"] or "なし", "/ Pillow:", "あり" if HAVE_PIL else "なし")
    print(" アクセスURL:")
    print("   http://localhost:%d/" % S["port"])
    for ip in local_ips():
        print("   http://%s:%d/" % (ip, S["port"]))
    print("=" * 56, flush=True)
    if FROZEN and IS_WIN:
        try:
            run_tray(srv)
            return
        except Exception as e:
            print("タスクトレイ起動失敗:", e)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
