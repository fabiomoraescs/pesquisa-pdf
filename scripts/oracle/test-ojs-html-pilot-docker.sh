#!/usr/bin/env bash
# Real Docker Engine integration harness for run-ojs-html-pilot.sh.
#
# It is intentionally CI-only: it creates the exact container and volume names
# used by the production procedure, but only on an ephemeral runner. The Docker
# wrapper changes *only* the immutable production image reference to a small
# local fixture. Every container, volume, cgroup/resource limit and network
# operation is otherwise handled by the real Docker Engine.
set -Eeuo pipefail
IFS=$'\n\t'

[[ "${CI:-}" == 'true' || "${ANALISYS_HTML_PILOT_ALLOW_LOCAL_DOCKER_TEST:-}" == '1' ]] \
  || { printf '%s\n' 'Refusing to run the Docker harness outside CI without explicit opt-in.' >&2; exit 64; }

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
pilot_script="$script_directory/run-ojs-html-pilot.sh"
fixture_image="${PILOT_FIXTURE_IMAGE:-analisys-html-pilot-fixture:ci}"
production_image='ghcr.io/fabiomoraescs/analisys@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
audit_run_id='11111111-2222-4333-8444-555555555555'
web_container='pesquisa-pdf'
oai_container='pesquisa-pdf-ojs-worker'
volume_name='pesquisa-pdf-data'
pilot_prefix='pesquisa-pdf-ojs-html-pilot-'
real_docker="${DOCKER_REAL:-$(command -v docker)}"
work_directory="$(mktemp -d "${TMPDIR:-/tmp}/analisys-html-pilot-docker.XXXXXX")"
fake_bin="$work_directory/bin"
backup_directory="$work_directory/backup"
env_file="$work_directory/analisys-web-ojs.env"

cleanup() {
  local name
  for name in "$web_container" "$oai_container"; do
    "$real_docker" ps -aq --filter "name=^/${name}$" | xargs -r "$real_docker" rm -f >/dev/null 2>&1 || true
  done
  "$real_docker" ps -aq --filter "name=^/${pilot_prefix}" | xargs -r "$real_docker" rm -f >/dev/null 2>&1 || true
  "$real_docker" volume inspect "$volume_name" >/dev/null 2>&1 && "$real_docker" volume rm "$volume_name" >/dev/null 2>&1 || true
  if [[ "${CI:-}" == 'true' ]]; then
    sudo rm -rf -- /var/lib/analisys-html-pilots
  fi
  rm -rf -- "$work_directory"
}
trap cleanup EXIT

require() {
  command -v "$1" >/dev/null 2>&1 || { printf 'Missing required command: %s\n' "$1" >&2; exit 65; }
}

for command in "$real_docker" sudo python3 tar sha256sum grep awk; do require "$command"; done
"$real_docker" info >/dev/null
"$real_docker" image inspect "$fixture_image" >/dev/null
mkdir -p "$fake_bin" "$backup_directory/payload/platform"

cat > "$fake_bin/hostname" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' instance-20260923-1635
EOF
cat > "$fake_bin/free" <<'EOF'
#!/usr/bin/env bash
if [[ "${PILOT_TEST_LOW_MEMORY:-0}" == '1' ]]; then
  printf '%s\n' 'Mem: 1000000000 1 1 1 1 470000000'
  printf '%s\n' 'Swap: 4000000000 0 4000000000'
  exit 0
fi
exec /usr/bin/free "$@"
EOF
cat > "$fake_bin/curl" <<'EOF'
#!/usr/bin/env bash
if [[ "${PILOT_TEST_HTTP_FAIL:-0}" == '1' ]]; then
  state_file="${PILOT_TEST_CURL_COUNTER:?}"
  count=0
  [[ -f "$state_file" ]] && count="$(<"$state_file")"
  count=$((count + 1))
  printf '%s\n' "$count" > "$state_file"
  if (( count > 1 )); then
    printf '%s' 500
  else
    printf '%s' 200
  fi
  exit 0
fi
exec /usr/bin/curl "$@"
EOF
cat > "$fake_bin/docker" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
real_docker="${DOCKER_REAL:?}"
production_image="${PILOT_PRODUCTION_IMAGE:?}"
fixture_image="${PILOT_FIXTURE_IMAGE:?}"
arguments=()
for argument in "$@"; do
  if [[ "$argument" == "$production_image" ]]; then
    arguments+=("$fixture_image")
  else
    arguments+=("$argument")
  fi
done
if [[ "${PILOT_TEST_DOCKER_RUN_FAILURE:-0}" == '1' && " ${arguments[*]} " == *' --detach '* ]]; then
  printf '%s\n' 'injected docker run failure' >&2
  exit 73
