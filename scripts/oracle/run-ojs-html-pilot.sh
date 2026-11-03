#!/usr/bin/env bash
#
# Piloto HTML OJS estritamente limitado. Este script não é acionado pelo
# deploy web e não deve ser executado sem uma autorização operacional nova.
# A imagem informada precisa conter o modo --max-pages-per-job introduzido
# para o piloto; o CMD da imagem é sempre substituído pelo comando abaixo.
set -Eeuo pipefail
IFS=$'\n\t'

EXPECTED_HOSTNAME='instance-20260923-1635'
WEB_CONTAINER='pesquisa-pdf'
OAI_WORKER_CONTAINER='pesquisa-pdf-ojs-worker'
VOLUME_NAME='pesquisa-pdf-data'
VOLUME_TARGET='/app/outputs'
PILOT_PREFIX='pesquisa-pdf-ojs-html-pilot-'
# Antes de reservar 320 MiB para o piloto, exige margem independente para a
# aplicação web, o worker OAI e o cache do kernel. Em uma VM de 956 MiB, não
# iniciaremos o teste se essa margem conservadora não estiver disponível.
MIN_HOST_MEMORY_BYTES=$((450 * 1024 * 1024))
MAX_ADDITIONAL_SWAP_BYTES=$((256 * 1024 * 1024))
MAX_SUSTAINED_HTML_CPU_PERCENT=75
MAX_SUSTAINED_HTML_CPU_SAMPLES=3

mode='--plan'
audit_run_id=''
image=''
env_file='/root/analisys-web-ojs-20261102.env'
backup_dir=''
max_pages=3
page_timeout_seconds=20
max_execution_seconds=150
min_request_interval_seconds=1
lease_seconds=300
confirmation=''

usage() {
  cat <<'EOF'
Uso:
  sudo /usr/bin/bash run-ojs-html-pilot.sh --plan \
    --audit-run-id <uuid> --image ghcr.io/fabiomoraescs/analisys@sha256:<digest> \
    --backup-dir /home/ubuntu/analisys-backups/<backup-validado>

  sudo /usr/bin/bash run-ojs-html-pilot.sh --execute --confirm EXECUTAR-PILOTO-HTML \
    --audit-run-id <uuid> --image ghcr.io/fabiomoraescs/analisys@sha256:<digest> \
    --backup-dir /home/ubuntu/analisys-backups/<backup-validado>

O plano somente consulta Docker, o backup e o SQLite em modo somente leitura.
O modo de execução cria um único contêiner sem portas, sem reinício automático,
com no máximo cinco páginas e uma única análise HTML identificada.
EOF
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Comando obrigatório ausente: $1"
}

memory_available_bytes() {
  free -b | awk '/Mem:/ {print $7}'
}

swap_used_bytes() {
  free -b | awk '/Swap:/ {print $3}'
}

container_metric() {
  local name="$1"
  docker stats --no-stream --format '{{.MemUsage}}|{{.CPUPerc}}|{{.PIDs}}' "$name" 2>/dev/null || printf 'unavailable|unavailable|unavailable\n'
}

http_status() {
  curl --silent --show-error --output /dev/null --write-out '%{http_code}' --max-time 5 http://127.0.0.1:5000/ || true
}

