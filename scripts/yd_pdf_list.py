#!/usr/bin/env python3
"""Яндекс Диск -> список ссылок на каждый PDF + обложки в одну папку. Всё локально, в гит не попадает.

Что делает:
  1. Обходит публичную папку Диска (TECMASTER, WAGNER и любые подпапки).
  2. Для каждого PDF берёт ссылку на файл внутри папки Диска:
     https://disk.yandex.ru/d/<ключ>/<папка>/<файл>.pdf
  3. Сохраняет обложку каждого PDF (первая страница) в yd_covers/<папка>/<файл>.jpg в корне проекта.
     Берёт превью Диска; если Диск не отдал (500) — качает PDF и рисует страницу через pdftoppm.
  4. Печатает список (по папкам) и пишет yd_pdfs.csv в корне: папка, название, ссылка на PDF,
     файл обложки.

Запуск:
  python3 scripts/yd_pdf_list.py                       # ссылка по умолчанию
  python3 scripts/yd_pdf_list.py <ссылка на папку>     # другая папка Диска
  python3 scripts/yd_pdf_list.py --direct              # + прямая ссылка на скачивание (временная, ~30 мин)
  python3 scripts/yd_pdf_list.py --force               # перекачать обложки заново

yd_pdfs.csv и yd_covers/ лежат только локально (они в .gitignore). Только стандартная библиотека.
"""
import argparse
import concurrent.futures
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

API = "https://cloud-api.yandex.net/v1/disk/public/resources"
DEFAULT_KEY = "https://disk.yandex.ru/d/xxunmuNTZX-2Dg"
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
COVERS = os.path.join(ROOT, "yd_covers")
DEFAULT_CSV = os.path.join(ROOT, "yd_pdfs.csv")
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
    """Отдаёт все файлы папки рекурсивно (с превью первой страницы)."""
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


def save_preview(url: str, dest: str) -> bool:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=45) as resp:
                data = resp.read()
            with open(dest, "wb") as f:
                f.write(data)
            return True
        except Exception:
            time.sleep(2 * (attempt + 1))
    return False


def cover_from_pdf(key: str, item: dict, dest: str) -> bool:
    """Запасной путь: качаем PDF и рендерим первую страницу (нужен pdftoppm)."""
    if not shutil.which("pdftoppm"):
        return False
    try:
        href = item.get("file") or get(
            f"{API}/download?" + urllib.parse.urlencode({"public_key": key, "path": item["path"]}))["href"]
        with tempfile.TemporaryDirectory() as tmp:
            pdf = os.path.join(tmp, "in.pdf")
            with urllib.request.urlopen(href, timeout=90) as resp, open(pdf, "wb") as f:
                shutil.copyfileobj(resp, f)
            subprocess.run(["pdftoppm", "-jpeg", "-f", "1", "-l", "1", "-scale-to-x", "800", "-scale-to-y", "-1",
                            pdf, os.path.join(tmp, "page")], check=True, capture_output=True)
            out = next(os.path.join(tmp, n) for n in os.listdir(tmp) if n.startswith("page"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(out, dest)
        return True
    except Exception as exc:
        print(f"  обложка из PDF не получилась ({item['name']}): {exc}", file=sys.stderr)
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("key", nargs="?", default=DEFAULT_KEY, help="публичная ссылка на папку Диска")
    ap.add_argument("--csv", default=DEFAULT_CSV, help="куда сохранить CSV (по умолчанию yd_pdfs.csv в корне проекта)")
    ap.add_argument("--direct", action="store_true", help="добавить прямую ссылку на скачивание (временная)")
    ap.add_argument("--force", action="store_true", help="перекачать обложки заново")
    ap.add_argument("--jobs", type=int, default=4, help="сколько обложек качать одновременно")
    args = ap.parse_args()

    root = get(f"{API}?" + urllib.parse.urlencode({"public_key": args.key, "limit": 0}))
    base = (root.get("public_url") or args.key).replace("https://yadi.sk/", "https://disk.yandex.ru/").rstrip("/")

    items = [it for it in walk(args.key) if it["name"].lower().endswith(".pdf")]

    def process(it):
        parts = it["path"].strip("/").split("/")
        folder = parts[-2] if len(parts) > 1 else "(корень)"  # ближайшая папка: TECMASTER / WAGNER
        stem = os.path.splitext(it["name"])[0]
        rel = os.path.join("yd_covers", folder, stem + ".jpg")
        dest = os.path.join(ROOT, rel)
        ok = os.path.exists(dest) and not args.force
        ok = ok or (bool(it.get("preview")) and save_preview(it["preview"], dest))
        ok = ok or cover_from_pdf(args.key, it, dest)
        direct = ""
        if args.direct:
            try:
                direct = it.get("file") or get(f"{API}/download?" + urllib.parse.urlencode(
                    {"public_key": args.key, "path": it["path"]}))["href"]
            except Exception as exc:
                print(f"  прямая ссылка не получена ({it['name']}): {exc}", file=sys.stderr)
        link = base + urllib.parse.quote(it["path"])
        return [folder, it["name"], link, rel if ok else ""] + ([direct] if args.direct else [])

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        rows = list(pool.map(process, items))

    rows.sort(key=lambda r: (r[0].lower(), r[1].lower()))
    last = None
    for r in rows:
        if r[0] != last:
            print(f"\n=== {r[0]} ({sum(1 for x in rows if x[0] == r[0])}) ===")
            last = r[0]
        print("\t".join([r[1], r[2], r[3]] + r[4:]))

    out = os.path.abspath(args.csv)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Папка", "Название", "Ссылка на PDF", "Файл обложки"] + (["Прямая ссылка (временная)"] if args.direct else []))
        w.writerows(rows)

    missing = [r[1] for r in rows if not r[3]]
    print(f"\nВсего PDF: {len(rows)}, обложек: {len(rows) - len(missing)} (папка {COVERS}). CSV: {out}", file=sys.stderr)
    if missing:
        print("Без обложки (запустите ещё раз — докачает только их):", file=sys.stderr)
        for n in missing:
            print(f"  {n}", file=sys.stderr)


if __name__ == "__main__":
    main()
