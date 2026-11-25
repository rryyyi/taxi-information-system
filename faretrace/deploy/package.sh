#!/usr/bin/env bash
# 本地打包（排除虚拟环境/缓存/本机数据库与日志）
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${1:-/tmp/faretrace-deploy}
rm -rf "$OUT" && mkdir -p "$OUT"
cp -r app config data scripts tests docs deploy requirements.txt seed.py package.json pytest.ini README.md ASSUMPTIONS.md TEAM_ROLES.md "$OUT"/
find "$OUT" -type d \( -name __pycache__ -o -name .pytest_cache -o -name .dream-rsi \) -prune -exec rm -rf {} +
find "$OUT" -type f \( -name '*.pyc' -o -name '*.log' \) -delete
tar -czf /tmp/faretrace-deploy.tar.gz -C "$OUT" .
echo "已生成 /tmp/faretrace-deploy.tar.gz"
