#!/usr/bin/env bash
# Запуск/обновление стейджинга на сервере: ./deploy/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f service/.env ] || { echo "Нет service/.env — скопируйте service/.env.example и заполните" >&2; exit 1; }
for k in GENAPI_KEY ADMIN_TOKEN; do
  grep -Eq "^$k=.+" service/.env || { echo "В service/.env не задан $k" >&2; exit 1; }
done

docker compose up -d --build
HOST="${SITE_ADDRESS:-158-160-204-91.sslip.io}"
echo "Жду готовности (первый старт ~1-2 минуты: строится индекс)..."
for _ in $(seq 1 60); do
  if curl -fsSk --resolve "$HOST:443:127.0.0.1" "https://$HOST/api/health" 2>/dev/null; then
    echo; echo "OK: https://$HOST/   админка: https://$HOST/admin"; exit 0
  fi
  sleep 5
done
echo "Сервис не поднялся, логи: docker compose logs app caddy" >&2; exit 1
