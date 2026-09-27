#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ANPhotoLab v2 «Проявочная» — генератор статического сайта. Python 3.9+, только стандартная библиотека.

    python3 build.py                                  полная сборка: site/ очищается и собирается заново
    python3 build.py --check content/pages/x.json     проверить одну страницу, ничего не записывая
    python3 build.py --only x                         собрать одну страницу (content/pages/x.json) без очистки site/
    python3 build.py --strict                         полная сборка, любая ошибка — сборка не записывается

Результат — папка site/ с чистыми HTML/CSS/JS. Все внутренние ссылки и пути к ассетам относительные
с явным index.html, поэтому сайт открывается двойным кликом по site/index.html (file://) и работает
на любом статическом хостинге. Canonical, og:url, sitemap и JSON-LD — абсолютные URL от site_url.
Документация по блокам: content/BLOCKS.md.
"""
import argparse
import datetime
import hashlib
import html
import json
import re
import shutil
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
CONTENT = ROOT / "content"
PAGES_DIR = CONTENT / "pages"
OUT = ROOT / "site"

NBSP = " "
MIN_COUNT_SHOWN = 6        # число кадров жанра выводим на сайте, только если их не меньше («1 кадр» выдаёт тонкое портфолио)
TITLE_SUFFIX = " — Александр Непомнящих"   # единый хвост <title> у всех страниц, кроме главной
TITLE_SUFFIX_SHORT = " — ANPhotoLab"       # если с полным хвостом title длиннее TITLE_MAX — хвост короче, чтобы не обрезался в выдаче
TITLE_MAX = 70
FIT_MAX_WORD = 0.4         # огромное слово первого экрана — не выше 40 % высоты окна (короткое слово не съедает первый экран)
FIT_MAX_404 = 0.34
PHOTO_DIR = "assets/img/photos"            # локальные AVIF/WebP (делает tools/photos.py), путь внутри src/ и site/
PHOTO_WIDTHS = (640, 1280, 1920)
LOADER = True              # лоадер «Проявка» (лампа + счётчик 000→100) при первом визите за сессию; False — сразу короткое появление
TRANSITIONS = True         # шторка между страницами и появление при повторной загрузке; False — выключает всё, включая лоадер
                           # (README → «Загрузка и переходы»; длительности — объект PT в src/assets/js/main.js)


# =============================================================================
# Вспомогательное
# =============================================================================

def load_json(path):
    """Читает JSON и превращает синтаксическую ошибку в понятное сообщение."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError("ошибка JSON в строке {}, столбце {}: {}".format(e.lineno, e.colno, e.msg))


def esc(s):
    return html.escape(str(s), quote=True)


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def strip_md(s):
    """Markdown-lite → чистый текст (для JSON-LD, alt, подсчёта)."""
    s = re.sub(r"\[([^\]]+)\]\([^)\s]+\)", r"\1", str(s))
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"\*(.+?)\*", r"\1", s)
    s = s.replace("### ", "")
    return s


def norm(s):
    return re.sub(r"\s+", " ", strip_md(s).replace(NBSP, " ")).strip().lower()


TEXT_KEYS = ("title", "lead", "note", "paragraphs", "list", "sub", "items", "text", "q", "a", "name",
             "task", "solution", "result")


def text_fields(node, key=None):
    """Содержательный текст блока для подсчёта объёма (без меток, кнопок, id и служебных полей)."""
    out = []
    if isinstance(node, str):
        if key in TEXT_KEYS or key is None:
            out.append(node)
    elif isinstance(node, list):
        for x in node:
            out.extend(text_fields(x, key))
    elif isinstance(node, dict):
        for k, v in node.items():
            if k in TEXT_KEYS:
                out.extend(text_fields(v, k))
    return out


# ----------------------------------------------------------------------------- Русская типографика
SHORT_WORDS = ["в", "во", "и", "к", "ко", "с", "со", "о", "об", "обо", "у", "а", "на", "по", "за", "от", "до",
               "не", "ни", "из", "для", "без", "при", "под", "над", "но", "я", "про", "через", "перед"]
RE_SHORT = re.compile(r"(?<![\w\-])(" + "|".join(sorted(SHORT_WORDS, key=len, reverse=True)) + r")[ \t\r\n]+(?=\S)",
                      re.IGNORECASE)
RE_DASH = re.compile(r"[ \t ]+[-–—][ \t\r\n]+")
RE_DASH_START = re.compile(r"^[-–—][ \t]+")
RE_NUMSIGN = re.compile(r"№[ \t]*(?=\d)")
RE_DIGIT_WORD = re.compile(r"(?<=\d)[ \t]+(?=[А-Яа-яЁё])")
RE_QUOTES = re.compile(r'"([^"\n]+)"')


def typo(raw):
    """Сырой текст → безопасный HTML с русской типографикой.
    «Ёлочки» сохраняются, прямые кавычки превращаются в «ёлочки»; перед тире — неразрывный пробел
    и длинное тире; после коротких слов (в, и, на, для…) — неразрывный пробел; № и число не разрываются."""
    s = str(raw)
    s = RE_QUOTES.sub("«\\1»", s)
    s = esc(s).replace("&quot;", '"').replace("&#x27;", "'")
    s = RE_DASH.sub(NBSP + "— ", s)
    s = RE_DASH_START.sub("— ", s)
    s = RE_SHORT.sub(lambda m: m.group(1) + NBSP, s)
    s = RE_NUMSIGN.sub("№" + NBSP, s)
    s = RE_DIGIT_WORD.sub(NBSP, s)
    return s


RE_INLINE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)|\*\*(.+?)\*\*|\*(.+?)\*")
RE_HYPHEN_WORD = re.compile(r"([\w ]+(?:-[\w]+)+)")


# =============================================================================
# Отчёт об ошибках
# =============================================================================

class Report:
    def __init__(self, name):
        self.name = name
        self.errors = []
        self.warnings = []
        self.info = []

    def err(self, where, msg):
        self.errors.append((where + ": " if where else "") + msg)

    def warn(self, where, msg):
        line = (where + ": " if where else "") + msg
        if line not in self.warnings:
            self.warnings.append(line)

    def note(self, msg):
        self.info.append(msg)

    def print(self, verbose=True):
        head = "OK   " if not self.errors else "FAIL "
        print("{}{}".format(head, self.name))
        if verbose:
            for m in self.info:
                print("     · " + m)
        for m in self.errors:
            print("     ОШИБКА  " + m)
        for m in self.warnings:
            print("     внимание " + m)


# =============================================================================
# Схема блоков v2: обязательные и необязательные поля (подробно — content/BLOCKS.md)
# =============================================================================

SEC = {"id": "str", "title": "str", "eyebrow": "str", "note": "str", "title_style": "str", "meta": "str"}


def _spec(req=None, opt=None, section=True):
    o = dict(SEC) if section else {"id": "str"}
    o.update(opt or {})
    return {"req": req or {}, "opt": o}


BLOCK_SPECS = {
    "hero-person": _spec({"photo": "int"}, {"name": "list", "tags": "list", "labels": "list", "lead": "str", "actions": "list",
                                            "meta": "list", "focus": "str", "messengers": "bool"}, section=False),
    "genre-hero": _spec({}, {"word": "str", "eyebrow": "str", "photo": "int", "variant": "str", "lead": "str",
                             "facts": "list", "actions": "list", "focus": "str", "focus_mobile": "str",
                             "caption": "str"}, section=False),
    "genre-tabs": _spec({}, {"active": "str", "label": "str"}, section=False),
    "directions": _spec({}, {"items": "list", "previews": "dict"}),
    "related-directions": _spec({"items": "list"}, {"layout": "str"}),
    "gallery": _spec({}, {"genre": "str", "ids": "list", "exclude": "list", "limit": "int", "layout": "str",
                          "min": "int", "end": "dict", "group": "str", "solo_note": "str"}),
    "stats": _spec({"items": "list"}, {"text": "str", "link": "dict"}),
    "cases": _spec({"items": "list"}, {}),
    "marquee": _spec({}, {"ids": "list", "items": "list", "link": "dict", "speed": "num", "label": "str"}, section=False),
    "points": _spec({"items": "list"}, {}),
    "formats": _spec({"items": "list"}, {"price_note": "str", "cta_label": "str"}),
    "faq": _spec({"items": "list"}, {}),
    "seo-text": _spec({}, {"lead": "str", "paragraphs": "list", "sub": "list", "open": "bool"}),
    "text": _spec({}, {"lead": "str", "paragraphs": "list", "list": "list", "aside": "dict", "actions": "list"}),
    "quote": _spec({}, {"review": "int", "text": "str", "author": "str", "cap": "str"}),
    "reviews": _spec({}, {"ids": "list", "layout": "str", "all_link": "any"}),
    "lead-form": _spec({}, {"lead": "str", "preset": "str", "contacts": "bool", "note": "str", "meta": "str", "button": "str"}),
}

# Блоки v1 («Каталог выставки») удалены — подсказка, чем заменить
V1_BLOCKS = {
    "page-hero": "hero-person (главная) или genre-hero (жанр, SEO, служебные)",
    "cta": "lead-form",
    "genre-index": "directions",
    "features": "points",
    "steps": "points или seo-text",
    "services": "related-directions",
    "split": "text или cases",
    "about": "stats (поле text) или text",
    "related": "related-directions",
}

HEROES = ("hero-person", "genre-hero")

PAGE_FIELDS = {"slug": "str", "type": "str", "genre": "str", "nav": "str", "title": "str", "description": "str",
               "h1": "str", "og_image": "int", "parent": "str", "service": "str", "noindex": "bool",
               "breadcrumbs": "bool", "schema": "list", "blocks": "list", "_comment": "any", "form_preset": "str"}
PAGE_REQUIRED = ["slug", "type", "title", "description", "h1", "blocks"]
PAGE_TYPES = ("home", "genre", "seo", "service")

DEFAULT_IDS = {"hero-person": "top", "genre-hero": "top", "genre-tabs": "zhanry", "directions": "napravleniya",
               "related-directions": "drugie", "gallery": "kadry", "stats": "opyt", "cases": "keysy",
               "marquee": "lenta-otzyvov", "points": "kak-snimayu", "formats": "formaty", "faq": "voprosy",
               "seo-text": "podrobnee", "text": "tekst", "quote": "citata", "reviews": "otzyvy", "lead-form": "zayavka"}

TYPE_CHECK = {
    "str": lambda v: isinstance(v, str),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "num": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "list": lambda v: isinstance(v, list),
    "dict": lambda v: isinstance(v, dict),
    "any": lambda v: True,
}
TYPE_NAMES = {"str": "строка", "int": "целое число", "num": "число", "bool": "true/false", "list": "массив",
              "dict": "объект", "any": "любое"}

# Бенто-раскладка галереи: шаблоны рядов на 12 колонок. Высота ряда-модуля --u = ширина колонки × 4/3,
# поэтому 8×4 и 6×3 и 4×2 дают 3:2 (горизонтальный кадр), 4×4 и 3×3 — 3:4 (вертикальный), 12×5 — широкий.
# Роль: xl/l/ls/one — горизонтальные, p/ps — вертикальные. Область — "ряд / колонка / ряд-конец / колонка-конец".
BENTO = {
    "xl2":   (4, [("xl", "L", "1 / 1 / 5 / 9"), ("ls", "L", "1 / 9 / 3 / 13"), ("ls", "L", "3 / 9 / 5 / 13")]),
    "2xl":   (4, [("ls", "L", "1 / 1 / 3 / 5"), ("ls", "L", "3 / 1 / 5 / 5"), ("xl", "L", "1 / 5 / 5 / 13")]),
    "ll":    (3, [("l", "L", "1 / 1 / 4 / 7"), ("l", "L", "1 / 7 / 4 / 13")]),
    "lll":   (2, [("ls", "L", "1 / 1 / 3 / 5"), ("ls", "L", "1 / 5 / 3 / 9"), ("ls", "L", "1 / 9 / 3 / 13")]),
    "xlp":   (4, [("xl", "L", "1 / 1 / 5 / 9"), ("p", "P", "1 / 9 / 5 / 13")]),
    "pxl":   (4, [("p", "P", "1 / 1 / 5 / 5"), ("xl", "L", "1 / 5 / 5 / 13")]),
    "plp":   (3, [("ps", "P", "1 / 1 / 4 / 4"), ("l", "L", "1 / 4 / 4 / 10"), ("ps", "P", "1 / 10 / 4 / 13")]),
    "ppp":   (4, [("p", "P", "1 / 1 / 5 / 5"), ("p", "P", "1 / 5 / 5 / 9"), ("p", "P", "1 / 9 / 5 / 13")]),
    "pp":    (4, [("p", "P", "1 / 3 / 5 / 7"), ("p", "P", "1 / 7 / 5 / 11")]),
    "one-l": (5, [("one", "L", "1 / 1 / 6 / 13")]),
    "one-p": (4, [("p", "P", "1 / 5 / 5 / 9")]),
}
BENTO_CYCLE_L = ["xl2", "lll", "2xl", "ll"]


# =============================================================================
# Сайт: конфиг, фото, отзывы, страницы
# =============================================================================

class Site:
    def __init__(self):
        self.cfg = load_json(SRC / "config.json")
        doc = load_json(CONTENT / "photos.json")
        self.photo_list = doc["photos"]
        self.photos = {p["id"]: p for p in self.photo_list}
        self.covers = doc.get("covers", {})
        self.author = self.photos.get(doc.get("author"))
        self.reviews = {r["id"]: r for r in load_json(CONTENT / "reviews.json")["reviews"]}
        self.su = self.cfg["site_url"].rstrip("/")
        self.genres = self.cfg["genres"]
        self.genre_by_key = {g["key"]: g for g in self.genres}
        self.genre_by_slug = {g["slug"]: g for g in self.genres}
        self.registry = {p["slug"]: p for p in self.cfg["pages"]}
        self.redirects = {r["from"]: r["to"] for r in self.cfg.get("redirects", [])}
        self.counts = {}
        for p in self.photo_list:
            self.counts[p["genre"]] = self.counts.get(p["genre"], 0) + 1
        self.today = datetime.date.today()
        self.local = self.scan_local_photos()
        self.pages = {}       # slug -> dict(name, path, data)
        self.page_errors = {}  # name -> message (не читается JSON)

    @staticmethod
    def scan_local_photos():
        """Локальные AVIF/WebP из src/assets/img/photos/: 093-1280.avif → {93: {"avif": [(1280, "…")], "webp": […]}}.
        Их делает tools/photos.py (для портрета и обложек — LCP-кадры). Нет файлов — остаются JPEG с CDN."""
        out = {}
        d = SRC / PHOTO_DIR
        if not d.is_dir():
            return out
        for f in sorted(d.iterdir()):
            m = re.match(r"^(\d+)-(\d+)\.(avif|webp)$", f.name)
            if not m:
                continue
            pid, w, fmt = int(m.group(1)), int(m.group(2)), m.group(3)
            out.setdefault(pid, {}).setdefault(fmt, []).append((w, PHOTO_DIR + "/" + f.name))
        for v in out.values():
            for lst in v.values():
                lst.sort()
        return out

    def load_pages(self):
        for path in sorted(PAGES_DIR.glob("*.json")):
            if path.name.startswith("_"):
                continue
            name = path.stem
            try:
                data = load_json(path)
            except ValueError as e:
                self.page_errors[name] = str(e)
                continue
            if not isinstance(data, dict):
                self.page_errors[name] = "файл должен содержать объект { ... }"
                continue
            slug = data.get("slug") if isinstance(data.get("slug"), str) else expected_slug(name)
            if slug in self.pages:
                self.page_errors[name] = "slug {} уже занят файлом {}.json".format(slug, self.pages[slug]["name"])
                continue
            self.pages[slug] = {"name": name, "path": path, "data": data}

    def label(self, slug):
        p = self.pages.get(slug)
        if p and isinstance(p["data"].get("nav"), str):
            return p["data"]["nav"]
        if slug in self.registry:
            return self.registry[slug]["label"]
        return slug.strip("/") or "Главная"

    def genre_name(self, key):
        if key in self.genre_by_key:
            return self.genre_by_key[key]["name"]
        return self.cfg.get("genre_labels", {}).get(key, key)

    def photo_caption(self, p):
        return "№ {:03d} — {}".format(p["id"], self.genre_name(p["genre"]))

    def genre_photos(self, key):
        return [p for p in self.photo_list if p["genre"] == key]

    def page_photo(self, slug):
        """Обложка страницы для превью: og_image → кадр героя → обложка жанра."""
        p = self.pages.get(slug)
        if p:
            d = p["data"]
            if isinstance(d.get("og_image"), int) and d["og_image"] in self.photos:
                return self.photos[d["og_image"]]
            for b in d.get("blocks", []):
                if isinstance(b, dict) and b.get("type") in HEROES and b.get("photo") in self.photos:
                    return self.photos[b["photo"]]
            g = d.get("genre")
            if g in self.covers:
                return self.photos.get(self.covers[g])
        g = self.genre_by_slug.get(slug)
        if g and g["key"] in self.covers:
            return self.photos.get(self.covers[g["key"]])
        # страница ещё не написана: берём обложку её жанра из карты сайта (config.json → pages[].genre)
        rg = self.registry.get(slug, {}).get("genre")
        if rg in self.covers:
            return self.photos.get(self.covers[rg])
        if slug == "/about/" and self.author:
            return self.author
        return None

    def page_genre(self, slug):
        """Ключ жанра страницы: из её JSON, из списка жанров или из карты сайта."""
        p = self.pages.get(slug)
        if p and isinstance(p["data"].get("genre"), str):
            return p["data"]["genre"]
        if slug in self.genre_by_slug:
            return self.genre_by_slug[slug]["key"]
        return self.registry.get(slug, {}).get("genre")


