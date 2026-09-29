#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Добавить реальный альбом съёмки из папки с фото (бриф v3 §3: «Иван и Анна», «Алёна и Дима» …).

    python3 tools/add_album.py --genre svadby --title "Иван и Анна" --folder ~/Desktop/ivan-anna
    python3 tools/add_album.py --genre svadby --title "Иван и Анна" --folder ~/Desktop/ivan-anna \
        --subtitle "Загородный клуб · сборы, регистрация, прогулка" --year 2025 --cover 3 --drop-demo

    python3 tools/add_album.py --genre svadby --remove ivan-i-anna      убрать альбом и его файлы

Что делает:
  1. Берёт из папки все JPG/JPEG/PNG (по имени файла, «IMG_2» раньше «IMG_10»).
  2. Кладёт их в src/assets/img/albums/<жанр>/<slug>/: 01.jpg, 02.jpg … — уменьшенные до 2400 px по длинной стороне
     (меньшие не увеличиваются), и превью 01-800.jpg … — 800 px по длинной стороне. Всё в JPEG, метаданные (GPS, камера)
     не копируются, поворот по EXIF учитывается (с Pillow; sips его не применяет). Оригиналы в папке не трогаются.
  3. Дописывает альбом в content/albums.json (demo: false): жанр, slug (транслит названия), название, подзаголовок,
     год, обложка (--cover N — N-й кадр по порядку, по умолчанию первый), кадры {"src", "thumb", "w", "h", "alt"}.
     Новый альбом встаёт первым среди альбомов жанра (после других реальных) — перед демо-альбомами.
  4. Печатает, что дальше: python3 build.py.

--drop-demo    убрать из content/albums.json демо-альбомы этого жанра (demo: true) — когда появились реальные.
--replace      заменить альбом с тем же slug (файлы и запись).
--alt "…"      основа подписи alt для кадров («Иван и Анна — свадьба»), к ней добавится «, кадр N».
--slug …       свой slug вместо транслита.

Обработка фото: Pillow (pip install pillow), если установлен; иначе системная утилита macOS sips.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALBUMS = ROOT / "content" / "albums.json"
CONFIG = ROOT / "src" / "config.json"
ALBUM_DIR = "assets/img/albums"
OUT_BASE = ROOT / "src" / ALBUM_DIR
MAX_SIDE = 2400
THUMB_SIDE = 800
QUALITY = 86
THUMB_QUALITY = 80
EXTS = (".jpg", ".jpeg", ".png")

try:
    from PIL import Image, ImageOps
    HAS_PIL = True
except Exception:  # Pillow не установлен
    HAS_PIL = False

TRANSLIT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i", "й": "y",
            "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
            "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}


def slugify(title):
    """«Иван и Анна» → ivan-i-anna, «Алёна и Дима» → alyona-i-dima."""
    s = "".join(TRANSLIT.get(c, c) for c in title.lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return re.sub(r"-{2,}", "-", s) or "album"


def natural_key(p):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)]


def die(msg):
    print("ОШИБКА  " + msg)
    sys.exit(1)


# ----------------------------------------------------------------------------- albums.json: чтение и запись в том же виде
def load_albums():
    if not ALBUMS.exists():
        return {"_comment": "Альбомы съёмок по жанрам (см. content/BLOCKS.md → «Альбомы»).", "albums": []}
    try:
        doc = json.loads(ALBUMS.read_text(encoding="utf-8"))
    except ValueError as e:
        die("content/albums.json не читается: {}".format(e))
    if not isinstance(doc, dict) or not isinstance(doc.get("albums"), list):
        die('content/albums.json — объект {"albums": [...]}')
    return doc


def dump_albums(doc):
    """Как в файле: альбом из id — одной строкой; альбом с локальными кадрами — поля строкой, кадры по одному в строке."""
    lines = ["{"]
    keys = [k for k in doc if k != "albums"]
    for k in keys:
        lines.append("  {}: {},".format(json.dumps(k), json.dumps(doc[k], ensure_ascii=False)))
    lines.append('  "albums": [')
    out = []
    for a in doc["albums"]:
        photos = a.get("photos") if isinstance(a, dict) else None
        if isinstance(photos, list) and any(isinstance(x, dict) for x in photos):
            head = {k: v for k, v in a.items() if k != "photos"}
            body = json.dumps(head, ensure_ascii=False)[:-1]
            rows = ",\n".join("      " + json.dumps(x, ensure_ascii=False) for x in photos)
            out.append("    " + body + ', "photos": [\n' + rows + "\n    ]}")
        else:
            out.append("    " + json.dumps(a, ensure_ascii=False))
    lines.append(",\n".join(out))
    lines.append("  ]")
    lines.append("}")
    text = "\n".join(lines) + "\n"
    json.loads(text)  # страховка: пишем только валидный JSON
    return text


