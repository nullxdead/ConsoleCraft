#!/bin/sh
# Console Launcher user CLI installer; no administrator privileges required.
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
BIN_DIR="$HOME/.local/bin"
COMMAND="$BIN_DIR/consolelauncher"
STATE_DIR="$HOME/.local/share/consolelauncher-cli"
PATH_STATE="$STATE_DIR/path-state"
STATE_MARKER='ConsoleLauncher-CLI-PATH-v1'
BLOCK_START='# >>> Console Launcher CLI PATH >>>'
BLOCK_END='# <<< Console Launcher CLI PATH <<<'
PATH_LINE='case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) export PATH="$HOME/.local/bin:$PATH" ;; esac'

path_block() {
    printf '%s\n' "$BLOCK_START"
    if [ "$1" = fish ]; then
        printf '%s\n' 'if not contains -- "$HOME/.local/bin" $PATH' '    set -gx PATH "$HOME/.local/bin" $PATH' 'end'
    else
        printf '%s\n' "$PATH_LINE"
    fi
    printf '%s\n' "$BLOCK_END"
}

configure_path() {
    case ":$PATH:" in *":$BIN_DIR:"*) return ;; esac
    if [ -e "$PATH_STATE" ] && [ "$(sed -n '1p' "$PATH_STATE")" != "$STATE_MARKER" ]; then
        printf '%s\n' "$PATH_STATE занят другой программой. PATH не изменён." >&2
        exit 1
    fi
    path_style=posix
    selected_shell=${SHELL:-sh}
    case "${selected_shell##*/}" in
        zsh) shell_config="$HOME/.zshrc" ;;
        bash)
            if [ "$(uname -s)" = Darwin ]; then shell_config="$HOME/.bash_profile"
            else shell_config="$HOME/.bashrc"; fi ;;
        fish) shell_config="$HOME/.config/fish/conf.d/consolelauncher-path.fish"; path_style=fish ;;
        *) shell_config="$HOME/.profile" ;;
    esac
    case "$shell_config" in *'
'*) printf '%s\n' 'Неподдерживаемый перевод строки в пути настройки оболочки.' >&2; exit 1 ;; esac
    if [ -e "$shell_config" ] && grep -Fqx "$BLOCK_START" "$shell_config"; then
        printf '%s\n' "PATH уже настроен в $shell_config. Откройте новый терминал."
        return
    fi
    if [ "$path_style" = fish ] && [ -e "$shell_config" ]; then
        printf '%s\n' "$shell_config уже существует. Чужой файл не изменён." >&2
        exit 1
    fi
    existed=no
    no_final_newline=no
    if [ -e "$shell_config" ]; then
        existed=yes
        if [ -s "$shell_config" ] && [ -n "$(tail -c 1 "$shell_config")" ]; then no_final_newline=yes; fi
    fi
    mkdir -p "$(dirname "$shell_config")" "$STATE_DIR"
    if [ "$no_final_newline" = yes ]; then printf '\n' >> "$shell_config"; fi
    path_block "$path_style" >> "$shell_config"
    # The state contains only ownership and file-format information, never shell contents.
    (umask 077; printf '%s\n' "$STATE_MARKER" "$shell_config" "$path_style" "$existed" "$no_final_newline" > "$PATH_STATE")
    printf '%s\n' "Пользовательский PATH настроен в $shell_config. Откройте новый терминал."
}