def expected_slug(name):
    return "/" if name == "home" else "/" + name + "/"


def out_file(slug):
    return OUT / "index.html" if slug == "/" else OUT / slug.strip("/") / "index.html"


def depth_of(slug):
    return 0 if slug == "/" else len([x for x in slug.strip("/").split("/") if x])


# =============================================================================
# Рендер страницы
# =============================================================================

class PageCtx:
    def __init__(self, site, name, data, rep):
        self.site = site
        self.cfg = site.cfg
        self.name = name
        self.d = data
        self.rep = rep
        self.slug = data.get("slug", expected_slug(name))
        self.prefix = "../" * depth_of(self.slug)
        self.type = data.get("type")
        self.faq = []
        self.gallery_photos = []
        self.has_form = any(isinstance(b, dict) and b.get("type") == "lead-form" for b in data.get("blocks", []))
        self.has_lb = False
        self.hero_photo = None
        self.hero_sizes = ""
        self.loader = LOADER   # лоадер первого визита (счётчик); на 404 — только шторка переходов
        self.lb_n = 0
        self.auto_excl = set()
        self.ids = set()
        self.pending = set()   # ссылки на страницы из карты сайта, которых ещё нет
        self.genre = self._genre()

    # ------------------------------------------------------------------ ссылки
    def asset(self, path):
        """Путь к ассету относительно страницы + ?v=хэш содержимого (сброс кэша после пересборки)."""
        src = SRC / path
        ver = ""
        if src.is_file():
            ver = "?v=" + hashlib.md5(src.read_bytes()).hexdigest()[:8]
        return self.prefix + path + ver

    def home(self):
        return self.prefix + "index.html"

    def slug_href(self, slug, frag=""):
        """Относительная ссылка на страницу по slug с явным index.html."""
        if slug == self.slug and frag:
            return "#" + frag
        target = "index.html" if slug == "/" else slug.strip("/") + "/index.html"
        return self.prefix + target + ("#" + frag if frag else "")

    def check_slug(self, slug, where):
        if slug in self.site.pages:
            return
        if slug in self.site.redirects:
            self.rep.warn(where, "ссылка на {} — это старый адрес, пишите {}".format(slug, self.site.redirects[slug]))
        elif slug in self.site.registry:
            self.pending.add(slug)
        else:
            self.rep.warn(where, "битая внутренняя ссылка: страницы {} нет ни в content/pages, ни в карте сайта".format(slug))

    def link(self, url, where="", check=True):
        if not isinstance(url, str) or not url.strip():
            self.rep.warn(where, "пустая ссылка")
            return "#"
        url = url.strip()
        if url == "#zayavka" or url.endswith("/#zayavka"):
            return self.cta_href()          # все CTA «Обсудить съёмку» ведут в Telegram
        if url.startswith(self.site.su + "/"):
            url = url[len(self.site.su):]
        if url.startswith(("http://", "https://", "tel:", "mailto:")):
            return url
        if url.startswith("#"):
            return url
        if url.startswith("/"):
            path, _, frag = url.partition("#")
            last = path.rsplit("/", 1)[-1]
            if last and "." in last:          # файл: /assets/...
                return self.prefix + path.lstrip("/") + ("#" + frag if frag else "")
            if not path.endswith("/"):
                self.rep.warn(where, "ссылка {}: у внутренних адресов на конце нужен слэш — добавил".format(url))
                path += "/"
            if check:
                self.check_slug(path, where)
            if path in self.site.redirects:
                path = self.site.redirects[path]
            return self.slug_href(path, frag)
        self.rep.warn(where, "непонятная ссылка «{}» — используйте /slug/, #якорь, tel:, https://".format(url))
        return url

    @staticmethod
    def ext_attrs(url):
        return ' target="_blank" rel="noopener"' if url.startswith(("http://", "https://")) else ""

    # ------------------------------------------------------------------ текст
    def md(self, s, where="", nw=False):
        """Markdown-lite: **жирный**, *акцент* (в тексте — инверсная плашка, в огромных заголовках — контур),
        [ссылка](/slug/). Плюс типографика."""
        if s is None:
            return ""
        # неразрывный пробел после короткого слова ставим до разбора разметки: иначе «подробнее о [ссылка](…)»
        # режется на куски и «о» повисает в конце строки
        s = RE_SHORT.sub(lambda m: m.group(1) + NBSP, str(s))
        out, pos = [], 0
        for m in RE_INLINE.finditer(s):
            out.append(self._frag(s[pos:m.start()], nw))
            if m.group(1) is not None:
                href = self.link(m.group(2), where)
                out.append('<a href="{}"{}>{}</a>'.format(esc(href), self.ext_attrs(href), self.md(m.group(1), where, nw)))
            elif m.group(3) is not None:
                out.append("<strong>" + self.md(m.group(3), where, nw) + "</strong>")
            else:
                out.append("<em>" + self.md(m.group(4), where, nw) + "</em>")
            pos = m.end()
        out.append(self._frag(s[pos:], nw))
        return "".join(out)

    @staticmethod
    def _frag(s, nw=False):
        t = typo(s)
        if nw:
            t = RE_HYPHEN_WORD.sub(r'<span class="nw">\1</span>', t)
        return t

    def t(self, s):
        """Простой текст с типографикой (без разметки)."""
        return typo(s) if s is not None else ""

    def paras(self, items, where, cls=""):
        """Абзацы: строка → <p>; массив строк → список; строка «### …» → подзаголовок H3."""
        out = []
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list):
            self.rep.err(where, "paragraphs должен быть массивом строк")
            return ""
        for i, p in enumerate(items):
            w = "{}[{}]".format(where, i)
            if isinstance(p, list):
                out.append(self.ul(p, w))
            elif isinstance(p, str):
                if p.startswith("### "):
                    out.append('<h3 class="txt__h3">{}</h3>'.format(self.md(p[4:], w)))
                else:
                    # слова через дефис («Event-фотограф», «бизнес-портрет») в абзацах не рвутся по дефису
                    out.append("<p{}>{}</p>".format(' class="' + cls + '"' if cls else "", self.md(p, w, nw=True)))
            else:
                self.rep.err(w, "абзац должен быть строкой или массивом строк (список)")
        return "".join(out)

    def ul(self, items, where):
        if not isinstance(items, list):
            self.rep.err(where, "список должен быть массивом строк")
            return ""
        return '<ul class="list">' + "".join("<li>{}</li>".format(self.md(x, where, nw=True)) for x in items) + "</ul>"

    @staticmethod
    def letters(word, start=0, cls="ch", gen=False):
        """Слово → буквы в масках (для появления снизу по одной, задержка по --i).
        gen=True — буква рисуется через CSS (content: attr(data-c)): в тексте заголовка её нет, поэтому
        H1/H2 с буквами-масками читаются поисковиком и скринридером один раз — из скрытой строки .vh рядом."""
        out = []
        k = start
        for c in str(word):
            if c == " ":
                out.append(" ")
                continue
            if gen:
                out.append('<span class="{}" style="--i:{}" data-c="{}"></span>'.format(cls, k, esc(c)))
            else:
                out.append('<span class="{}" style="--i:{}">{}</span>'.format(cls, k, esc(c)))
            k += 1
        return "".join(out), k

    # ------------------------------------------------------------------ фото
    def photo(self, pid, where):
        if isinstance(pid, bool) or not isinstance(pid, int):
            self.rep.err(where, "id фото должен быть числом, получено {!r}".format(pid))
            return None
        p = self.site.photos.get(pid)
        if not p:
            self.rep.err(where, "фото с id {} нет в content/photos.json (есть 0–{})".format(pid, max(self.site.photos)))
        return p

    def purl(self, u):
        """URL фото для HTML: внешний — как есть, свой (/assets/...) — относительный от страницы."""
        return self.prefix + u.lstrip("/") if isinstance(u, str) and u.startswith("/") else u

    def aurl(self, u):
        """URL фото для SEO (og:image, JSON-LD): всегда абсолютный."""
        return self.site.su + u if isinstance(u, str) and u.startswith("/") else u

    def srcset(self, p):
        return "{} 480w, {} 1280w, {} 1920w".format(self.purl(p["thumb"]), self.purl(p["mid"]), self.purl(p["large"]))

    def img(self, p, sizes, lazy=True, priority=False, alt=None, cls=""):
        srcset = self.srcset(p)
        a = ['src="{}"'.format(esc(self.purl(p["mid"]))), 'srcset="{}"'.format(esc(srcset)), 'sizes="{}"'.format(esc(sizes)),
             'width="{}"'.format(p["w"]), 'height="{}"'.format(p["h"]),
             'alt="{}"'.format(esc(p.get("alt", "") if alt is None else alt))]
        if cls:
            a.insert(0, 'class="{}"'.format(cls))
        if priority:
            a.append('fetchpriority="high"')
        elif lazy:
            a.append('loading="lazy"')
        if not priority:
            a.append('decoding="async"')
        tag = "<img " + " ".join(a) + ">"
        loc = self.site.local.get(p["id"])
        if not loc:
            return tag
        srcs = "".join('<source type="image/{}" srcset="{}" sizes="{}">'.format(fmt, esc(self.local_srcset(loc[fmt])), esc(sizes))
                       for fmt in ("avif", "webp") if loc.get(fmt))
        return "<picture>" + srcs + tag + "</picture>"

    def local_srcset(self, lst):
        return ", ".join("{}{} {}w".format(self.prefix, path, w) for w, path in lst)

    def thumb(self, p, cls="", alt=""):
        return '<img{} src="{}" width="480" height="{}" alt="{}" loading="lazy" decoding="async">'.format(
            ' class="{}"'.format(cls) if cls else "", esc(self.purl(p["thumb"])), round(480 * p["h"] / p["w"]), esc(alt))

    @staticmethod
    def orient(p):
        return "P" if p.get("orientation") == "portrait" else "L"

    @staticmethod
    def focus_vars(focus):
        if not focus:
            return ""
        fx, _, fy = str(focus).partition(" ")
        return "--fx:{};--fy:{}".format(fx, fy or "50%")

    # ------------------------------------------------------------------ жанр страницы
    def _genre(self):
        g = self.d.get("genre")
        if isinstance(g, str) and g in self.site.genre_by_key:
            return self.site.genre_by_key[g]
        if self.slug in self.site.genre_by_slug:
            return self.site.genre_by_slug[self.slug]
        return None

    def nav_label(self):
        return self.d.get("nav") or self.site.label(self.slug)

    def cta_href(self):
        """Все CTA ведут в Telegram (config.json → contacts.telegram). Форм на сайте нет."""
        return self.cfg["contacts"]["telegram"]

    def is_cta(self, href):
        return href == self.cta_href()

    def dt_attr(self, href):
        """Атрибуты ссылки CTA: внешние — в новой вкладке; у Telegram-CTA метка data-cta для аналитики."""
        return self.ext_attrs(href) + (' data-cta="tg"' if self.is_cta(href) else "")

    @staticmethod
    def arr(href):
        return "↗" if href.startswith(("http://", "https://")) else "→"

    # ------------------------------------------------------------------ секция
    def section(self, b, inner, cls="", header=True, wrap=True, style=None):
        classes = ["sec", "b-" + b["type"]]
        if cls:
            classes.append(cls)
        sid = b["_id"]
        head = self.sh(b, style) if header else ""
        labelled = ' aria-labelledby="h-{}"'.format(sid) if (head and b.get("title")) else ""
        body = head + inner
        if wrap:
            body = '<div class="wrap">' + body + "</div>"
        return '<section class="{}" id="{}"{}>{}</section>'.format(" ".join(classes), sid, labelled, body)

    def sh(self, b, style=None):
        """Шапка секции: пунктирная метка с «лампой» + огромный заголовок H2 (или H2 в виде метки)."""
        style = b.get("title_style") or style or "xl"
        if style not in ("xl", "l", "tag"):
            self.rep.warn(b["_w"], "title_style бывает xl | l | tag")
            style = "xl"
        title, eyebrow, note, meta = b.get("title"), b.get("eyebrow"), b.get("note"), b.get("meta")
        if not (title or eyebrow):
            return ""
        if title and eyebrow:
            # метка над заголовком не повторяет его: «Кейс» над «Кейс», «Как снимаю» над «Как снимаю сцену» — тавтология
            # на экране и двойное чтение скринридером
            e, t = norm(eyebrow).rstrip(".:"), norm(title)
            if t == e or t.startswith(e + " ") or t.startswith(e + ":"):
                eyebrow = None
        sid, w = b["_id"], b["_w"]
        meta_html = '<span class="sh__meta mono">{}</span>'.format(self.md(meta, w + ".meta")) if meta else ""
        note_html = '<p class="sh__note">{}</p>'.format(self.md(note, w + ".note")) if note else ""
        lamp = '<i class="lamp" aria-hidden="true"></i>'
        if style == "tag" or not title:
            tagname = "h2" if title else "p"
            idattr = ' id="h-{}"'.format(sid) if title else ""
            top = '<div class="sh__top"><{t} class="tag"{i}>{l}<span>{x}</span></{t}>{m}</div>'.format(
                t=tagname, i=idattr, l=lamp, x=self.md(title or eyebrow, w), m=meta_html)
            return '<header class="sh sh--tag" data-rv>{}{}</header>'.format(top, note_html)
        top = ""
        if eyebrow or meta:
            tg = '<p class="tag">{}<span>{}</span></p>'.format(lamp, self.t(eyebrow)) if eyebrow else "<span></span>"
            top = '<div class="sh__top">{}{}</div>'.format(tg, meta_html)
        h2 = '<h2 class="sh__t sh__t--{s}" id="h-{i}"><span class="mk"><span>{x}</span></span></h2>'.format(
            s=style, i=sid, x=self.md(title, w + ".title"))
        return '<header class="sh sh--{}" data-rv>{}{}{}</header>'.format(style, top, h2, note_html)

    def actions(self, acts, where, default=None, cls="acts"):
        if acts is None:
            acts = default or []
        if not isinstance(acts, list):
            self.rep.err(where, "actions должен быть массивом объектов {label, href, style}")
            return ""
        out = []
        for i, a in enumerate(acts):
            w = "{}[{}]".format(where, i)
            if not isinstance(a, dict) or "label" not in a or "href" not in a:
                self.rep.err(w, "у кнопки нужны поля label и href")
                continue
            href = self.link(a["href"], w)
            style = a.get("style", "btn")
            if style in ("btn", "ghost"):
                out.append('<a class="btn{}" href="{}"{}><span>{}</span><span class="arr" aria-hidden="true">{}</span></a>'.format(
                    " btn--ghost" if style == "ghost" else "", esc(href), self.dt_attr(href), self.t(a["label"]), self.arr(href)))
            else:
                out.append('<a class="ulink" href="{}"{}>{}</a>'.format(esc(href), self.dt_attr(href), self.t(a["label"])))
        return '<div class="{}">{}</div>'.format(cls, "".join(out)) if out else ""

    # ------------------------------------------------------------------ подготовка блоков
    def prepare(self):
        blocks = self.d.get("blocks", [])
        if not isinstance(blocks, list):
            return
        heroes = 0
        for i, b in enumerate(blocks):
            if not isinstance(b, dict):
                continue
            b["_w"] = "blocks[{}] {}".format(i, b.get("type", "?"))
            t = b.get("type")
            if t in HEROES:
                heroes += 1
            if t in BLOCK_SPECS:
                base = b.get("id") if isinstance(b.get("id"), str) else DEFAULT_IDS.get(t, t)
                if t == "lead-form":
                    if b.get("id") and b["id"] != "zayavka":
                        self.rep.warn(b["_w"], "id блока lead-form всегда «zayavka» (CTA-полоса Telegram; на неё ведёт якорь #zayavka в тексте)")
                    base = "zayavka"
                sid, k = base, 2
                while sid in self.ids:
                    sid = "{}-{}".format(base, k)
                    k += 1
                if isinstance(b.get("id"), str) and sid != b["id"] and t != "lead-form":
                    self.rep.err(b["_w"], "id «{}» уже используется на странице".format(b["id"]))
                self.ids.add(sid)
                b["_id"] = sid
        # кадры первого экрана и кейсов не повторяются в галерее той же страницы (иначе один снимок идёт дважды подряд)
        self.auto_excl = set()
        k = 0
        for b in blocks:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t in HEROES and isinstance(b.get("photo"), int) and not isinstance(b.get("photo"), bool):
                self.auto_excl.add(b["photo"])
            elif t == "cases":
                for it in b.get("items") or []:
                    if isinstance(it, dict) and isinstance(it.get("photo"), int) and not isinstance(it.get("photo"), bool):
                        self.auto_excl.add(it["photo"])
            elif t == "gallery":
                k += 1
                b["_group"] = b.get("group") if isinstance(b.get("group"), str) else "g{}".format(k)
        if heroes == 0:
            self.rep.err("blocks", "на странице нет первого экрана (hero-person или genre-hero) — в нём живёт единственный H1")
        elif heroes > 1:
            self.rep.err("blocks", "первый экран (hero-person / genre-hero) должен быть один — один H1 на страницу")
        if sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "lead-form") > 1:
            self.rep.err("blocks", "блок lead-form (CTA-полоса Telegram) должен быть один на странице")

    def validate_block(self, b):
        t = b.get("type")
        spec = BLOCK_SPECS[t]
        for f, ty in spec["req"].items():
            if f not in b:
                self.rep.err(b["_w"], "нет обязательного поля «{}»".format(f))
            elif not TYPE_CHECK[ty](b[f]):
                self.rep.err(b["_w"], "поле «{}» должно быть: {}".format(f, TYPE_NAMES[ty]))
        for f, v in b.items():
            if f in ("type",) or f.startswith("_"):
                continue
            if f in spec["req"]:
                continue
            if f not in spec["opt"]:
                self.rep.warn(b["_w"], "неизвестное поле «{}» — будет проигнорировано".format(f))
            elif not TYPE_CHECK[spec["opt"][f]](v):
                self.rep.err(b["_w"], "поле «{}» должно быть: {}".format(f, TYPE_NAMES[spec["opt"][f]]))

    # =================================================================== БЛОКИ
    def render_block(self, b):
        t = b.get("type")
        fn = getattr(self, "b_" + t.replace("-", "_"))
        return fn(b)

    # ------------------------------------------------------------------ крошки
    def crumbs(self):
        if self.slug == "/" or self.d.get("breadcrumbs") is False:
            return ""
        items = self.crumb_items()
        parts = []
        for i, (label, slug) in enumerate(items):
            if i == len(items) - 1:
                parts.append('<li><span aria-current="page">{}</span></li>'.format(self.t(label)))
            else:
                parts.append('<li><a href="{}">{}</a></li>'.format(esc(self.slug_href(slug)), self.t(label)))
        return '<nav class="crumbs mono" aria-label="Хлебные крошки"><ol>{}</ol></nav>'.format("".join(parts))

    def crumb_items(self):
        items = [("Главная", "/")]
        parent = self.d.get("parent")
        if parent is None and self.type == "seo":
            parent = "/uslugi/"
        if isinstance(parent, str) and parent not in ("/", self.slug):
            if not parent.endswith("/"):
                parent += "/"
            items.append((self.site.label(parent), parent))
        items.append((self.nav_label(), self.slug))
        return items

    # ------------------------------------------------------------------ hero-person (главная)
    def b_hero_person(self, b):
        w = b["_w"]
        p = self.photo(b.get("photo"), w + ".photo")
        name = b.get("name") or self.cfg["person"]["name"].split()
        tags = b.get("tags") or ["фотограф", "и видеограф", "в " + self.cfg.get("city_in", self.cfg["city"])]
        if not all(isinstance(x, str) and x for x in name + tags):
            self.rep.err(w, "name и tags — массивы непустых строк")
            return ""
        h1 = " ".join(name) + " — " + " ".join(tags)
        if norm(h1) != norm(self.d.get("h1", "")):
            self.rep.warn(w, "имя + метки («{}») не совпадают с h1 страницы («{}»)".format(h1, self.d.get("h1", "")))
        k = 0
        words = []
        for word in name:
            chs, k = self.letters(word, k, gen=True)
            words.append('<span class="hp__w" style="--n:{}"><span class="hp__in">{}</span></span>'.format(len(word), chs))
        # буквы в масках — только для глаз (aria-hidden); скринридер читает имя целиком из скрытой строки
        name_html = '<span class="vh">{}</span><span class="hp__name" data-fit-name aria-hidden="true" style="--n:{}">{}</span>'.format(
            esc(" ".join(name)), len(" ".join(name)), '<span class="hp__sp"> </span>'.join(words))

        def tag_html(i, t):
            first, _, rest = t.partition(" ")
            if rest and first.lower() in ("и", "в", "во", "на", "с", "для"):
                inner = '<span class="dim">{}</span> {}'.format(esc(first), esc(rest))
            else:
                inner = esc(t)
            return '<span class="hp__tag" style="--i:{}"><span>{}</span></span>'.format(i, inner)
        labels = b.get("labels")
        if labels:
            # плашки — короткие слова для глаз («Фотограф · Видеограф · Новосибирск»); H1 для поиска и скринридера —
            # полная фраза из tags («фотограф и видеограф в Новосибирске») в скрытой строке
            if not all(isinstance(x, str) and x for x in labels):
                self.rep.err(w + ".labels", "labels — массив непустых строк")
                labels = tags
            tags_html = '<span class="vh">{}</span><span class="hp__tags" aria-hidden="true">{}</span>'.format(
                esc(" ".join(tags)), " ".join('<span class="hp__tag" style="--i:{}"><span>{}</span></span>'.format(i, esc(x)) for i, x in enumerate(labels)))
        else:
            tags_html = '<span class="hp__tags">{}</span>'.format(" ".join(tag_html(i, x) for i, x in enumerate(tags)))
        h1_html = '<h1 class="hp__h1" id="h1">{}<span class="vh"> — </span>{}</h1>'.format(name_html, tags_html)
        meta = b.get("meta") or [self.cfg["brand"], self.cfg["city"]]
        meta_html = '<div class="hp__meta mono">{}</div>'.format("".join("<span>{}</span>".format(self.t(m)) for m in meta))
        lead = '<p class="hp__lead">{}</p>'.format(self.md(b["lead"], w + ".lead")) if b.get("lead") else ""
        acts = self.actions(b.get("actions"), w + ".actions", default=[{"label": "Обсудить съёмку", "href": "#zayavka"}],
                            cls="acts hp__acts")
        msg = ""
        if b.get("messengers", True):
            c = self.cfg["contacts"]
            msg = '<p class="hp__msg mono">Telegram: <a href="{}" target="_blank" rel="noopener" data-cta="tg">{}</a> · <a href="{}" target="_blank" rel="noopener">WhatsApp</a></p>'.format(
                esc(c["telegram"]), esc(c.get("telegram_handle", "Telegram")), esc(c["whatsapp"]))
        fig = ""
        if p:
            sizes = "(min-width: 900px) 40vw, 86vw"
            self.hero_photo, self.hero_sizes = p, sizes
            fig = '<figure class="hp__photo" style="{}">{}</figure>'.format(
                esc(self.focus_vars(b.get("focus") or p.get("focus"))), self.img(p, sizes, priority=True))
        return ('<section class="hp" id="{id}" aria-labelledby="h1">'
                '<div class="hp__glow grain" aria-hidden="true"></div>{fig}'
                '<div class="hp__screen wrap">{meta}<div class="hp__bottom">{h1}</div></div>'
                '<div class="hp__mid wrap"><div class="hp__side hp__side--l">{lead}</div>'
                '<div class="hp__side hp__side--r">{acts}{msg}</div></div></section>').format(
            id=b["_id"], fig=fig, meta=meta_html, h1=h1_html, lead=lead, acts=acts, msg=msg)

    # ------------------------------------------------------------------ genre-hero (жанр, SEO, служебные)
    def b_genre_hero(self, b):
        w = b["_w"]
        g = self.genre if self.type == "genre" else None
        word = b.get("word") or (g["name"] if g else self.nav_label())
        variant = b.get("variant") or ("photo" if "photo" in b else "text")
        if variant not in ("photo", "solo", "text"):
            self.rep.warn(w, "variant бывает photo | solo | text")
            variant = "photo"
        p = self.photo(b["photo"], w + ".photo") if "photo" in b else None
        if variant != "text" and not p:
            if "photo" not in b:
                self.rep.err(w, "для variant «{}» нужно поле photo (id кадра)".format(variant))
            variant = "text"
        facts = b.get("facts")
        if facts is None:
            facts = []
            if g:
                # число кадров показываем, только когда оно работает на доверие (≥ MIN_COUNT_SHOWN):
                # «1 кадр» у свадеб или авто выдаёт тонкое портфолио
                n = self.site.counts.get(g["key"], 0)
                if n >= MIN_COUNT_SHOWN:
                    facts.append("{} {}".format(n, plural(n, "кадр", "кадра", "кадров")))
            facts += [self.cfg["city"], "Стоимость — по запросу"]
        facts_html = '<p class="gh__facts mono">{}</p>'.format("".join("<span>{}</span>".format(self.t(x)) for x in facts)) if facts else ""
        chs, _ = self.letters(word)
        # data-fit-max: слово подгоняется по ширине, но не выше доли окна — короткое слово («Отзывы») не съедает первый экран
        word_html = '<p class="gh__word" aria-hidden="true" data-fit data-fit-max="{}" style="--n:{}"><span class="gh__in">{}</span></p>'.format(
            FIT_MAX_WORD, len(word), chs)
        eb = '<p class="gh__eyebrow tag"><i class="lamp" aria-hidden="true"></i><span>{}</span></p>'.format(self.t(b["eyebrow"])) if b.get("eyebrow") else ""
        lead = '<p class="gh__lead">{}</p>'.format(self.md(b["lead"], w + ".lead", nw=True)) if b.get("lead") else ""
        default = [{"label": "Обсудить съёмку", "href": "#zayavka"}]
        gal = self.first_gallery()
        if gal and gal.get("_id"):
            default.append({"label": "Смотреть кадры ↓", "href": "#" + gal["_id"], "style": "link"})
        acts = self.actions(b.get("actions"), w + ".actions", default=default)
        fig = ""
        portrait = bool(p) and variant == "photo" and self.orient(p) == "P"
        if p and variant in ("photo", "solo"):
            sizes = "(min-width: 900px) 560px, calc(100vw - 32px)" if portrait else "calc(100vw - 32px)"
            self.hero_photo, self.hero_sizes = p, sizes
            self.has_lb = True
            style = self.focus_vars(b.get("focus") or p.get("focus"))
            if b.get("focus_mobile"):
                fx, _, fy = b["focus_mobile"].partition(" ")
                style += ";--mfx:{};--mfy:{}".format(fx, fy or "50%")
            # подпись на кадре — только короткая строка из JSON (caption), без номера из архива и без alt:
            # alt живёт в alt, клиенту номер кадра ничего не говорит
            cap = b.get("caption")
            capt = '<figcaption class="gh__cap mono"><span>{}</span></figcaption>'.format(self.t(cap)) if cap else ""
            # обложка открывается в лайтбоксе первой, дальше стрелками — кадры галереи страницы
            wth = ' data-lb-with="{}"'.format(esc(gal["_group"])) if gal and gal.get("_group") else ""
            fig = ('<figure class="gh__fig{pc}" style="{st}"><a class="gh__a" href="{href}" data-lb="hero"{wth} data-cap="{cap}">{img}</a>'
                   '{capt}</figure>').format(
                pc=" gh__fig--portrait" if portrait else "", st=esc(style.strip(";")), href=esc(self.purl(p["large"])),
                wth=wth, cap=esc(cap or self.lb_cap(p)), img=self.img(p, sizes, priority=True), capt=capt)
        row = ('<div class="gh__row"><h1 class="gh__h1" id="h1">{h1}</h1><div class="gh__side">{lead}{acts}</div></div>').format(
            h1=self.md(self.d.get("h1", ""), w, nw=True), lead=lead, acts=acts)
        if portrait:
            # вертикальный кадр (портрет автора) не режем в полосу 21:9: он встаёт рядом с H1 и текстом в пропорции 4:5
            return ('<section class="gh gh--photo gh--portrait" id="{id}" aria-labelledby="h1"><div class="wrap">'
                    '<div class="gh__top">{crumbs}{facts}</div>{eb}{word}<div class="gh__pr">{row}{fig}</div></div></section>').format(
                id=b["_id"], crumbs=self.crumbs(), facts=facts_html, eb=eb, word=word_html, row=row, fig=fig)
        return ('<section class="gh gh--{v}" id="{id}" aria-labelledby="h1"><div class="wrap">'
                '<div class="gh__top">{crumbs}{facts}</div>{eb}{word}{row}'
                '</div>{fig}</section>').format(
            v=variant, id=b["_id"], crumbs=self.crumbs(), facts=facts_html, eb=eb, word=word_html, row=row, fig=fig)

    # ------------------------------------------------------------------ genre-tabs
    def b_genre_tabs(self, b):
        active = b.get("active") or (self.genre["slug"] if self.genre else self.slug)
        lis = []
        # вместо числа кадров — порядковый номер 01–07, как в списке направлений (счётчик «1» выдаёт тонкое портфолио)
        for i, g in enumerate(self.site.genres):
            cur = ' aria-current="page"' if g["slug"] == active else ""
            self.check_slug(g["slug"], b["_w"])
            lis.append('<li><a href="{}"{}><span class="br" aria-hidden="true">[</span><span class="gtabs__no" aria-hidden="true">{:02d}</span>{}<span class="br" aria-hidden="true">]</span></a></li>'.format(
                esc(self.slug_href(g["slug"])), cur, i + 1, self.t(g["name"])))
        label = b.get("label", "Направления")
        return ('<nav class="gtabs" id="{}" aria-label="{}"><div class="wrap gtabs__in">'
                '<span class="gtabs__label mono" aria-hidden="true">{}</span><div class="gtabs__sc" data-tabs><ul>{}</ul></div></div></nav>').format(
            b["_id"], esc(label), self.t(label), "".join(lis))

    # ------------------------------------------------------------------ directions (список iampolie)
    def dir_list(self, items, compact=False):
        """items: [{href, label, cat, no, photos:[p, …]}] → список огромных слов + два плавающих превью."""
        rows, left, right = [], [], []
        for i, it in enumerate(items):
            ph = it["photos"]
            th = self.thumb(ph[0], "dir__th") if ph else ""
            rows.append(('<li class="dir__i"><a class="dir__a" href="{href}" data-i="{i}">'
                         '<span class="dir__cat mono">{cat}</span><span class="dir__w">{label}</span>'
                         '<span class="dir__no mono">{no}</span><span class="dir__go" aria-hidden="true">→</span>{th}</a></li>').format(
                href=esc(it["href"]), i=i, cat=self.t(it.get("cat", "")), label=self.t(it["label"]),
                no=esc(it.get("no", "")), th=th))
            if ph:
                right.append(self.thumb(ph[0], alt="").replace("<img ", '<img data-i="{}" '.format(i), 1))
            if len(ph) > 1:
                left.append(self.thumb(ph[1], alt="").replace("<img ", '<img data-i="{}" '.format(i), 1))
        pv = ('<div class="dir__pv" aria-hidden="true"><div class="dir__card dir__card--l">{}</div>'
              '<div class="dir__card dir__card--r">{}</div></div>').format("".join(left), "".join(right))
        return '<div class="dir-box{}" data-dir><ul class="dir">{}</ul>{}</div>'.format(
            " dir-box--s" if compact else "", "".join(rows), pv)

    def genre_previews(self, key, override=None):
        if isinstance(override, list) and override:
            return [p for p in (self.site.photos.get(x) for x in override) if p]
        cover = self.site.photos.get(self.site.covers.get(key))
        rest = [p for p in self.site.genre_photos(key) if not cover or p["id"] != cover["id"]]
        second = next((p for p in rest if p.get("orientation") == "portrait"), rest[0] if rest else None)
        return [x for x in (cover, second) if x]

    def b_directions(self, b):
        w = b["_w"]
        prev = b.get("previews") or {}
        items = []
        slugs = b.get("items")
        if slugs is None:
            slugs = [g["slug"] for g in self.site.genres]
        for i, it in enumerate(slugs):
            wi = "{}.items[{}]".format(w, i)
            if isinstance(it, str):
                it = {"slug": it}
            if not isinstance(it, dict) or not isinstance(it.get("slug"), str):
                self.rep.err(wi, "элемент — строка \"/slug/\" или объект {\"slug\": …, \"label\": …, \"cat\": …}")
                continue
            slug = it["slug"] if it["slug"].endswith("/") else it["slug"] + "/"
            g = self.site.genre_by_slug.get(slug)
            href = self.link(slug, wi)
            photos = self.genre_previews(g["key"], prev.get(g["key"])) if g else [x for x in [self.site.page_photo(slug)] if x]
            items.append({"href": href, "label": it.get("label") or (g["name"] if g else self.site.label(slug)),
                          "cat": it.get("cat") or (g.get("cat", "") if g else ""), "no": "{:02d}".format(i + 1), "photos": photos})
        head = '<div class="wrap">{}</div>'.format(self.sh(b, "tag"))
        return '<section class="sec b-directions" id="{}"{}>{}{}</section>'.format(
            b["_id"], ' aria-labelledby="h-{}"'.format(b["_id"]) if b.get("title") else "", head, self.dir_list(items))

    def b_related_directions(self, b):
        w = b["_w"]
        items = []
        used = {self.hero_photo["id"]} if self.hero_photo else set()
        for i, it in enumerate(b.get("items") or []):
            wi = "{}.items[{}]".format(w, i)
            if isinstance(it, str):
                it = {"slug": it}
            if not isinstance(it, dict) or not isinstance(it.get("slug"), str):
                self.rep.err(wi, "элемент — строка \"/slug/\" или объект {\"slug\": …, \"label\": …, \"cat\": …}")
                continue
            slug = it["slug"] if it["slug"].endswith("/") else it["slug"] + "/"
            if slug == self.slug:
                self.rep.warn(wi, "ссылка на эту же страницу — пропущена")
                continue
            if slug not in self.site.pages and slug not in self.site.registry:
                self.rep.warn(wi, "страницы {} нет в карте сайта — пропущена".format(slug))
                continue
            g = self.site.genre_by_slug.get(slug)
            href = self.link(slug, wi)
            p = self.photo(it["photo"], wi + ".photo") if "photo" in it else self.site.page_photo(slug)
            if p is None or (p["id"] in used and "photo" not in it):
                # миниатюра нужна у каждой строки (ритм списка): берём другой кадр того же жанра
                gk = self.site.page_genre(slug)
                alt = [x for x in self.site.genre_photos(gk) if x["id"] not in used] if gk else []
                alt.sort(key=lambda x: x.get("orientation") != "landscape")
                p = alt[0] if alt else p
            if p:
                used.add(p["id"])
            td = self.site.pages.get(slug, {}).get("data", {})
            cat = it.get("cat") or (g.get("cat", "") if g else ("Услуга" if td.get("type") == "seo" or self.site.registry.get(slug, {}).get("type") == "seo" else "Раздел"))
            items.append({"href": href, "label": it.get("label") or self.site.label(slug), "cat": cat,
                          "no": "→", "photos": [p] if p else []})
        if not items:
            self.rep.warn(w, "ни одной ссылки — блок пропущен")
            return ""
        b.setdefault("title", "Другие направления")
        layout = b.get("layout", "list")
        if layout not in ("list", "grid"):
            self.rep.warn(w, "layout бывает list | grid")
            layout = "list"
        if layout == "grid":
            # компактная сетка (2–3 колонки): миниатюра, подпись-категория и слово поменьше — когда списков на странице
            # несколько подряд (/uslugi/), второй и третий огромный список превращают страницу в стену слов
            lis = []
            for it in items:
                ph = it["photos"]
                th = self.thumb(ph[0], "rg__th") if ph else '<span class="rg__th" aria-hidden="true"></span>'
                lis.append(('<li class="rg__i"><a class="rg__a" href="{h}">{th}<span class="rg__txt"><span class="rg__cat mono">{c}</span>'
                            '<span class="rg__w">{l}</span></span><span class="rg__go" aria-hidden="true">→</span></a></li>').format(
                    h=esc(it["href"]), th=th, c=self.t(it.get("cat", "")), l=self.t(it["label"])))
            head = self.sh(b, "tag")
            return '<section class="sec b-related b-related--grid" id="{}" aria-labelledby="h-{}"><div class="wrap">{}<ul class="rg">{}</ul></div></section>'.format(
                b["_id"], b["_id"], head, "".join(lis))
        head = '<div class="wrap">{}</div>'.format(self.sh(b, "tag"))
        return '<section class="sec b-directions b-related" id="{}" aria-labelledby="h-{}">{}{}</section>'.format(
            b["_id"], b["_id"], head, self.dir_list(items, compact=True))

    # ------------------------------------------------------------------ gallery (бенто)
    def first_gallery(self):
        """Первая галерея страницы, которая будет показана (для кнопки «Смотреть кадры ↓» и лайтбокса обложки)."""
        return next((x for x in self.d.get("blocks", []) if isinstance(x, dict) and x.get("type") == "gallery"
                     and self.gallery_visible(x)), None)

    def gallery_ids(self, b):
        """Список id кадров галереи после exclude, авто-исключения (кадры первого экрана и кейсов) и limit — без сообщений."""
        if isinstance(b.get("ids"), list):
            ids = [x if isinstance(x, int) else x.get("id") for x in b["ids"]
                   if (isinstance(x, int) and not isinstance(x, bool)) or (isinstance(x, dict) and "id" in x)]
        else:
            ids = [p["id"] for p in self.site.photo_list if p["genre"] == b.get("genre")]
        excl = set(b.get("exclude") or []) | getattr(self, "auto_excl", set())
        ids = [x for x in ids if x not in excl]
        if isinstance(b.get("limit"), int):
            ids = ids[:b["limit"]]
        return ids

    def gallery_visible(self, b):
        """Будет ли галерея показана (есть кадры и их не меньше min) — без сообщений об ошибках."""
        n = len(self.gallery_ids(b))
        return n > 0 and not (isinstance(b.get("min"), int) and n < b["min"])

    def lb_cap(self, p):
        """Подпись в лайтбоксе: на главной и жанрах — «№ 020 — Портреты», на SEO и служебных — название страницы
        (метка чужого жанра и alt-описание там выглядят каталогом, а не портфолио)."""
        if self.type in ("home", "genre"):
            return self.site.photo_caption(p)
        return self.nav_label()

    def gallery_items(self, b):
        w = b["_w"]
        items = []
        if "ids" in b:
            for k, it in enumerate(b["ids"]):
                wk = "{}.ids[{}]".format(w, k)
                if isinstance(it, int) and not isinstance(it, bool):
                    items.append({"id": it})
                elif isinstance(it, dict) and "id" in it:
                    items.append(dict(it))
                else:
                    self.rep.err(wk, "элемент галереи — число (id фото) или {\"id\": …, \"focus\": …}")
        elif "genre" in b:
            g = b["genre"]
            if g not in self.site.genre_by_key and g != "hero":
                self.rep.err(w + ".genre", "нет такого жанра «{}» (есть: {})".format(g, ", ".join(self.site.genre_by_key)))
            items = [{"id": p["id"]} for p in self.site.photo_list if p["genre"] == g]
        else:
            self.rep.err(w, "укажите ids (список id фото) или genre (ключ жанра)")
        if "ids" in b and isinstance(b.get("genre"), str):
            g = b["genre"]
            own = [p["id"] for p in self.site.photo_list if p["genre"] == g]
            listed = [x.get("id") for x in items]
            alien = [i for i in listed if i in self.site.photos and self.site.photos[i]["genre"] != g]
            lost = [i for i in own if i not in listed and i not in (b.get("exclude") or []) and i not in self.auto_excl]
            if alien:
                self.rep.warn(w, "кадры не из жанра {}: {}".format(g, ", ".join(map(str, alien))))
            if lost:
                self.rep.warn(w, "в галерее нет кадров жанра {}: {} (добавьте или перечислите в exclude)".format(g, ", ".join(map(str, lost))))
        excl = b.get("exclude") or []
        auto = [x.get("id") for x in items if x.get("id") in self.auto_excl and x.get("id") not in excl]
        if auto:
            self.rep.note("{}: кадры {} уже стоят в первом экране или кейсе — в галерее не повторяем".format(
                w, ", ".join(map(str, auto))))
        items = [x for x in items if x.get("id") not in excl and x.get("id") not in self.auto_excl]
        if isinstance(b.get("limit"), int):
            items = items[:b["limit"]]
        seen = set()
        for x in items:
            x["_p"] = self.photo(x["id"], w + ".ids")
            if x["id"] in seen:
                self.rep.warn(w, "кадр {} повторяется в галерее".format(x["id"]))
            seen.add(x["id"])
        return [x for x in items if x.get("_p")]

    def bento_groups(self, items):
        """Раскладывает кадры по шаблонам рядов BENTO. Внутри ряда порядок может чуть поменяться
        (вертикальный кадр встаёт в вертикальный слот)."""
        queue = list(items)
        groups = []
        cyc = 0
        flip = 0
        while queue:
            head = queue[:3]
            o = [self.orient(x["_p"]) for x in head]
            n = len(head)
            if n == 1:
                groups.append(("one-p" if o[0] == "P" else "one-l", [queue.pop(0)]))
                continue
            pcount = o.count("P")
            if n == 2 and pcount != 1:
                groups.append(("pp" if pcount == 2 else "ll", [queue.pop(0), queue.pop(0)]))
                continue
            if pcount == 0:
                name = BENTO_CYCLE_L[cyc % len(BENTO_CYCLE_L)]
                cyc += 1
                take = len(BENTO[name][1])
                groups.append((name, [queue.pop(0) for _ in range(take)]))
            elif pcount == 1:
                pi = o.index("P")
                li = o.index("L")
                pit, lit = head[pi], head[li]
                queue.remove(pit)
                queue.remove(lit)
                name = "xlp" if flip % 2 == 0 else "pxl"
                flip += 1
                groups.append((name, [lit, pit] if name == "xlp" else [pit, lit]))
            elif pcount == 2:
                ps = [x for x, oo in zip(head, o) if oo == "P"]
                ls = [x for x, oo in zip(head, o) if oo == "L"]
                for x in head:
                    queue.remove(x)
                groups.append(("plp", [ps[0], ls[0], ps[1]]))
            else:
                for x in head:
                    queue.remove(x)
                groups.append(("ppp", head))
        return groups

    def ph(self, x, group, style, sizes, dl=0, cls="ph"):
        p = x["_p"]
        self.gallery_photos.append(p)
        self.has_lb = True
        f = self.focus_vars(x.get("focus") or p.get("focus"))
        st = ";".join(s for s in (style, f, "--dl:{}ms".format(dl) if dl else "") if s)
        # подпись «№ 020 · Портреты» — только на главной и жанрах; на SEO и служебных страницах метка чужого жанра
        # («Корпоративы» на странице конференций) выглядит каталогом — там подписи нет
        cap = ""
        if self.type in ("home", "genre"):
            cap = '<figcaption class="ph__cap mono"><span class="ph__no">№&nbsp;{:03d}</span><span class="ph__g">{}</span></figcaption>'.format(
                p["id"], self.t(self.site.genre_name(p["genre"])))
        return ('<figure class="{cls}" data-rv style="{st}"><a class="ph__a" href="{href}" data-lb="{g}" data-cap="{lc}">{img}</a>'
                '{cap}</figure>').format(
            cls=cls, st=esc(st), href=esc(self.purl(p["large"])), g=esc(group), lc=esc(self.lb_cap(p)),
            img=self.img(p, sizes), cap=cap)

    def b_gallery(self, b):
        items = self.gallery_items(b)
        n = len(items)
        if isinstance(b.get("min"), int) and n < b["min"]:
            self.rep.note("{}: кадров {} < min {} — галерея скрыта".format(b["_w"], n, b["min"]))
            return ""
        if n == 0:
            self.rep.warn(b["_w"], "в галерее нет ни одного кадра — блок пропущен")
            return ""
        layout = b.get("layout", "bento")
        if layout not in ("bento", "solo"):
            self.rep.warn(b["_w"], "layout бывает bento | solo")
            layout = "bento"
        self.lb_n += 1
        group = b.get("_group") or "g{}".format(self.lb_n)
        # meta с «{n}» — число кадров считает генератор: «{n} · нажмите, чтобы открыть» → «11 кадров · …»
        if isinstance(b.get("meta"), str) and "{n}" in b["meta"]:
            b = dict(b, meta=b["meta"].replace("{n}", "{} {}".format(n, plural(n, "кадр", "кадра", "кадров"))))
        elif isinstance(b.get("meta"), str) and re.match(r"^\d+\s+кадр", b["meta"]) and not b["meta"].startswith("{} ".format(n)):
            self.rep.warn(b["_w"], "в meta «{}» число кадров не совпадает с галереей ({}) — пишите «{{n}} · …»".format(b["meta"], n))
        if layout == "solo" or n == 1:
            parts = []
            for x in items[:2]:
                p = x["_p"]
                parts.append(self.ph(x, group, "--r:{}".format("2/3" if self.orient(p) == "P" else "3/2"), "(min-width: 900px) 70vw, 100vw", cls="ph ph--solo"))
            note = '<p class="solo__note">{}</p>'.format(self.md(b["solo_note"], b["_w"])) if b.get("solo_note") else ""
            body = '<div class="solo">{}{}</div>'.format("".join(parts), note)
        else:
            gs = []
            # на телефоне подпись жанра — только на двух первых крупных (во всю ширину) тайлах
            # и только в смешанной подборке (на странице жанра она повторяла бы заголовок)
            labs = 0 if len({x["_p"]["genre"] for x in items}) > 1 else 2
            for gi, (name, its) in enumerate(self.bento_groups(items)):
                rows, slots = BENTO[name]
                roles = [s[0] for s in slots]
                # мобильная раскладка: 2 колонки; одиночный вертикальный / нечётный малый кадр — на всю ширину
                pidx = [k for k, r in enumerate(roles) if r in ("p", "ps")]
                sidx = [k for k, r in enumerate(roles) if r == "ls"]
                parts = []
                for k, (x, (role, _o, area)) in enumerate(zip(its, slots)):
                    if role in ("p", "ps"):
                        single = len(pidx) % 2 == 1 and k == pidx[0]
                        ms, mr = (2, "4/5") if single else (1, "2/3")
                    elif role == "ls":
                        single = len(sidx) % 2 == 1 and k == sidx[0]
                        ms, mr = (2, "3/2") if single else (1, "1/1")
                    else:
                        ms, mr = 2, "3/2"
                    c0, c1 = [int(v) for v in area.split("/")[1::2]]
                    vw = round((c1 - c0) / 12 * 100)
                    sizes = "(min-width: 700px) {}vw, {}vw".format(vw, 100 if ms == 2 else 50)
                    cls = "ph"
                    if ms == 2 and labs < 2:
                        cls, labs = "ph ph--lab", labs + 1
                    parts.append(self.ph(x, group, "--a:{};--ms:{};--mr:{}".format(area, ms, mr), sizes, dl=(k % 3) * 80, cls=cls))
                gs.append('<div class="bento__g bento__g--{}" style="--rows:{}">{}</div>'.format(name, rows, "".join(parts)))
            body = '<div class="bento">{}</div>'.format("".join(gs))
        end = ""
        e = b.get("end")
        if isinstance(e, dict):
            link = ""
            if isinstance(e.get("link"), dict) and e["link"].get("href"):
                href = self.link(e["link"]["href"], b["_w"] + ".end.link")
                link = '<a class="btn btn--ghost" href="{}"{}><span>{}</span><span class="arr" aria-hidden="true">{}</span></a>'.format(
                    esc(href), self.dt_attr(href), self.t(e["link"].get("label", "Подробнее")), self.arr(href))
            txt = '<p>{}</p>'.format(self.md(e["text"], b["_w"] + ".end")) if e.get("text") else ""
            end = '<div class="gal__end" data-rv>{}{}</div>'.format(txt, link)
        return self.section(b, body + end, cls="b-gallery--" + layout)

    # ------------------------------------------------------------------ stats (опыт в цифрах)
    def b_stats(self, b):
        w = b["_w"]
        lis = []
        for i, it in enumerate(b.get("items") or []):
            wi = "{}.items[{}]".format(w, i)
            if not isinstance(it, dict) or "value" not in it or not it.get("label"):
                self.rep.err(wi, "пункт — объект {\"value\": 300, \"suffix\": \"+\", \"label\": \"событий снято\"}")
                continue
            v = it["value"]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                self.rep.err(wi, "value — число (без пробелов и знаков), суффикс — в suffix")
                continue
            vs = str(int(v)) if float(v).is_integer() else str(v)
            suf = '<span class="st__suf">{}</span>'.format(esc(it["suffix"])) if it.get("suffix") else ""
            num = ('<span class="st__n"><span class="st__num" data-count="{v}"><span class="st__ghost" aria-hidden="true">{v}</span>'
                   '<span class="st__v">{v}</span></span>{suf}</span>').format(v=esc(vs), suf=suf)
            label = '<span class="st__l mono">{}</span>'.format(self.t(it["label"]))
            if it.get("href"):
                href = self.link(it["href"], wi)
                inner = '<a class="st__a" href="{}">{}{}</a>'.format(esc(href), num, label)
            else:
                inner = num + label
            lis.append('<li class="st__i" data-rv style="--dl:{}ms">{}</li>'.format(i * 90, inner))
        if not 2 <= len(lis) <= 4:
            self.rep.warn(w, "в stats лучше 3 пункта (сейчас {})".format(len(lis)))
        about = ""
        if b.get("text") or b.get("link"):
            link = ""
            if isinstance(b.get("link"), dict):
                href = self.link(b["link"].get("href", "/about/"), w + ".link")
                link = '<a class="ulink" href="{}">{}</a>'.format(esc(href), self.t(b["link"].get("label", "Обо мне →")))
            about = '<div class="st__about" data-rv><p>{}</p>{}</div>'.format(self.md(b.get("text", ""), w + ".text"), link)
        inner = '<div class="st__head">{}{}</div><ul class="st" style="--n:{}">{}</ul>'.format(
            self.sh(b, "tag"), about, max(1, len(lis)), "".join(lis))
        return self.section(b, inner, header=False)

    # ------------------------------------------------------------------ cases (задача → решение → результат)
    def b_cases(self, b):
        w = b["_w"]
        lis = []
        items = b.get("items") or []
        for i, it in enumerate(items):
            wi = "{}.items[{}]".format(w, i)
            if not isinstance(it, dict) or not it.get("title") or not all(it.get(k) for k in ("task", "solution", "result")):
                self.rep.err(wi, "кейс — объект {photo, title, tags[], task, solution, result, link?}")
                continue
            p = self.photo(it.get("photo"), wi + ".photo") if "photo" in it else None
            tags = "".join("<li>{}</li>".format(self.t(x)) for x in (it.get("tags") or []))
            fig = ""
            if p:
                self.has_lb = True
                self.gallery_photos.append(p)
                fig = '<figure class="cs__fig" style="{}"><a class="ph__a" href="{}" data-lb="cases" data-cap="{}">{}</a></figure>'.format(
                    esc(self.focus_vars(it.get("focus") or p.get("focus"))), esc(self.purl(p["large"])),
                    esc(self.lb_cap(p)), self.img(p, "(min-width: 900px) 50vw, 100vw"))
            link = ""
            if isinstance(it.get("link"), dict) and it["link"].get("href"):
                href = self.link(it["link"]["href"], wi + ".link")
                link = '<a class="ulink cs__link" href="{}"{}>{}</a>'.format(esc(href), self.dt_attr(href), self.t(it["link"].get("label", "Смотреть кадры →")))
            row = lambda k, lab, cls="": '<div class="cs__row{}"><dt class="mono">{}</dt><dd>{}</dd></div>'.format(
                cls, lab, self.md(it[k], wi + "." + k, nw=True))
            # «Задача» и «Решение» — в <details>: на десктопе раскрыты всегда (summary скрыт), на телефоне свёрнуты
            # в «Задача и решение ↓», чтобы на виду остались метки, кадр и результат. Без JS — раскрыты везде.
            more = ('<details class="cs__more" open><summary class="cs__sum mono"><span>Задача и{n}решение</span>'
                    '<span class="cs__ic" aria-hidden="true"></span></summary><dl class="cs__dl">{a}{b}</dl></details>').format(
                n=NBSP, a=row("task", "Задача"), b=row("solution", "Решение"))
            res = '<dl class="cs__dl cs__dl--res">{}</dl>'.format(row("result", "Результат", " cs__row--res"))
            # счётчик «01 / 04» — только когда кейсов несколько; «01 / 01» — технический шум
            no = '<span class="cs__no mono">{:02d} / {:02d}</span>'.format(i + 1, len(items)) if len(items) > 1 else ""
            # кейс без кадра: текст на всю ширину карточки, без пустого слота под фото
            lis.append(('<li class="cs__i" style="--i:{i}"><article class="cs__card{nf}" aria-labelledby="{sid}-{i}">'
                        '<header class="cs__head">{no}<h3 class="cs__t" id="{sid}-{i}">{title}</h3>'
                        '<ul class="cs__tags mono" aria-label="Факты">{tags}</ul></header>'
                        '<div class="cs__body">{fig}<div class="cs__txt">{more}{res}{link}</div></div></article></li>').format(
                i=i, nf="" if fig else " cs__card--nophoto", sid=b["_id"], no=no, title=self.md(it["title"], wi), tags=tags, fig=fig,
                more=more, res=res, link=link))
        inner = '<ol class="cs{}">{}</ol>'.format(" cs--one" if len(lis) == 1 else "", "".join(lis))
        return self.section(b, inner, style="xl")

    # ------------------------------------------------------------------ marquee (бегущая строка отзывов)
    def b_marquee(self, b):
        w = b["_w"]
        rv = self.site.reviews
        items = []
        for i, rid in enumerate(b.get("ids") or []):
            r = rv.get(rid)
            if not r:
                self.rep.err("{}.ids[{}]".format(w, i), "отзыва с id {} нет в content/reviews.json".format(rid))
                continue
            items.append((r["text"], r["name"]))
        for i, it in enumerate(b.get("items") or []):
            if isinstance(it, str):
                items.append((it, ""))
            else:
                self.rep.err("{}.items[{}]".format(w, i), "элемент — строка")
        if not items:
            self.rep.warn(w, "бегущая строка пустая — блок пропущен")
            return ""
        lis = "".join('<li><q>{}</q>{}<i class="lamp" aria-hidden="true"></i></li>'.format(
            self.t(t.strip("«»")), '<span class="mq__by mono">{}</span>'.format(self.t(a)) if a else "") for t, a in items)
        link = ""
        if isinstance(b.get("link"), dict) and b["link"].get("href"):
            href = self.link(b["link"]["href"], w + ".link")
            link = '<a class="mq__all mono" href="{}">{}</a>'.format(esc(href), self.t(b["link"].get("label", "Все отзывы →")))
        speed = b.get("speed", 60)
        label = b.get("label", "Отзывы клиентов")
        return ('<section class="mq" id="{id}" aria-label="{lab}"><div class="mq__bar wrap"><span class="mq__lab mono">{labt}</span>{link}</div>'
                '<div class="mq__view" data-mq style="--speed:{sp}"><div class="mq__track"><ul class="mq__list">{lis}</ul>'
                '<ul class="mq__list" aria-hidden="true">{lis}</ul></div></div></section>').format(
            id=b["_id"], lab=esc(label), labt=self.t(label), link=link, sp=speed, lis=lis)

    # ------------------------------------------------------------------ points («как снимаю»)
    def b_points(self, b):
        lis = []
        items = b.get("items") or []
        for i, it in enumerate(items):
            wi = "{}.items[{}]".format(b["_w"], i)
            if not isinstance(it, dict) or not it.get("title"):
                self.rep.err(wi, "пункт — объект {\"title\": …, \"text\": …}")
                continue
            lis.append('<li class="pt__i" data-rv style="--dl:{}ms"><span class="pt__n mono">{:02d}</span><h3 class="pt__t">{}</h3>{}</li>'.format(
                i * 90, i + 1, self.md(it["title"], wi), "<p>{}</p>".format(self.md(it.get("text", ""), wi)) if it.get("text") else ""))
        if not 2 <= len(items) <= 4:
            self.rep.warn(b["_w"], "в points лучше 3 пункта (сейчас {})".format(len(items)))
        inner = '<ol class="pt" style="--n:{}">{}</ol>'.format(max(1, len(lis)), "".join(lis))
        return self.section(b, inner, style="l")

    # ------------------------------------------------------------------ formats (без цен)
    def b_formats(self, b):
        items = b.get("items") or []
        rows = []
        pv = b.get("price_note", "по запросу")
        cl = b.get("cta_label", "Узнать стоимость")
        for i, it in enumerate(items):
            wi = "{}.items[{}]".format(b["_w"], i)
            if not isinstance(it, dict) or not it.get("name"):
                self.rep.err(wi, "формат — объект {\"name\": …, \"text\": …, \"includes\": [...]} ")
                continue
            if re.search(r"\d\s*(₽|руб|р\.)", json.dumps(it, ensure_ascii=False)):
                self.rep.err(wi, "в форматах не указываем цены — только «по запросу»")
            inc = ""
            if it.get("includes"):
                inc = '<ul class="fm__inc mono">{}</ul>'.format("".join("<li>{}</li>".format(self.t(x)) for x in it["includes"]))
            rows.append(('<li class="fm__row" data-rv><span class="fm__no mono">{:02d}</span><h3 class="fm__name">{}</h3>'
                         '<div class="fm__body"><p>{}</p>{}</div><div class="fm__price"><span class="mono">{}</span>'
                         '<a class="ulink" href="{}"{}>{} ↗</a></div></li>').format(
                i + 1, self.md(it["name"], wi), self.md(it.get("text", ""), wi + ".text", nw=True), inc, self.t("Стоимость — " + pv),
                esc(self.cta_href()), self.dt_attr(self.cta_href()), self.t(cl)))
        inner = '<ol class="fm">{}</ol>'.format("".join(rows))
        return self.section(b, inner, style="l")

    # ------------------------------------------------------------------ faq
    def b_faq(self, b):
        items = b.get("items") or []
        out = []
        for i, it in enumerate(items):
            wi = "{}.items[{}]".format(b["_w"], i)
            if not isinstance(it, dict) or not it.get("q") or not it.get("a"):
                self.rep.err(wi, "вопрос — объект {\"q\": \"…\", \"a\": \"…\"}")
                continue
            a = it["a"] if isinstance(it["a"], list) else [it["a"]]
            self.faq.append((strip_md(it["q"]), " ".join(strip_md(x) if isinstance(x, str) else " ".join(map(strip_md, x)) for x in a)))
            out.append('<details class="faq__i"{}><summary><span class="faq__n mono" aria-hidden="true">{:02d}</span><h3 class="faq__q">{}</h3><span class="faq__ic" aria-hidden="true"></span></summary><div class="faq__a"><div class="faq__ai">{}</div></div></details>'.format(
                " open" if it.get("open") else "", i + 1, self.md(it["q"], wi), self.paras(a, wi + ".a")))
        if not 3 <= len(items) <= 8:
            self.rep.warn(b["_w"], "в FAQ лучше 5–6 вопросов (сейчас {})".format(len(items)))
        head = self.sh(b, "l")
        inner = '<div class="faq-wrap"><div class="faq-wrap__head">{}</div><div class="faq" data-rv>{}</div></div>'.format(head, "".join(out))
        return self.section(b, inner, header=False).replace("<section ", '<section aria-labelledby="h-{}" '.format(b["_id"]) if b.get("title") else "<section ", 1)

    # ------------------------------------------------------------------ seo-text (раскрываемый «Подробнее о съёмке»)
    def b_seo_text(self, b):
        w = b["_w"]
        b.setdefault("title", "Подробнее о съёмке")
        body = []
        if b.get("lead"):
            body.append('<p class="seo__lead">{}</p>'.format(self.md(b["lead"], w + ".lead", nw=True)))
        if b.get("paragraphs"):
            body.append(self.paras(b["paragraphs"], w + ".paragraphs"))
        for i, s in enumerate(b.get("sub") or []):
            ws = "{}.sub[{}]".format(w, i)
            if not isinstance(s, dict) or not s.get("title"):
                self.rep.err(ws, "подраздел — объект {\"title\": …, \"paragraphs\": [...]} ")
                continue
            body.append('<h3 class="txt__h3">{}</h3>{}{}'.format(
                self.md(s["title"], ws), self.paras(s.get("paragraphs", []), ws + ".paragraphs"),
                self.ul(s["list"], ws + ".list") if s.get("list") else ""))
        if not body:
            self.rep.warn(w, "seo-text без текста")
        bid = b["_id"] + "-body"
        opened = b.get("open") is True
        inner = ('<div class="seo" data-more{op}><div class="seo__side">{head}</div><div class="seo__main">'
                 '<div class="seo__body txt" id="{bid}">{body}</div>'
                 '<button class="seo__btn mono" type="button" aria-expanded="{ex}" aria-controls="{bid}" hidden>'
                 '<span class="seo__more">Читать полностью</span><span class="seo__less">Свернуть</span><span class="seo__ic" aria-hidden="true">↓</span></button></div></div>').format(
            op=" data-open" if opened else "", head=self.sh(b, "tag"), bid=bid, body="".join(body), ex="true" if opened else "false")
        return self.section(b, inner, header=False).replace("<section ", '<section aria-labelledby="h-{}" '.format(b["_id"]), 1)

    # ------------------------------------------------------------------ text (для служебных страниц)
    def b_text(self, b):
        w = b["_w"]
        body = []
        if b.get("lead"):
            body.append('<p class="txt__lead">{}</p>'.format(self.md(b["lead"], w + ".lead", nw=True)))
        if b.get("paragraphs"):
            body.append(self.paras(b["paragraphs"], w + ".paragraphs"))
        if b.get("list"):
            body.append(self.ul(b["list"], w + ".list"))
        if b.get("actions"):
            body.append(self.actions(b["actions"], w + ".actions"))
        if not body:
            self.rep.warn(w, "текстовый блок без текста")
        aside = ""
        a = b.get("aside")
        if isinstance(a, dict):
            parts = []
            if a.get("title"):
                parts.append('<p class="mono">{}</p>'.format(self.t(a["title"])))
            if a.get("text"):
                parts.append(self.paras(a["text"] if isinstance(a["text"], list) else [a["text"]], w + ".aside.text"))
            if a.get("items"):
                parts.append(self.ul(a["items"], w + ".aside.items"))
            aside = '<aside class="txt__aside" data-rv>{}</aside>'.format("".join(parts))
        inner = '<div class="txt-wrap"><div class="txt" data-rv>{}</div>{}</div>'.format("".join(body), aside)
        return self.section(b, inner, style="l")

    # ------------------------------------------------------------------ quote / reviews
    def b_quote(self, b):
        w = b["_w"]
        if "review" in b:
            r = self.site.reviews.get(b["review"])
            if not r:
                self.rep.err(w + ".review", "отзыва с id {} нет в content/reviews.json".format(b["review"]))
                return ""
            text, author, cap = r["text"], r["name"], b.get("cap", "")
        else:
            if not b.get("text"):
                self.rep.err(w, "укажите review (id отзыва) или text")
                return ""
            text, author, cap = b["text"], b.get("author", ""), b.get("cap", "")
        tag = '<p class="tag"><i class="lamp" aria-hidden="true"></i><span>{}</span></p>'.format(self.t(b["eyebrow"])) if b.get("eyebrow") else ""
        fc = '<figcaption><b>{}</b>{}</figcaption>'.format(
            self.t(author), '<span class="mono">{}</span>'.format(self.t(cap)) if cap else "") if (author or cap) else ""
        inner = '<figure class="bq" data-rv>{}<blockquote><p>«{}»</p></blockquote>{}</figure>'.format(tag, self.md(str(text).strip("«»"), w), fc)
        return self.section(b, inner, header=False)

    def b_reviews(self, b):
        w = b["_w"]
        rv = self.site.reviews
        ids = b.get("ids") or list(rv)
        cards = []
        for i, rid in enumerate(ids):
            r = rv.get(rid)
            if not r:
                self.rep.err("{}.ids[{}]".format(w, i), "отзыва с id {} нет в content/reviews.json".format(rid))
                continue
            cards.append('<figure class="rv" data-rv style="--dl:{}ms"><blockquote>«{}»</blockquote><figcaption><b>{}</b></figcaption></figure>'.format(
                (i % 3) * 70, self.md(r["text"].strip("«»")), self.t(r["name"])))
        al = b.get("all_link")
        link = ""
        if al:
            al = al if isinstance(al, dict) else {}
            href = self.link(al.get("href", "/otzyvy/"), w + ".all_link")
            link = '<div class="gal__end"><a class="btn btn--ghost" href="{}"{}><span>{}</span><span class="arr" aria-hidden="true">{}</span></a></div>'.format(
                esc(href), self.dt_attr(href), self.t(al.get("label", "Все отзывы")), self.arr(href))
        inner = '<div class="rvs">{}</div>{}'.format("".join(cards), link)
        return self.section(b, inner, style="l")

    # ------------------------------------------------------------------ lead-form → CTA-полоса «Обсудим съёмку» (без формы)
    def b_lead_form(self, b):
        """Финальная CTA-полоса на safelight-градиенте: огромная ссылка на Telegram-ник. Форм на сайте нет:
        все кнопки ведут в Telegram (config.json → contacts.telegram / telegram_handle). id блока — «zayavka»."""
        w = b["_w"]
        c = self.cfg["contacts"]
        tg, handle = c["telegram"], c.get("telegram_handle", "Telegram")
        b.setdefault("title", "Обсудим съёмку")
        b.setdefault("eyebrow", "Telegram")
        meta = b.get("meta") or "Стоимость — по запросу"
        lead = '<p class="lf__lead">{}</p>'.format(self.md(b.get("lead", "Напишите в Telegram: дата, площадка, формат — остальное обсудим в переписке."), w + ".lead"))
        chs, _ = self.letters(strip_md(b["title"]), gen=True)
        head = ('<div class="lf__top" data-rv><p class="tag"><i class="lamp" aria-hidden="true"></i><span>{eb}</span></p><span class="mono">{meta}</span></div>'
                '<h2 class="lf__t" id="h-{id}" data-fit data-rv style="--n:{n}"><span class="lf__in" aria-hidden="true">{t}</span><span class="vh">{tt}</span></h2>{lead}').format(
            eb=self.t(b["eyebrow"]), meta=self.md(meta, w), id=b["_id"], t=chs, tt=self.t(strip_md(b["title"])), lead=lead,
            n=len(strip_md(b["title"])))
        band = ('<a class="lf__tg" href="{tg}" target="_blank" rel="noopener" data-cta="tg" aria-label="{al}">'
                '<span class="lf__tg-cap mono"><span>{btn}</span><span>Telegram</span></span>'
                '<span class="lf__tg-h">{h}</span><span class="lf__tg-arr" aria-hidden="true">↗</span></a>').format(
            tg=esc(tg), al=esc("Написать в Telegram " + handle), btn=self.t(b.get("button") or "Написать сейчас"), h=esc(handle))
        contacts = ""
        if b.get("contacts", True):
            contacts = ('<ul class="lf__contacts">'
                        '<li><a href="{ph}"><span class="mono">Телефон</span><b>{phd}</b></a></li>'
                        '<li><a href="{wa}" target="_blank" rel="noopener"><span class="mono">WhatsApp</span><b>Написать ↗</b></a></li>'
                        '<li><span class="mono">Город</span><b>{city}</b></li></ul>').format(
                ph=esc(c["phone_href"]), phd=esc(c["phone_display"]).replace(" ", NBSP),
                wa=esc(c["whatsapp"]), city=esc(self.cfg["city"]))
        return ('<section class="lf" id="{id}" aria-labelledby="h-{id}"><div class="wrap">{head}</div>'
                '<div class="lf__band grain" data-rv><div class="wrap">{band}</div></div>'
                '<div class="wrap lf__bottom{solo}">{contacts}{note}</div></section>').format(
            id=b["_id"], head=head, band=band, contacts=contacts, solo="" if contacts else " lf__bottom--solo",
            note='<p class="lf__legal mono">{}</p>'.format(self.t(b["note"])) if b.get("note") else "")

    # =================================================================== КАРКАС СТРАНИЦЫ
    def header(self):
        cfg = self.cfg
        nav = []
        for i, it in enumerate(cfg.get("header_nav", [])):
            href = self.link(it["href"], "config.header_nav[{}]".format(i))
            nav.append('<li><a href="{}"><span class="br" aria-hidden="true">[</span>{}<span class="br" aria-hidden="true">]</span></a></li>'.format(
                esc(href), self.t(it["label"])))
        cta = self.cta_href()
        home_cur = ' aria-current="page"' if self.slug == "/" else ""
        person = cfg["person"]["name"]
        hdr = ('<header class="hdr" id="hdr"><div class="wrap hdr__in">'
               '<a class="logo" href="{home}"{hc} aria-label="{person}, {brand} — на главную"><i class="lamp" aria-hidden="true"></i>'
               '<span class="logo__n">{person}</span><span class="logo__b mono">{brand}</span></a>'
               '<nav class="hnav mono" aria-label="Основная навигация"><ul>{nav}</ul></nav>'
               '<div class="hdr__side"><a class="btn btn--sm hdr__cta" href="{cta}"{dt}><span class="long">Обсудить съёмку</span><span class="short">Написать</span><span class="arr" aria-hidden="true">↗</span></a>'
               '<button class="burger mono" type="button" aria-expanded="false" aria-controls="menu"><span class="burger__t">Меню</span><span class="burger__i" aria-hidden="true"><i></i><i></i></span></button>'
               '</div></div></header>').format(home=esc(self.home()), hc=home_cur, person=esc(person), brand=esc(cfg["brand"]),
                                              nav="".join(nav), cta=esc(cta), dt=self.dt_attr(cta))
        return hdr + self.menu()

    def menu(self):
        cfg = self.cfg
        lis = []
        for i, g in enumerate(self.site.genres):
            cur = ' aria-current="page"' if g["slug"] == self.slug else ""
            lis.append('<li style="--i:{i}"><a href="{h}"{c}><span class="menu__no mono">{no:02d}</span><span class="menu__w">{nm}</span></a></li>'.format(
                i=i, h=esc(self.slug_href(g["slug"])), c=cur, no=i + 1, nm=self.t(g["name"])))
        extra = []
        for s in cfg["menu_extra"]:
            cur = ' aria-current="page"' if s == self.slug else ""
            extra.append('<li><a href="{}"{}>{}</a></li>'.format(esc(self.slug_href(s)), cur, self.t(self.site.label(s))))
        c = cfg["contacts"]
        return ('<div class="menu" id="menu" aria-hidden="true" inert>'
                '<div class="wrap menu__in"><nav class="menu__nav" aria-label="Направления"><p class="mono menu__cap">Направления</p><ol class="menu__list">{lis}</ol></nav>'
                '<div class="menu__side"><nav aria-label="Разделы"><p class="mono menu__cap">Разделы</p><ul class="menu__sec">{extra}</ul></nav>'
                '<div class="menu__contacts"><p class="mono menu__cap">Связь · {city}</p><a class="menu__ph" href="{ph}">{phd}</a>'
                '<p class="menu__msg"><a class="ulink" href="{tg}" target="_blank" rel="noopener">Telegram</a><a class="ulink" href="{wa}" target="_blank" rel="noopener">WhatsApp</a></p>'
                '<a class="btn" href="{cta}" data-menu-close{dt}><span>Написать в{nb}Telegram</span><span class="arr" aria-hidden="true">↗</span></a></div></div></div></div>').format(
            lis="".join(lis), extra="".join(extra), city=esc(cfg["city"]), ph=esc(c["phone_href"]),
            phd=esc(c["phone_display"]).replace(" ", NBSP), tg=esc(c["telegram"]), wa=esc(c["whatsapp"]),
            cta=esc(self.cta_href()), dt=self.dt_attr(self.cta_href()), nb=NBSP)

    def footer(self):
        cfg = self.cfg
        f = cfg["footer"]

        def li(s):
            cur = ' aria-current="page"' if s == self.slug else ""
            return '<li><a href="{}"{}>{}</a></li>'.format(esc(self.slug_href(s)), cur, self.t(self.site.label(s)))
        genres = "".join(li(g["slug"]) for g in self.site.genres)
        events = "".join(li(s) for s in f["events"])
        sections = "".join(li(s) for s in f["sections"])
        c = cfg["contacts"]
        contact = ('<li><a href="{}">{}</a></li><li><a href="{}" target="_blank" rel="noopener">Telegram ↗</a></li>'
                   '<li><a href="{}" target="_blank" rel="noopener">WhatsApp ↗</a></li><li><span>{}</span></li>').format(
            esc(c["phone_href"]), esc(c["phone_display"]).replace(" ", NBSP), esc(c["telegram"]), esc(c["whatsapp"]), esc(cfg["city"]))
        cta = self.cta_href()
        return ('<footer class="ftr"><div class="wrap">'
                '<div class="ftr__cols">'
                '<div class="ftr__intro"><p class="ftr__claim">{motto}</p><p class="ftr__who">{who}</p>'
                '<a class="btn" href="{cta}"{dt}><span>Обсудить съёмку</span><span class="arr" aria-hidden="true">↗</span></a></div>'
                '<nav class="ftr__c" aria-label="Направления"><p class="ftr__h mono">Направления</p><ul>{genres}</ul></nav>'
                '<nav class="ftr__c" aria-label="{et}"><p class="ftr__h mono">{et}</p><ul>{events}</ul></nav>'
                '<nav class="ftr__c" aria-label="{st}"><p class="ftr__h mono">{st}</p><ul>{sections}</ul></nav>'
                '<div class="ftr__c"><p class="ftr__h mono">Связь</p><ul>{contact}</ul></div>'
                '</div><p class="ftr__mark" aria-hidden="true" data-fit><span class="ftr__in">{brand}</span></p>'
                '<div class="ftr__bottom mono"><span>© {brand}, {year}</span><span>{sig}</span>'
                '<button class="ftr__top mono" type="button" data-top>Наверх <span aria-hidden="true">↑</span></button></div></div></footer>').format(
            motto=self.t(cfg["motto"]), who=self.md("{} — event-фотограф и видеограф, {}.".format(cfg["person"]["name"], cfg["city"]), nw=True),
            cta=esc(cta), dt=self.dt_attr(cta), genres=genres, et=esc(f["events_title"]), events=events, st=esc(f["sections_title"]),
            sections=sections, contact=contact, brand=esc(cfg["brand"]), year=self.site.today.year, sig=esc(cfg["signature"]))

    def curtain(self):
        """Лоадер «Проявка» и шторка переходов — первый элемент <body> (README → «Загрузка и переходы»).
        Только для глаз: aria-hidden, без ссылок и кнопок, фокус не забирает. Без JS не показывается (стили — под html.js),
        какой режим включить (первый визит / повторная загрузка), решает inline-скрипт в <head> до первой отрисовки."""
        if not TRANSITIONS:
            return ""
        ld = ""
        if self.loader:
            cfg = self.cfg
            ld = ('<div class="ld">'
                  '<p class="ld__k ld__k--tl mono"><span>{brand}</span><span class="ld__dim">{person}</span></p>'
                  '<p class="ld__k ld__k--tr mono"><span>Photo &amp; Video Lab</span><span class="ld__dim">{city}</span></p>'
                  '<p class="ld__st mono">Проявка<i>.</i><i>.</i><i>.</i></p>'
                  '<p class="ld__n"><span class="ld__v">000</span></p>'
                  '<span class="ld__bar"><i></i></span></div>').format(
                brand=esc(cfg["brand"]), person=esc(cfg["person"]["name"]).replace(" ", NBSP), city=esc(cfg["city"]))
        return ('<div class="veil" id="veil" aria-hidden="true"><div class="veil__sheet"><div class="veil__in">'
                '<span class="veil__glow"></span><span class="veil__lamp"></span>{}</div></div></div>').format(ld)

    @staticmethod
    def lightbox():
        return ('<dialog class="lb" id="lb" aria-label="Просмотр кадра">'
                '<div class="lb__top mono"><span id="lbCount">01 / 01</span><button type="button" class="mono" data-lb-close>Закрыть <span aria-hidden="true">✕</span></button></div>'
                '<div class="lb__stage"><button class="lb__side lb__side--prev" type="button" data-lb-prev aria-label="Предыдущий кадр"></button><button class="lb__side lb__side--next" type="button" data-lb-next aria-label="Следующий кадр"></button></div>'
                '<div class="lb__bot mono"><span class="lb__cap" id="lbCap"></span><div class="lb__nav">'
                '<button type="button" class="mono" data-lb-prev aria-label="Предыдущий кадр">← <span class="t">Назад</span></button>'
                '<button type="button" class="mono" data-lb-next aria-label="Следующий кадр"><span class="t">Вперёд</span> →</button></div></div></dialog>')

    # ------------------------------------------------------------------ SEO: head + JSON-LD
    def url(self, slug=None):
        return self.site.su + (slug or self.slug)

    def full_title(self):
        """<title> по единому шаблону: «{SEO-фраза} — Александр Непомнящих»; у главной — как в JSON.
        Старый хвост « — ANPhotoLab» / « | ANPhotoLab» генератор убирает сам."""
        t = re.sub(r"\s+", " ", str(self.d.get("title", ""))).strip()
        if self.type == "home":
            return t
        t = re.sub(r"\s*[—|–-]\s*ANPhotoLab\s*$", "", t, flags=re.IGNORECASE).strip()
        if t.endswith(TITLE_SUFFIX.strip(" —")):
            return t
        # в выдаче видно ~70 знаков: длинной фразе — короткий хвост бренда, чтобы он не обрезался посередине
        for suf in (TITLE_SUFFIX, TITLE_SUFFIX_SHORT):
            if len(t + suf) <= TITLE_MAX:
                return t + suf
        return t

    def og_photo(self):
        oid = self.d.get("og_image")
        if isinstance(oid, int):
            p = self.photo(oid, "og_image")
            if p:
                return p
        if self.hero_photo:
            return self.hero_photo
        return self.site.photos.get(0)

    def jsonld(self):
        su, cfg = self.site.su, self.cfg
        c = cfg["contacts"]
        url = self.url()
        og = self.og_photo()
        biz_id, person_id, site_id = su + "/#business", su + "/#person", su + "/#website"
        city = {"@type": "City", "name": cfg["city"]}
        addr = {"@type": "PostalAddress", "addressLocality": cfg["city"], "addressCountry": "RU"}
        about_url = su + "/about/" if "/about/" in self.site.pages or "/about/" in self.site.registry else su + "/"
        # бренд — портрет Александра (первый экран главной); запасной — прежняя обложка сайта
        brand_img = self.site.author or self.site.photos.get(0) or og
        person = {"@type": "Person", "@id": person_id, "name": cfg["person"]["name"], "alternateName": cfg["person"]["latin"],
                  "jobTitle": cfg["person"]["job_title"], "worksFor": {"@id": biz_id}, "address": addr, "url": about_url}
        if self.site.author:
            person["image"] = self.aurl(self.site.author["large"])
        # sameAs — только публичные профили (VK, канал, Яндекс Бизнес, 2ГИС: config.json → same_as).
        # Telegram-профиль (t.me/hoonigan866) — публичный профиль, он в same_as; wa.me/номер — телефон, он в contactPoint
        same_as = [x for x in cfg.get("same_as", []) if isinstance(x, str) and x.startswith("http")]
        biz = {"@type": "ProfessionalService", "@id": biz_id, "name": cfg["brand"], "alternateName": cfg["signature"],
               "description": cfg["tagline"], "url": su + "/", "image": self.aurl(brand_img["large"]), "telephone": c["phone_schema"],
               "address": addr, "areaServed": city, "founder": {"@id": person_id}, "employee": {"@id": person_id},
               "contactPoint": [{"@type": "ContactPoint", "contactType": "customer service", "telephone": c["phone_schema"],
                                 "areaServed": city, "availableLanguage": "ru"}],
               "knowsLanguage": "ru"}
        if same_as:
            biz["sameAs"] = same_as
            person["sameAs"] = same_as
        graph = [
            {"@type": "WebSite", "@id": site_id, "url": su + "/", "name": cfg["brand"], "inLanguage": "ru",
             "publisher": {"@id": biz_id}},
            biz,
            person,
        ]
        ptype = {"/about/": "AboutPage", "/contacts/": "ContactPage"}.get(self.slug) or (
            "CollectionPage" if self.type == "genre" else "WebPage")
        page = {"@type": ptype, "@id": url + "#webpage", "url": url,
                "name": self.full_title(), "description": self.d.get("description", ""), "inLanguage": "ru",
                "isPartOf": {"@id": site_id}, "about": {"@id": biz_id}, "dateModified": self.site.today.isoformat()}
        if ptype == "AboutPage":
            page["mainEntity"] = {"@id": person_id}
        if og:
            page["primaryImageOfPage"] = {"@id": url + "#primaryimage"}
            graph.append(self.image_obj(og, url + "#primaryimage"))
        if self.slug != "/" and self.d.get("breadcrumbs") is not False:
            crumbs = self.crumb_items()
            graph.append({"@type": "BreadcrumbList", "@id": url + "#breadcrumb", "itemListElement": [
                {"@type": "ListItem", "position": i + 1, "name": label, "item": self.url(slug)} for i, (label, slug) in enumerate(crumbs)]})
            page["breadcrumb"] = {"@id": url + "#breadcrumb"}
        graph.append(page)
        schema = self.d.get("schema") or []
        if self.type in ("genre", "seo") or "Service" in schema:
            svc = {"@type": "Service", "@id": url + "#service", "name": self.d.get("h1", ""),
                   "serviceType": self.d.get("service") or self.nav_label(), "provider": {"@id": biz_id},
                   "areaServed": city, "url": url, "description": self.d.get("description", "")}
            if og:
                svc["image"] = self.aurl(og["large"])
            graph.append(svc)
        if (self.type == "genre" or "ImageGallery" in schema) and self.gallery_photos:
            uniq, seen = [], set()
            for p in self.gallery_photos:
                if p["id"] not in seen:
                    seen.add(p["id"])
                    uniq.append(p)
            graph.append({"@type": "ImageGallery", "@id": url + "#gallery", "name": "{} — кадры".format(self.nav_label()),
                          "url": url, "creator": {"@id": person_id},
                          "associatedMedia": [self.image_obj(p) for p in uniq[:40]]})
        if self.faq:
            graph.append({"@type": "FAQPage", "@id": url + "#faq", "mainEntity": [
                {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in self.faq]})
        data = {"@context": "https://schema.org", "@graph": graph}
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    def image_obj(self, p, iid=None):
        o = {"@type": "ImageObject", "contentUrl": self.aurl(p["large"]), "url": self.aurl(p["large"]), "width": 1920,
             "height": round(1920 * p["h"] / p["w"]), "caption": p.get("alt", ""), "creator": {"@id": self.site.su + "/#person"},
             "creditText": self.cfg["brand"], "copyrightNotice": "© {}, {}".format(self.cfg["brand"], self.cfg["person"]["name"])}
        if iid:
            o["@id"] = iid
        return o

    def head(self):
        d, cfg = self.d, self.cfg
        title, desc = self.full_title(), d.get("description", "")
        url = self.url()
        og = self.og_photo()
        robots = '<meta name="robots" content="noindex, follow">' if d.get("noindex") else ""
        og_img = ""
        if og:
            og_img = ('<meta property="og:image" content="{u}"><meta property="og:image:width" content="1920">'
                      '<meta property="og:image:height" content="{h}"><meta property="og:image:alt" content="{a}">'
                      '<meta name="twitter:image" content="{u}">').format(u=esc(self.aurl(og["large"])), h=round(1920 * og["h"] / og["w"]), a=esc(og.get("alt", "")))
        preload = ""
        if self.hero_photo:
            p = self.hero_photo
            loc = self.site.local.get(p["id"], {})
            fmt = "avif" if loc.get("avif") else ("webp" if loc.get("webp") else "")
            if fmt:
                # LCP-кадр локально в AVIF/WebP: браузер без поддержки типа preload просто пропустит
                lst = loc[fmt]
                mid = next((path for w, path in lst if w >= 1280), lst[-1][1])
                preload = '<link rel="preload" as="image" type="image/{}" href="{}" imagesrcset="{}" imagesizes="{}" fetchpriority="high">'.format(
                    fmt, esc(self.prefix + mid), esc(self.local_srcset(lst)), esc(self.hero_sizes))
            else:
                preload = '<link rel="preload" as="image" href="{}" imagesrcset="{}" imagesizes="{}" fetchpriority="high">'.format(
                    esc(self.purl(p["mid"])), esc(self.srcset(p)), esc(self.hero_sizes))
        verif = ""
        cnt = cfg.get("counters", {})
        if cnt.get("yandex_verification"):
            verif += '<meta name="yandex-verification" content="{}">'.format(esc(cnt["yandex_verification"]))
        if cnt.get("google_site_verification"):
            verif += '<meta name="google-site-verification" content="{}">'.format(esc(cnt["google_site_verification"]))
        return ('<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                '<title>{title}</title>\n<meta name="description" content="{desc}">\n{robots}'
                '<link rel="canonical" href="{url}">\n'
                '<meta property="og:type" content="website"><meta property="og:locale" content="ru_RU"><meta property="og:site_name" content="{brand}">\n'
                '<meta property="og:title" content="{title}"><meta property="og:description" content="{desc}"><meta property="og:url" content="{url}">\n'
                '{ogimg}\n<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{title}"><meta name="twitter:description" content="{desc}">\n'
                '{verif}<meta name="theme-color" content="{theme}"><meta name="color-scheme" content="dark">\n'
                '<link rel="icon" href="{fav}" type="image/svg+xml"><link rel="apple-touch-icon" href="{ati}">\n'
                '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link rel="preconnect" href="https://i.wfolio.ru">\n'
                '{boot}\n{preload}\n<link rel="stylesheet" href="{fonts}">\n<link rel="stylesheet" href="{css}">\n'
                '<script src="{js}" defer></script>\n'
                '<script type="application/ld+json">{ld}</script>\n</head>\n').format(
            title=esc(title), desc=esc(desc), robots=robots + "\n" if robots else "", url=esc(url), brand=esc(cfg["brand"]), ogimg=og_img,
            verif=verif + "\n" if verif else "", theme=esc(cfg["theme_color"]), fav=esc(self.asset("assets/img/favicon.svg")),
            ati=esc(self.asset("assets/img/apple-touch-icon.png")), boot=boot_script(self.loader), preload=preload, fonts=esc(cfg["fonts_css"]),
            css=esc(self.asset("assets/css/main.css")), js=esc(self.asset("assets/js/main.js")), ld=self.jsonld())

    # ------------------------------------------------------------------ проверка полей страницы
    def validate_page(self):
        d = self.d
        for f in PAGE_REQUIRED:
            if f not in d:
                self.rep.err("", "нет обязательного поля страницы «{}»".format(f))
        for f, v in d.items():
            if f not in PAGE_FIELDS:
                self.rep.warn("", "неизвестное поле страницы «{}» — будет проигнорировано".format(f))
            elif not TYPE_CHECK[PAGE_FIELDS[f]](v):
                self.rep.err("", "поле «{}» должно быть: {}".format(f, TYPE_NAMES[PAGE_FIELDS[f]]))
        exp = expected_slug(self.name)
        if isinstance(d.get("slug"), str) and d["slug"] != exp:
            self.rep.err("slug", "для файла {}.json slug должен быть «{}» (имя файла = slug без слэшей), сейчас «{}»".format(self.name, exp, d["slug"]))
        if d.get("type") not in PAGE_TYPES:
            self.rep.err("type", "type бывает: {}".format(" | ".join(PAGE_TYPES)))
        if d.get("type") == "genre" and not self.genre:
            self.rep.err("genre", "для жанровой страницы укажите genre (ключ из config.json → genres)")
        if isinstance(d.get("genre"), str) and d["genre"] not in self.site.genre_by_key:
            self.rep.err("genre", "нет жанра «{}» (есть: {})".format(d["genre"], ", ".join(self.site.genre_by_key)))
        if self.slug not in self.site.registry:
            self.rep.warn("slug", "{} нет в карте сайта (config.json → pages) — в меню и подвале её не будет".format(self.slug))
        t, desc = d.get("title", ""), d.get("description", "")
        if isinstance(t, str):
            ft = self.full_title()
            if not 30 <= len(ft) <= TITLE_MAX:
                self.rep.warn("title", "«{}» — {} знаков с хвостом, держите 30–{} (ключ — в начале)".format(
                    ft, len(ft), TITLE_MAX))
        if isinstance(desc, str) and not 110 <= len(desc) <= 170:
            self.rep.warn("description", "длина {} знаков — держите 110–170".format(len(desc)))
        if re.search(r"\bСаш", json.dumps({k: v for k, v in d.items() if k != "blocks"}, ensure_ascii=False) +
                     json.dumps([b for b in d.get("blocks", []) if isinstance(b, dict) and b.get("type") not in ("marquee", "quote", "reviews")], ensure_ascii=False)):
            self.rep.warn("", "на сайте обращение «Александр», не «Саша» (дословные цитаты отзывов — исключение)")
        if "og_image" in d:
            self.photo(d["og_image"], "og_image")
        if not isinstance(d.get("blocks"), list):
            return
        for i, b in enumerate(d["blocks"]):
            if not isinstance(b, dict):
                self.rep.err("blocks[{}]".format(i), "блок должен быть объектом {\"type\": …}")
                continue
            t = b.get("type")
            if t in V1_BLOCKS:
                self.rep.err("blocks[{}]".format(i), "блок «{}» из v1 удалён — используйте {}".format(t, V1_BLOCKS[t]))
                continue
            if t not in BLOCK_SPECS:
                self.rep.err("blocks[{}]".format(i), "неизвестный тип блока «{}». Есть: {}".format(t, ", ".join(BLOCK_SPECS)))
                continue
            self.validate_block(b)

    # ------------------------------------------------------------------ сборка
    def render(self):
        self.prepare()
        self.validate_page()
        blocks = [b for b in self.d.get("blocks", []) if isinstance(b, dict) and b.get("type") in BLOCK_SPECS]
        parts, useful, visible = [], [], []
        for b in blocks:
            try:
                h = self.render_block(b)
            except Exception as e:  # ошибка рендера блока не должна ронять всю сборку
                self.rep.err(b.get("_w", "blocks"), "не удалось собрать блок: {}: {}".format(type(e).__name__, e))
                h = ""
            parts.append(h)
            if b["type"] not in ("lead-form", "genre-tabs", "directions", "related-directions", "marquee", "reviews", "quote"):
                tf = text_fields(b)
                useful.extend(tf)
                if b["type"] == "faq":      # ответы свёрнуты — на виду только вопросы
                    visible.extend(text_fields({"title": b.get("title", ""), "note": b.get("note", ""),
                                                "items": [{"q": x.get("q", "")} for x in b.get("items", []) if isinstance(x, dict)]}))
                elif b["type"] != "seo-text":
                    visible.extend(tf)
        count = lambda xs: len(re.sub(r"\s+", " ", " ".join(strip_md(x) for x in xs)).strip())
        chars, vis = count(useful), count(visible)
        self.rep.note("полезного текста: {} знаков с пробелами, из них на виду (без раскрываемого seo-text): {}".format(chars, vis))
        self.rep.note("блоков: {}, кадров: {}, вопросов FAQ: {}".format(len(blocks), len({p['id'] for p in self.gallery_photos}), len(self.faq)))
        if self.type == "home" and vis > 1600:
            self.rep.warn("", "на главной на виду {} знаков текста — фотографа оценивают глазами, держите до ~1500".format(vis))
        self.chars = chars
        if self.pending:
            self.rep.warn("ссылки", "страницы ещё не написаны, ссылки заработают после их появления: " + ", ".join(sorted(self.pending)))
        # sticky-вкладки жанров живут в обёртке до CTA-полосы: у неё и подвала они больше не висят
        ti = next((i for i, b in enumerate(blocks) if b["type"] == "genre-tabs"), None)
        if ti is not None:
            li = next((i for i, b in enumerate(blocks) if b["type"] == "lead-form" and i > ti), len(blocks))
            if li - 1 >= ti:
                parts[ti] = '<div class="gtabs-scope">' + parts[ti]
                parts[li - 1] += "</div>"
        main = "".join(parts)
        body = ('<body class="p-{type}">\n{pt}\n<a class="skip" href="#main">Перейти к{n}содержанию</a>\n{hdr}\n<main id="main">\n{main}\n</main>\n{ftr}\n{lb}\n</body>\n</html>\n').format(
            type=esc(self.type or "page"), pt=self.curtain(), n=NBSP, hdr=self.header(), main=main, ftr=self.footer(),
            lb=self.lightbox() if self.has_lb else "")
        return self.head() + body


# =============================================================================
# Служебные страницы
# =============================================================================

def boot_script(loader=True):
    """Inline-скрипт в <head> (до стилей — не ждёт их загрузки): до первой отрисовки ставит на <html> класс js и режим
    шторки, чтобы не было вспышки контента. ld-on — первый визит за сессию (лоадер со счётчиком), veil-in — повторная
    загрузка или страница без лоадера (шторка закрыта с первого кадра и уходит вверх), ld-off — на странице нет лоадера (404).
    Флаг визита — sessionStorage «anp-seen» (ставит main.js); если хранилище недоступно (file://, приватный режим) —
    запасной флаг в window.name, иначе считаем визит первым. prefers-reduced-motion — ни лоадера, ни шторки.
    Страховки: main.js не загрузился за 2,5 с — снимаем js и шторку; через 4,5 с шторка снимается в любом случае
    (main.js отменяет этот таймер и ставит свой, от момента, когда вкладка стала видимой)."""
    if not TRANSITIONS:
        return ('<script>document.documentElement.className+=" js";'
                'setTimeout(function(){if(!window.ANP)document.documentElement.classList.remove("js")},2500)</script>')
    return ('<script>(function(r,w){var c=" js",s=0,L=%d;'
            'try{s=w.sessionStorage.getItem("anp-seen")}catch(e){}'
            'if(!s)try{s=/(^| )anp-seen( |$)/.test(w.name)}catch(e){}'
            'if(!(w.matchMedia&&w.matchMedia("(prefers-reduced-motion: reduce)").matches))c+=s||!L?" veil-in":" ld-on";'
            'if(!L)c+=" ld-off";r.className+=c;'
            'function k(){r.classList.remove("ld-on","veil-in")}'
            'w.ANPfs=setTimeout(k,4500);'
            'setTimeout(function(){if(!w.ANP){r.classList.remove("js");k()}},2500)'
            '})(document.documentElement,window)</script>') % (1 if loader else 0)


def render_404(site):
    rep = Report("404.html")
    data = {"slug": "/404.html", "type": "service", "nav": "Страница не найдена", "title": "Страница не найдена — ANPhotoLab",
            "description": "Такой страницы нет. Посмотрите направления съёмки ANPhotoLab.", "h1": "Такой страницы нет", "noindex": True,
            "breadcrumbs": False, "blocks": []}
    ctx = PageCtx(site, "404", data, rep)
    # 404 лежит в корне сайта, но это не главная: ссылки шапки «/#napravleniya» ведут на index.html#…,
    # у логотипа нет aria-current (slug «/404.html» не совпадает ни с одной страницей)
    ctx.slug = "/404.html"
    ctx.prefix = ""
    ctx.loader = False   # на 404 не держим человека счётчиком: только короткое появление и шторка при уходе
    links = "".join('<li><a href="{}"><span class="br" aria-hidden="true">[</span>{}<span class="br" aria-hidden="true">]</span></a></li>'.format(
        esc(ctx.slug_href(g["slug"])), esc(g["name"])) for g in site.genres)
    links += '<li><a href="{}"><span class="br" aria-hidden="true">[</span>Контакты<span class="br" aria-hidden="true">]</span></a></li>'.format(esc(ctx.slug_href("/contacts/")))
    main = ('<section class="sys wrap" aria-labelledby="h1"><p class="sys__no" aria-hidden="true" data-fit data-fit-max="{fm}"><span class="sys__in">404</span></p>'
            '<h1 class="sys__h" id="h1">Такой страницы нет</h1>'
            '<p class="sys__lead">Возможно, её перенесли. Начните с{n}главной или выберите направление:</p>'
            '<ul class="sys__links mono">{links}</ul>'
            '<div class="acts"><a class="btn" href="{home}"><span>На главную</span><span class="arr" aria-hidden="true">→</span></a>'
            '<a class="ulink" href="{tg}" target="_blank" rel="noopener" data-cta="tg">Написать в{n}Telegram ↗</a></div></section>').format(
        n=NBSP, fm=FIT_MAX_404, links=links, home=esc(ctx.home()), tg=esc(site.cfg["contacts"]["telegram"]))
    base = site.cfg.get("base_path_404", "/")
    # 404 хостинг отдаёт по любому адресу: <base> считает относительные пути от корня сайта. Ссылка «#main»
    # с <base> увела бы на главную — её перехватывает main.js (фокус на #main без перехода)
    head = ctx.head().replace('<meta charset="utf-8">',
                              '<meta charset="utf-8">\n<script>/* 404 отдаётся хостингом по любому адресу: пути считаем от корня сайта */'
                              'if(location.protocol!=="file:"){{var b=document.createElement("base");b.href=location.origin+"{}";document.head.appendChild(b)}}</script>'.format(base), 1)
    head = head.replace('<link rel="canonical" href="{}">\n'.format(esc(ctx.url())), "")
    body = '<body class="p-sys">\n{}\n<a class="skip" href="#main">Перейти к{}содержанию</a>\n{}\n<main id="main">{}</main>\n{}\n</body>\n</html>\n'.format(
        ctx.curtain(), NBSP, ctx.header(), main, ctx.footer())
    return head + body


def render_redirect(site, src, dst):
    depth = depth_of(src)
    prefix = "../" * depth
    target = "index.html" if dst == "/" else dst.strip("/") + "/index.html"
    rel = prefix + target
    canon = site.su + dst
    # запасной вариант, если 301 на сервере не настроен: canonical на новый адрес + мгновенный переход.
    # noindex сюда не ставим — вместе с canonical это противоречивый сигнал. Настоящий 301 — .htaccess / _redirects
    return ('<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n<title>Страница переехала — Александр Непомнящих</title>\n'
            '<link rel="canonical" href="{canon}">\n'
            '<meta http-equiv="refresh" content="0; url={rel}">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="theme-color" content="{theme}"><meta name="color-scheme" content="dark">\n'
            '<link rel="icon" href="{pre}assets/img/favicon.svg" type="image/svg+xml">\n<link rel="stylesheet" href="{fonts}">\n<link rel="stylesheet" href="{pre}assets/css/main.css">\n'
            '<script>location.replace("{rel}"+location.hash)</script>\n</head>\n<body class="p-sys">\n<main class="sys wrap">'
            '<p class="sys__no" aria-hidden="true"><span class="sys__in">→</span></p><h1 class="sys__h">Страница переехала</h1>'
            '<p class="sys__lead">Отзывы теперь живут по{n}новому адресу.</p>'
            '<div class="acts"><a class="btn" href="{rel}"><span>Перейти к{n}отзывам</span><span class="arr" aria-hidden="true">→</span></a></div>'
            '</main>\n</body>\n</html>\n').format(canon=esc(canon), rel=esc(rel), pre=prefix, n=NBSP, theme=esc(site.cfg["theme_color"]),
                                                   fonts=esc(site.cfg["fonts_css"]))


def render_redirect_rules(site):
    """Готовые правила 301 для хостинга: .htaccess (Apache, в т. ч. обычный виртуальный хостинг) и _redirects
    (Netlify, Cloudflare Pages). HTML-заглушка в site/<старый адрес>/ остаётся запасным вариантом."""
    ht = ["# 301-редиректы со старых адресов (генерирует build.py из config.json → redirects)", "<IfModule mod_alias.c>"]
    nr = ["# 301-редиректы со старых адресов (Netlify, Cloudflare Pages; генерирует build.py из config.json → redirects)"]
    for src, dst in site.redirects.items():
        base = src.rstrip("/")
        ht.append("  RedirectMatch 301 ^{}(/|/index\\.html)?$ {}".format(re.escape(base).replace("\\-", "-"), dst))
        for u in (base, base + "/", base + "/index.html"):
            nr.append("{} {} 301".format(u, dst))
    ht.append("</IfModule>")
    ht.append("ErrorDocument 404 /404.html")
    return "\n".join(ht) + "\n", "\n".join(nr) + "\n"


def render_sitemap(site, slugs):
    rows = "".join("  <url><loc>{}</loc><lastmod>{}</lastmod></url>\n".format(esc(site.su + s), site.today.isoformat()) for s in slugs)
    return '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + rows + "</urlset>\n"


def render_robots(site):
    return ("User-agent: *\nAllow: /\n\n"
            "User-agent: Yandex\nAllow: /\nClean-param: utm_source&utm_medium&utm_campaign&utm_content&utm_term&yclid&gclid\n\n"
            "Sitemap: {}/sitemap.xml\n").format(site.su)


# ----------------------------------------------------------------------------- apple-touch-icon без зависимостей
def ensure_touch_icon():
    """PNG 180×180: тёмная карточка с «лампой» safelight и буквой A — рисуется один раз, если файла нет."""
    path = SRC / "assets" / "img" / "apple-touch-icon.png"
    if path.exists():
        return
    W = H = 180
    bg, fg = (0x0B, 0x0A, 0x09), (0xF2, 0xEE, 0xE6)

    def in_poly(x, y, poly):
        c = False
        j = len(poly) - 1
        for i in range(len(poly)):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi + 1e-9) + xi:
                c = not c
            j = i
        return c
    polys = [
        [(58, 136), (74, 136), (96, 44), (84, 44)],
        [(84, 44), (96, 44), (122, 136), (106, 136)],
        [(72, 110), (110, 110), (108, 100), (75, 100)],
    ]
    rows = []
    for y in range(H):
        row = bytearray([0])
        for x in range(W):
            col = bg
            # свечение-лампа в правом верхнем углу
            dx, dy = x - 140, y - 40
            r = (dx * dx + dy * dy) ** 0.5
            if r < 34:
                a = max(0.0, 1 - r / 34) ** 1.6
                glow = (0xFF, 0x62, 0x00) if r < 10 else (0x9B, 0x00, 0x00)
                col = tuple(round(col[i] * (1 - a) + glow[i] * a) for i in range(3))
            hits = sum(1 for ddx in (0.25, 0.75) for ddy in (0.25, 0.75) if any(in_poly(x + ddx, y + ddy, pl) for pl in polys))
            if hits:
                a = hits / 4
                col = tuple(round(col[i] * (1 - a) + fg[i] * a) for i in range(3))
            row += bytes(col)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(t, data):
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")
    path.write_bytes(png)