fi
exec "$real_docker" "${arguments[@]}"
EOF
chmod 700 "$fake_bin"/*

python3 - "$backup_directory/payload/platform/platform.sqlite3" <<'PY'
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
connection.execute("CREATE TABLE ojs_html_audit_runs (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
connection.commit()
connection.close()
PY
tar -C "$backup_directory/payload" -czf "$backup_directory/outputs.tar.gz" platform
(cd "$backup_directory" && sha256sum outputs.tar.gz > outputs.tar.gz.sha256)

"$real_docker" volume create "$volume_name" >/dev/null
"$real_docker" run --rm --interactive --mount "type=volume,src=${volume_name},dst=/app/outputs" "$fixture_image" \
  python - "$audit_run_id" <<'PY'
import sqlite3
import sys
from pathlib import Path

database_path = Path("/app/outputs/platform/platform.sqlite3")
database_path.parent.mkdir(parents=True, exist_ok=True)
connection = sqlite3.connect(database_path)
connection.execute("CREATE TABLE ojs_html_audit_runs (id TEXT PRIMARY KEY, status TEXT NOT NULL)")
connection.execute("INSERT INTO ojs_html_audit_runs (id, status) VALUES (?, 'pending')", (sys.argv[1],))
connection.commit()
connection.close()
PY
"$real_docker" run --detach --name "$web_container" --label analisys.html-pilot-test=true \
  --mount "type=volume,src=${volume_name},dst=/app/outputs" -p 127.0.0.1:5000:5000 \
  "$fixture_image" python -m http.server 5000 >/dev/null
"$real_docker" run --detach --name "$oai_container" --label analisys.html-pilot-test=true \
  --mount "type=volume,src=${volume_name},dst=/app/outputs" "$fixture_image" sleep infinity >/dev/null

for attempt in $(seq 1 20); do
  if /usr/bin/curl --silent --fail --output /dev/null http://127.0.0.1:5000/; then
    break
  fi
  if (( attempt == 20 )); then
    printf '%s\n' 'temporary web fixture did not become healthy' >&2
    exit 74
  fi
  sleep 0.5
done

printf '%s\n' 'PILOT_FIXTURE_EXIT_CODE=0' | sudo tee "$env_file" >/dev/null
sudo chown root:root "$env_file"
sudo chmod 600 "$env_file"

run_pilot() {
  local pilot_mode="$1"
  shift
  sudo env \
    "PATH=$fake_bin:$PATH" \
    "DOCKER_REAL=$real_docker" \
    "PILOT_PRODUCTION_IMAGE=$production_image" \
    "PILOT_FIXTURE_IMAGE=$fixture_image" \
    "PILOT_TEST_LOW_MEMORY=${PILOT_TEST_LOW_MEMORY:-0}" \
    "PILOT_TEST_HTTP_FAIL=${PILOT_TEST_HTTP_FAIL:-0}" \
    "PILOT_TEST_CURL_COUNTER=$work_directory/curl-count" \
    "PILOT_TEST_DOCKER_RUN_FAILURE=${PILOT_TEST_DOCKER_RUN_FAILURE:-0}" \
    /usr/bin/bash "$pilot_script" "$pilot_mode" \
      --audit-run-id "$audit_run_id" --image "$production_image" \
      --env-file "$env_file" --backup-dir "$backup_directory" "$@"
}

audit_status() {
  "$real_docker" run --rm --interactive --network none --mount "type=volume,src=${volume_name},dst=/app/outputs,readonly" \
    "$fixture_image" python - "$audit_run_id" <<'PY'
import sqlite3
import sys
connection = sqlite3.connect("file:/app/outputs/platform/platform.sqlite3?mode=ro", uri=True)
print(connection.execute("SELECT status FROM ojs_html_audit_runs WHERE id = ?", (sys.argv[1],)).fetchone()[0])
connection.close()
PY
}

audit_run_count() {
  "$real_docker" run --rm --interactive --network none --mount "type=volume,src=${volume_name},dst=/app/outputs,readonly" \
    "$fixture_image" python - <<'PY'
import sqlite3
connection = sqlite3.connect("file:/app/outputs/platform/platform.sqlite3?mode=ro", uri=True)
print(connection.execute("SELECT COUNT(*) FROM ojs_html_audit_runs").fetchone()[0])
connection.close()
PY
}

reset_audit() {
  "$real_docker" run --rm --interactive --mount "type=volume,src=${volume_name},dst=/app/outputs" "$fixture_image" \
    python - "$audit_run_id" <<'PY'
import sqlite3
import sys
connection = sqlite3.connect("/app/outputs/platform/platform.sqlite3")
connection.execute("UPDATE ojs_html_audit_runs SET status = 'pending' WHERE id = ?", (sys.argv[1],))
connection.commit()
connection.close()
PY
}

pilot_containers() {
  "$real_docker" ps -aq --filter "name=^/${pilot_prefix}"
}

remove_pilot_container() {
  pilot_containers | xargs -r "$real_docker" rm -f >/dev/null
}

run_pilot --plan > "$work_directory/plan.out"
grep -Fxq 'Plano validado: nenhuma alteração foi aplicada.' "$work_directory/plan.out"
[[ "$(audit_status)" == 'pending' ]]
[[ "$(audit_run_count)" == '1' ]]
[[ -z "$(pilot_containers)" ]]

if run_pilot --execute > /dev/null 2>&1; then
  printf '%s\n' 'execute without confirmation unexpectedly succeeded' >&2
  exit 66
fi
[[ -z "$(pilot_containers)" ]]

if run_pilot --plan --max-pages 6 > /dev/null 2>&1; then
  printf '%s\n' 'page limit above three was accepted' >&2
  exit 67
fi
[[ -z "$(pilot_containers)" ]]

if PILOT_TEST_LOW_MEMORY=1 run_pilot --plan > /dev/null 2>&1; then
  printf '%s\n' 'low host memory was accepted' >&2
  exit 68
fi
[[ -z "$(pilot_containers)" ]]

run_pilot --execute --confirm EXECUTAR-PILOTO-HTML > "$work_directory/execute.out"
pilot_id="$(pilot_containers)"
[[ -n "$pilot_id" ]]
resource_profile="$("$real_docker" inspect --format '{{.HostConfig.Memory}}|{{.HostConfig.MemoryReservation}}|{{.HostConfig.MemorySwap}}|{{.HostConfig.NanoCpus}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.RestartPolicy.Name}}|{{json .HostConfig.PortBindings}}' "$pilot_id")"
[[ "$resource_profile" == '335544320|201326592|536870912|500000000|96|no|null' || "$resource_profile" == '335544320|201326592|536870912|500000000|96|no|{}' ]] \
  || { printf 'unexpected real Docker resource profile: %s\n' "$resource_profile" >&2; exit 69; }
"$real_docker" inspect --format '{{json .Config.Cmd}}' "$pilot_id" | grep -Fq -- '--max-pages-per-job'
"$real_docker" inspect --format '{{json .Config.Cmd}}' "$pilot_id" | grep -Fq -- '3'
[[ "$(audit_status)" == 'partial' ]]
[[ "$(audit_run_count)" == '1' ]]
[[ "$("$real_docker" inspect --format '{{.State.ExitCode}}' "$pilot_id")" == '0' ]]
remove_pilot_container
reset_audit

printf '%s\n' 'PILOT_FIXTURE_EXIT_CODE=17' | sudo tee "$env_file" >/dev/null
if run_pilot --execute --confirm EXECUTAR-PILOTO-HTML > "$work_directory/exit-failure.out" 2>&1; then
  printf '%s\n' 'unexpected worker exit was accepted' >&2
  exit 70
fi
grep -Fq 'exit_code=17' "$work_directory/exit-failure.out" || true
remove_pilot_container
reset_audit

printf '%s\n' 'PILOT_FIXTURE_EXIT_CODE=0' | sudo tee "$env_file" >/dev/null
if PILOT_TEST_DOCKER_RUN_FAILURE=1 run_pilot --execute --confirm EXECUTAR-PILOTO-HTML > /dev/null 2>&1; then
  printf '%s\n' 'injected Docker start failure was accepted' >&2
  exit 71
fi
[[ -z "$(pilot_containers)" ]]

printf '%s\n%s\n' 'PILOT_FIXTURE_EXIT_CODE=0' 'PILOT_FIXTURE_SLEEP_SECONDS=15' | sudo tee "$env_file" >/dev/null
if PILOT_TEST_HTTP_FAIL=1 run_pilot --execute --confirm EXECUTAR-PILOTO-HTML > "$work_directory/http-failure.out" 2>&1; then
  printf '%s\n' 'web health failure was accepted' >&2
  exit 72
fi
sudo grep -Fq 'web_health_check_failed' /var/lib/analisys-html-pilots/*.report
remove_pilot_container
reset_audit

printf '%s\n%s\n' 'PILOT_FIXTURE_EXIT_CODE=0' 'PILOT_FIXTURE_SLEEP_SECONDS=120' | sudo tee "$env_file" >/dev/null
if run_pilot --execute --confirm EXECUTAR-PILOTO-HTML --max-execution-seconds 30 > "$work_directory/timeout.out" 2>&1; then
  printf '%s\n' 'pilot runtime timeout was accepted' >&2
  exit 73
fi
sudo grep -Fq 'pilot_runtime_limit' /var/lib/analisys-html-pilots/*.report
remove_pilot_container

printf '%s\n' 'html_pilot_real_docker_test=ok'
