#!/usr/bin/env bash
# gtop - установщик: копирует gtop.py в PATH как команду `gtop`.
#
#   ./install.sh                 в /usr/local/bin (через sudo/doas, если нужно)
#   ./install.sh --user          в ~/.local/bin, без root
#   ./install.sh --prefix DIR    в DIR/bin
#   ./install.sh --uninstall     удалить
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SRC_DIR/gtop.py"
NAME="gtop"

MODE="auto"        # auto | user | prefix
ACTION="install"   # install | uninstall
PREFIX=""
FORCE=0

if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_OFF=""
fi
ok()   { printf '%s==>%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%s!!%s  %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
err()  { printf '%sxx%s  %s\n' "$C_ERR" "$C_OFF" "$*" >&2; }

usage() {
  cat <<USAGE
Использование: ./install.sh [опции]

  (без опций)      поставить в /usr/local/bin (через sudo/doas, если не root)
  --user           поставить в ~/.local/bin, без root
  --prefix DIR     поставить в DIR/bin
  --force          перезаписать чужой файл с таким же именем
  --uninstall      удалить gtop
  -h, --help       эта справка
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --user)       MODE="user" ;;
    --prefix)     MODE="prefix"; shift; PREFIX="${1:-}"
                  [ -n "$PREFIX" ] || { err "после --prefix нужен путь"; exit 2; } ;;
    --prefix=*)   MODE="prefix"; PREFIX="${1#--prefix=}" ;;
    --force)      FORCE=1 ;;
    --uninstall)  ACTION="uninstall" ;;
    -h|--help)    usage; exit 0 ;;
    *)            err "неизвестная опция: $1"; usage; exit 2 ;;
  esac
  shift
done

# ---------- вспомогательное ----------
is_ours() {   # наш ли это файл (а не чужой `gtop`, например из npm)
  [ -f "$1" ] && head -c 4000 "$1" 2>/dev/null | grep -q 'gtop - '
}

nearest_existing() {
  local d="$1"
  while [ ! -e "$d" ] && [ "$d" != "/" ]; do d="$(dirname "$d")"; done
  printf '%s' "$d"
}

pick_priv_tool() {
  if command -v sudo >/dev/null 2>&1; then echo sudo
  elif command -v doas >/dev/null 2>&1; then echo doas
  fi
}

SUDO=""
priv() { if [ -n "$SUDO" ]; then "$SUDO" "$@"; else "$@"; fi; }

# ---------- куда ставить ----------
case "$MODE" in
  user)   DEST="$HOME/.local/bin" ;;
  prefix) DEST="${PREFIX%/}/bin" ;;
  auto)   DEST="/usr/local/bin" ;;
esac

if [ "$MODE" != "user" ] && [ "$(id -u)" -ne 0 ] && [ ! -w "$(nearest_existing "$DEST")" ]; then
  SUDO="$(pick_priv_tool)"
  if [ -z "$SUDO" ]; then
    if [ "$MODE" = "auto" ]; then
      warn "нет прав root, а sudo/doas не найдены - ставлю в ~/.local/bin"
      DEST="$HOME/.local/bin"
    else
      err "нет прав на запись в $DEST, а sudo/doas не найдены"; exit 1
    fi
  fi
fi

TARGET="$DEST/$NAME"

# ---------- удаление ----------
if [ "$ACTION" = "uninstall" ]; then
  removed=0
  for f in "$TARGET" "/usr/local/bin/$NAME" "$HOME/.local/bin/$NAME"; do
    if is_ours "$f"; then
      if [ -w "$(dirname "$f")" ]; then rm -f "$f"; else
        [ -n "$SUDO" ] || SUDO="$(pick_priv_tool)"
        priv rm -f "$f"
      fi
      ok "удалено: $f"; removed=1
    fi
  done
  [ "$removed" -eq 1 ] || warn "установленный gtop не найден"
  exit 0
fi

# ---------- проверки ----------
[ -f "$SRC" ] || { err "не нашёл $SRC (запускай install.sh из папки репозитория)"; exit 1; }

command -v python3 >/dev/null 2>&1 || { err "python3 не найден"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || { err "нужен Python 3.8 или новее"; exit 1; }
python3 -c 'import curses' 2>/dev/null || {
  err "в Python нет модуля curses."
  err "Gentoo: включи USE=ncurses для dev-lang/python и пересобери его."
  exit 1
}

if ! python3 - <<'PY' 2>/dev/null
import locale, sys
try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    sys.exit(1)
sys.exit(0 if "UTF" in locale.getpreferredencoding().upper() else 1)
PY
then
  warn "текущая локаль не UTF-8, кольца могут не отобразиться."
  warn "Gentoo: eselect locale list  &&  eselect locale set <номер с UTF-8>"
fi

if [ -e "$TARGET" ] && ! is_ours "$TARGET" && [ "$FORCE" -ne 1 ]; then
  err "$TARGET уже существует и это не наш gtop. Используй --force, чтобы перезаписать."
  exit 1
fi

# ---------- установка ----------
priv install -d "$DEST"
priv install -m 755 "$SRC" "$TARGET"

"$TARGET" --help >/dev/null 2>&1 || { err "установленный файл не запускается: $TARGET"; exit 1; }

ok "gtop установлен: $TARGET"
case ":$PATH:" in
  *":$DEST:"*) ;;
  *) warn "$DEST нет в PATH. Добавь в ~/.bashrc:"
     warn "    export PATH=\"$DEST:\$PATH\""
     warn "(для fish: fish_add_path $DEST)" ;;
esac
echo
echo "Запуск:   gtop            (серая тема: gtop --gray)"
echo "Удалить:  ./install.sh --uninstall"
