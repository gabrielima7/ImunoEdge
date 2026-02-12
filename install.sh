#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# ImunoEdge — Instalador Automatizado
# ═══════════════════════════════════════════════════════════════
#
# Uso:
#   sudo bash install.sh
#
# O que este script faz:
#   1. Verifica se está rodando como root
#   2. Instala dependências do sistema (python3, python3-venv, git)
#   3. Cria o usuário de serviço 'imunoedge'
#   4. Copia o projeto para /opt/imunoedge
#   5. Cria virtual environment e instala dependências Python
#   6. Configura o arquivo .env
#   7. Instala e ativa o serviço Systemd
#
# ═══════════════════════════════════════════════════════════════

set -euo pipefail

# ─── Constantes ──────────────────────────────────────────────
readonly INSTALL_DIR="/opt/imunoedge"
readonly SERVICE_NAME="imunoedge"
readonly SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
readonly BUFFER_DIR="/var/lib/imunoedge"
readonly LOG_DIR="/var/log/imunoedge"
readonly SERVICE_USER="imunoedge"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR

# ─── Cores ───────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# ─── Funções de Log ──────────────────────────────────────────
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[  OK]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERRO]${NC}  $*" >&2; }
step()    { echo -e "\n${BOLD}═══ $* ═══${NC}"; }

# ─── Banner ──────────────────────────────────────────────────
banner() {
    echo -e "${CYAN}"
    cat << 'EOF'
    ╔══════════════════════════════════════════════╗
    ║   ImunoEdge Installer v0.1.0                ║
    ║   IoT Runtime com Autocura                  ║
    ║   🛡️  Powered by TaipanStack                ║
    ╚══════════════════════════════════════════════╝
EOF
    echo -e "${NC}"
}

# ─── Verificação de Root ─────────────────────────────────────
check_root() {
    step "Verificando permissões"
    if [[ $EUID -ne 0 ]]; then
        error "Este script precisa ser executado como root."
        error "Use: sudo bash install.sh"
        exit 1
    fi
    success "Executando como root"
}

# ─── Verificação do Diretório Fonte ─────────────────────────
check_source() {
    step "Verificando arquivos fonte"
    local missing=0

    for f in pyproject.toml imunoedge.service .env.example; do
        if [[ ! -f "${SCRIPT_DIR}/${f}" ]]; then
            error "Arquivo não encontrado: ${f}"
            missing=1
        fi
    done

    if [[ ! -d "${SCRIPT_DIR}/src/imunoedge" ]]; then
        error "Diretório src/imunoedge não encontrado"
        missing=1
    fi

    if [[ ! -d "${SCRIPT_DIR}/TaipanStack" ]]; then
        error "Submodule TaipanStack não encontrado"
        error "Execute: git submodule update --init --recursive"
        missing=1
    fi

    if [[ $missing -eq 1 ]]; then
        error "Arquivos fonte incompletos. Abortando."
        exit 1
    fi

    success "Todos os arquivos fonte encontrados"
}

# ─── Instalação de Dependências do Sistema ───────────────────
install_system_deps() {
    step "Instalando dependências do sistema"

    # Detecta distro
    if command -v apt-get &>/dev/null; then
        info "Distribuição baseada em Debian/Ubuntu detectada"
        apt-get update -qq
        apt-get install -y -qq python3 python3-venv python3-pip git >/dev/null 2>&1
    elif command -v dnf &>/dev/null; then
        info "Distribuição baseada em Fedora/RHEL detectada"
        dnf install -y -q python3 python3-pip git >/dev/null 2>&1
    elif command -v pacman &>/dev/null; then
        info "Distribuição baseada em Arch detectada"
        pacman -Sy --noconfirm python python-pip git >/dev/null 2>&1
    else
        warn "Gerenciador de pacotes não reconhecido."
        warn "Certifique-se de ter instalado: python3, python3-venv, pip, git"
    fi

    # Verifica Python 3.11+
    local py_version
    py_version=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "0.0")

    local py_major py_minor
    py_major=$(echo "$py_version" | cut -d. -f1)
    py_minor=$(echo "$py_version" | cut -d. -f2)

    if [[ $py_major -lt 3 ]] || { [[ $py_major -eq 3 ]] && [[ $py_minor -lt 11 ]]; }; then
        error "Python 3.11+ é necessário. Versão encontrada: ${py_version}"
        exit 1
    fi

    success "Python ${py_version} encontrado"
}

# ─── Criação do Usuário de Serviço ───────────────────────────
create_service_user() {
    step "Configurando usuário de serviço"

    if id "${SERVICE_USER}" &>/dev/null; then
        info "Usuário '${SERVICE_USER}' já existe"
    else
        useradd \
            --system \
            --no-create-home \
            --home-dir "${INSTALL_DIR}" \
            --shell /usr/sbin/nologin \
            --comment "ImunoEdge IoT Runtime" \
            "${SERVICE_USER}"
        success "Usuário '${SERVICE_USER}' criado"
    fi
}

