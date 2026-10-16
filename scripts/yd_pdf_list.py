#!/usr/bin/env python3
"""Яндекс Диск -> GitHub Pages: PDF и обложки + список «название / ссылка на PDF / ссылка на обложку».

Что делает:
  1. Обходит публичную папку Диска (TECMASTER, WAGNER и любые подпапки).
  2. Скачивает каждый PDF в docs/files/<папка>/<имя>.pdf — папки те же, что на Диске.
     Уже скачанные файлы (тот же размер) пропускает, поэтому повторный запуск докачивает только новое.
  3. Делает обложку из первой страницы PDF: docs/covers/<папка>/<имя>.jpg (нужен pdftoppm, poppler).
  4. Печатает список и пишет yd_pdfs.csv в корне проекта (он в .gitignore): папка, название,
     ссылка на PDF, ссылка на обложку. Ссылки — постоянные, на GitHub Pages:
     https://<пользователь>.github.io/<репозиторий>/docs/files/<папка>/<имя>.pdf

Дальше: git add docs && git commit -m "Спрейгайды" && git push — Pages раздаёт папку docs после пуша.

Запуск:
  python3 scripts/yd_pdf_list.py                       # синхронизация + список + CSV
  python3 scripts/yd_pdf_list.py <ссылка на папку>     # другая папка Диска
  python3 scripts/yd_pdf_list.py --list-only           # только список (без скачивания), ссылки на Pages
  python3 scripts/yd_pdf_list.py --force               # перекачать всё заново
  PAGES_BASE=https://имя.github.io/репо python3 scripts/yd_pdf_list.py   # другой адрес Pages

Только стандартная библиотека.
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
DEFAULT_KEY = "https://disk.360.yandex.ru/d/xxunmuNTZX-2Dg"
PAGES_BASE = os.environ.get("PAGES_BASE", "https://defrome.github.io/spraystore-ai").rstrip("/")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DOCS = os.path.join(ROOT, "docs")
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


def download(key: str, item: dict, dest: str) -> bool:
    """Скачивает PDF: прямая ссылка из листинга -> /download. Пишет через .part, чтобы не оставлять обрывки."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    last = None
    for attempt in range(2):
        for source in ("listing", "download"):
            try:
                if source == "listing":
                    link = item.get("file")
                    if not link:
                        continue
                else:
                    link = get(f"{API}/download?" + urllib.parse.urlencode(
                        {"public_key": key, "path": item["path"]}))["href"]
                with urllib.request.urlopen(link, timeout=45) as resp, open(tmp, "wb") as f:
                    shutil.copyfileobj(resp, f)
                if item.get("size") and os.path.getsize(tmp) != int(item["size"]):
                    raise IOError("размер не совпал — файл скачан не полностью")
                os.replace(tmp, dest)
                return True
            except Exception as exc:
                last = exc
                print(f"  {source}: попытка {attempt + 1}/2 для {item['name']}: {exc}", file=sys.stderr)
        time.sleep(3 * (attempt + 1))
    if os.path.exists(tmp):
        os.remove(tmp)
    print(f"  НЕ СКАЧАН: {item['path']} ({last})", file=sys.stderr)
    return False


def make_cover(pdf: str, dest: str) -> bool:
    """Первая страница PDF -> jpg шириной 800 px."""
    if not shutil.which("pdftoppm"):
        return False
    try:
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "page")
            subprocess.run(["pdftoppm", "-jpeg", "-f", "1", "-l", "1", "-scale-to-x", "800",
                            "-scale-to-y", "-1", pdf, base], check=True, capture_output=True)
            out = next(os.path.join(tmp, n) for n in os.listdir(tmp) if n.startswith("page"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(out, dest)
        return True
    except Exception as exc:
        print(f"  обложка не получилась ({os.path.basename(pdf)}): {exc}", file=sys.stderr)
        return False


def url_for(kind: str, folder: str, filename: str) -> str:
    return f"{PAGES_BASE}/docs/{kind}/" + "/".join(urllib.parse.quote(p) for p in (folder, filename))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("key", nargs="?", default=DEFAULT_KEY, help="публичная ссылка на папку Диска")
    ap.add_argument("--csv", default=DEFAULT_CSV, help="куда сохранить CSV (по умолчанию yd_pdfs.csv в корне проекта)")
    ap.add_argument("--list-only", action="store_true", help="не скачивать, только вывести список")
    ap.add_argument("--jobs", type=int, default=6, help="сколько файлов качать одновременно (по умолчанию 6)")
    ap.add_argument("--force", action="store_true", help="перекачать файлы и обложки заново")
    args = ap.parse_args()

    if not args.list_only and not shutil.which("pdftoppm"):
        print("Нет pdftoppm — обложки не получатся. Установите poppler "
              "(Fedora: sudo dnf install poppler-utils, Mac: brew install poppler).", file=sys.stderr)

    items = []
    for it in walk(args.key):
        if it["name"].lower().endswith(".pdf"):
            items.append(it)

    def process(it):
        """Скачивает PDF (если нужно), делает обложку. Возвращает (строка CSV | None, путь неудачи | None)."""
        name = it["name"]
        parts = it["path"].strip("/").split("/")
        folder = parts[0] if len(parts) > 1 else "(корень)"
        stem = os.path.splitext(name)[0]
        pdf_path = os.path.join(DOCS, "files", folder, name)
        cover_path = os.path.join(DOCS, "covers", folder, stem + ".jpg")

        if not args.list_only:
            have = os.path.exists(pdf_path) and (not it.get("size") or os.path.getsize(pdf_path) == int(it["size"]))
            if args.force or not have:
                print(f"скачиваю {it['path']}", file=sys.stderr)
                if not download(args.key, it, pdf_path):
                    return None, it["path"]
                if os.path.exists(cover_path):
                    os.remove(cover_path)  # PDF обновился — обложку сделаем заново
            if args.force or not os.path.exists(cover_path):
                make_cover(pdf_path, cover_path)

        cover_url = url_for("covers", folder, stem + ".jpg") if args.list_only or os.path.exists(cover_path) else ""
        return (folder, name, url_for("files", folder, name), cover_url), None

    rows, failed = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for row, bad in pool.map(process, items):
            if row:
                rows.append(row)
            if bad:
                failed.append(bad)

    rows.sort(key=lambda r: (r[0].lower(), r[1].lower()))
    last = None
    for folder, name, pdf_url, cover_url in rows:
        if folder != last:
            print(f"\n=== {folder} ({sum(1 for r in rows if r[0] == folder)}) ===")
            last = folder
        print(f"{name}\t{pdf_url}\t{cover_url}")

    out = os.path.abspath(args.csv)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Папка", "Название", "Ссылка на PDF", "Ссылка на обложку"])
        w.writerows(rows)

    print(f"\nВсего PDF: {len(rows)}. CSV: {out}", file=sys.stderr)
    if failed:
        print(f"Не скачались ({len(failed)}) — запустите скрипт ещё раз, он докачает только их:", file=sys.stderr)
        for p in failed:
            print(f"  {p}", file=sys.stderr)
    if not args.list_only:
        print("Дальше: git add docs && git commit -m 'Спрейгайды' && git push", file=sys.stderr)


if __name__ == "__main__":
    main()
