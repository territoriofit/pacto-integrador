#!/bin/bash
# Pausa TODAS as mensagens automáticas do número "Ceo Território Digital"
# (5516997936831) pro André e pra equipe de vendas. Pedido do André 12/09/2026.
# Rodar no Claude Code com:  ! bash /c/Users/Acer/pacto-integrador/ops/pausar_ceo.sh
# Desfazer com:              ! bash /c/Users/Acer/pacto-integrador/ops/retomar_ceo.sh
set -u
INTEG=/c/Users/Acer/pacto-integrador
CRM=/c/Users/Acer/crm-territoriofit
source <(grep -E "^(SUPABASE_URL|SUPABASE_KEY)=" /c/Users/Acer/.env.integracao | sed 's/\r$//')

echo "### 1/5 Workflows do GitHub (monitor 2x/h, relatório takeover, aviso story, indicação julho)"
cd "$INTEG"
for w in monitor-atendimento.yml monitor-takeover.yml avisar-story.yml disparo-indicacao.yml; do
  gh workflow disable "$w" && echo "  desativado: $w"
done

echo "### 2/5 Relatório diário da régua de cobrança (a régua continua, só o resumo pausa)"
gh variable set RELATORIO_PARA --body off && echo "  RELATORIO_PARA=off"
git push origin HEAD 2>&1 | tail -1

echo "### 3/5 Chave de pausa no CRM (config NOTIFY_PAUSADO=1)"
curl.exe -s -X POST "$SUPABASE_URL/rest/v1/config" \
  -H "apikey: $SUPABASE_KEY" -H "Authorization: Bearer $SUPABASE_KEY" \
  -H "Content-Type: application/json" -H "Prefer: resolution=merge-duplicates,return=minimal" \
  -d '{"key":"NOTIFY_PAUSADO","value":"1"}' -w "  HTTP %{http_code}\n"

echo "### 4/5 Deploy das edge functions que leem a chave (avisos celular sem CRM + resumo de ads)"
cd "$CRM"
git push origin HEAD 2>&1 | tail -1
npx supabase functions deploy ai-sales-agent 2>&1 | tail -2
npx supabase functions deploy daily-ads-summary --no-verify-jwt 2>&1 | tail -2

echo "### 5/5 Crons do Supabase (resumo ads 8h, saldo Google 9h) — cinto e suspensório"
npx supabase db query --linked "update cron.job set active=false where jobname in ('daily-ads-summary-8h','google-balance-check-9h')" 2>&1 | tail -2

echo; echo "PRONTO. Continuam ativos: Clara, takeover/follow-ups, régua de cobrança (sem relatório), NPS, pesquisas, aniversariantes, syncs."