# ─── Instalação dos Arquivos ─────────────────────────────────
install_files() {
    step "Instalando arquivos em ${INSTALL_DIR}"

    # Cria o diretório de instalação
    mkdir -p "${INSTALL_DIR}"

    # Copia arquivos do projeto (preserva estrutura)
    info "Copiando código fonte..."
    rsync -a --delete \
        --exclude='.venv' \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='.mypy_cache' \
        --exclude='.ruff_cache' \
        --exclude='*.pyc' \
        --exclude='.env' \
        "${SCRIPT_DIR}/" "${INSTALL_DIR}/"

    success "Arquivos copiados para ${INSTALL_DIR}"
}

# ─── Criação do Virtual Environment ─────────────────────────
setup_venv() {
    step "Configurando Virtual Environment"

    local venv_dir="${INSTALL_DIR}/.venv"

    if [[ -d "${venv_dir}" ]]; then
        info "Virtual environment já existe, atualizando..."
    else
        info "Criando virtual environment..."
        python3 -m venv "${venv_dir}"
        success "Virtual environment criado"
    fi

    # Atualiza pip
    info "Atualizando pip..."
    "${venv_dir}/bin/pip" install --upgrade pip --quiet

    # Instala dependências do projeto
    info "Instalando dependências Python (isso pode levar alguns minutos)..."
    "${venv_dir}/bin/pip" install -e "${INSTALL_DIR}" --quiet

    # Instala python-dotenv como opcional
    "${venv_dir}/bin/pip" install python-dotenv --quiet

    success "Dependências instaladas"

    # Verifica instalação
    info "Verificando instalação..."
    if "${venv_dir}/bin/python3" -c "from imunoedge.core import ProcessOrchestrator, HealthMonitor, TelemetryClient; print('OK')" 2>/dev/null; then
        success "Imports verificados com sucesso"
    else
        error "Falha na verificação de imports"
        exit 1
    fi
}

# ─── Configuração do .env ───────────────────────────────────
setup_env() {
    step "Configurando variáveis de ambiente"

    local env_file="${INSTALL_DIR}/.env"

    if [[ -f "${env_file}" ]]; then
        warn "Arquivo .env já existe — preservando configuração atual"
        info "Novo template disponível em: ${INSTALL_DIR}/.env.example"
    else
        cp "${INSTALL_DIR}/.env.example" "${env_file}"
        success "Arquivo .env criado a partir do template"
        info "Edite ${env_file} para configurar seu dispositivo"
    fi
}

# ─── Configuração do Serviço Systemd ─────────────────────────
setup_systemd() {
    step "Configurando serviço Systemd"

    # Gera o .service com o usuário correto a partir do template
    sed \
        -e "s|User=pi|User=${SERVICE_USER}|g" \
        -e "s|Group=pi|Group=${SERVICE_USER}|g" \
        -e "s|/opt/imunoedge|${INSTALL_DIR}|g" \
        "${INSTALL_DIR}/imunoedge.service" > "${SERVICE_FILE}"

    success "Service unit instalado em ${SERVICE_FILE}"

    if [[ -d /run/systemd/system ]]; then
        # Recarrega o daemon
        systemctl daemon-reload
        success "Systemd daemon recarregado"

        # Habilita no boot
        systemctl enable "${SERVICE_NAME}.service" --quiet
        success "Serviço habilitado no boot"
    else
        warn "Systemd não detectado ou não ativo. Puleando reload/enable."
        warn "Você precisará iniciar o serviço manualmente se estiver em um container."
    fi
}

# ─── Configuração de Permissões ──────────────────────────────
setup_permissions() {
    step "Configurando permissões"

    # Diretório de instalação
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}"
    chmod 750 "${INSTALL_DIR}"
    success "Permissões do diretório de instalação configuradas"

    # Diretório de dados (FHS: /var/lib/imunoedge)
    mkdir -p "${BUFFER_DIR}"
    chown "${SERVICE_USER}:${SERVICE_USER}" "${BUFFER_DIR}"
    chmod 750 "${BUFFER_DIR}"
    success "Diretório de dados FHS criado: ${BUFFER_DIR}"

    # Diretório de logs (FHS: /var/log/imunoedge)
    mkdir -p "${LOG_DIR}"
    chown "${SERVICE_USER}:${SERVICE_USER}" "${LOG_DIR}"
    chmod 750 "${LOG_DIR}"
    success "Diretório de logs FHS criado: ${LOG_DIR}"

    # Protege o .env (contém credenciais)
    if [[ -f "${INSTALL_DIR}/.env" ]]; then
        chmod 600 "${INSTALL_DIR}/.env"
        chown "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/.env"
        success "Arquivo .env protegido (chmod 600)"
    fi
}

