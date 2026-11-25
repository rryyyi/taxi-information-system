#!/usr/bin/env bash
# FareTrace 服务器端安装脚本（Ubuntu/Debian 与 RHEL/Alibaba Cloud Linux/CentOS 通用）
# 用法：以 root 执行  bash /tmp/faretrace-deploy/setup_server.sh /tmp/faretrace-deploy
set -euo pipefail

SRC_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
APP_DIR=/opt/faretrace
SERVICE_USER=faretrace
PY_MIN_MAJOR=3
PY_MIN_MINOR=10

log()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 执行本脚本"

# ---------- 1. 识别发行版与包管理器 ----------
if command -v dnf >/dev/null 2>&1; then PKG=dnf
elif command -v yum >/dev/null 2>&1; then PKG=yum
elif command -v apt-get >/dev/null 2>&1; then PKG=apt
else die "未识别的发行版（找不到 dnf/yum/apt-get）"
fi

if [ "$PKG" = apt ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip nginx tar curl ca-certificates
else
  # Alibaba Cloud Linux 3 / CentOS Stream 8+ / Rocky / Alma 用 dnf
  $PKG install -y python3 python3-pip nginx tar curl ca-certificates
fi

# ---------- 2. 检查 Python 版本 ----------
PY_BIN=$(command -v python3) || die "未安装 python3"
read -r PY_OK <<<"$("$PY_BIN" - <<'PY'
import sys
print(1 if sys.version_info >= (3, 10) else 0)
PY
)"
if [ "$PY_OK" != "1" ]; then
  warn "系统 Python $($PY_BIN --version) 低于 3.10，尝试安装更高版本"
  if [ "$PKG" = apt ]; then
    apt-get install -y python3.11 python3.11-venv python3.11-dev || apt-get install -y python3.12 python3.12-venv || true
  else
    $PKG install -y python3.11 || $PKG install -y python3.12 || true
  fi
  for cand in python3.12 python3.11 python3.13; do
    if command -v "$cand" >/dev/null 2>&1; then PY_BIN=$(command -v "$cand"); break; fi
  done
fi
"$PY_BIN" - <<'PY' || die "Python 版本仍低于 3.10，请手动安装 python3.11+ 后重跑"
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY
log "使用 Python: $("$PY_BIN" --version) ($PY_BIN)"

# ---------- 3. 创建运行用户 ----------
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  log "已创建系统用户 $SERVICE_USER"
fi

# ---------- 4. 复制代码到 /opt/faretrace ----------
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/app" "$APP_DIR/config" "$APP_DIR/data" "$APP_DIR/scripts" "$APP_DIR/deploy"
cp -r "$SRC_DIR/app" "$SRC_DIR/config" "$SRC_DIR/data" "$APP_DIR/"
[ -d "$SRC_DIR/scripts" ] && cp -r "$SRC_DIR/scripts" "$APP_DIR/"
[ -d "$SRC_DIR/deploy" ] && cp -r "$SRC_DIR/deploy" "$APP_DIR/"
for f in requirements.txt seed.py package.json pytest.ini README.md ASSUMPTIONS.md TEAM_ROLES.md; do
  [ -f "$SRC_DIR/$f" ] && cp "$SRC_DIR/$f" "$APP_DIR/"
done
[ -d "$SRC_DIR/docs" ] && cp -r "$SRC_DIR/docs" "$APP_DIR/"
[ -d "$SRC_DIR/tests" ] && cp -r "$SRC_DIR/tests" "$APP_DIR/"
# 数据库不随包上传：首次启动自动建表 + 灌种子
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
log "代码已就位：$APP_DIR"

# ---------- 5. venv + 依赖 ----------
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  "$PY_BIN" -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --upgrade pip setuptools wheel -q
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
# 生产用 ASGI 服务（requirements 已含 uvicorn，这里显式确认）
"$APP_DIR/.venv/bin/python" - <<'PY' || die "依赖安装不完整"
import fastapi, uvicorn, sqlalchemy, pydantic, jinja2, yaml
print("fastapi", fastapi.__version__, "| uvicorn", uvicorn.__version__)
PY
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.venv"
log "Python 依赖安装完成"

# ---------- 6. systemd ----------
if command -v systemctl >/dev/null 2>&1; then
  cp "$APP_DIR/deploy/faretrace.service" /etc/systemd/system/faretrace.service
  systemctl daemon-reload
  systemctl enable faretrace >/dev/null
  systemctl restart faretrace
  log "systemd 服务已启动：faretrace"
else
  die "未找到 systemd，请手动用 uvicorn 启动"
fi

# ---------- 7. nginx ----------
# RHEL 家族发行版自带的 nginx.conf 内嵌 default server 块，会抢占 80 端口；
# 存在内嵌 server 块时换成干净的 nginx.conf（原文件已备份）。
NGX_CONF=/etc/nginx/nginx.conf
if [ -f "$NGX_CONF" ] && grep -qE 'server[[:space:]]*\{' "$NGX_CONF"; then
  [ -f "$NGX_CONF.dist-backup" ] || cp "$NGX_CONF" "$NGX_CONF.dist-backup"
  cat > "$NGX_CONF" <<'NGXCONF'
user  nginx;
worker_processes auto;
error_log  /var/log/nginx/error.log warn;
pid        /run/nginx.pid;

events { worker_connections 1024; }

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;
    log_format  main  '$remote_addr - $remote_user [$time_local] "$request" '
                      '$status $body_bytes_sent "$http_referer" '
                      '"$http_user_agent" "$http_x_forwarded_for"';
    access_log  /var/log/nginx/access.log  main;
    sendfile        on;
    keepalive_timeout  65;
    server_tokens off;
    client_max_body_size 20m;
    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml;
    gzip_min_length 1024;
    include /etc/nginx/conf.d/*.conf;
}
NGXCONF
  log "已替换发行版默认 nginx.conf（备份为 $NGX_CONF.dist-backup）"
fi

cp "$APP_DIR/deploy/nginx-faretrace.conf" /etc/nginx/conf.d/faretrace.conf
rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
rm -f /etc/nginx/conf.d/default.conf 2>/dev/null || true
nginx -t
systemctl enable nginx >/dev/null 2>&1 || true
systemctl restart nginx
log "nginx 已启动（80 端口反代 127.0.0.1:8000）"

# ---------- 8. 自检 ----------
sleep 2
for i in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; then break; fi
  sleep 1
done
log "后端健康检查: $(curl -fsS http://127.0.0.1:8000/api/health || echo '失败')"
log "经 nginx 健康检查: $(curl -fsS -o /dev/null -w 'HTTP %{http_code}' http://127.0.0.1/api/health || echo '失败')"

# ---------- 9. 防火墙（尽力而为） ----------
if command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-service=http >/dev/null 2>&1 || true
  firewall-cmd --permanent --add-service=https >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
  log "firewalld 已放行 80/443"
elif command -v ufw >/dev/null 2>&1; then
  ufw allow 80/tcp >/dev/null 2>&1 || true
  ufw allow 443/tcp >/dev/null 2>&1 || true
  log "ufw 已放行 80/443"
fi

log "部署完成 ✅  管理命令：systemctl status|restart faretrace；日志：journalctl -u faretrace -f"
