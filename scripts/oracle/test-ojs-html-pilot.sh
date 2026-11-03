#!/usr/bin/env bash
# Integração do modo --plan sem Docker real: substitui somente os comandos do
# host e prova que o script não pede criação de contêiner persistente.
set -Eeuo pipefail
IFS=$'\n\t'

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
pilot_script="$script_directory/run-ojs-html-pilot.sh"
work_directory="$(mktemp -d "${TMPDIR:-/tmp}/analisys-html-pilot-test.XXXXXX")"
trap 'rm -rf -- "$work_directory"' EXIT
fake_bin="$work_directory/bin"
backup_directory="$work_directory/backup"
docker_log="$work_directory/docker.log"
mkdir -p "$fake_bin" "$backup_directory"
env_file="$work_directory/analisys.env"
printf '%s\n' 'TEST_ONLY=1' > "$env_file"
printf '%s\n' 'backup-content' > "$backup_directory/outputs.tar.gz"
checksum="$(sha256sum "$backup_directory/outputs.tar.gz" | awk '{print $1}')"
printf '%s  %s\n' "$checksum" 'outputs.tar.gz' > "$backup_directory/outputs.tar.gz.sha256"

cat > "$fake_bin/docker" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%q ' "$@" >> "$DOCKER_LOG"
printf '\n' >> "$DOCKER_LOG"
case "${1:-}" in
  inspect)
    if [[ " $* " == *'{{.State.Running}}'* ]]; then printf '%s\n' true
    elif [[ " $* " == *'{{.Architecture}}'* ]]; then printf '%s\n' amd64
    fi
    ;;
  volume) ;;
  image)
    if [[ " $* " == *'{{.Architecture}}'* ]]; then printf '%s\n' amd64; fi
    ;;
  ps) ;;
  stats) printf '%s\n' '32MiB / 320MiB|1.0%|8' ;;
  run)
    [[ " $* " == *' --rm '* && " $* " == *' --network none '* && " $* " == *'readonly'* ]] || exit 61
    printf '%s\n' pending
    ;;
  *) exit 62 ;;
esac
EOF
cat > "$fake_bin/curl" <<'EOF'
#!/usr/bin/env bash
printf '%s' 302
EOF
cat > "$fake_bin/df" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' 'Avail'
printf '%s\n' 21474836480
EOF
cat > "$fake_bin/free" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' 'Mem: 1000000000 1 1 1 1 600000000'
printf '%s\n' 'Swap: 4000000000 0 4000000000'
EOF
cat > "$fake_bin/stat" <<'EOF'
#!/usr/bin/env bash
case "$2" in
  %a) printf '%s\n' 600 ;;
  %U:%G) printf '%s\n' root:root ;;
  *) exit 63 ;;
esac
EOF
cat > "$fake_bin/hostname" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' instance-20260923-1635
EOF
cat > "$fake_bin/id" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == '-u' ]] && { printf '%s\n' 0; exit 0; }
exit 64
EOF
chmod 700 "$fake_bin"/*

PATH="$fake_bin:$PATH" DOCKER_LOG="$docker_log" "$pilot_script" --plan \
  --audit-run-id '11111111-2222-4333-8444-555555555555' \
  --image 'ghcr.io/fabiomoraescs/analisys@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  --env-file "$env_file" \
  --backup-dir "$backup_directory" > "$work_directory/plan.out"
grep -Fxq 'Plano validado: nenhuma alteração foi aplicada.' "$work_directory/plan.out"
grep -Fq 'run --rm' "$docker_log"
! grep -Eq -- '--detach|--restart|--name' "$docker_log"

: > "$docker_log"
if PATH="$fake_bin:$PATH" DOCKER_LOG="$docker_log" "$pilot_script" --execute \
  --audit-run-id '11111111-2222-4333-8444-555555555555' \
  --image 'ghcr.io/fabiomoraescs/analisys@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' \
  --env-file "$env_file" --backup-dir "$backup_directory" > /dev/null 2>&1; then
  printf '%s\n' 'modo execute sem confirmação foi aceito' >&2
  exit 65
fi
! grep -Eq -- '--detach|--restart|--name' "$docker_log"
printf '%s\n' 'html_pilot_plan_test=ok'