unconfigure_path() {
    [ -f "$PATH_STATE" ] || return 0
    [ "$(sed -n '1p' "$PATH_STATE")" = "$STATE_MARKER" ] || return 0
    shell_config=$(sed -n '2p' "$PATH_STATE")
    path_style=$(sed -n '3p' "$PATH_STATE")
    existed=$(sed -n '4p' "$PATH_STATE")
    no_final_newline=$(sed -n '5p' "$PATH_STATE")
    case "$shell_config" in
        "$HOME/.zshrc"|"$HOME/.bashrc"|"$HOME/.bash_profile"|"$HOME/.profile"|"$HOME/.config/fish/conf.d/consolelauncher-path.fish") ;;
        *) printf '%s\n' 'Неизвестный путь в состоянии установщика; файлы не изменены.' >&2; return ;;
    esac
    if [ ! -f "$shell_config" ]; then rm "$PATH_STATE"; return; fi
    temporary=$(mktemp "$shell_config.consolelauncher.XXXXXX")
    if [ "$path_style" = fish ]; then
        path_block fish > "$temporary"
        if cmp -s "$temporary" "$shell_config"; then rm "$shell_config" "$PATH_STATE"
        else printf '%s\n' "Настройка $shell_config изменена пользователем и сохранена."; fi
        rm "$temporary"
        return
    fi
    # Remove only the exact three-line block owned by this installer.
    if ! awk -v first="$BLOCK_START" -v middle="$PATH_LINE" -v last="$BLOCK_END" '
        function flush() { if (buffer != "") printf "%s", buffer; buffer=""; state=0 }
        state == 0 && $0 == first { buffer=$0 ORS; state=1; next }
        state == 1 { buffer=buffer $0 ORS; if ($0 == middle) {state=2; next}; flush(); next }
        state == 2 { if ($0 == last) {buffer=""; state=0; removed=1; next}; buffer=buffer $0 ORS; flush(); next }
        { print }
        END { flush(); if (!removed) exit 4 }
    ' "$shell_config" > "$temporary"; then
        rm "$temporary"
        printf '%s\n' "Блок PATH в $shell_config изменён пользователем и сохранён."
        return
    fi
    if [ "$no_final_newline" = yes ] && [ "$(tail -n 1 "$shell_config")" = "$BLOCK_END" ] && [ -s "$temporary" ]; then
        # Restore an original file that had no final newline.
        bytes=$(wc -c < "$temporary")
        trimmed=$(mktemp "$shell_config.consolelauncher.XXXXXX")
        dd if="$temporary" of="$trimmed" bs=1 count=$((bytes - 1)) 2>/dev/null
        cat "$trimmed" > "$temporary"
        rm "$trimmed"
    fi
    if [ "$existed" = no ] && [ ! -s "$temporary" ]; then rm "$shell_config"
    else cat "$temporary" > "$shell_config"; fi
    rm "$temporary" "$PATH_STATE"
}

INSTALLED="$STATE_DIR/bin/consolelauncher"
BINARY_OWNER="$STATE_DIR/binary-owner"
if [ "${1:-}" = '--uninstall' ]; then
    if [ -L "$COMMAND" ]; then
        target=$(readlink "$COMMAND")
        if [ "$target" = "$INSTALLED" ]; then
            rm "$COMMAND"
            printf '%s\n' 'Команда consolelauncher удалена. Игра и настройки сохранены.'
        else printf '%s\n' 'Ссылка принадлежит другой установке и сохранена.'; exit 1; fi
    elif [ -e "$COMMAND" ]; then
        printf '%s\n' 'Команда принадлежит другой программе и сохранена.'; exit 1
    fi
    if [ -f "$BINARY_OWNER" ] && [ "$(cat "$BINARY_OWNER")" = 'ConsoleLauncher-CLI-v4' ]; then rm -f "$INSTALLED" "$BINARY_OWNER"; fi
    unconfigure_path
    exit 0
fi
if [ -f "$APP_DIR/consolelauncher" ]; then
    EXECUTABLE="$APP_DIR/consolelauncher"
else
    printf '%s\n' 'Не найден Console Launcher рядом с установщиком.' >&2
    exit 1
fi
if [ -e "$COMMAND" ] || [ -L "$COMMAND" ]; then
    if [ ! -L "$COMMAND" ] || [ "$(readlink "$COMMAND")" != "$INSTALLED" ]; then
        printf '%s\n' "$COMMAND уже занят. Файл не изменён." >&2
        exit 1
    fi
else
    mkdir -p "$BIN_DIR"
fi
if [ -e "$INSTALLED" ] && { [ ! -f "$BINARY_OWNER" ] || [ "$(cat "$BINARY_OWNER")" != 'ConsoleLauncher-CLI-v4' ]; }; then
    printf '%s\n' "$INSTALLED уже занят. Файл не изменён." >&2; exit 1
fi
mkdir -p "$(dirname "$INSTALLED")"
if [ -e "$INSTALLED.tmp" ] || [ -L "$INSTALLED.tmp" ]; then printf '%s\n' 'Временный файл занят другой программой.' >&2; exit 1; fi
cp "$EXECUTABLE" "$INSTALLED.tmp"
chmod 755 "$INSTALLED.tmp"
mv "$INSTALLED.tmp" "$INSTALLED"
printf '%s\n' 'ConsoleLauncher-CLI-v4' > "$BINARY_OWNER"
if [ ! -L "$COMMAND" ]; then ln -s "$INSTALLED" "$COMMAND"; fi
configure_path
printf '%s\n' 'Установлена команда consolelauncher. Консольное меню: consolelauncher cli'
