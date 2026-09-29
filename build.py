#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ANPhotoLab v2 «Проявочная» — генератор статического сайта. Python 3.9+, только стандартная библиотека.

    python3 build.py                                  полная сборка: site/ очищается и собирается заново
    python3 build.py --check content/pages/x.json     проверить одну страницу, ничего не записывая
    python3 build.py --only x                         собрать одну страницу (content/pages/x.json) без очистки site/
                                                      (жанровую — вместе со страницами её альбомов)
    python3 build.py --check content/albums.json      проверить альбомы съёмок и собрать их страницы в памяти
    python3 build.py --strict                         полная сборка, любая ошибка — сборка не записывается

Альбомы съёмок (бриф v3 §3): content/albums.json → блок albums на жанровых страницах и страницы /<жанр>/<альбом>/
(AlbumCtx). Реальный альбом из папки с фото — tools/add_album.py.

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
ALBUMS_FILE = CONTENT / "albums.json"      # альбомы съёмок по жанрам (бриф v3 §3), формат — content/BLOCKS.md → «Альбомы»
ALBUM_DIR = "assets/img/albums"            # локальные кадры альбомов (делает tools/add_album.py): src/assets/img/albums/<жанр>/<slug>/
ALBUM_THUMB = "-800"                       # превью кадра рядом с ним: 01.jpg → 01-800.jpg (800 px по длинной стороне)
ALBUM_MIN_COUNT_SHOWN = 2                  # «N кадров» в мете альбома — от двух: «1 кадр» выдаёт тонкое портфолио
ALBUM_LD_MAX = 60                          # сколько кадров альбома перечислять в JSON-LD ImageGallery
SECTION_META = False       # v3 «аккуратнее»: правые мета-подписи в шапках секций («04 истории», «Нажмите — откроется крупно»)
HERO_FACTS = False         # v3 «аккуратнее»: моно-факты справа от крошек в первом экране («22 кадра · Новосибирск · Стоимость — по запросу»)
                           # не выводятся — поле meta в JSON допустимо, но молчит. True — вернуть подписи
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
    "contact-hero": _spec({}, {"word": "str", "photo": "int", "lead": "str", "focus": "str", "focus_mobile": "str",
                               "tg_label": "str", "items": "list"}, section=False),
    "genre-hero": _spec({}, {"word": "str", "eyebrow": "str", "photo": "int", "variant": "str", "lead": "str",
                             "facts": "list", "actions": "list", "focus": "str", "focus_mobile": "str",
                             "caption": "str"}, section=False),
    "genre-tabs": _spec({}, {"active": "str", "label": "str"}, section=False),
    "directions": _spec({}, {"items": "list", "previews": "dict"}),
    "related-directions": _spec({"items": "list"}, {"layout": "str", "columns": "int"}),
    "gallery": _spec({}, {"genre": "str", "ids": "list", "exclude": "list", "limit": "int", "layout": "str",
                          "min": "int", "end": "dict", "group": "str", "solo_note": "str"}),
    "albums": _spec({}, {"genre": "str", "items": "list", "columns": "int", "ratio": "str", "limit": "int"}),
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

HEROES = ("hero-person", "genre-hero", "contact-hero", "album-hero")

# Служебные блоки страницы альбома (/<жанр>/<альбом>/): их собирает генератор из content/albums.json,
# в JSON-страницах их нет. Значение — id секции по умолчанию
INTERNAL_BLOCKS = {"album-hero": "top", "album-gallery": "kadry", "album-nav": "dalshe"}

PAGE_FIELDS = {"slug": "str", "type": "str", "genre": "str", "nav": "str", "title": "str", "description": "str",
               "h1": "str", "og_image": "int", "parent": "str", "service": "str", "noindex": "bool",
               "breadcrumbs": "bool", "schema": "list", "blocks": "list", "_comment": "any", "form_preset": "str"}
PAGE_REQUIRED = ["slug", "type", "title", "description", "h1", "blocks"]
PAGE_TYPES = ("home", "genre", "seo", "service")

