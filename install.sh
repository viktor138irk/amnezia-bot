#!/bin/bash

# Конфигурация. Любое значение можно переопределить переменной окружения.
SERVICE_NAME="${AWG_SERVICE_NAME:-awg_bot}"

# Каталог установки бота (по умолчанию — как в прежних версиях).
BASE_DIR="${AWG_BOT_HOME:-/root/amnezia-bot}"
PARENT_DIR="$(dirname "$BASE_DIR")"
BOT_DIRNAME="$(basename "$BASE_DIR")"

# Репозиторий: берём origin уже установленной копии, иначе значение по умолчанию.
DEFAULT_REPO_URL="https://github.com/viktor138irk/amnezia-bot.git"
if [[ -z "$AWG_REPO_URL" && -d "$BASE_DIR/.git" ]]; then
    AWG_REPO_URL="$(git -C "$BASE_DIR" remote get-url origin 2>/dev/null)"
fi
REPO_URL="${AWG_REPO_URL:-$DEFAULT_REPO_URL}"
REPO_BRANCH="${AWG_REPO_BRANCH:-main}"
# https://github.com/<owner>/<repo>.git -> https://api.github.com/repos/<owner>/<repo>
REPO_API="https://api.github.com/repos/$(echo "$REPO_URL" | sed -E 's#^.*github\.com[:/]##; s#\.git$##')"
LOCAL_VERSION_FILE="$BASE_DIR/.version"

# Python: нужен 3.10+, конкретная минорная версия не важна.
PYTHON_BIN="${AWG_PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
    for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$candidate" &>/dev/null; then PYTHON_BIN="$candidate"; break; fi
    done
fi

# Цвета для вывода
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
RED=$'\033[0;31m'
BLUE=$'\033[0;34m'
NC=$'\033[0m'

ENABLE_LOGS=true
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_NAME="$(basename "$0")"
SCRIPT_PATH="$SCRIPT_DIR/$SCRIPT_NAME"

# Проверка прав суперпользователя
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Этот скрипт должен быть запущен с правами суперпользователя (root)${NC}"
    exit 1
fi

# Функция для вывода ошибок и завершения
error_exit() {
    echo -e "${RED}Ошибка: $1${NC}" >&2
    exit 1
}

# Функция запуска команд с индикатором
run_with_spinner() {
    local description="$1"; shift
    local cmd="$@"
    if [ "$ENABLE_LOGS" = true ]; then
        echo -e "${BLUE}${description}...${NC}"
        eval "$cmd" 2>&1
        local stat=$?
        if [ $stat -eq 0 ]; then echo -e "${GREEN}${description}... Done!${NC}\n"; else echo -e "${RED}${description}... Failed!${NC}\n"; error_exit "$cmd"; fi
    else
        local out=$(mktemp) err=$(mktemp)
        eval "$cmd" >"$out" 2>"$err" & pid=$!
        local spinner='|/-\\' i=0
        while kill -0 "$pid" 2>/dev/null; do printf "\r${BLUE}${description}...${NC} ${spinner:i++%${#spinner}:1}"; sleep 0.1; done
        wait "$pid"; stat=$?
        if [ $stat -eq 0 ]; then printf "\r${BLUE}${description}...${NC} ${GREEN}Done!${NC}\n\n"; else printf "\r${BLUE}${description}...${NC} ${RED}Failed!${NC}\n\n"; echo -e "${RED}Ошибка: $cmd${NC}"; cat "$err"; rm -f "$out" "$err"; error_exit "$cmd"; fi
        rm -f "$out" "$err"
    fi
}

# Функция проверки обновлений на GitHub
check_github_updates() {
    local current_sha local_sha latest_sha auto_mode="$1"
    cd "$BASE_DIR" || { echo -e "${RED}Каталог $BASE_DIR не найден${NC}"; return 1; }
    
    echo -e "${YELLOW}Текущая директория: $(pwd)${NC}"
    echo -e "${YELLOW}Содержимое $BASE_DIR:${NC}"
    ls -la
    
    # Получение текущего SHA коммита
    local_sha=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
    
    # Получение последнего коммита через GitHub API
    if command -v curl &>/dev/null; then
        latest_sha=$(curl -s "$REPO_API/commits/$REPO_BRANCH" | jq -r '.sha' 2>/dev/null)
        [[ -z "$latest_sha" ]] && { echo -e "${RED}Не удалось получить данные с GitHub${NC}"; cd ..; return 1; }
    else
        echo -e "${RED}curl не установлен${NC}"; cd ..; return 1
    fi
    
    # Сравнение версий
    if [[ "$local_sha" == "$latest_sha" ]]; then
        echo -e "${GREEN}Репозиторий актуален (SHA: $local_sha)${NC}"
        cd ..; return 0
    fi
    
    echo -e "${YELLOW}Доступно обновление (текущий SHA: $local_sha, последний SHA: $latest_sha)${NC}"
    if [[ "$auto_mode" == "--auto" ]]; then
        # Проверка на наличие локальных изменений
        if git status --porcelain | grep -q .; then
            echo -e "${YELLOW}Обнаружены локальные изменения. Сбрасываем их...${NC}"
            run_with_spinner "Сброс локальных изменений" "git reset --hard && git clean -fd"
        fi
        run_with_spinner "Обновление репозитория" "git pull origin $REPO_BRANCH"
        echo "$latest_sha" > "$LOCAL_VERSION_FILE"
        check_script_update
        # Проверка и создание виртуального окружения, если отсутствует
        if [[ ! -d "myenv" ]]; then
            run_with_spinner "Создание виртуального окружения" "$PYTHON_BIN -m venv myenv"
        fi
        run_with_spinner "Обновление Python-зависимостей" "source myenv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt && deactivate"
        run_with_spinner "Перезапуск службы" "systemctl restart $SERVICE_NAME"
    else
        echo -ne "${BLUE}1) Установить 2) Отменить: ${NC}"; read choice
        if [[ "$choice" == "1" ]]; then
            if git status --porcelain | grep -q .; then
                echo -e "${YELLOW}Обнаружены локальные изменения. Сбрасываем их...${NC}"
                run_with_spinner "Сброс локальных изменений" "git reset --hard && git clean -fd"
            fi
            run_with_spinner "Обновление репозитория" "git pull origin $REPO_BRANCH"
            echo "$latest_sha" > "$LOCAL_VERSION_FILE"
            check_script_update
            # Проверка и создание виртуального окружения, если отсутствует
            if [[ ! -d "myenv" ]]; then
                run_with_spinner "Создание виртуального окружения" "$PYTHON_BIN -m venv myenv"
            fi
            run_with_spinner "Обновление Python-зависимостей" "source myenv/bin/activate && pip install --upgrade pip && pip install -r requirements.txt && deactivate"
            run_with_spinner "Перезапуск службы" "systemctl restart $SERVICE_NAME"
        else
            echo -e "${YELLOW}Обновление отменено${NC}"
        fi
    fi
    cd ..
}

