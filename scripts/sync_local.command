#!/bin/bash
# Двойной клик на Mac: качает спрейгайды с Яндекс Диска и пушит в репозиторий.
cd "$(dirname "$0")/.." || exit 1
command -v pdftoppm >/dev/null || brew install poppler
git pull --rebase origin main
YD_PUBLIC_KEY="https://disk.360.yandex.ru/d/xxunmuNTZX-2Dg" python3 scripts/sync_sprayguides.py
git add sprayguides
if git diff --cached --quiet; then echo "Изменений нет"; else
  git commit -m "Обновить спрейгайды с Яндекс Диска (локально)" && git push origin main
fi
echo "Готово. Окно можно закрыть."; read -r -n1
