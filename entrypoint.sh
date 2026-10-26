#!/bin/bash
# Gera o dashboard a cada REFRESH_MINUTES e serve via HTTP na porta PORT.
OUT="${OUTPUT_DIR:-/data}"
WEB="$OUT/www"
MIN="${REFRESH_MINUTES:-60}"
PORT="${PORT:-8080}"
HIST="${MESES_HISTORICO:-3}"
mkdir -p "$WEB"

publicar() {
  cp "$OUT/dashboard.html" "$WEB/index.html.tmp" && mv "$WEB/index.html.tmp" "$WEB/index.html"
}

gerar() {  # $1 = meses de histórico
  echo "[$(date '+%F %T')] Gerando dashboard (histórico: $1 meses)..."
  python /app/azure_dashboard.py --historico "$1" 2>&1 | tee "$OUT/ultimo.log"
  if [ "${PIPESTATUS[0]}" = 0 ]; then
    publicar && echo "[$(date '+%F %T')] Dashboard publicado."
  else
    echo "[$(date '+%F %T')] FALHA ao gerar; mantendo a versão anterior."
    # sem dashboard anterior: mostra o motivo na própria página
    if ! grep -q "const D = " "$WEB/index.html" 2>/dev/null; then
      MSG=$(tail -n 1 "$OUT/ultimo.log" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')
      cat > "$WEB/index.html" <<HTML
<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>Azure Dashboard - erro</title></head>
<body style="font:16px system-ui;padding:40px;text-align:center">
<h2>Não foi possível gerar o dashboard</h2>
<p style="color:#b91c1c">${MSG}</p>
<p>Verifique as credenciais do .env e as permissões (Reader + Cost Management Reader). Nova tentativa automática em ${MIN} min.<br>
Detalhes: podman logs azure-dashboard</p></body></html>
HTML
    fi
  fi
}

# página provisória enquanto a primeira geração não termina
if ! grep -q "const D = " "$WEB/index.html" 2>/dev/null; then
  cat > "$WEB/index.html" <<'HTML'
<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8"><meta http-equiv="refresh" content="20">
<title>Azure Dashboard</title></head>
<body style="font:16px system-ui;padding:40px;text-align:center">
<h2>Gerando o dashboard pela primeira vez...</h2>
<p>Isso pode levar alguns minutos (a API de custos do Azure limita as consultas). A página atualiza sozinha.</p>
<p style="color:#888">Acompanhe com: podman logs -f azure-dashboard</p></body></html>
HTML
  PRIMEIRA=1
fi

(
  while true; do
    # primeira vez: publica rápido só o mês atual; depois completa com o histórico
    if [ "${PRIMEIRA:-0}" = 1 ] && [ "$HIST" != 0 ]; then gerar 0; fi
    PRIMEIRA=0
    gerar "$HIST"
    sleep $((MIN * 60))
  done
) &

# serve apenas a pasta www (data.json e cache ficam fora da web)
exec python -m http.server "$PORT" --directory "$WEB" --bind 0.0.0.0