# ----------------------------------------------------------------------------- обработка фото
def process_pil(src, dst, side, quality):
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        icc = im.info.get("icc_profile")
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (11, 10, 9))       # прозрачность PNG — на фон сайта
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        if max(im.size) > side:
            im.thumbnail((side, side), Image.LANCZOS)
        kw = {"quality": quality, "optimize": True, "progressive": True}
        if icc:
            kw["icc_profile"] = icc
        im.save(dst, "JPEG", **kw)
        return im.size


def sips_size(path):
    out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)], capture_output=True, text=True, check=True).stdout
    w = int(re.search(r"pixelWidth:\s*(\d+)", out).group(1))
    h = int(re.search(r"pixelHeight:\s*(\d+)", out).group(1))
    return w, h


def process_sips(src, dst, side, quality):
    w, h = sips_size(src)
    cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(quality)]
    if max(w, h) > side:
        cmd += ["-Z", str(side)]          # по длинной стороне, пропорции сохраняются; меньшие кадры не увеличиваем
    subprocess.run(cmd + [str(src), "--out", str(dst)], capture_output=True, text=True, check=True)
    return sips_size(dst)


def pick_engine(name):
    if name == "pil" or (name == "auto" and HAS_PIL):
        if not HAS_PIL:
            die("Pillow не установлен: pip install pillow (или --engine sips на macOS)")
        return "pil", process_pil
    if shutil.which("sips"):
        return "sips", process_sips
    die("нет ни Pillow (pip install pillow), ни sips (macOS) — фото нечем уменьшить")


# ----------------------------------------------------------------------------- команды
def genres():
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    return {g["key"]: g for g in cfg["genres"]}


def safe_album_dir(genre, slug):
    d = (OUT_BASE / genre / slug).resolve()
    if not str(d).startswith(str(OUT_BASE.resolve()) + "/"):
        die("странный путь альбома: {}".format(d))
    return d


def cmd_remove(args, g):
    doc = load_albums()
    before = len(doc["albums"])
    doc["albums"] = [a for a in doc["albums"] if not (a.get("genre") == args.genre and a.get("slug") == args.remove)]
    d = safe_album_dir(args.genre, args.remove)
    if before == len(doc["albums"]) and not d.exists():
        die("альбома {}/{} нет ни в content/albums.json, ни в src/{}/".format(args.genre, args.remove, ALBUM_DIR))
    ALBUMS.write_text(dump_albums(doc), encoding="utf-8")
    if d.exists():
        shutil.rmtree(d)
    for parent in (d.parent, OUT_BASE):      # пустые папки жанра и albums/ не оставляем
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    print("Убрано: {}/{} ({} из content/albums.json, папка src/{}/{}/{}/)".format(
        args.genre, args.remove, "запись удалена" if before != len(doc["albums"]) else "записи не было", ALBUM_DIR, args.genre, args.remove))
    print("Дальше: python3 build.py")
    return 0