DEFAULT_IDS = {"hero-person": "top", "genre-hero": "top", "contact-hero": "top", "genre-tabs": "zhanry", "directions": "napravleniya",
               "related-directions": "drugie", "gallery": "kadry", "albums": "syomki", "stats": "opyt", "cases": "keysy",
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
        self.load_albums()

    # ------------------------------------------------------------------ альбомы съёмок (content/albums.json)
    ALBUM_FIELDS = {"genre": "str", "slug": "str", "title": "str", "subtitle": "any", "year": "any", "cover": "any",
                    "photos": "list", "text": "any", "demo": "bool", "description": "any", "_comment": "any"}
    FRAME_FIELDS = {"src": "str", "thumb": "str", "w": "int", "h": "int", "alt": "str", "focus": "str"}

    def load_albums(self, path=None):
        """Читает content/albums.json и проверяет каждый альбом: жанр, slug, кадры (id из photos.json своего жанра
        или локальный файл {"src", "w", "h", "alt"}), дубли, обложку. Альбом с ошибкой не выводится — ни в сетке жанра,
        ни отдельной страницей (сообщение — в отчёте «content/albums.json»). Результат: self.albums — проверенные альбомы
        в порядке файла, self.album_rep — отчёт, self.album_bad — {жанр: [slug альбомов с ошибками]}."""
        path = Path(path) if path else ALBUMS_FILE
        name = str(path.relative_to(ROOT)) if str(path).startswith(str(ROOT)) else str(path)
        rep = self.album_rep = Report(name)
        self.albums, self.album_bad = [], {}
        self.album_by_slug = {}
        if not path.exists():
            rep.note("файла нет — альбомов нет: блок albums на жанровых страницах будет скрыт")
            return
        try:
            doc = load_json(path)
        except ValueError as e:
            rep.err("", str(e))
            return
        if not isinstance(doc, dict) or not isinstance(doc.get("albums"), list):
            rep.err("", 'файл — объект {"albums": [ {…}, {…} ]}')
            return
        for k in doc:
            if k not in ("albums", "_comment"):
                rep.warn("", "неизвестное поле «{}» — будет проигнорировано".format(k))
        pairs, used = {}, {}
        for i, a in enumerate(doc["albums"]):
            before = len(rep.errors)
            al = self.parse_album(a, i, rep, pairs, used)
            if al and len(rep.errors) == before:
                self.albums.append(al)
                self.album_by_slug[al["slug"]] = al
            elif isinstance(a, dict) and isinstance(a.get("genre"), str):
                self.album_bad.setdefault(a["genre"], []).append(str(a.get("slug") or "#{}".format(i)))
        by_genre = {}
        for al in self.albums:
            by_genre.setdefault(al["genre"], []).append(al)
        demo = sum(1 for al in self.albums if al["demo"])
        rep.note("альбомов: {} ({}), из них демо: {} — заменить реальными (content/TODO-facts.md)".format(
            len(self.albums), ", ".join("{} {}".format(g, len(v)) for g, v in by_genre.items()) or "—", demo))
        # кадры жанра, которые не попали ни в один альбом (не ошибка: например, дубль серии убран намеренно)
        for g in self.genre_by_key:
            inside = {pk for al in by_genre.get(g, []) for pk in (photo_key(x["_p"]) for x in al["frames"])}
            out = [p["id"] for p in self.genre_photos(g) if p["id"] not in inside]
            if by_genre.get(g) and out:
                rep.note("{}: кадры жанра вне альбомов — {}".format(g, ", ".join(map(str, out))))
            if not by_genre.get(g):
                rep.note("{}: альбомов нет — на странице жанра блок albums будет скрыт".format(g))

    def parse_album(self, a, i, rep, pairs, used):
        w = "albums[{}]".format(i)
        if not isinstance(a, dict):
            rep.err(w, "альбом — объект {\"genre\", \"slug\", \"title\", \"photos\": [...]}")
            return None
        if isinstance(a.get("genre"), str) and isinstance(a.get("slug"), str):
            w = "albums[{}] {}/{}".format(i, a["genre"], a["slug"])
        for f, v in a.items():
            if f not in self.ALBUM_FIELDS:
                rep.warn(w, "неизвестное поле «{}» — будет проигнорировано".format(f))
            elif not TYPE_CHECK[self.ALBUM_FIELDS[f]](v):
                rep.err(w, "поле «{}» должно быть: {}".format(f, TYPE_NAMES[self.ALBUM_FIELDS[f]]))
        for f in ("genre", "slug", "title", "photos"):
            if f not in a:
                rep.err(w, "нет обязательного поля «{}»".format(f))
        gk = a.get("genre")
        g = self.genre_by_key.get(gk) if isinstance(gk, str) else None
        if isinstance(gk, str) and not g:
            rep.err(w + ".genre", "нет жанра «{}» (есть: {})".format(gk, ", ".join(self.genre_by_key)))
        slug = a.get("slug")
        if isinstance(slug, str) and not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", slug):
            rep.err(w + ".slug", "slug — латиница, цифры и дефис: «ivan-i-anna» (сейчас «{}»)".format(slug))
        title = a.get("title")
        if isinstance(title, str) and not title.strip():
            rep.err(w + ".title", "пустое название альбома")
        if g and isinstance(slug, str):
            if (gk, slug) in pairs:
                rep.err(w + ".slug", "альбом {}/{} уже есть (albums[{}]) — slug внутри жанра не повторяется".format(gk, slug, pairs[(gk, slug)]))
            pairs[(gk, slug)] = i
        for f in ("subtitle", "description"):
            if f in a and a[f] is not None and not isinstance(a[f], str):
                rep.err(w + "." + f, "строка или null")
        year = a.get("year")
        if year is not None and (isinstance(year, bool) or not isinstance(year, int) or not 1990 <= year <= 2100):
            rep.err(w + ".year", "год — число (2025) или null")
        text = a.get("text")
        if text not in (None, "") and not (isinstance(text, str) or (isinstance(text, list) and all(isinstance(x, str) for x in text))):
            rep.err(w + ".text", "текст — строка или массив строк (абзацы)")
        photos = a.get("photos")
        if not isinstance(photos, list):
            return None
        if not photos:
            rep.err(w + ".photos", "пустой альбом: нужен хотя бы один кадр")
            return None
        frames, seen = [], set()
        for k, x in enumerate(photos):
            wk = "{}.photos[{}]".format(w, k)
            fr = self.parse_frame(x, wk, rep, gk if g else None, a, k)
            if not fr:
                continue
            key = photo_key(fr["_p"])
            if key in seen:
                rep.err(wk, "кадр {} повторяется в альбоме".format(frame_label(fr["_p"])))
                continue
            seen.add(key)
            if key in used and used[key] != w:
                rep.warn(wk, "кадр {} уже есть в альбоме {} — один кадр лучше держать в одной съёмке".format(frame_label(fr["_p"]), used[key]))
            used.setdefault(key, w)
            frames.append(fr)
        if not frames or not g or not isinstance(slug, str) or not isinstance(title, str):
            return None
        cover = frames[0]
        cv = a.get("cover")
        if cv is not None:
            if isinstance(cv, bool) or not isinstance(cv, (int, str)):
                rep.err(w + ".cover", "обложка — id кадра (число) или путь src локального кадра этого альбома")
            else:
                if isinstance(cv, str):
                    cv = cv.lstrip("/")
                hit = next((f for f in frames if (f["_p"].get("id") == cv if isinstance(cv, int) else f["_p"].get("src") == cv)), None)
                if hit:
                    cover = hit
                elif isinstance(cv, int) and cv in self.photos:
                    rep.err(w + ".cover", "кадр {} — не из этого альбома: обложка берётся из его кадров".format(cv))
                else:
                    rep.err(w + ".cover", "обложки «{}» нет среди кадров альбома".format(cv))
        text_list = [text] if isinstance(text, str) and text.strip() else (text if isinstance(text, list) else [])
        return {"i": i, "w": w, "genre": gk, "g": g, "key": slug, "slug": g["slug"] + slug + "/", "title": title.strip(),
                "subtitle": (a.get("subtitle") or "").strip() if isinstance(a.get("subtitle"), str) else "",
                "year": year if isinstance(year, int) and not isinstance(year, bool) else None, "text": text_list,
                "demo": a.get("demo") is True, "description": a.get("description") if isinstance(a.get("description"), str) else "",
                "frames": frames, "cover": cover}

    def parse_frame(self, x, wk, rep, gk, a, k):
        """Кадр альбома → {"_p": фото, "focus": …}. Фото — запись из photos.json (по id) или «локальное фото»
        того же вида (thumb/mid/large/w/h/alt), собранное из объекта {"src", "w", "h", "alt"[, "thumb", "focus"]}."""
        if isinstance(x, int) and not isinstance(x, bool):
            x = {"id": x}
        if isinstance(x, dict) and "id" in x:
            pid = x["id"]
            if isinstance(pid, bool) or not isinstance(pid, int):
                rep.err(wk, "id кадра — число, получено {!r}".format(pid))
                return None
            p = self.photos.get(pid)
            if not p:
                rep.err(wk, "кадра с id {} нет в content/photos.json (есть 0–{})".format(pid, max(self.photos)))
                return None
            if gk and p["genre"] != gk:
                rep.err(wk, "кадр {} из жанра «{}», а альбом — «{}»: чужие кадры в альбом не ставим".format(pid, p["genre"], gk))
                return None
            for f in x:
                if f not in ("id", "focus"):
                    rep.warn(wk, "неизвестное поле «{}» — будет проигнорировано".format(f))
            return {"_p": p, "focus": x.get("focus") if isinstance(x.get("focus"), str) else None}
        if not isinstance(x, dict) or "src" not in x:
            rep.err(wk, "кадр — id из photos.json (число) или объект {\"src\": \"assets/img/albums/<жанр>/<slug>/01.jpg\", \"w\": 2400, \"h\": 1600, \"alt\": \"…\"}")
            return None
        for f, v in x.items():
            if f not in self.FRAME_FIELDS:
                rep.warn(wk, "неизвестное поле «{}» — будет проигнорировано".format(f))
            elif not TYPE_CHECK[self.FRAME_FIELDS[f]](v):
                rep.err(wk, "поле «{}» должно быть: {}".format(f, TYPE_NAMES[self.FRAME_FIELDS[f]]))
                return None
        src = x["src"].strip().lstrip("/")
        if not src.startswith(ALBUM_DIR + "/") or ".." in src.split("/") or "\\" in src:
            rep.err(wk, "src — путь внутри src/{}/ (например «{}/{}/{}/01.jpg»), сейчас «{}»".format(
                ALBUM_DIR, ALBUM_DIR, gk or "<жанр>", a.get("slug") or "<slug>", x["src"]))
            return None
        if not re.search(r"\.(jpe?g|png|webp|avif)$", src, re.IGNORECASE):
            rep.err(wk, "src — картинка .jpg, .png, .webp или .avif")
            return None
        if not (SRC / src).is_file():
            rep.err(wk, "файла src/{} нет — добавьте его (tools/add_album.py) или поправьте путь".format(src))
            return None
        pw, ph_ = x.get("w"), x.get("h")
        if not (isinstance(pw, int) and isinstance(ph_, int) and pw > 0 and ph_ > 0):
            rep.err(wk, "нужны w и h — размер файла в пикселях (их пишет tools/add_album.py)")
            return None
        alt = x.get("alt") if isinstance(x.get("alt"), str) else ""
        if not alt.strip():
            rep.warn(wk, "нет alt — подставлю «{}, кадр {}»; лучше описать, что в кадре".format(a.get("title", "Альбом"), k + 1))
            alt = "{}, кадр {}".format(a.get("title", "Альбом"), k + 1)
        thumb = x.get("thumb", "").strip().lstrip("/") if isinstance(x.get("thumb"), str) else ""
        if thumb and not (SRC / thumb).is_file():
            rep.warn(wk, "превью src/{} нет — беру основной файл".format(thumb))
            thumb = ""
        if not thumb:
            stem, dot, ext = src.rpartition(".")
            conv = stem + ALBUM_THUMB + dot + ext
            thumb = conv if (SRC / conv).is_file() else ""
        r = pw / ph_
        orient = "portrait" if r < 0.9 else ("wide" if r > 1.7 else "landscape")
        big = "/" + src
        srcset = [(pw, big)]
        if thumb:
            tw = round(pw * 800 / max(pw, ph_)) if max(pw, ph_) > 800 else pw
            if tw < pw:
                srcset.insert(0, (tw, "/" + thumb))
        p = {"id": None, "src": src, "genre": gk, "orientation": orient, "ratio": round(r, 3), "w": pw, "h": ph_, "alt": alt.strip(),
             "thumb": "/" + (thumb or src), "mid": big, "large": big, "_srcset": srcset, "_lw": pw, "_lh": ph_}
        if isinstance(x.get("focus"), str):
            p["focus"] = x["focus"]
        return {"_p": p, "focus": None}

    def genre_albums(self, key):
        return [al for al in self.albums if al["genre"] == key]

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
        """Подпись кадра в лайтбоксе — название жанра. Номер кадра клиенту ничего не говорит (бриф v3 §1)."""
        return self.genre_name(p["genre"])

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


def photo_key(p):
    """Ключ кадра: id из photos.json или путь локального файла альбома (у него id нет)."""
    return p["id"] if p.get("id") is not None else p.get("large")


def frame_label(p):
    return str(p["id"]) if p.get("id") is not None else "«{}»".format(p.get("src", p.get("large")))


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
        if slug in self.site.pages or slug in self.site.album_by_slug:
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
        if p.get("_srcset"):   # локальный кадр альбома: превью 800 px + основной файл (ширины — по факту)
            return ", ".join("{} {}w".format(self.purl(u), w) for w, u in p["_srcset"])
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
        """Шапка секции v3 «аккуратнее»: простая моно-метка (без рамки и «лампы») + заголовок H2 одного веса
        (или H2 в виде метки). Правая мета-подпись — только при SECTION_META = True."""
        style = b.get("title_style") or style or "xl"
        if style not in ("xl", "l", "tag"):
            self.rep.warn(b["_w"], "title_style бывает xl | l | tag")
            style = "xl"
        title, eyebrow, note, meta = b.get("title"), b.get("eyebrow"), b.get("note"), b.get("meta")
        if not SECTION_META:
            meta = None
        if not (title or eyebrow):
            return ""
        if title and eyebrow:
            # метка над заголовком не повторяет его: «Кейс» над «Кейс», «Как снимаю» над «Как снимаю сцену» — тавтология
            # на экране и двойное чтение скринридером
            e, t = norm(eyebrow).rstrip(".:"), norm(title)
            # v3: и когда все слова метки уже есть в заголовке («Вопросы» над «Частые вопросы») — метка лишняя
            ew, tw = set(re.findall(r"[\wё]+", e)), set(re.findall(r"[\wё]+", t))
            if t == e or t.startswith(e + " ") or t.startswith(e + ":") or (ew and ew <= tw):
                eyebrow = None
        sid, w = b["_id"], b["_w"]
        meta_html = '<span class="sh__meta mono">{}</span>'.format(self.md(meta, w + ".meta")) if meta else ""
        note_html = '<p class="sh__note">{}</p>'.format(self.md(note, w + ".note")) if note else ""
        if style == "tag" or not title:
            tagname = "h2" if title else "p"
            idattr = ' id="h-{}"'.format(sid) if title else ""
            top = '<div class="sh__top"><{t} class="tag"{i}><span>{x}</span></{t}>{m}</div>'.format(
                t=tagname, i=idattr, x=self.md(title or eyebrow, w), m=meta_html)
            return '<header class="sh sh--tag" data-rv>{}{}</header>'.format(top, note_html)
        top = ""
        if eyebrow or meta:
            tg = '<p class="tag"><span>{}</span></p>'.format(self.t(eyebrow)) if eyebrow else "<span></span>"
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
            if t in BLOCK_SPECS or self.internal(t):
                base = b.get("id") if isinstance(b.get("id"), str) else DEFAULT_IDS.get(t) or INTERNAL_BLOCKS.get(t, t)
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
            self.rep.err("blocks", "на странице нет первого экрана (hero-person, genre-hero или contact-hero) — в нём живёт единственный H1")
        elif heroes > 1:
            self.rep.err("blocks", "первый экран (hero-person / genre-hero / contact-hero) должен быть один — один H1 на страницу")
        if sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "lead-form") > 1:
            self.rep.err("blocks", "блок lead-form (CTA-полоса Telegram) должен быть один на странице")

    def internal(self, t):
        """Служебный блок страницы альбома — только на страницах, которые генератор собирает сам (AlbumCtx)."""
        return False

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
        # v3: мета-строка над портретом (координаты, Photo & Video Lab, город) и строка мессенджеров под кнопкой —
        # только если явно заданы в JSON (meta: [...], messengers: true). По умолчанию первый экран чистый:
        # портрет, имя, плашки, одна строка, одна кнопка
        meta = b.get("meta") or []
        meta_html = '<div class="hp__meta mono">{}</div>'.format("".join("<span>{}</span>".format(self.t(m)) for m in meta)) if meta else ""
        lead = '<p class="hp__lead">{}</p>'.format(self.md(b["lead"], w + ".lead")) if b.get("lead") else ""
        acts = self.actions(b.get("actions"), w + ".actions", default=[{"label": "Обсудить съёмку", "href": "#zayavka"}],
                            cls="acts hp__acts")
        msg = ""
        if b.get("messengers", False):
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
        # v3: строка фактов справа от крошек — шум (бриф v3 §1: «07 направлений», «Стоимость — по запросу»); при
        # HERO_FACTS = False первый экран показывает только крошки, поле facts в JSON молча пропускается
        facts = b.get("facts") if HERO_FACTS else []
        if facts is None:
            facts = []
            if g:
                # жанр показывает съёмки (альбомы), а не стену кадров: «4 съёмки»; число — от двух, «1 съёмка» выдаёт
                # тонкое портфолио. Без альбомов — число кадров, только когда оно работает на доверие (≥ MIN_COUNT_SHOWN)
                na = len(self.site.genre_albums(g["key"]))
                n = self.site.counts.get(g["key"], 0)
                if na >= 2:
                    facts.append("{} {}".format(na, plural(na, "съёмка", "съёмки", "съёмок")))
                elif not na and n >= MIN_COUNT_SHOWN:
                    facts.append("{} {}".format(n, plural(n, "кадр", "кадра", "кадров")))
            facts.append(self.cfg["city"])
        facts_html = '<p class="gh__facts mono">{}</p>'.format("".join("<span>{}</span>".format(self.t(x)) for x in facts)) if facts else ""
        chs, _ = self.letters(word)
        # data-fit-max: слово подгоняется по ширине, но не выше доли окна — короткое слово («Отзывы») не съедает первый экран
        word_html = '<p class="gh__word" aria-hidden="true" data-fit data-fit-max="{}" style="--n:{}"><span class="gh__in">{}</span></p>'.format(
            FIT_MAX_WORD, len(word), chs)
        eb = '<p class="gh__eyebrow tag"><span>{}</span></p>'.format(self.t(b["eyebrow"])) if b.get("eyebrow") else ""
        lead = '<p class="gh__lead">{}</p>'.format(self.md(b["lead"], w + ".lead", nw=True)) if b.get("lead") else ""
        default = [{"label": "Обсудить съёмку", "href": "#zayavka"}]
        gal = self.first_gallery()
        if gal and gal.get("_id"):
            # под первым экраном — сетка альбомов (жанр, бриф v3 §3) или галерея кадров (SEO-страницы)
            label = "Смотреть съёмки ↓" if gal.get("type") == "albums" else "Смотреть кадры ↓"
            default.append({"label": label, "href": "#" + gal["_id"], "style": "link"})
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
            # (у сетки альбомов группы лайтбокса нет: карточка ведёт на страницу альбома — обложка открывается одна)
            wth = ' data-lb-with="{}"'.format(esc(gal["_group"])) if gal and gal.get("type") == "gallery" and gal.get("_group") else ""
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

    # ------------------------------------------------------------------ contact-hero (первый экран «Контактов»)
    def b_contact_hero(self, b):
        """Первый экран «Контактов» (бриф v3 §4): слева — крупное слово + H1, строка-лид, главный контакт — ник Telegram
        крупно, ниже строка «Телефон · WhatsApp · Город»; справа — портрет Александра (id 93) 4:5. На телефоне:
        слово → портрет (квадрат по лицу, focus_mobile) → лид → Telegram → остальные контакты.
        Если H1 начинается со слова («Контакты фотографа в Новосибирске») — слово и есть начало H1, остальное — подписью."""
        w = b["_w"]
        c = self.cfg["contacts"]
        word = b.get("word") or self.nav_label()
        h1 = str(self.d.get("h1", ""))
        if norm(h1).startswith(norm(word)):
            rest = h1[len(word):].strip()
            h1_html = '<h1 class="ct__h1" id="h1"><span class="ct__word">{}</span>{}</h1>'.format(
                self.t(h1[:len(word)]), ' <span class="ct__sub">{}</span>'.format(self.md(rest, w, nw=True)) if rest else "")
        else:
            h1_html = ('<p class="ct__word" aria-hidden="true">{}</p><h1 class="ct__h1" id="h1"><span class="ct__sub">{}</span></h1>').format(
                self.t(word), self.md(h1, w, nw=True))
        lead = '<p class="ct__lead">{}</p>'.format(self.md(b["lead"], w + ".lead", nw=True)) if b.get("lead") else ""
        tg, handle = c["telegram"], c.get("telegram_handle", "Telegram")
        tg_html = ('<a class="ct__tg" href="{tg}" target="_blank" rel="noopener" data-cta="tg" aria-label="{al}">'
                   '<span class="ct__cap mono">{cap}</span><span class="ct__h">{h}</span>'
                   '<span class="ct__arr" aria-hidden="true">↗</span></a>').format(
            tg=esc(tg), al=esc("Написать в Telegram " + handle), cap=self.t(b.get("tg_label") or "Telegram — быстрее всего"), h=esc(handle))
        items = b.get("items")
        lis = []
        if isinstance(items, list) and items:
            for i, it in enumerate(items):
                wi = "{}.items[{}]".format(w, i)
                if not isinstance(it, dict) or not it.get("label") or not it.get("text"):
                    self.rep.err(wi, "контакт — объект {\"label\": \"Телефон\", \"text\": \"+7 …\", \"href\": \"tel:…\"}")
                    continue
                val = self.t(it["text"]).replace(" ", NBSP)
                if it.get("href"):
                    href = self.link(it["href"], wi)
                    val = '<a href="{}"{}>{}</a>'.format(esc(href), self.ext_attrs(href), val)
                lis.append('<li><span class="mono">{}</span>{}</li>'.format(self.t(it["label"]), val))
        else:
            lis = ['<li><span class="mono">Телефон</span><a href="{}">{}</a></li>'.format(
                       esc(c["phone_href"]), esc(c["phone_display"]).replace(" ", NBSP)),
                   '<li><span class="mono">WhatsApp</span><a href="{}" target="_blank" rel="noopener">{}{}↗</a></li>'.format(
                       esc(c["whatsapp"]), esc(c.get("messenger_display", "Написать")).replace(" ", NBSP), NBSP),
                   '<li><span class="mono">Город</span><span>{}</span></li>'.format(esc(self.cfg["city"]))]
        fig = ""
        p = self.photo(b["photo"], w + ".photo") if "photo" in b else None
        if p:
            sizes = "(min-width: 900px) 38vw, calc(100vw - 32px)"
            self.hero_photo, self.hero_sizes = p, sizes
            style = self.focus_vars(b.get("focus") or p.get("focus"))
            if b.get("focus_mobile"):
                fx, _, fy = b["focus_mobile"].partition(" ")
                style += ";--mfx:{};--mfy:{}".format(fx, fy or "50%")
            fig = '<figure class="ct__fig" style="{}">{}</figure>'.format(esc(style.strip(";")), self.img(p, sizes, priority=True))
        return ('<section class="ct" id="{id}" aria-labelledby="h1"><div class="wrap">'
                '<div class="gh__top">{crumbs}</div>'
                '<div class="ct__grid{nf}"><div class="ct__head">{h1}</div>{fig}{lead}{tg}<ul class="ct__list">{lis}</ul></div>'
                '</div></section>').format(
            id=b["_id"], crumbs=self.crumbs(), nf="" if fig else " ct__grid--nophoto", h1=h1_html, fig=fig, lead=lead,
            tg=tg_html, lis="".join(lis))

    # ------------------------------------------------------------------ genre-tabs
    def b_genre_tabs(self, b):
        active = b.get("active") or (self.genre["slug"] if self.genre else self.slug)
        lis = []
        # v3: без номеров 01–07 и без счётчиков кадров — только название в скобках, активная — кремовая плашка
        for g in self.site.genres:
            cur = ' aria-current="page"' if g["slug"] == active else ""
            self.check_slug(g["slug"], b["_w"])
            lis.append('<li><a href="{}"{}><span class="br" aria-hidden="true">[</span>{}<span class="br" aria-hidden="true">]</span></a></li>'.format(
                esc(self.slug_href(g["slug"])), cur, self.t(g["name"])))
        label = b.get("label", "Направления")
        return ('<nav class="gtabs" id="{}" aria-label="{}"><div class="wrap gtabs__in">'
                '<span class="gtabs__label mono" aria-hidden="true">{}</span><div class="gtabs__sc" data-tabs><ul>{}</ul></div></div></nav>').format(
            b["_id"], esc(label), self.t(label), "".join(lis))

    # ------------------------------------------------------------------ directions (сдержанный список слов)
    def dir_list(self, items, compact=False):
        """items: [{href, label}] → столбик слов по центру. v3 «сдержанно»: без плашки, всплывающих фото, подписей-категорий
        и номеров. Слово приглушено; наведение/фокус — лёгкое увеличение (scale 1.05) и полная яркость, клик — переход
        (со шторкой). Ссылка шириной в слово: увеличивается ровно то, на что навели."""
        rows = []
        for it in items:
            rows.append('<li class="dir__i"><a class="dir__a" href="{}"><span class="dir__w">{}</span></a></li>'.format(
                esc(it["href"]), self.t(it["label"])))
        return '<div class="dir-box{}"><ul class="dir" data-rv>{}</ul></div>'.format(
            " dir-box--s" if compact else "", "".join(rows))

    def b_directions(self, b):
        w = b["_w"]
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
            items.append({"href": href, "label": it.get("label") or (g["name"] if g else self.site.label(slug))})
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
            # компактная сетка: миниатюра, слово поменьше и стрелка — когда списков на странице несколько подряд (/uslugi/),
            # второй и третий огромный список превращают страницу в стену слов. v3 «аккуратнее»: без подписей-категорий
            # (поле cat не выводится, как и у направлений). Колонки — 3 или 4, чтобы последний ряд был полнее:
            # 7 → 4 + 3, 6 → 3 + 3, 5 → 3 + 2, 8 → 4 + 4; до трёх пунктов — в один ряд. Поле columns (2–4) — вручную.
            n = len(items)
            cols = b.get("columns")
            if cols not in (None, 2, 3, 4):
                self.rep.warn(w + ".columns", "columns бывает 2, 3 или 4")
                cols = None
            if cols is None:
                cols = max(n, 1) if n <= 3 else min((4, 3), key=lambda c: ((c - n % c) % c, -c))
            lis = []
            for it in items:
                ph = it["photos"]
                th = self.thumb(ph[0], "rg__th") if ph else '<span class="rg__th" aria-hidden="true"></span>'
                lis.append(('<li class="rg__i"><a class="rg__a" href="{h}">{th}<span class="rg__txt">'
                            '<span class="rg__w">{l}</span></span><span class="rg__go" aria-hidden="true">→</span></a></li>').format(
                    h=esc(it["href"]), th=th, l=self.t(it["label"])))
            head = self.sh(b, "tag")
            return ('<section class="sec b-related b-related--grid" id="{}" aria-labelledby="h-{}"><div class="wrap">{}'
                    '<ul class="rg rg--c{}">{}</ul></div></section>').format(b["_id"], b["_id"], head, cols, "".join(lis))
        head = '<div class="wrap">{}</div>'.format(self.sh(b, "tag"))
        return '<section class="sec b-directions b-related" id="{}" aria-labelledby="h-{}">{}{}</section>'.format(
            b["_id"], b["_id"], head, self.dir_list(items, compact=True))

    # ------------------------------------------------------------------ gallery (бенто)
    def first_gallery(self):
        """Первая галерея или сетка альбомов страницы, которая будет показана (для кнопки «Смотреть кадры ↓» /
        «Смотреть съёмки ↓» и лайтбокса обложки)."""
        for x in self.d.get("blocks", []):
            if not isinstance(x, dict):
                continue
            if x.get("type") == "gallery" and self.gallery_visible(x):
                return x
            if x.get("type") == "albums" and self.album_list(x):
                return x
        return None

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
        """Подпись в лайтбоксе (на плитках подписей нет — бриф v3 §1): на главной и жанрах — название жанра кадра
        («Портреты»), на SEO и служебных — название страницы (метка чужого жанра там выглядит каталогом)."""
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

    def ph(self, x, group, style, sizes, dl=0, cls="ph", lazy=True, priority=False):
        p = x["_p"]
        self.gallery_photos.append(p)
        self.has_lb = True
        if priority and not self.hero_photo:
            # первый кадр страницы альбома — LCP: preload в <head> с теми же sizes
            self.hero_photo, self.hero_sizes = p, sizes
        f = self.focus_vars(x.get("focus") or p.get("focus"))
        st = ";".join(s for s in (style, f, "--dl:{}ms".format(dl) if dl else "") if s)
        # v3: на плитке только кадр — без «№ 020 · Портреты» (номер кадра — шум); подпись живёт в лайтбоксе (data-cap)
        return ('<figure class="{cls}" data-rv style="{st}"><a class="ph__a" href="{href}" data-lb="{g}" data-cap="{lc}">{img}</a>'
                '</figure>').format(
            cls=cls, st=esc(st), href=esc(self.purl(p["large"])), g=esc(group), lc=esc(self.lb_cap(p)),
            img=self.img(p, sizes, lazy=lazy, priority=priority))

    def gallery_body(self, items, group, layout="bento", solo_note="", eager=0):
        """Раскладка кадров: bento (шаблоны рядов BENTO) или solo (1–2 кадра крупно, без обрезки: кадр целиком
        помещается в окно). eager — сколько первых кадров грузить сразу (страница альбома: кадры сразу под заголовком)."""
        n = len(items)
        if layout == "solo" or n == 1:
            parts = []
            for k, x in enumerate(items[:2]):
                p = x["_p"]
                r = "2/3" if self.orient(p) == "P" else "3/2"
                rn = 2 / 3 if self.orient(p) == "P" else 1.5
                parts.append(self.ph(x, group, "--r:{};--rn:{}".format(r, round(rn, 4)), "(min-width: 900px) 80vw, 100vw",
                                     cls="ph ph--solo", lazy=k >= eager, priority=eager > 0 and k == 0))
            note = '<p class="solo__note">{}</p>'.format(solo_note) if solo_note else ""
            return '<div class="solo{}">{}{}</div>'.format(" solo--2" if len(parts) == 2 else "", "".join(parts), note)
        gs = []
        k_all = 0
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
                parts.append(self.ph(x, group, "--a:{};--ms:{};--mr:{}".format(area, ms, mr), sizes, dl=(k % 3) * 80,
                                     lazy=k_all >= eager, priority=eager > 0 and k_all == 0))
                k_all += 1
            gs.append('<div class="bento__g bento__g--{}" style="--rows:{}">{}</div>'.format(name, rows, "".join(parts)))
        return '<div class="bento">{}</div>'.format("".join(gs))

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
        note = self.md(b["solo_note"], b["_w"]) if b.get("solo_note") else ""
        body = self.gallery_body(items, group, layout, solo_note=note)
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

    # ------------------------------------------------------------------ albums (сетка альбомов съёмок жанра, бриф v3 §3)
    def album_genre(self, b):
        g = b.get("genre")
        if isinstance(g, str):
            return g
        return self.genre["key"] if self.genre else None

    def album_list(self, b, report=False):
        """Альбомы блока: жанр страницы (или поле genre), порядок — как в content/albums.json или по списку items
        (slug альбомов), limit — сколько показать. report=True — с сообщениями в отчёт страницы."""
        w = b.get("_w", "albums")
        gk = self.album_genre(b)
        if gk not in self.site.genre_by_key:
            if report:
                self.rep.err(w + ".genre", "укажите genre (ключ жанра) — на странице «{}» жанра нет".format(self.slug)
                             if gk is None else "нет жанра «{}» (есть: {})".format(gk, ", ".join(self.site.genre_by_key)))
            return []
        albums = self.site.genre_albums(gk)
        if report and self.site.album_bad.get(gk):
            self.rep.warn(w, "альбомы {} с ошибками не выводятся — проверьте: python3 build.py --check content/albums.json".format(
                ", ".join(self.site.album_bad[gk])))
        items = b.get("items")
        if isinstance(items, list) and items:
            by = {al["key"]: al for al in albums}
            picked = []
            for i, s in enumerate(items):
                s = s.strip("/").rsplit("/", 1)[-1] if isinstance(s, str) else s
                if s in by and by[s] not in picked:
                    picked.append(by[s])
                elif report:
                    self.rep.warn("{}.items[{}]".format(w, i), "альбома «{}» в жанре {} нет — пропущен".format(s, gk))
            albums = picked
        if isinstance(b.get("limit"), int):
            albums = albums[:b["limit"]]
        return albums

    def album_meta(self, al, genre=True):
        """Мета альбома: «Свадьба · 2025 · 48 кадров». Год — если есть; число кадров — от ALBUM_MIN_COUNT_SHOWN."""
        n = len(al["frames"])
        parts = [al["g"].get("single") or al["g"]["name"]] if genre else []
        if al.get("year"):
            parts.append(str(al["year"]))
        if n >= ALBUM_MIN_COUNT_SHOWN:
            parts.append("{} {}".format(n, plural(n, "кадр", "кадра", "кадров")))
        return parts

    def album_card_cover(self, al):
        """Обложка карточки на странице жанра: cover альбома, но не тот же кадр, что стоит в первом экране страницы
        (иначе один снимок идёт дважды подряд) — тогда следующий кадр альбома. У альбома из одного кадра выбора нет."""
        c = al["cover"]
        if photo_key(c["_p"]) in self.auto_excl:
            alt = next((f for f in al["frames"] if photo_key(f["_p"]) not in self.auto_excl), None)
            if alt:
                return alt
        return c

    def b_albums(self, b):
        """Карточки-«окна» съёмок: обложка в рамке, название, мета «Жанр · год · N кадров». Клик — страница альбома
        /<жанр>/<альбом>/ (со шторкой перехода). Наведение — кадр чуть приближается внутри рамки (1.04), без плашек.
        Колонки: 1 альбом — широкая карточка (обложка + текст рядом), 2 и 4 — две колонки, 3 и от 5 — три; телефон — одна.
        Пропорция обложек одна на сетку: 4:5, если вертикальных обложек больше, иначе 3:2 (поле ratio — вручную).
        Без «дырок» в последнем ряду трёх колонок (сетка на 6 долей, карточка — 2 доли): при 3:2 и 1:1 хвост из двух
        карточек растягивается на половину ширины каждая (5 → 3 + 2, 7 → 3 + 2 + 2); при 4:5 половина ширины вытянула бы
        портреты на весь экран — хвост того же размера встаёт по центру (5 → 3 + 2 по центру, 7 → 3 + 3 + 1 по центру).
        На планшете (2 колонки) нечётная последняя карточка — на всю строку, обложка и текст рядом."""
        w = b["_w"]
        albums = self.album_list(b, report=True)
        if not albums:
            if self.album_genre(b) in self.site.genre_by_key:
                self.rep.warn(w, "у жанра {} нет альбомов в content/albums.json — блок пропущен".format(self.album_genre(b)))
            return ""
        b.setdefault("title", "Съёмки")
        n = len(albums)
        cols = b.get("columns")
        if cols not in (None, 1, 2, 3):
            self.rep.warn(w, "columns бывает 1, 2 или 3")
            cols = None
        if cols is None:
            cols = 1 if n == 1 else (3 if n == 3 or n >= 5 else 2)
        covers = [self.album_card_cover(al) for al in albums]
        ratio = b.get("ratio")
        if ratio not in (None, "3/2", "4/5", "1/1"):
            self.rep.warn(w, "ratio бывает 3/2, 4/5 или 1/1")
            ratio = None
        if ratio is None:
            portraits = sum(1 for c in covers if self.orient(c["_p"]) == "P")
            ratio = "4/5" if portraits > n - portraits else "3/2"
        one = cols == 1 and n == 1
        # хвост последнего ряда при трёх колонках: {индекс: класс}. al__i--h — половина ширины (span 3 из 6),
        # al__i--c2 / al__i--c3 — та же треть, но со сдвигом к центру (со 2-й или 3-й доли из 6)
        tail = {}
        if cols == 3 and n > 3 and n % 3:
            k = n % 3
            if ratio == "4/5":
                if k == 2:
                    tail = {n - 2: "al__i--c2"}
                else:
                    tail = {n - 1: "al__i--c3"}
            else:
                tail = {i: "al__i--h" for i in range(n - (2 if k == 2 else 4), n)}
        # две колонки (планшет или columns: 2) и нечётное число — последняя карточка на всю строку, обложка и текст рядом
        odd_last = n - 1 if cols in (2, 3) and n % 2 and n > 1 else None
        lis = []
        for i, (al, c) in enumerate(zip(albums, covers)):
            p = c["_p"]
            self.gallery_photos.append(p)
            href = self.slug_href(al["slug"])
            meta = " · ".join(self.t(x) for x in self.album_meta(al))
            if one:
                r = "4/5" if self.orient(p) == "P" else "3/2"
                sizes = "(min-width: 900px) {}vw, calc(100vw - 32px)".format(40 if r == "4/5" else 56)
            else:
                r = ratio
                sizes = {3: "(min-width: 1100px) 31vw, (min-width: 700px) 47vw, calc(100vw - 32px)",
                         2: "(min-width: 700px) 47vw, calc(100vw - 32px)"}.get(cols, "calc(100vw - 32px)")
                if tail.get(i) == "al__i--h":
                    sizes = "(min-width: 700px) 47vw, calc(100vw - 32px)"
            st = ";".join(s for s in (self.focus_vars(c.get("focus") or p.get("focus")), "--r:{}".format(r)) if s)
            fig = '<span class="al__fig" style="{}">{}</span>'.format(esc(st), self.img(p, sizes, alt=p.get("alt", "")))
            extra = ""
            if one:
                sub = '<span class="al__sub">{}</span>'.format(self.t(al["subtitle"])) if al.get("subtitle") else ""
                extra = sub + '<span class="al__go" aria-hidden="true">Смотреть съёмку →</span>'
            ic = " ".join(x for x in ("al__i", tail.get(i, ""), "al__i--last" if i == odd_last else "") if x)
            lis.append(('<li class="{ic}" data-rv style="--dl:{dl}ms"><a class="al__a" href="{h}">{fig}'
                        '<span class="al__txt"><h3 class="al__t">{t}</h3><span class="al__meta mono">{m}</span>{x}</span></a></li>').format(
                ic=ic, dl=(i % cols) * 90, h=esc(href), fig=fig, t=self.t(al["title"]), m=meta, x=extra))
        cls = "al al--one" + (" al--one-p" if self.orient(covers[0]["_p"]) == "P" else "") if one else "al al--c{}".format(cols)
        inner = '<ul class="{}">{}</ul>'.format(cls, "".join(lis))
        self.rep.note("{}: альбомов {} — {}".format(w, n, ", ".join(al["key"] for al in albums)))
        return self.section(b, inner, style="l")

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
        """v3 «аккуратнее»: простая сетка карточек (2 колонки на десктопе, 1 на телефоне) — обложка 3:2, название,
        2–3 метки строкой, три короткие строки Задача / Решение / Результат, ссылка. Без стопки, затемнения,
        счётчиков «01 / 04» и свёрнутых блоков."""
        w = b["_w"]
        lis = []
        items = b.get("items") or []
        for i, it in enumerate(items):
            wi = "{}.items[{}]".format(w, i)
            if not isinstance(it, dict) or not it.get("title") or not all(it.get(k) for k in ("task", "solution", "result")):
                self.rep.err(wi, "кейс — объект {photo, title, tags[], task, solution, result, link?}")
                continue
            p = self.photo(it.get("photo"), wi + ".photo") if "photo" in it else None
            tags = it.get("tags") or []
            if len(tags) > 3:
                self.rep.warn(wi, "в кейсе лучше 2–3 метки (сейчас {})".format(len(tags)))
            tags_html = '<ul class="cs__tags mono" aria-label="Факты">{}</ul>'.format(
                "".join("<li>{}</li>".format(self.t(x)) for x in tags)) if tags else ""
            fig = ""
            if p:
                self.has_lb = True
                self.gallery_photos.append(p)
                fig = '<figure class="cs__fig" style="{}"><a class="ph__a" href="{}" data-lb="cases" data-cap="{}">{}</a></figure>'.format(
                    esc(self.focus_vars(it.get("focus") or p.get("focus"))), esc(self.purl(p["large"])),
                    esc(strip_md(it["title"])),
                    self.img(p, "(min-width: 900px) 46vw, calc(100vw - 32px)"))
            link = ""
            if isinstance(it.get("link"), dict) and it["link"].get("href"):
                href = self.link(it["link"]["href"], wi + ".link")
                link = '<a class="ulink cs__link" href="{}"{}>{}</a>'.format(esc(href), self.dt_attr(href), self.t(it["link"].get("label", "Смотреть кадры →")))
            row = lambda k, lab, cls="": '<div class="cs__row{}"><dt class="mono">{}</dt><dd>{}</dd></div>'.format(
                cls, lab, self.md(it[k], wi + "." + k, nw=True))
            dl = '<dl class="cs__dl">{}{}{}</dl>'.format(row("task", "Задача"), row("solution", "Решение"),
                                                         row("result", "Результат", " cs__row--res"))
            lis.append(('<li class="cs__i" data-rv style="--dl:{dl}ms"><article class="cs__card{nf}" aria-labelledby="{sid}-{i}">{fig}'
                        '<header class="cs__head"><h3 class="cs__t" id="{sid}-{i}">{title}</h3>{tags}</header>'
                        '{dlist}{link}</article></li>').format(
                dl=(i % 2) * 90, i=i, nf="" if fig else " cs__card--nophoto", sid=b["_id"], fig=fig,
                title=self.md(it["title"], wi), tags=tags_html, dlist=dl, link=link))
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
            # v3: без номеров 01–03 — тонкая линия сверху, заголовок пункта и строка текста
            lis.append('<li class="pt__i" data-rv style="--dl:{}ms"><h3 class="pt__t">{}</h3>{}</li>'.format(
                i * 90, self.md(it["title"], wi), "<p>{}</p>".format(self.md(it.get("text", ""), wi)) if it.get("text") else ""))
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
            out.append('<details class="faq__i"{}><summary><h3 class="faq__q">{}</h3><span class="faq__ic" aria-hidden="true"></span></summary><div class="faq__a"><div class="faq__ai">{}</div></div></details>'.format(
                " open" if it.get("open") else "", self.md(it["q"], wi), self.paras(a, wi + ".a")))
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
        tag = '<p class="tag"><span>{}</span></p>'.format(self.t(b["eyebrow"])) if b.get("eyebrow") else ""
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
        """Финальная CTA-полоса: заголовок секции (как у всех, одного веса, не на всю ширину) + строка-лид, ниже на
        safelight-градиенте — плашка-ссылка с ником Telegram. Форм на сайте нет: все кнопки ведут в Telegram
        (config.json → contacts.telegram / telegram_handle). id блока — «zayavka». v3: без метки-«лампы» и правой
        мета-подписи над заголовком (поля eyebrow и meta допустимы, но не выводятся)."""
        w = b["_w"]
        c = self.cfg["contacts"]
        tg, handle = c["telegram"], c.get("telegram_handle", "Telegram")
        b.setdefault("title", "Обсудим съёмку")
        lead = self.md(b.get("lead", "Напишите в Telegram: дата, площадка, формат — остальное обсудим в переписке."), w + ".lead")
        head = ('<header class="sh sh--xl lf__head" data-rv><h2 class="sh__t sh__t--xl" id="h-{id}"><span class="mk"><span>{t}</span></span></h2>'
                '<p class="sh__note lf__lead">{lead}</p></header>').format(id=b["_id"], t=self.md(b["title"], w + ".title"), lead=lead)
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
            # v3: как список направлений — без номеров, слово приглушено, наведение/фокус — лёгкое увеличение и полная яркость
            lis.append('<li style="--i:{i}"><a href="{h}"{c}><span class="menu__w">{nm}</span></a></li>'.format(
                i=i, h=esc(self.slug_href(g["slug"])), c=cur, nm=self.t(g["name"])))
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
            "CollectionPage" if self.type in ("genre", "album") else "WebPage")
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
                if photo_key(p) not in seen:
                    seen.add(photo_key(p))
                    uniq.append(p)
            gal = {"@type": "ImageGallery", "@id": url + "#gallery", "name": self.gallery_name(),
                   "url": url, "creator": {"@id": person_id}, "isPartOf": {"@id": url + "#webpage"},
                   "associatedMedia": [self.image_obj(p) for p in uniq[:self.gallery_ld_max()]]}
            gal.update(self.gallery_ld_extra())
            graph.append(gal)
        if self.faq:
            graph.append({"@type": "FAQPage", "@id": url + "#faq", "mainEntity": [
                {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in self.faq]})
        data = {"@context": "https://schema.org", "@graph": graph}
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    def gallery_name(self):
        return "{} — кадры".format(self.nav_label())

    def gallery_ld_max(self):
        return 40

    def gallery_ld_extra(self):
        return {}

    @staticmethod
    def large_size(p):
        """Размер варианта large: у кадров из photos.json — 1920 по ширине, у локальных кадров альбома — фактический."""
        if p.get("_lw"):
            return p["_lw"], p["_lh"]
        return 1920, round(1920 * p["h"] / p["w"])

    def image_obj(self, p, iid=None):
        lw, lh = self.large_size(p)
        o = {"@type": "ImageObject", "contentUrl": self.aurl(p["large"]), "url": self.aurl(p["large"]), "width": lw,
             "height": lh, "caption": p.get("alt", ""), "creator": {"@id": self.site.su + "/#person"},
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
            ow, oh = self.large_size(og)
            og_img = ('<meta property="og:image" content="{u}"><meta property="og:image:width" content="{w}">'
                      '<meta property="og:image:height" content="{h}"><meta property="og:image:alt" content="{a}">'
                      '<meta name="twitter:image" content="{u}">').format(u=esc(self.aurl(og["large"])), w=ow, h=oh, a=esc(og.get("alt", "")))
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
        blocks = [b for b in self.d.get("blocks", []) if isinstance(b, dict) and (b.get("type") in BLOCK_SPECS or self.internal(b.get("type")))]
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
        self.rep.note("блоков: {}, кадров: {}, вопросов FAQ: {}".format(len(blocks), len({photo_key(p) for p in self.gallery_photos}), len(self.faq)))
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
# Страница альбома /<жанр>/<альбом>/ (бриф v3 §3) — собирается из content/albums.json, JSON-страницы у неё нет
# =============================================================================

def album_title(site, al):
    """<title> альбома: «Иван и Анна — свадьба в Новосибирске — Александр Непомнящих» (≤ TITLE_MAX знаков).
    Если жанр уже есть в названии («Выпускной 11 класса», «Портрет у окна») — «<Название>, Новосибирск — …».
    Длинное — короткий хвост бренда, затем без жанра, затем название сокращается."""
    t = re.sub(r"\s+", " ", al["title"]).strip()
    cfg = site.cfg
    single = (al["g"].get("single") or al["g"]["name"]).lower()
    stem = single.split()[0][:6]
    heads = []
    if stem not in t.lower():
        heads.append("{} — {} в {}".format(t, single, cfg.get("city_in", cfg["city"])))
    heads += ["{}, {}".format(t, cfg["city"]), t]
    for h in heads:
        for suf in (TITLE_SUFFIX, TITLE_SUFFIX_SHORT):
            if len(h + suf) <= TITLE_MAX:
                return h + suf
    return t[:TITLE_MAX - len(TITLE_SUFFIX_SHORT) - 1].rstrip() + "…" + TITLE_SUFFIX_SHORT


def album_description(site, al, service=""):
    """description альбома (110–170 знаков), если в albums.json не задан свой: название + подзаголовок, услуга жанра
    в городе и число кадров, имя фотографа, Telegram — сколько поместится."""
    if al.get("description"):
        return al["description"].strip()
    cfg = site.cfg
    n = len(al["frames"])
    sub = re.sub(r"\s*·\s*", ", ", al.get("subtitle") or "").strip(" ,.")
    head = "{}. {}.".format(al["title"].rstrip("."), sub[0].upper() + sub[1:]) if sub else al["title"].rstrip(".") + "."
    svc = service or "Фотосъёмка: {}".format((al["g"].get("single") or al["g"]["name"]).lower())
    what = "{} в {}".format(svc, cfg.get("city_in", cfg["city"]))
    if al.get("year"):
        what += ", {}".format(al["year"])
    if n >= ALBUM_MIN_COUNT_SHOWN:
        what += " — {} {}".format(n, plural(n, "кадр", "кадра", "кадров"))
    parts = [head, what + ".", "Фотограф {}.".format(cfg["person"]["name"]), "Обсудить съёмку — в Telegram."]
    out = parts[0]
    for p in parts[1:]:
        if len(out + " " + p) <= 170:
            out += " " + p
    if len(out) > 170:
        out = out[:168].rsplit(" ", 1)[0].rstrip(",.:;—- ") + "…"
    return out


class AlbumCtx(PageCtx):
    """Страница альбома: крошки «Главная / Жанр / Альбом», заголовок, мета, подзаголовок и текст (если есть),
    галерея всех кадров с лайтбоксом (один кадр — крупно, целиком), «← Все съёмки жанра» и «Следующий альбом →»
    (по кругу внутри жанра), CTA-полоса Telegram (lead-form с заголовком и строкой со страницы жанра).
    SEO: свой title и description, canonical, JSON-LD CollectionPage + ImageGallery (все кадры) + BreadcrumbList, sitemap."""

    def __init__(self, site, al, rep):
        self.album = al
        g = al["g"]
        gp = site.pages.get(g["slug"], {}).get("data", {})
        lf = next((b for b in gp.get("blocks", []) if isinstance(b, dict) and b.get("type") == "lead-form"), {})
        lead_form = {k: v for k, v in lf.items() if k in ("title", "lead", "button", "contacts", "note")}
        lead_form["type"] = "lead-form"
        data = {"slug": al["slug"], "type": "album", "genre": al["genre"], "nav": al["title"], "parent": g["slug"],
                "title": album_title(site, al), "description": album_description(site, al, gp.get("service", "")),
                "h1": al["title"], "schema": ["ImageGallery"],
                "blocks": [{"type": "album-hero"}, {"type": "album-gallery"}, {"type": "album-nav"}, lead_form]}
        super().__init__(site, "album", data, rep)
        self.genre_page = gp

    def internal(self, t):
        return t in INTERNAL_BLOCKS

    def validate_page(self):
        # альбом уже проверен при чтении content/albums.json (Site.load_albums); здесь — только SEO-длины
        ft, desc = self.full_title(), self.d["description"]
        if len(ft) > TITLE_MAX:
            self.rep.warn("title", "«{}» — {} знаков, держите до {}".format(ft, len(ft), TITLE_MAX))
        if not 90 <= len(desc) <= 170:
            self.rep.warn("description", "длина {} знаков — держите 110–170 (поле description в albums.json)".format(len(desc)))
        if self.album["genre"] and self.album["g"]["slug"] not in self.site.pages:
            self.rep.warn("", "страницы жанра {} нет — крошка и «Все съёмки» ведут в пустоту".format(self.album["g"]["slug"]))

    def full_title(self):
        return self.d["title"]

    def lb_cap(self, p):
        return self.album["title"]

    def og_photo(self):
        return self.album["cover"]["_p"]

    def gallery_name(self):
        return self.album["title"]

    def gallery_ld_max(self):
        return ALBUM_LD_MAX

    def gallery_ld_extra(self):
        o = {"description": self.d["description"], "genre": self.album["g"]["name"]}
        if self.album.get("year"):
            o["dateCreated"] = str(self.album["year"])
        o["primaryImageOfPage"] = {"@id": self.url() + "#primaryimage"}
        return o

    def albums_anchor(self):
        """id блока albums на странице жанра — «Все съёмки» ведут прямо к сетке."""
        for b in self.genre_page.get("blocks", []):
            if isinstance(b, dict) and b.get("type") == "albums":
                return b.get("id") if isinstance(b.get("id"), str) else DEFAULT_IDS["albums"]
        return ""

    # ------------------------------------------------------------------ первый экран альбома
    def b_album_hero(self, b):
        """v3 «аккуратнее»: одна колонка слева — мета «Портрет · 8 кадров» над названием, название, подзаголовок строкой
        под ним и текст (если есть). Правый угол пустой: крошки сверху, без разрозненных точек текста по краям."""
        al = self.album
        meta = " · ".join(self.t(x) for x in self.album_meta(al))
        meta_html = '<p class="ah__meta mono">{}</p>'.format(meta) if meta else ""
        side = []
        if al.get("subtitle"):
            side.append('<p class="ah__lead">{}</p>'.format(self.t(al["subtitle"])))
        if al.get("text"):
            side.append('<div class="ah__txt">{}</div>'.format(self.paras(al["text"], "albums.{}.text".format(al["key"]))))
        side_html = '<div class="ah__side">{}</div>'.format("".join(side)) if side else ""
        return ('<section class="ah" id="{id}" aria-labelledby="h1"><div class="wrap">'
                '<div class="gh__top">{crumbs}</div>'
                '<div class="ah__row">{meta}<h1 class="ah__h1" id="h1">{h1}</h1>{side}</div>'
                '</div></section>').format(
            id=b["_id"], crumbs=self.crumbs(), meta=meta_html,
            h1=self.md(al["title"], "albums.{}.title".format(al["key"]), nw=True), side=side_html)

    # ------------------------------------------------------------------ все кадры альбома
    def b_album_gallery(self, b):
        al = self.album
        items = [dict(f) for f in al["frames"]]
        n = len(items)
        # один кадр — крупно и целиком (без пустой сетки), 2+ — бенто; первые кадры грузятся сразу: они под заголовком
        body = self.gallery_body(items, "album", "solo" if n == 1 else "bento", eager=3 if n > 1 else 1)
        h2 = '<h2 class="vh" id="h-{}">Кадры: {}</h2>'.format(b["_id"], self.t(al["title"]))
        return ('<section class="sec b-gallery b-album-gallery b-gallery--{lay}" id="{id}" aria-labelledby="h-{id}">'
                '<div class="wrap">{h2}{body}</div></section>').format(
            lay="solo" if n == 1 else "bento", id=b["_id"], h2=h2, body=body)

    # ------------------------------------------------------------------ «← Все съёмки жанра» и «Следующий альбом →»
    def b_album_nav(self, b):
        al = self.album
        g = al["g"]
        same = self.site.genre_albums(al["genre"])
        k = next((i for i, x in enumerate(same) if x is al), 0)
        nxt = same[(k + 1) % len(same)] if len(same) > 1 else None
        back = ('<a class="an__a an__back" href="{h}"><span class="an__arr" aria-hidden="true">←</span>'
                '<span class="an__txt"><span class="an__cap mono">Все съёмки жанра</span><span class="an__t">{t}</span></span></a>').format(
            h=esc(self.slug_href(g["slug"], self.albums_anchor())), t=self.t(g["name"]))
        nx = ""
        if nxt:
            c = nxt["cover"]["_p"]
            th = '<span class="an__th" style="{}">{}</span>'.format(
                esc(self.focus_vars(nxt["cover"].get("focus") or c.get("focus"))),
                self.img(c, "(min-width: 700px) 120px, 72px", alt=""))
            nx = ('<a class="an__a an__next" href="{h}"><span class="an__txt"><span class="an__cap mono">Следующий альбом</span>'
                  '<span class="an__t">{t}</span></span>{th}<span class="an__arr" aria-hidden="true">→</span></a>').format(
                h=esc(self.slug_href(nxt["slug"])), t=self.t(nxt["title"]), th=th)
        return ('<nav class="an" id="{id}" aria-label="Другие съёмки"><div class="wrap"><div class="an__in{one}">{back}{nx}</div></div></nav>').format(
            id=b["_id"], one="" if nx else " an__in--one", back=back, nx=nx)


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


def build_albums(site, genre=None):
    """Страницы альбомов (все или одного жанра): [(slug, html, отчёт, ctx)]. Альбомы с ошибками в albums.json сюда
    не попадают — их ошибки в отчёте site.album_rep."""
    out = []
    for al in site.albums:
        if genre and al["genre"] != genre:
            continue
        rep = Report("{} → {}".format(site.album_rep.name, al["slug"]))
        ctx = AlbumCtx(site, al, rep)
        try:
            html_out = ctx.render()
        except Exception as e:  # ошибка одной страницы альбома не роняет сборку
            rep.err("", "не удалось собрать страницу альбома: {}: {}".format(type(e).__name__, e))
            html_out = ""
        out.append((al["slug"], html_out, rep, ctx))
    return out


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
    if path.name == ALBUMS_FILE.name:
        return cmd_check_albums(site, path)
    try:
        data = load_json(path)
    except ValueError as e:
        rep.err("", str(e))
        rep.print()
        return 1
    if isinstance(data, dict) and "albums" in data and "blocks" not in data:   # копия albums.json под другим именем
        return cmd_check_albums(site, path)
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


def cmd_check_albums(site, path):
    """--check content/albums.json: жанры, slug, кадры (несуществующий id, чужой жанр, дубли, пустой альбом, нет файла),
    обложки — и сборка каждой страницы альбома в памяти (ничего не записывается)."""
    site.load_albums(path)
    rep = site.album_rep
    rep.print()
    errs, warns = len(rep.errors), len(rep.warnings)
    for slug, _html, r, _ctx in build_albums(site):
        if r.errors or r.warnings:
            r.print(verbose=False)
        else:
            print("OK   {}  «{}»".format(slug, _ctx.full_title()))
        errs += len(r.errors)
        warns += len(r.warnings)
    print("Итог: {} ошибок, {} предупреждений, страниц альбомов: {}. Ничего не записано.".format(errs, warns, len(site.albums)))
    return 1 if errs else 0


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
    # жанровая страница: вместе с ней — страницы её альбомов (сетка на жанре ссылается на них)
    albums = []
    if info["data"].get("type") == "genre" and ctx.genre:
        if site.album_rep.errors:
            site.album_rep.print(verbose=False)
        for aslug, ahtml, arep, _actx in build_albums(site, ctx.genre["key"]):
            if arep.errors:
                arep.print(verbose=False)
                continue
            albums.append((aslug, ahtml))
    OUT.mkdir(exist_ok=True)
    copy_assets()
    write(out_file(slug), html_out)
    for aslug, ahtml in albums:
        write(out_file(aslug), ahtml)
    print("Записано: site/{}{}  (ассеты обновлены, остальные страницы не тронуты)".format(
        out_file(slug).relative_to(OUT), " + страниц альбомов: {}".format(len(albums)) if albums else ""))
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
    # альбомы: отчёт по content/albums.json и страницы /<жанр>/<альбом>/ (в sitemap — сразу после своего жанра)
    reports.append(site.album_rep)
    total_err += len(site.album_rep.errors)
    album_slugs = {}
    seen_t, seen_d = {}, {}
    for aslug, ahtml, arep, actx in build_albums(site):
        reports.append(arep)
        if arep.errors:
            total_err += len(arep.errors)
            continue
        outputs[out_file(aslug)] = ahtml
        album_slugs.setdefault(actx.album["g"]["slug"], []).append(aslug)
        seen_t.setdefault(actx.full_title(), []).append(aslug)
        seen_d.setdefault(actx.d["description"], []).append(aslug)
    sitemap = []
    for s in ok_slugs:
        sitemap.append(s)
        sitemap.extend(album_slugs.pop(s, []))
    for rest in album_slugs.values():
        sitemap.extend(rest)
    # дубли title/description
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
    write(OUT / "sitemap.xml", render_sitemap(site, sitemap))
    write(OUT / "robots.txt", render_robots(site))
    missing_links = check_site_links()
    pending = sorted(k for k in missing_links if k.endswith("/index.html") and "/" + k[:-len("index.html")] in site.registry)
    broken = sorted(k for k in missing_links if k not in pending)
    n_alb = sum(1 for p in outputs if len(p.relative_to(OUT).parts) > 2)
    print("\nСобрано страниц: {} из {} в карте сайта + страниц альбомов: {}. Ошибок: {}.".format(
        len(outputs) - n_alb, len(site.registry), n_alb, total_err))
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
