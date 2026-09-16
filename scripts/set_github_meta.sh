#!/usr/bin/env bash
# Пометить репозиторий как «в активной разработке / pre-alpha» через GitHub API.
#
# Токен берётся из GITHUB_TOKEN/GH_TOKEN либо из файла .gh_token в корне репо.
# Нужные права токена (fine-grained): Administration: Read and write,
# Contents: Read and write.
#
# Использование:
#   ./scripts/set_github_meta.sh
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "${ROOT}"

TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
if [ -z "${TOKEN}" ] && [ -f .gh_token ]; then
    TOKEN=$(tr -d '\r\n' < .gh_token)
fi
if [ -z "${TOKEN}" ]; then
    echo "Ошибка: нет токена. Задайте GITHUB_TOKEN/GH_TOKEN или создайте .gh_token" >&2
    exit 1
fi

REMOTE=$(git remote get-url origin)
SLUG=$(printf '%s' "$REMOTE" | sed -E 's#(git@|https://)github.com[:/]##; s#\.git$##')
OWNER=${SLUG%%/*}
REPO=${SLUG##*/}
echo "[+] Репозиторий: ${OWNER}/${REPO}"

api() { curl -sS -H "Authorization: Bearer ${TOKEN}" -H "Accept: application/vnd.github+json" "$@"; }

DESC='🚧 pre-alpha — кроссплатформенный ВКС-клиент (SIP/H.323). Активная разработка, функции не завершены.'

# 1. Описание репозитория
api -X PATCH "https://api.github.com/repos/${OWNER}/${REPO}" \
    -d "$(python3 -c 'import json,sys; print(json.dumps({"description": sys.argv[1]}))' "$DESC")" \
    -o /dev/null -w 'description: HTTP %{http_code}\n'

# 2. Topics
api -X PUT "https://api.github.com/repos/${OWNER}/${REPO}/topics" \
    -d '{"names":["pre-alpha","work-in-progress","videoconference","sip","h323","mcu","pjsip","qt","python"]}' \
    -o /dev/null -w 'topics: HTTP %{http_code}\n'

# 3. Пометить последний релиз как pre-release (если есть)
REL_ID=$(api "https://api.github.com/repos/${OWNER}/${REPO}/releases/latest" \
    | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("id",""))' 2>/dev/null || true)
if [ -n "${REL_ID}" ]; then
    api -X PATCH "https://api.github.com/repos/${OWNER}/${REPO}/releases/${REL_ID}" \
        -d '{"prerelease": true}' \
        -o /dev/null -w 'release prerelease: HTTP %{http_code}\n'
else
    echo "[i] Релизов пока нет — пометку pre-release пропускаю"
fi

echo "[+] Готово"