# Проверка обновления самого скрипта
check_script_update() {
    local temp_script=$(mktemp)
    if [[ -f "$BASE_DIR/install.sh" ]]; then
        cp "$BASE_DIR/install.sh" "$temp_script"
        if ! cmp -s "$SCRIPT_PATH" "$temp_script"; then
            echo -e "${YELLOW}Обнаружено обновление скрипта install.sh${NC}"
            run_with_spinner "Обновление скрипта" "mv $temp_script $SCRIPT_PATH && chmod +x $SCRIPT_PATH"
            echo -e "${GREEN}Скрипт обновлён, перезапускаю...${NC}"
            exec "$SCRIPT_PATH" --check-update
        else
            rm -f "$temp_script"
        fi
    else
        echo -e "${YELLOW}Скрипт install.sh не найден в репозитории, копируем локальную версию${NC}"
        cp "$SCRIPT_PATH" "$BASE_DIR/install.sh"
        chmod +x "$BASE_DIR/install.sh"
    fi
}

# Проверка и применение обновлений
check_updates() {
    # Проверка и установка jq, если отсутствует
    if ! command -v jq &>/dev/null; then
        echo -e "${YELLOW}jq не установлен. Устанавливаем jq...${NC}"
        run_with_spinner "Установка jq" "apt-get update -qq && apt-get install -y jq -qq"
        if ! command -v jq &>/dev/null; then
            error_exit "Не удалось установить jq"
        fi
    fi

    # Проверка остальных зависимостей
    if [[ -z "$PYTHON_BIN" ]]; then
        echo -e "${RED}Python 3 не найден. Установите python3 (3.10 или новее).${NC}"
        exit 1
    fi
    for cmd in git curl "$PYTHON_BIN"; do
        if ! command -v "$cmd" &>/dev/null; then
            echo -e "${RED}$cmd не установлен. Установите $cmd для продолжения.${NC}"
            exit 1
        fi
    done

    # Проверка доступности репозитория
    echo -e "${YELLOW}Проверка доступности репозитория...${NC}"
    if ! curl -s -I "$REPO_URL" | grep -q "HTTP/.* 200"; then
        error_exit "Репозиторий $REPO_URL недоступен. Проверьте URL или сетевое подключение."
    fi

    # Проверка и клонирование репозитория, если он отсутствует
    if [[ ! -d "$BASE_DIR/.git" ]]; then
        echo -e "${YELLOW}Репозиторий не найден. Проверяем $BASE_DIR...${NC}"
        echo -e "${YELLOW}Содержимое $PARENT_DIR:${NC}"
        ls -la "$PARENT_DIR"
        if [[ -d "$BASE_DIR" ]]; then
            echo -e "${YELLOW}Директория $BASE_DIR существует, но не является git-репозиторием. Удаляем её...${NC}"
            rm -rf $BASE_DIR || error_exit "Не удалось удалить поврежденную директорию $BASE_DIR"
        fi
        echo -e "${YELLOW}Клонируем репозиторий...${NC}"
        mkdir -p "$PARENT_DIR" || error_exit "Не удалось создать $PARENT_DIR"
        cd "$PARENT_DIR" || error_exit "Не удалось перейти в $PARENT_DIR"
        run_with_spinner "Клонирование репозитория" "git clone -b $REPO_BRANCH $REPO_URL $BOT_DIRNAME"
        cd "$BOT_DIRNAME" || error_exit "Не удалось перейти в каталог $BOT_DIRNAME"
        if [[ ! -d ".git" ]]; then
            error_exit "Клонирование репозитория не удалось. Проверьте доступ к $REPO_URL"
        fi
        echo -e "${YELLOW}Содержимое $BASE_DIR после клонирования:${NC}"
        ls -la
    fi
    check_github_updates "$1"
}

# Точка входа
if [[ "$1" == "--check-update" ]]; then
    check_updates --auto
else
    check_updates
fi