# ─── Migração de Dados ───────────────────────────────────────
run_migration() {
    step "Executando migração de dados (Legacy -> SQLite)"
    local venv_python="${INSTALL_DIR}/.venv/bin/python3"
    local migration_script="${INSTALL_DIR}/scripts/migrate_v1_to_v2.py"

    if [[ -f "${migration_script}" ]]; then
        info "Rodando script de migração..."
        if "${venv_python}" "${migration_script}"; then
            success "Migração concluída"
        else
            warn "Script de migração falhou. Verifique os logs."
        fi

        # Garante permissões corretas no banco criado (root -> imunoedge)
        chown -R "${SERVICE_USER}:${SERVICE_USER}" "${BUFFER_DIR}"
    else
        warn "Script de migração não encontrado em ${migration_script}"
    fi
}

# ─── Iniciar Serviço ────────────────────────────────────────
start_service() {
    step "Iniciando serviço"

    if [[ ! -d /run/systemd/system ]]; then
        warn "Systemd não disponível. Serviço não iniciado automaticamente."
        return
    fi

    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        info "Serviço já está rodando, reiniciando..."
        systemctl restart "${SERVICE_NAME}"
    else
        systemctl start "${SERVICE_NAME}"
    fi

    # Aguarda 3 segundos e verifica status
    sleep 3

    if systemctl is-active --quiet "${SERVICE_NAME}"; then
        success "Serviço ${SERVICE_NAME} está ATIVO"
    else
        error "Serviço falhou ao iniciar. Verifique com:"
        error "  journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
        exit 1
    fi
}

# ─── Resumo Final ───────────────────────────────────────────
print_summary() {
    echo ""
    echo -e "${GREEN}${BOLD}"
    cat << EOF
╔══════════════════════════════════════════════════════════╗
║   ✅ ImunoEdge instalado com sucesso!                    ║
╚══════════════════════════════════════════════════════════╝
EOF
    echo -e "${NC}"

    echo -e "  ${BOLD}Diretório:${NC}      ${INSTALL_DIR}"
    echo -e "  ${BOLD}Usuário:${NC}        ${SERVICE_USER}"
    echo -e "  ${BOLD}Serviço:${NC}        ${SERVICE_NAME}.service"
    echo -e "  ${BOLD}Configuração:${NC}   ${INSTALL_DIR}/.env"
    echo ""
    echo -e "  ${BOLD}Comandos úteis:${NC}"
    echo -e "    Ver status:       ${CYAN}sudo systemctl status ${SERVICE_NAME}${NC}"
    echo -e "    Ver logs:         ${CYAN}journalctl -u ${SERVICE_NAME} -f${NC}"
    echo -e "    Reiniciar:        ${CYAN}sudo systemctl restart ${SERVICE_NAME}${NC}"
    echo -e "    Parar:            ${CYAN}sudo systemctl stop ${SERVICE_NAME}${NC}"
    echo -e "    Editar config:    ${CYAN}sudo nano ${INSTALL_DIR}/.env${NC}"
    echo ""
    echo -e "  ${BOLD}Próximo passo:${NC}"
    echo -e "    Edite ${CYAN}${INSTALL_DIR}/.env${NC} com o endpoint da sua API"
    echo -e "    e reinicie: ${CYAN}sudo systemctl restart ${SERVICE_NAME}${NC}"
    echo ""
    echo -e "  ${BOLD}Guia de testes:${NC}"
    echo -e "    Leia ${CYAN}STRESS_TEST.md${NC} para validar a autocura"
    echo ""
}

# ─── Script de Desinstalação ─────────────────────────────────
create_uninstall_hint() {
    cat > "${INSTALL_DIR}/uninstall.sh" << 'UNINSTALL_EOF'
#!/usr/bin/env bash
# ImunoEdge — Desinstalador
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Execute como root: sudo bash uninstall.sh"
    exit 1
fi

SERVICE_NAME="imunoedge"
INSTALL_DIR="/opt/imunoedge"
SERVICE_USER="imunoedge"

echo "Parando e desabilitando serviço..."
if [[ -d /run/systemd/system ]]; then
    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    systemctl daemon-reload
fi

rm -f "/etc/systemd/system/${SERVICE_NAME}.service"

echo "Removendo arquivos..."
rm -rf "${INSTALL_DIR}"
rm -rf "/var/lib/imunoedge"
rm -rf "/var/log/imunoedge"

echo "Removendo usuário..."
userdel "${SERVICE_USER}" 2>/dev/null || true

echo "✅ ImunoEdge desinstalado com sucesso."
UNINSTALL_EOF
    chmod +x "${INSTALL_DIR}/uninstall.sh"
}

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
main() {
    banner
    check_root
    check_source
    install_system_deps
    create_service_user
    install_files
    setup_venv
    setup_env
    setup_systemd
    setup_permissions
    run_migration
    create_uninstall_hint
    start_service
    print_summary
}

main "$@"