assert_environment() {
  [[ "$(id -u)" -eq 0 ]] || fail 'O piloto deve ser iniciado via sudo como root.'
  [[ "$(hostname)" == "$EXPECTED_HOSTNAME" ]] || fail 'Hostname inesperado; piloto recusado.'
  [[ "$audit_run_id" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}$ ]] || fail 'O identificador da auditoria HTML é inválido.'
  [[ "$image" =~ ^ghcr\.io/fabiomoraescs/analisys@sha256:[a-f0-9]{64}$ ]] || fail 'A imagem precisa ser um digest GHCR Análisys imutável.'
  [[ -d "$backup_dir" && -f "$backup_dir/outputs.tar.gz" && -f "$backup_dir/outputs.tar.gz.sha256" ]] \
    || fail 'O backup local validado não possui outputs.tar.gz e seu manifesto.'
  (cd "$backup_dir" && sha256sum --check --status outputs.tar.gz.sha256) \
    || fail 'O checksum do backup local não confere.'
  [[ -f "$env_file" ]] || fail 'O arquivo de ambiente protegido não foi localizado.'
  local mode_bits owner
  mode_bits="$(stat -c '%a' "$env_file")"
  owner="$(stat -c '%U:%G' "$env_file")"
  [[ "$owner" == 'root:root' && $((8#$mode_bits & 8#077)) -eq 0 ]] \
    || fail 'O arquivo de ambiente não possui propriedade/permissões restritas.'
  docker inspect "$WEB_CONTAINER" >/dev/null 2>&1 || fail 'Contêiner web não encontrado.'
  docker inspect "$OAI_WORKER_CONTAINER" >/dev/null 2>&1 || fail 'Worker OAI-PMH não encontrado.'
  [[ "$(docker inspect --format '{{.State.Running}}' "$WEB_CONTAINER")" == 'true' ]] \
    || fail 'Contêiner web não está em execução.'
  [[ "$(docker inspect --format '{{.State.Running}}' "$OAI_WORKER_CONTAINER")" == 'true' ]] \
    || fail 'Worker OAI-PMH não está em execução.'
  docker volume inspect "$VOLUME_NAME" >/dev/null 2>&1 || fail 'Volume persistente não encontrado.'
  docker image inspect "$image" >/dev/null 2>&1 || fail 'A imagem indicada ainda não está disponível localmente.'
  [[ "$(docker image inspect --format '{{.Architecture}}' "$image")" == 'amd64' ]] \
    || fail 'A imagem indicada não é linux/amd64.'
  ! docker ps -a --format '{{.Names}}' | grep -Eq "^${PILOT_PREFIX}" \
    || fail 'Já existe um contêiner HTML de piloto preservado; não crie outro antes de avaliá-lo.'
  ! docker ps --format '{{.Names}}' | grep -Eq '^pesquisa-pdf-ojs-html' \
    || fail 'Há um worker HTML ativo; o piloto concorrente foi recusado.'
  local available
  available="$(memory_available_bytes)"
  [[ "$available" =~ ^[0-9]+$ && "$available" -ge "$MIN_HOST_MEMORY_BYTES" ]] \
    || fail 'Memória disponível insuficiente para iniciar o piloto.'
  [[ "$(http_status)" =~ ^(200|301|302|303|307|308)$ ]] \
    || fail 'O health check HTTP local do Análisys falhou.'
}

assert_target_is_pending_read_only() {
  local result
  result="$(docker run --rm --network none --mount "type=volume,src=${VOLUME_NAME},dst=${VOLUME_TARGET},readonly" \
    "$image" python -c '
import sqlite3, sys
audit_id = sys.argv[1]
connection = sqlite3.connect("file:/app/outputs/platform/platform.sqlite3?mode=ro", uri=True)
row = connection.execute("SELECT status FROM ojs_html_audit_runs WHERE id = ?", (audit_id,)).fetchone()
if row is None:
    raise SystemExit("not_found")
print(row[0])
' "$audit_run_id" 2>/dev/null)" || fail 'Não foi possível consultar a auditoria HTML em modo somente leitura.'
  [[ "$result" == 'pending' ]] || fail "A auditoria alvo não está pendente (estado: ${result:-indisponível})."
}

print_plan() {
  local free_bytes memory_bytes
  free_bytes="$(df -B1 --output=avail / | tail -n 1 | tr -d '[:space:]')"
  memory_bytes="$(memory_available_bytes)"
  printf 'hostname=%s\nimage=%s\naudit_run_id=%s\n' "$EXPECTED_HOSTNAME" "$image" "$audit_run_id"
  printf 'max_pages=%s\npage_timeout_seconds=%s\nmax_execution_seconds=%s\nlease_seconds=%s\n' \
    "$max_pages" "$page_timeout_seconds" "$max_execution_seconds" "$lease_seconds"
  printf 'docker_free_bytes=%s\nmemory_available_bytes=%s\n' "$free_bytes" "$memory_bytes"
  printf 'web_metrics=%s\noai_worker_metrics=%s\nhttp_status=%s\n' \
    "$(container_metric "$WEB_CONTAINER")" "$(container_metric "$OAI_WORKER_CONTAINER")" "$(http_status)"
  printf '%s\n' 'Plano validado: nenhuma alteração foi aplicada.'
}

stop_pilot() {
  local reason="$1"
  pilot_stop_reason="$reason"
  printf 'stop_reason=%s\n' "$reason" >> "$report_file"
  docker stop --time 180 "$pilot_container" >/dev/null || true
}

run_pilot() {
  local run_stamp pilot_container report_root report_file baseline_swap start_epoch elapsed sustained_high_cpu=0
  local pilot_stop_reason='' exit_code oom_killed
  run_stamp="$(date -u +%Y%m%dT%H%M%SZ)-${audit_run_id:0:8}"
  pilot_container="${PILOT_PREFIX}${run_stamp}"
  report_root='/var/lib/analisys-html-pilots'
  install -d -m 700 "$report_root"
  report_file="$report_root/${run_stamp}.report"
  touch "$report_file"
  chmod 600 "$report_file"
  baseline_swap="$(swap_used_bytes)"
  start_epoch="$(date +%s)"
  {
    printf 'started_at=%s\ncontainer=%s\naudit_run_id=%s\nimage=%s\n' "$(date -u +%FT%TZ)" "$pilot_container" "$audit_run_id" "$image"
    printf 'baseline_memory_available_bytes=%s\nbaseline_swap_used_bytes=%s\n' "$(memory_available_bytes)" "$baseline_swap"
  } >> "$report_file"

  docker run --detach --name "$pilot_container" --network bridge --restart no \
    --memory 320m --memory-reservation 192m --memory-swap 512m --cpus 0.50 --pids-limit 96 --stop-timeout 180 \
    --env-file "$env_file" --mount "type=volume,src=${VOLUME_NAME},dst=${VOLUME_TARGET}" \
    "$image" flask --app app ojs-seo-html-worker \
      --audit-run-id "$audit_run_id" --max-jobs 1 --max-pages-per-job "$max_pages" \
      --page-timeout-seconds "$page_timeout_seconds" --max-execution-seconds "$max_execution_seconds" \
      --min-request-interval "$min_request_interval_seconds" --lease-seconds "$lease_seconds" \
      --poll-interval 5 >/dev/null

  while [[ "$(docker inspect --format '{{.State.Running}}' "$pilot_container")" == 'true' ]]; do
    elapsed=$(( $(date +%s) - start_epoch ))
    local available current_swap status html_metrics web_metrics oai_metrics html_cpu
    available="$(memory_available_bytes)"
    current_swap="$(swap_used_bytes)"
    status="$(http_status)"
    html_metrics="$(container_metric "$pilot_container")"
    web_metrics="$(container_metric "$WEB_CONTAINER")"
    oai_metrics="$(container_metric "$OAI_WORKER_CONTAINER")"
    printf '%s elapsed=%s memory_available=%s swap_used=%s html_metrics=%s web_metrics=%s oai_metrics=%s http=%s\n' \
      "$(date -u +%FT%TZ)" "$elapsed" "$available" "$current_swap" "$html_metrics" \
      "$web_metrics" "$oai_metrics" "$status" >> "$report_file"
    if (( available < MIN_HOST_MEMORY_BYTES )); then
      stop_pilot 'host_memory_pressure'
      break
    fi
    if (( current_swap - baseline_swap > MAX_ADDITIONAL_SWAP_BYTES )); then
      stop_pilot 'swap_growth_limit'
      break
    fi
    if [[ ! "$status" =~ ^(200|301|302|303|307|308)$ ]]; then
      stop_pilot 'web_health_check_failed'
      break
    fi
    html_cpu="${html_metrics#*|}"
    html_cpu="${html_cpu%%|*}"
    html_cpu="${html_cpu%\%}"
    if [[ "$html_cpu" =~ ^[0-9]+([.][0-9]+)?$ ]] && awk -v value="$html_cpu" -v limit="$MAX_SUSTAINED_HTML_CPU_PERCENT" 'BEGIN { exit !(value > limit) }'; then
      sustained_high_cpu=$((sustained_high_cpu + 1))
    else
      sustained_high_cpu=0
    fi
    if (( sustained_high_cpu >= MAX_SUSTAINED_HTML_CPU_SAMPLES )); then
      stop_pilot 'sustained_html_cpu'
      break
    fi
    if (( elapsed > max_execution_seconds + 60 )); then
      stop_pilot 'pilot_runtime_limit'
      break
    fi
    sleep 5
  done

  {
    printf 'finished_at=%s\n' "$(date -u +%FT%TZ)"
    printf 'container_state=%s\n' "$(docker inspect --format '{{.State.Status}}|exit={{.State.ExitCode}}|oom={{.State.OOMKilled}}|restart={{.RestartCount}}' "$pilot_container")"
    printf 'final_memory_available_bytes=%s\nfinal_swap_used_bytes=%s\nfinal_http_status=%s\n' \
      "$(memory_available_bytes)" "$(swap_used_bytes)" "$(http_status)"
  } >> "$report_file"
  docker logs "$pilot_container" >> "$report_file" 2>&1 || true
  printf 'pilot_report=%s\n' "$report_file"
  printf 'pilot_container=%s\n' "$pilot_container"

  exit_code="$(docker inspect --format '{{.State.ExitCode}}' "$pilot_container")"
  oom_killed="$(docker inspect --format '{{.State.OOMKilled}}' "$pilot_container")"
  if [[ -n "$pilot_stop_reason" || "$oom_killed" == 'true' || "$exit_code" != '0' ]]; then
    printf 'ERROR: O piloto terminou sem aprovação (motivo=%s, exit_code=%s, oom_killed=%s).\n' \
      "${pilot_stop_reason:-container_exit}" "$exit_code" "$oom_killed" >&2
    return 1
  fi
}

while (($#)); do
  case "$1" in
    --plan|--execute) mode="$1" ;;
    --audit-run-id|--image|--env-file|--backup-dir|--max-pages|--page-timeout-seconds|--max-execution-seconds|--lease-seconds)
      (($# >= 2)) || fail "Valor ausente para $1"
      key="${1#--}"; key="${key//-/_}"
      printf -v "$key" '%s' "$2"
      shift
      ;;
    --min-request-interval)
      (($# >= 2)) || fail 'Valor ausente para --min-request-interval'
      min_request_interval_seconds="$2"
      shift
      ;;
    --confirm)
      (($# >= 2)) || fail 'Valor ausente para --confirm'
      confirmation="$2"
      shift
      ;;
    --help|-h) usage; exit 0 ;;
    *) fail "Opção desconhecida: $1" ;;
  esac
  shift
done

[[ "$mode" == '--plan' || "$mode" == '--execute' ]] || fail 'Modo inválido.'
[[ "$max_pages" =~ ^[1-5]$ ]] || fail 'O piloto aceita de uma a cinco páginas.'
[[ "$page_timeout_seconds" =~ ^([5-9]|[12][0-9]|30)$ ]] || fail 'O prazo por página deve estar entre 5 e 30 segundos.'
[[ "$max_execution_seconds" =~ ^[0-9]+$ && "$lease_seconds" =~ ^[0-9]+$ ]] || fail 'Os prazos precisam ser inteiros positivos.'
(( max_execution_seconds >= 30 && max_execution_seconds <= 240 )) || fail 'A duração máxima deve estar entre 30 e 240 segundos.'
(( lease_seconds >= max_execution_seconds + 30 )) || fail 'O lease deve exceder a duração do piloto em pelo menos 30 segundos.'

for command in docker curl df free sha256sum stat awk; do require_command "$command"; done
assert_environment
assert_target_is_pending_read_only
if [[ "$mode" == '--plan' ]]; then
  print_plan
  exit 0
fi
[[ "$confirmation" == 'EXECUTAR-PILOTO-HTML' ]] || fail 'Confirmação explícita do piloto ausente.'
run_pilot
