#!/bin/sh
# Download a verified native build and install it for the current user.
set -eu
version=4.4.1
case "$(uname -s):$(uname -m)" in
    Linux:x86_64) asset="ConsoleLauncher-$version-Linux-x86_64.zip" ;;
    Darwin:arm64) asset="ConsoleLauncher-$version-Darwin-arm64.zip" ;;
    *) printf '%s\n' 'Для этой архитектуры используйте установку из исходников (Python 3.11+).' >&2; exit 1 ;;
esac
for tool in curl unzip; do command -v "$tool" >/dev/null || { printf '%s\n' "Требуется $tool." >&2; exit 1; }; done
base="https://github.com/nullxdead/ConsoleLauncher/releases/download/v$version"
temp_root=${TMPDIR:-/tmp}
temp_dir=$(mktemp -d "$temp_root/consolelauncher.XXXXXX")
trap 'case "$temp_dir" in "$temp_root"/consolelauncher.*) rm -rf "$temp_dir";; esac' EXIT HUP INT TERM
printf '%s\n' 'Скачивание Console Launcher...'
curl -fL --retry 2 "$base/$asset" -o "$temp_dir/$asset"
curl -fL --retry 2 "$base/SHA256SUMS.txt" -o "$temp_dir/SHA256SUMS.txt"
expected=$(awk -v file="$asset" '$2 == file { print $1 }' "$temp_dir/SHA256SUMS.txt")
[ ${#expected} -eq 64 ] || { printf '%s\n' 'Контрольная сумма не найдена.' >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then actual=$(sha256sum "$temp_dir/$asset" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then actual=$(shasum -a 256 "$temp_dir/$asset" | awk '{print $1}')
else printf '%s\n' 'Требуется sha256sum или shasum.' >&2; exit 1; fi
[ "$actual" = "$expected" ] || { printf '%s\n' 'Контрольная сумма не совпадает.' >&2; exit 1; }
unzip -q "$temp_dir/$asset" -d "$temp_dir/package"
sh "$temp_dir/package/install-cli.sh"
printf '%s\n' 'Готово. Откройте новый терминал и запустите consolelauncher.'