# =============================================================================
# Режимы запуска
# =============================================================================

def copy_assets():
    dst = OUT / "assets"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(SRC / "assets", dst, ignore=shutil.ignore_patterns(".DS_Store"))


def build_page(site, slug):
    info = site.pages[slug]
    rep = Report("content/pages/{}.json → {}".format(info["name"], slug))
    ctx = PageCtx(site, info["name"], info["data"], rep)
    html_out = ctx.render()
    return html_out, rep, ctx


def cmd_check(site, path):
    path = Path(path)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    name = path.stem
    rep = Report(str(path.relative_to(ROOT)) if str(path).startswith(str(ROOT)) else str(path))
    if not path.exists():
        rep.err("", "файл не найден")
        rep.print()
        return 1
    try:
        data = load_json(path)
    except ValueError as e:
        rep.err("", str(e))
        rep.print()
        return 1
    if not isinstance(data, dict):
        rep.err("", "файл должен содержать объект { ... }")
        rep.print()
        return 1
    slug = data.get("slug") if isinstance(data.get("slug"), str) else expected_slug(name)
    site.pages[slug] = {"name": name, "path": path, "data": data}
    ctx = PageCtx(site, name, data, rep)
    ctx.render()
    rep.print()
    print("Итог: {} ошибок, {} предупреждений. Ничего не записано.".format(len(rep.errors), len(rep.warnings)))
    return 1 if rep.errors else 0


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def cmd_only(site, name):
    name = Path(name).stem
    info = next((v for v in site.pages.values() if v["name"] == name), None)
    if name in site.page_errors:
        print("FAIL content/pages/{}.json: {}".format(name, site.page_errors[name]))
        return 1
    if not info:
        print("FAIL нет файла content/pages/{}.json".format(name))
        return 1
    slug = info["data"].get("slug", expected_slug(name))
    ensure_touch_icon()
    html_out, rep, ctx = build_page(site, slug)
    rep.print()
    if rep.errors:
        print("Страница не записана: исправьте ошибки.")
        return 1
    OUT.mkdir(exist_ok=True)
    copy_assets()
    write(out_file(slug), html_out)
    print("Записано: site/{}  (ассеты обновлены, остальные страницы не тронуты)".format(out_file(slug).relative_to(OUT)))
    print("Смотреть: http://127.0.0.1:8080/{}  или  site/{}".format("" if slug == "/" else slug.strip("/") + "/", out_file(slug).relative_to(OUT)))
    return 0