def cmd_add(args, g):
    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        die("папки {} нет".format(folder))
    files = sorted([p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in EXTS and not p.name.startswith(".")], key=natural_key)
    if not files:
        die("в папке {} нет JPG/PNG".format(folder))
    title = re.sub(r"\s+", " ", args.title).strip()
    if not title:
        die("пустое название альбома")
    slug = args.slug or slugify(title)
    if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", slug):
        die("slug — латиница, цифры и дефис («ivan-i-anna»), сейчас «{}»".format(slug))
    if args.cover is not None and not 1 <= args.cover <= len(files):
        die("--cover {}: в папке {} кадров — укажите номер от 1 до {}".format(args.cover, len(files), len(files)))
    if args.year is not None and not 1990 <= args.year <= 2100:
        die("--year — год, например 2025")
    doc = load_albums()
    exists = [a for a in doc["albums"] if a.get("genre") == args.genre and a.get("slug") == slug]
    out_dir = safe_album_dir(args.genre, slug)
    if (exists or (out_dir.exists() and any(out_dir.iterdir()))) and not args.replace:
        die("альбом {}/{} уже есть — другое название, --slug или --replace".format(args.genre, slug))
    engine, process = pick_engine(args.engine)

    # фото — сначала во временную папку: если что-то упадёт на середине, старый альбом и albums.json не тронуты
    tmp = Path(tempfile.mkdtemp(prefix="album-"))
    frames = []
    single = (g.get("single") or g["name"]).lower()
    base_alt = args.alt or "{} — {}".format(title, single)
    try:
        for i, src in enumerate(files, 1):
            name = "{:02d}".format(i) if len(files) < 100 else "{:03d}".format(i)
            w, h = process(src, tmp / (name + ".jpg"), MAX_SIDE, QUALITY)
            process(tmp / (name + ".jpg"), tmp / (name + "-800.jpg"), THUMB_SIDE, THUMB_QUALITY)
            rel = "{}/{}/{}/{}".format(ALBUM_DIR, args.genre, slug, name)
            frames.append({"src": rel + ".jpg", "thumb": rel + "-800.jpg", "w": w, "h": h, "alt": "{}, кадр {}".format(base_alt, i)})
            print("  {:>3}. {}  →  {}.jpg  {}×{}".format(i, src.name, name, w, h))
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(out_dir))
        out_dir.chmod(0o755)   # mkdtemp создаёт папку 0700 — на хостинге веб-сервер её бы не прочитал
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

    album = {"genre": args.genre, "slug": slug, "title": title, "subtitle": args.subtitle or "", "year": args.year,
             "cover": frames[(args.cover or 1) - 1]["src"], "text": args.text or "", "demo": False, "photos": frames}
    items = doc["albums"]
    if exists:
        k = items.index(exists[0])
        items[k] = album
    else:
        # после последнего реального альбома жанра, иначе — перед первым альбомом жанра, иначе — в конец
        same = [k for k, a in enumerate(items) if a.get("genre") == args.genre]
        real = [k for k in same if not items[k].get("demo")]
        k = real[-1] + 1 if real else (same[0] if same else len(items))
        items.insert(k, album)
    dropped = []
    if args.drop_demo:
        dropped = [a.get("slug") for a in items if a.get("genre") == args.genre and a.get("demo")]
        doc["albums"] = [a for a in items if not (a.get("genre") == args.genre and a.get("demo"))]
    ALBUMS.write_text(dump_albums(doc), encoding="utf-8")

    demo_left = [a.get("title") for a in doc["albums"] if a.get("genre") == args.genre and a.get("demo")]
    n = len(frames)
    word = "кадр" if n % 10 == 1 and n % 100 != 11 else ("кадра" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "кадров")
    print("\nГотово ({}): альбом «{}» — {} {} → src/{}/{}/{}/ и content/albums.json".format(engine, title, n, word, ALBUM_DIR, args.genre, slug))
    print("Адрес: {}{}/  (обложка — кадр {})".format(g["slug"], slug, args.cover or 1))
    if dropped:
        print("Убраны демо-альбомы жанра: " + ", ".join(dropped))
    elif demo_left:
        print("В жанре остались демо-альбомы: {} — уберите их (--drop-demo или вручную в content/albums.json), когда наберутся реальные.".format(
            ", ".join("«{}»".format(t) for t in demo_left)))
    print("\nДальше:")
    print("  1. Проверьте подписи alt в content/albums.json (что в кадре) и подзаголовок альбома.")
    print("  2. python3 build.py --check content/albums.json")
    print("  3. python3 build.py   → site{}{}/index.html".format(g["slug"], slug))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Добавить альбом съёмки из папки с фото (content/albums.json + src/assets/img/albums/)")
    ap.add_argument("--genre", required=True, help="ключ жанра: svadby, reportazh, portrety, avtomobili, vypusknye, kontserty, korporativy")
    ap.add_argument("--title", help="название альбома: «Иван и Анна»")
    ap.add_argument("--folder", help="папка с JPG/PNG")
    ap.add_argument("--subtitle", help="строка под названием: «Загородный клуб · сборы, регистрация, прогулка»")
    ap.add_argument("--year", type=int, help="год съёмки")
    ap.add_argument("--cover", type=int, help="номер кадра-обложки по порядку (с 1)")
    ap.add_argument("--text", help="1–2 строки об этой съёмке (необязательно)")
    ap.add_argument("--alt", help="основа alt для кадров")
    ap.add_argument("--slug", help="свой slug вместо транслита названия")
    ap.add_argument("--replace", action="store_true", help="заменить альбом с тем же slug")
    ap.add_argument("--drop-demo", action="store_true", help="убрать демо-альбомы этого жанра")
    ap.add_argument("--remove", metavar="SLUG", help="убрать альбом (запись и файлы)")
    ap.add_argument("--engine", choices=("auto", "pil", "sips"), default="auto", help="чем уменьшать фото (по умолчанию Pillow, иначе sips)")
    args = ap.parse_args(argv)
    gs = genres()
    if args.genre not in gs:
        die("нет жанра «{}» (есть: {})".format(args.genre, ", ".join(gs)))
    if args.remove:
        return cmd_remove(args, gs[args.genre])
    if not args.title or not args.folder:
        ap.error("нужны --title и --folder (или --remove SLUG)")
    return cmd_add(args, gs[args.genre])


if __name__ == "__main__":
    sys.exit(main())
