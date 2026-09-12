#!/bin/bash
# Desfaz o pausar_ceo.sh — volta as mensagens automáticas do número Ceo Território Digital.
set -u
INTEG=/c/Users/Acer/pacto-integrador
CRM=/c/Users/Acer/crm-territoriofit
source <(grep -E "^(SUPABASE_URL|SUPABASE_KEY)=" /c/Users/Acer/.env.integracao | sed 's/\r$//')
cd "$INTEG"
for w in monitor-atendimento.yml monitor-takeover.yml avisar-story.yml; do gh workflow enable "$w" && echo "  reativado: $w"; done
echo "  (disparo-indicacao.yml fica desativado: campanha de julho já concluída)"
gh variable delete RELATORIO_PARA 2>/dev/null && echo "  RELATORIO_PARA removida (volta pro número do André)"
curl.exe -s -X DELETE "$SUPABASE_URL/rest/v1/config?key=eq.NOTIFY_PAUSADO" \
  -H "apikey: $SUPABASE_KEY" -H "Authorization: Bearer $SUPABASE_KEY" -w "  config NOTIFY_PAUSADO removida HTTP %{http_code}\n"
cd "$CRM"
npx supabase db query --linked "update cron.job set active=true where jobname in ('daily-ads-summary-8h','google-balance-check-9h')" 2>&1 | tail -2
echo "PRONTO. Mensagens automáticas do número Ceo retomadas."