def check_site_links():
    """После сборки: каждая относительная ссылка и ассет в site/ должны разрешаться в файл (как при file://)."""
    missing = {}
    for f in OUT.rglob("*.html"):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r'(?:href|src)="([^"]+)"', text):
            u = m.group(1)
            if u.startswith(("http:", "https:", "tel:", "mailto:", "#", "data:")):
                continue
            path = u.split("#")[0].split("?")[0]
            if path and not (f.parent / path).resolve().exists():
                missing.setdefault(str((f.parent / path).resolve().relative_to(OUT.resolve())), set()).add(str(f.relative_to(OUT)))
    return missing


def cmd_build(site, strict=False):
    ensure_touch_icon()
    outputs, reports, ok_slugs, total_err = {}, [], [], 0
    for name, msg in sorted(site.page_errors.items()):
        r = Report("content/pages/{}.json".format(name))
        r.err("", msg)
        reports.append(r)
        total_err += 1
    for slug in sorted(site.pages, key=lambda s: (s != "/", s)):
        html_out, rep, ctx = build_page(site, slug)
        reports.append(rep)
        if rep.errors:
            total_err += len(rep.errors)
            continue
        outputs[out_file(slug)] = html_out
        if not ctx.d.get("noindex"):
            ok_slugs.append(slug)
    # дубли title/description
    seen_t, seen_d = {}, {}
    for slug, info in site.pages.items():
        t, dsc = info["data"].get("title"), info["data"].get("description")
        if t:
            seen_t.setdefault(t, []).append(slug)
        if dsc:
            seen_d.setdefault(dsc, []).append(slug)
    dup = [("title", k, v) for k, v in seen_t.items() if len(v) > 1] + [("description", k, v) for k, v in seen_d.items() if len(v) > 1]
    for rep in reports:
        rep.print(verbose=False)
    for kind, _, v in dup:
        print("     внимание одинаковый {} у страниц: {}".format(kind, ", ".join(v)))
    missing = [s for s in site.registry if s not in site.pages]
    if strict and total_err:
        print("\n--strict: есть ошибки ({}), site/ не тронут.".format(total_err))
        return 1
    # запись: всё отрендерено в памяти — теперь очищаем site/ и пишем
    OUT.mkdir(exist_ok=True)
    for child in OUT.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    copy_assets()
    for path, text in outputs.items():
        write(path, text)
    write(OUT / "404.html", render_404(site))
    for src, dst in site.redirects.items():
        write(out_file(src), render_redirect(site, src, dst))
    ht, nr = render_redirect_rules(site)
    write(OUT / ".htaccess", ht)
    write(OUT / "_redirects", nr)
    write(OUT / "sitemap.xml", render_sitemap(site, ok_slugs))
    write(OUT / "robots.txt", render_robots(site))
    missing_links = check_site_links()
    pending = sorted(k for k in missing_links if k.endswith("/index.html") and "/" + k[:-len("index.html")] in site.registry)
    broken = sorted(k for k in missing_links if k not in pending)
    print("\nСобрано страниц: {} из {} в карте сайта. Ошибок: {}.".format(len(outputs), len(site.registry), total_err))
    if broken:
        print("БИТЫЕ ссылки в site/: " + "; ".join("{} (из {})".format(k, ", ".join(sorted(missing_links[k]))[:80]) for k in broken))
    if pending:
        print("Ссылки на ещё не написанные страницы: {} адресов — заработают, когда появятся их JSON.".format(len(pending)))
    if missing:
        print("Ещё не написаны: " + ", ".join(missing))
    print("Готово: site/index.html (открывается двойным кликом) · sitemap.xml · robots.txt · 404.html · .htaccess / _redirects (301): {}".format(
        ", ".join("{}→{}".format(a, b) for a, b in site.redirects.items())))
    return 1 if total_err else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Сборка сайта ANPhotoLab (см. README.md и content/BLOCKS.md)")
    ap.add_argument("--check", metavar="PAGE_JSON", help="проверить одну страницу без записи")
    ap.add_argument("--only", metavar="NAME", help="собрать одну страницу (имя файла без .json)")
    ap.add_argument("--strict", action="store_true", help="не записывать site/, если есть хоть одна ошибка")
    args = ap.parse_args(argv)
    try:
        site = Site()
    except (ValueError, KeyError, FileNotFoundError) as e:
        print("FAIL не читаются src/config.json, content/photos.json или content/reviews.json: {}".format(e))
        return 1
    site.load_pages()
    if args.check:
        return cmd_check(site, args.check)
    if args.only:
        return cmd_only(site, args.only)
    return cmd_build(site, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
