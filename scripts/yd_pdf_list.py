#!/usr/bin/env python3
"""Выписывает названия и ссылки на все PDF из публичной папки Яндекс Диска (с подпапками).

Запуск:
  python3 scripts/yd_pdf_list.py                    # вывод в консоль + файл yd_pdfs.csv в корне проекта
  python3 scripts/yd_pdf_list.py <ссылка на папку>  # другая папка
  python3 scripts/yd_pdf_list.py --csv путь.csv     # сохранить в другое место
  python3 scripts/yd_pdf_list.py --direct           # прямые ссылки на скачивание (временные, ~30 мин)

Файл yd_pdfs.csv лежит в корне проекта только локально (он в .gitignore).

Ссылка по умолчанию — вида https://disk.yandex.ru/d/<ключ>/<папка>/<файл>.pdf — постоянная,
открывает файл в просмотрщике Диска. Только стандартная библиотека.
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request

API = "https://cloud-api.yandex.net/v1/disk/public/resources"
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


def walk(key: str, path: str = "/"):
    """Отдаёт все файлы папки рекурсивно."""
    offset = 0
    while True:
        q = urllib.parse.urlencode({"public_key": key, "path": path, "limit": PAGE, "offset": offset})
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
        rows.append((folder, name, link))

    rows.sort(key=lambda r: (r[0].lower(), r[1].lower()))
    last = None
    for folder, name, link in rows:
        if folder != last:
            count = sum(1 for r in rows if r[0] == folder)
            print(f"\n=== {folder} ({count}) ===")
            last = folder
        print(f"{name}\t{link}")
    print(f"\nВсего PDF: {len(rows)}", file=sys.stderr)

    out = os.path.abspath(args.csv)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Папка", "Название", "Ссылка"])
        w.writerows(rows)
    print(f"Сохранено: {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
