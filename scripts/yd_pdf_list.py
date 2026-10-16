#!/usr/bin/env python3
"""Выписывает названия и ссылки на все PDF из публичной папки Яндекс Диска (с подпапками).

Запуск:
  python3 scripts/yd_pdf_list.py                    # вывод в консоль + файл yd_pdfs.csv в корне проекта
  python3 scripts/yd_pdf_list.py <ссылка на папку>  # другая папка
  python3 scripts/yd_pdf_list.py --csv путь.csv     # сохранить в другое место
  python3 scripts/yd_pdf_list.py --direct           # прямые ссылки на скачивание (временные, ~30 мин)
  python3 scripts/yd_pdf_list.py --save-covers      # ещё и сохранить картинки обложек в yd_covers/
  python3 scripts/yd_pdf_list.py --force-covers     # (с --save-covers) перекачать уже сохранённые

В списке для каждого PDF: папка, название, ссылка на сам PDF и ссылка на обложку (превью первой
страницы с Диска). ВНИМАНИЕ: ссылки на обложки у Яндекса временные — живут около 4 часов, потом
их нужно получить заново (запустить скрипт ещё раз). Для постоянного доступа используйте --save-covers.

Файл yd_pdfs.csv и папка yd_covers лежат в корне проекта только локально (они в .gitignore).

Ссылка по умолчанию — вида https://disk.yandex.ru/d/<ключ>/<папка>/<файл>.pdf — постоянная,
открывает файл в просмотрщике Диска. Только стандартная библиотека.
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import tempfile
import sys
import time
import urllib.parse
import urllib.request

API = "https://cloud-api.yandex.net/v1/disk/public/resources"
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
COVERS = os.path.join(ROOT, "yd_covers")
DEFAULT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "yd_pdfs.csv")
DEFAULT_KEY = "https://disk.360.yandex.ru/d/xxunmuNTZX-2Dg"
PAGE = 100


def get(url: str) -> dict:
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return json.load(resp)
        except Exception as exc:  # 500/лимиты Диска — повторяем
            if attempt == 4:
                raise
            print(f"  повтор ({exc})", file=sys.stderr)
            time.sleep(3 * (attempt + 1))


def save_cover(url: str, dest: str) -> bool:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                data = resp.read()
            with open(dest, "wb") as f:
                f.write(data)
            return True
        except Exception as exc:
            if attempt == 2:
                print(f"  обложка не скачалась ({dest}): {exc}", file=sys.stderr)
                return False
            time.sleep(2 * (attempt + 1))


def cover_from_pdf(key: str, item: dict, dest: str) -> bool:
    """Запасной путь, если превью Диска отдаёт 500: качаем PDF и рендерим 1-ю страницу (нужен pdftoppm)."""
    if not shutil.which("pdftoppm"):
        return False
    try:
        href = item.get("file") or get(
            f"{API}/download?" + urllib.parse.urlencode({"public_key": key, "path": item["path"]})
        )["href"]
        with tempfile.TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "in.pdf")
            with urllib.request.urlopen(href, timeout=120) as resp, open(pdf, "wb") as f:
                shutil.copyfileobj(resp, f)
            base = os.path.join(tmp, "page")
            subprocess.run(["pdftoppm", "-jpeg", "-f", "1", "-l", "1", "-scale-to-x", "800", "-scale-to-y", "-1",
                            pdf, base], check=True, capture_output=True)
            out = next(os.path.join(tmp, n) for n in os.listdir(tmp) if n.startswith("page"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(out, dest)
        return True
    except Exception as exc:
        print(f"  обложка из PDF не получилась ({item['name']}): {exc}", file=sys.stderr)
        return False


def walk(key: str, path: str = "/"):
    """Отдаёт все файлы папки рекурсивно."""
    offset = 0
    while True:
        q = urllib.parse.urlencode({"public_key": key, "path": path, "limit": PAGE, "offset": offset,
                                "preview_size": "XL", "preview_crop": "false"})
        data = get(f"{API}?{q}")
        items = data.get("_embedded", {}).get("items", [])
        for it in items:
            if it["type"] == "dir":
                yield from walk(key, it["path"])
            else:
                yield it
        if len(items) < PAGE:
            return
        offset += PAGE


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("key", nargs="?", default=DEFAULT_KEY, help="публичная ссылка на папку")
    ap.add_argument("--csv", default=DEFAULT_CSV, help="куда сохранить CSV (по умолчанию yd_pdfs.csv в корне проекта)")
    ap.add_argument("--save-covers", action="store_true", help="сохранить картинки обложек в yd_covers/")
    ap.add_argument("--force-covers", action="store_true", help="перекачать уже сохранённые обложки")
    ap.add_argument("--direct", action="store_true", help="прямые ссылки на скачивание")
    args = ap.parse_args()

    root = get(f"{API}?" + urllib.parse.urlencode({"public_key": args.key, "limit": 0}))
    base = root.get("public_url") or args.key
    rows = []
    for it in walk(args.key):
        name = it["name"]
        if not name.lower().endswith(".pdf"):
            continue
        if args.direct:
            link = it.get("file") or get(
                f"{API}/download?" + urllib.parse.urlencode({"public_key": args.key, "path": it["path"]})
            )["href"]
        else:
            link = base.rstrip("/") + urllib.parse.quote(it["path"])
        folder = it["path"].strip("/").split("/")[0] if it["path"].count("/") > 1 else "(корень)"
        cover_url = it.get("preview") or ""
        cover = ""
        if args.save_covers:
            rel = os.path.join("yd_covers", folder, os.path.splitext(name)[0] + ".jpg")
            dest = os.path.join(ROOT, rel)
            if (os.path.exists(dest) and not args.force_covers) \
                    or (it.get("preview") and save_cover(it["preview"], dest)) \
                    or cover_from_pdf(args.key, it, dest):
                cover = rel
        rows.append((folder, name, link, cover_url, cover))

    rows.sort(key=lambda r: (r[0].lower(), r[1].lower()))
    last = None
    for folder, name, link, cover_url, _ in rows:
        if folder != last:
            count = sum(1 for r in rows if r[0] == folder)
            print(f"\n=== {folder} ({count}) ===")
            last = folder
        print(f"{name}\t{link}\t{cover_url}")
    got = sum(1 for r in rows if r[3])
    print(f"\nВсего PDF: {len(rows)}, ссылок на обложки: {got}", file=sys.stderr)

    out = os.path.abspath(args.csv)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        head = ["Папка", "Название", "Ссылка на PDF", "Ссылка на обложку"]
        if args.save_covers:
            head.append("Файл обложки")
        w.writerow(head)
        w.writerows(r if args.save_covers else r[:4] for r in rows)
    print(f"Сохранено: {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
