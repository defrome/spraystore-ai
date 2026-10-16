#!/usr/bin/env python3
"""Синхронизация спрейгайдов: публичная папка Яндекс Диска -> sprayguides/ в репозитории.

Что делает:
  1. Обходит публичную папку (и все подпапки) через API Яндекс Диска.
  2. Скачивает новые/изменённые PDF в sprayguides/files/<папка>/<имя>.pdf
     (неизменённые файлы не перекачивает — сверяет md5).
  3. Делает обложку из первой страницы PDF (нужен pdftoppm из poppler-utils).
  4. Удаляет файлы, которых больше нет на Диске.
  5. Пишет sprayguides/data.json — по нему строится страница каталога.

Запуск: YD_PUBLIC_KEY=<публичная ссылка на папку> python3 scripts/sync_sprayguides.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
import urllib.parse
import urllib.request

API = "https://cloud-api.yandex.net/v1/disk/public/resources"
PUBLIC_KEY = os.environ.get("YD_PUBLIC_KEY", "").strip()
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sprayguides")
FILES = os.path.join(ROOT, "files")
COVERS = os.path.join(ROOT, "covers")
DATA = os.path.join(ROOT, "data.json")

TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
     "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"],
))

MODEL_RE = re.compile(r"(?i)\b(tec\s?\d+|trex\s?\d+|t\s?\d{3,4}(?:extra)?|w\s?\d{3,4})\b")


def slug(text: str) -> str:
    text = "".join(TRANSLIT.get(ch, ch) for ch in text.lower())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "file"


def api_get(params: dict, path_suffix: str = "") -> dict:
    url = API + path_suffix + "?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                return json.load(resp)
        except Exception as exc:  # сеть/лимиты — пробуем ещё раз
            if attempt == 3:
                raise
            print(f"  повтор запроса ({exc})", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("unreachable")


def walk(path: str = "/"):
    """Возвращает все PDF внутри папки (рекурсивно), с учётом пагинации."""
    found, offset = [], 0
    while True:
        data = api_get({"public_key": PUBLIC_KEY, "path": path, "limit": 100, "offset": offset})
        emb = data.get("_embedded", {})
        items = emb.get("items", [])
        for it in items:
            if it.get("type") == "dir":
                found += walk(it["path"])
            elif it.get("type") == "file" and (
                it.get("mime_type") == "application/pdf" or it["name"].lower().endswith(".pdf")
            ):
                found.append(it)
        offset += len(items)
        if not items or offset >= emb.get("total", 0):
            return found


def pretty_title(filename: str):
    """'DYE_Dufa_Kraski_T1000.pdf' -> ('Dufa Kraski', 'T1000')."""
    base = os.path.splitext(filename)[0]
    base = re.sub(r"(?i)^dye[_\-\s]+", "", base)
    base = re.sub(r"[_\-]+", " ", base)
    base = re.sub(r"\s+\d{1,2}$", "", base).strip()  # хвостовые «-2», «_1» — номера версий
    m = MODEL_RE.search(base)
    model = ""
    if m:
        model = re.sub(r"\s+", "", m.group(1))
        model = model[0].upper() + model[1:]
        base = (base[: m.start()] + base[m.end():]).strip()
    base = re.sub(r"\s{2,}", " ", base).strip(" -") or model or filename
    return base, model


def _fetch(link: str, tmp: str, dest: str):
    req = urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0 (sprayguides-sync)"})
    with urllib.request.urlopen(req, timeout=180) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out)
    if os.path.getsize(tmp) < 100:
        raise RuntimeError("пустой ответ")
    os.replace(tmp, dest)


_ZIP = {"zf": None, "tried": False}


def _open_zip():
    """Один раз качает всю публичную папку архивом (один запрос вместо десятков)."""
    if _ZIP["tried"]:
        return _ZIP["zf"]
    _ZIP["tried"] = True
    for attempt in range(3):
        try:
            link = api_get({"public_key": PUBLIC_KEY}, "/download")["href"]
            fd, path = tempfile.mkstemp(suffix=".zip")
            os.close(fd)
            print("  запасной канал: скачиваю всю папку архивом…")
            _fetch(link, path + ".part", path)
            _ZIP["zf"] = zipfile.ZipFile(path)
            print(f"  архив получен: {len(_ZIP['zf'].namelist())} записей")
            return _ZIP["zf"]
        except Exception as exc:
            print(f"  архив: попытка {attempt + 1}/3: {exc}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    return None


def _from_zip(item: dict, dest: str):
    zf = _open_zip()
    if not zf:
        return False
    want = item["path"].strip("/").lower()
    base = os.path.basename(want)
    names = zf.namelist()
    hit = next((n for n in names if n.lower().rstrip("/").endswith(want)), None)
    if not hit:  # архив мог получить корневую папку или другое имя — ищем по имени файла
        cands = [n for n in names if os.path.basename(n.lower()) == base]
        hit = cands[0] if len(cands) == 1 else None
    if not hit:
        return False
    with zf.open(hit) as src, open(dest, "wb") as out:
        shutil.copyfileobj(src, out)
    return True


def download(item: dict, dest: str):
    """Скачивает файл: прямая ссылка из листинга -> /download -> общий архив папки."""
    tmp = dest + ".part"
    last = None
    for attempt in range(2):
        for name in ("listing", "download"):
            try:
                if name == "listing":
                    link = item.get("file")
                    if not link:
                        continue
                else:
                    link = api_get({"public_key": PUBLIC_KEY, "path": item["path"]}, "/download")["href"]
                _fetch(link, tmp, dest)
                return True
            except Exception as exc:
                last = exc
                print(f"  {name}: попытка {attempt + 1}/2 для {item['path']}: {exc}", file=sys.stderr)
        time.sleep(3)
    if os.path.exists(tmp):
        os.remove(tmp)
    try:
        if _from_zip(item, dest):
            print(f"  из архива: {item['path']}")
            return True
    except Exception as exc:
        last = exc
    print(f"  ПРОПУЩЕН {item['path']} ({item.get('size', 0) // 1024} КБ): {last}", file=sys.stderr)
    return False


def make_cover(pdf: str, jpg: str):
    stem = jpg[:-4]
    subprocess.run(
        ["pdftoppm", "-jpeg", "-jpegopt", "quality=80", "-f", "1", "-l", "1",
         "-scale-to", "700", "-singlefile", pdf, stem],
        check=True,
    )


def main():
    if not PUBLIC_KEY:
        sys.exit("Нужна переменная окружения YD_PUBLIC_KEY (публичная ссылка на папку)")

    old = {}
    if os.path.exists(DATA):
        with open(DATA, encoding="utf-8") as f:
            for g in json.load(f).get("groups", []):
                for it in g["items"]:
                    old[it["file"]] = it

    items = walk("/")
    print(f"На Диске PDF: {len(items)}")

    groups, keep = {}, set()
    for it in sorted(items, key=lambda x: x["path"].lower()):
        parts = it["path"].strip("/").split("/")
        group = parts[0] if len(parts) > 1 else "Общее"
        sub = "/".join(slug(p) for p in parts[:-1]) or "root"
        name = slug(os.path.splitext(parts[-1])[0])
        rel_pdf = f"files/{sub}/{name}.pdf"
        rel_jpg = f"covers/{sub}/{name}.jpg"
        abs_pdf = os.path.join(ROOT, rel_pdf)
        abs_jpg = os.path.join(ROOT, rel_jpg)
        os.makedirs(os.path.dirname(abs_pdf), exist_ok=True)
        os.makedirs(os.path.dirname(abs_jpg), exist_ok=True)

        prev = old.get(rel_pdf)
        fresh = prev and prev.get("md5") == it.get("md5") and os.path.exists(abs_pdf)
        if not fresh:
            print(f"  скачиваю {it['path']}")
            if not download(it, abs_pdf):
                if prev and os.path.exists(abs_pdf):
                    keep.update({rel_pdf, rel_jpg})
                    groups.setdefault(group, []).append(prev)
                continue
        if not fresh or not os.path.exists(abs_jpg):
            try:
                make_cover(abs_pdf, abs_jpg)
            except Exception as exc:
                print(f"  обложка не получилась для {it['path']}: {exc}", file=sys.stderr)

        title, model = pretty_title(parts[-1])
        groups.setdefault(group, []).append({
            "title": title,
            "model": model,
            "name": parts[-1],
            "file": rel_pdf,
            "cover": rel_jpg if os.path.exists(abs_jpg) else "",
            "size": it.get("size", 0),
            "md5": it.get("md5", ""),
        })
        keep.update({rel_pdf, rel_jpg})

    # убрать то, чего больше нет на Диске
    for base in (FILES, COVERS):
        for dirpath, _, names in os.walk(base):
            for n in names:
                rel = os.path.relpath(os.path.join(dirpath, n), ROOT).replace(os.sep, "/")
                if rel not in keep:
                    os.remove(os.path.join(dirpath, n))
                    print(f"  удалён {rel}")

    out = {
        "groups": [{"name": g, "items": groups[g]} for g in sorted(groups)],
    }
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"Готово: {sum(len(g['items']) for g in out['groups'])} файлов в {len(out['groups'])} папках")


if __name__ == "__main__":
    main()
