#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Локальные AVIF и WebP для LCP-кадров: портрет Александра на главной (id 93) и обложки жанров.
Остальные кадры можно пока тянуть с CDN wfolio: они грузятся лениво и на скорость первого экрана не влияют.

    python3 tools/photos.py                 портрет, обложки жанров, обложки SEO-страниц (og_image) из content/photos-src/
    python3 tools/photos.py 93 37           только эти id
    python3 tools/photos.py --from-cdn      если оригинала нет — скачать вариант large с CDN (хуже оригинала, см. README)
    python3 tools/photos.py --force         пересобрать, даже если файлы уже есть

Откуда берётся исходник: content/photos-src/<id>.<jpg|jpeg|png|tif|webp>, например 093.jpg или 93.jpg
(лучше всего — оригиналы у Александра). Результат: src/assets/img/photos/<id:03d>-<ширина>.avif и .webp
для ширин 640 / 1280 / 1920 (не больше ширины исходника). После этого `python3 build.py` сам поставит
<picture> с AVIF → WebP → JPEG и preload на AVIF для первого экрана. Удалите файлы — вернётся JPEG с CDN.

Кодирование: Pillow (pip install pillow; AVIF — Pillow 11.2+ или pillow-avif-plugin). Если Pillow нет,
на macOS AVIF делает системная утилита sips (WebP тогда не создаётся — это не страшно, <picture> обойдётся AVIF + JPEG).
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "content" / "photos.json"
PAGES = ROOT / "content" / "pages"
SRC_DIR = ROOT / "content" / "photos-src"
OUT_DIR = ROOT / "src" / "assets" / "img" / "photos"
WIDTHS = (640, 1280, 1920)
EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic")

try:
    from PIL import Image, ImageOps, features
    HAS_PIL = True
    PIL_AVIF = bool(features.check("avif"))
    PIL_WEBP = bool(features.check("webp"))
except Exception:  # Pillow не установлен
    HAS_PIL = PIL_AVIF = PIL_WEBP = False


def default_ids(doc):
    """LCP-кадры: портрет автора, обложки жанров, обложки (og_image / фото героя) написанных страниц."""
    ids = []
    if isinstance(doc.get("author"), int):
        ids.append(doc["author"])
    ids += [v for v in doc.get("covers", {}).values() if isinstance(v, int)]
    for f in sorted(PAGES.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for b in d.get("blocks", []):
            if isinstance(b, dict) and b.get("type") in ("hero-person", "genre-hero") and isinstance(b.get("photo"), int):
                ids.append(b["photo"])
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def find_source(pid):
    for stem in ("{:03d}".format(pid), str(pid)):
        for ext in EXTS:
            for e in (ext, ext.upper()):
                f = SRC_DIR / (stem + e)
                if f.is_file():
                    return f
    return None


def fetch(url, dst):
    req = urllib.request.Request(url, headers={"User-Agent": "ANPhotoLab-build/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as fh:
        shutil.copyfileobj(r, fh)


def encode_pil(src, pid, widths, force):
    made = []
    with Image.open(src) as im0:
        im0 = ImageOps.exif_transpose(im0).convert("RGB")
        for w in widths:
            h = round(im0.height * w / im0.width)
            im = im0 if w == im0.width else im0.resize((w, h), Image.LANCZOS)
            if PIL_AVIF:
                dst = OUT_DIR / "{:03d}-{}.avif".format(pid, w)
                if force or not dst.exists():
                    im.save(dst, "AVIF", quality=58, speed=4)
                    made.append(dst)
            if PIL_WEBP:
                dst = OUT_DIR / "{:03d}-{}.webp".format(pid, w)
                if force or not dst.exists():
                    im.save(dst, "WEBP", quality=80, method=6)
                    made.append(dst)
    return made


def encode_sips(src, pid, widths, force):
    made = []
    for w in widths:
        dst = OUT_DIR / "{:03d}-{}.avif".format(pid, w)
        if dst.exists() and not force:
            continue
        subprocess.run(["sips", "-s", "format", "avif", "-s", "formatOptions", "60", "--resampleWidth", str(w),
                        str(src), "--out", str(dst)], check=True, capture_output=True)
        made.append(dst)
    return made


def source_width(src):
    if HAS_PIL:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            return im.width
    out = subprocess.run(["sips", "-g", "pixelWidth", str(src)], capture_output=True, text=True).stdout
    return int(out.strip().split()[-1])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Локальные AVIF/WebP для первых экранов (см. README → «Фото»)")
    ap.add_argument("ids", nargs="*", type=int, help="id кадров (по умолчанию — портрет и обложки)")
    ap.add_argument("--from-cdn", action="store_true", help="нет оригинала — скачать large с CDN wfolio")
    ap.add_argument("--force", action="store_true", help="перезаписать готовые файлы")
    args = ap.parse_args(argv)

    if not HAS_PIL and not shutil.which("sips"):
        print("Нужен Pillow (pip install pillow) или macOS-утилита sips.")
        return 1
    doc = json.loads(MANIFEST.read_text(encoding="utf-8"))
    photos = {p["id"]: p for p in doc["photos"]}
    ids = args.ids or default_ids(doc)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="anp-photos-"))
    missing, total = [], 0
    try:
        for pid in ids:
            p = photos.get(pid)
            if not p:
                print("  нет кадра {} в content/photos.json — пропущен".format(pid))
                continue
            src = find_source(pid)
            if not src and args.from_cdn:
                src = tmp / "{:03d}.jpg".format(pid)
                print("  {:03d}: скачиваю large с CDN…".format(pid))
                fetch(p["large"], src)
            if not src:
                missing.append(pid)
                continue
            sw = source_width(src)
            widths = [w for w in WIDTHS if w <= sw] or [sw]
            if sw < WIDTHS[-1] and sw not in widths:
                widths.append(sw)
            made = encode_pil(src, pid, widths, args.force) if (HAS_PIL and (PIL_AVIF or PIL_WEBP)) else encode_sips(src, pid, widths, args.force)
            total += len(made)
            print("  {:03d}: {} → {}".format(pid, src.name, ", ".join(f.name for f in made) or "уже готово"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if missing:
        print("\nНет исходников для: {}. Положите оригиналы в content/photos-src/ (например, {:03d}.jpg) "
              "или запустите с --from-cdn.".format(", ".join(map(str, missing)), missing[0]))
    print("Готово: {} файлов в src/assets/img/photos/. Теперь: python3 build.py".format(total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
