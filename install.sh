#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

REPO_URL=https://github.com/FalcaoOtavio/ipv6-proxy-server.git
RAW_URL=https://raw.githubusercontent.com/FalcaoOtavio/ipv6-proxy-server
THREEPROXY_TAG=0.9.4
THREEPROXY_SHA=346880eb2e73836385e5f7648f738efcece580ac
API_URL=http://127.0.0.1:8000

usage() {
    cat <<'EOF'
Instala ou atualiza o IPv6 Proxy Server (API, proxies por instancia systemd, CLI e TUI)
no Ubuntu 22.04/24.04/26.04 ou Debian 12/13. Fonte: FalcaoOtavio/ipv6-proxy-server.

Uso:
  sudo bash install.sh [--ref REF] [--subnet CIDR --interface IF --ipv4 IP] [--self-test] [--yes]
  sudo bash install.sh --update [--ref REF] [--self-test] [--yes] [--keep-backups N]
  sudo bash install.sh --cli-only | --tui-only

Opcoes:
  --ref REF         Branch, tag ou SHA do fork (padrao: main). Resolvido para UM SHA,
                    gravado em /opt/ipv6-proxy-server/VERSION. Sem --ref, um checkout
                    local completo instala os proprios arquivos (registra o SHA dele).
  --subnet CIDR     Subnet IPv6 dedicada e roteada (prefixo <= 126).
  --interface IF    Interface de rede (padrao: a da rota IPv4 padrao).
  --ipv4 IP         IPv4 local onde os proxies escutam (deve existir na interface).
  --update          Atualiza instalacao existente: backup em /var/backups/ipv6-proxy/<data>/,
                    nova revisao, healthcheck e rollback automatico em falha. Migra a
                    versao base (aa29685/main ou bdfe9c7/ashford) para um servico por
                    proxy; essa migracao interrompe todos os proxies UMA vez e pede
                    confirmacao. Atualizacoes seguintes reiniciam so a API, exceto se a
                    configuracao gerada de proxies mudar (anuncia quantos e pede confirmacao).
  --yes             Confirma migracao/reinicio de proxies sem perguntar (obrigatorio sem terminal).
  --keep-backups N  Backups mantidos em /var/backups/ipv6-proxy/ apos atualizar (padrao: 5).
  --self-test       Ao final, cria um proxy de teste com senha aleatoria, confere e o remove.
                    Nao toca proxies existentes.
  --cli-only        Instala/atualiza so o CLI (checkout completo). Nao reinicia nada.
  --tui-only        Instala/atualiza so a TUI (checkout completo). Nao reinicia nada.
  -h, --help        Esta ajuda.

Reexecutar sem mudancas nao altera token, env, estado, unidades nem processos.
A subnet deve ser dedicada e roteada pelo provedor. Nao configura NDP/firewall.
Documentacao: docs/INSTALL.md e docs/MIGRATION.md.
EOF
}

die() { printf 'Erro: %s\n' "$*" >&2; exit 1; }
# /dev/tty can be readable with no controlling terminal; only opening it proves one.
have_tty() { { : </dev/tty; } 2>/dev/null; }
say() { printf '%s\n' "$*"; }

set_paths() {
    # Test-only prefix: honored only when the script is sourced as a library.
    local root=''
    if [[ ${IPV6_PROXY_INSTALL_LIB:-} == 1 ]]; then
        root=${IPV6_PROXY_TEST_ROOT:-}
    fi
    app_dir=$root/opt/ipv6-proxy-server
    env_file=$root/etc/ipv6-proxy-api.env
    unit_dir=$root/etc/systemd/system
    unit_file=$unit_dir/ipv6-proxy-api.service
    template_file=$unit_dir/ipv6-proxy@.service
    state_dir=$root/var/lib/ipv6-proxy-api
    backup_root=$root/var/backups/ipv6-proxy
    bin_dir=$root/usr/local/bin
    cli_file=$bin_dir/ipv6-proxy
    tui_file=$bin_dir/ipv6-proxy-tui
    threeproxy_file=$bin_dir/3proxy
    proc_dir=$root/proc
}

parse_args() {
    subnet='' interface='' ipv4='' ref='' mode=install assume_yes=false self_test=false keep_backups=5
    while (($#)); do
        case "$1" in
            --help|-h) usage; exit 0 ;;
            --cli-only|--tui-only|--update)
                [[ $mode == install ]] || die "Use somente um de --update, --cli-only, --tui-only."
                mode=${1#--}; shift ;;
            --yes) assume_yes=true; shift ;;
            --self-test) self_test=true; shift ;;
            --subnet|--interface|--ipv4|--ref|--keep-backups)
                if (($# < 2)) || [[ -z "$2" || "$2" == -* ]]; then
                    die "Falta valor para $1"
                fi
                case "$1" in
                    --subnet) subnet=$2 ;;
                    --interface) interface=$2 ;;
                    --ipv4) ipv4=$2 ;;
                    --ref) ref=$2 ;;
                    --keep-backups) keep_backups=$2 ;;
                esac
                shift 2 ;;
            *) die "Opcao desconhecida: $1 (use --help)" ;;
        esac
    done
    if [[ -n $ref && ! $ref =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$ ]]; then
        die "--ref invalido: use nome de branch/tag ou SHA."
    fi
    [[ $keep_backups =~ ^[1-9][0-9]{0,2}$ ]] || die "--keep-backups invalido: use um inteiro de 1 a 999."
    if [[ $mode != install && -n "$subnet$interface$ipv4" ]]; then
        die "--$mode nao aceita --subnet, --interface ou --ipv4 (a configuracao existente e mantida)."
    fi
    if [[ $mode == cli-only || $mode == tui-only ]] && [[ -n $ref || $self_test == true ]]; then
        die "--$mode usa o checkout local; nao aceita --ref nem --self-test."
    fi
}

# --- Source ------------------------------------------------------------------

is_full_checkout() {
    [[ -f "$1/proxy_api/app.py" && -f "$1/deploy/ipv6-proxy-api.service" && -f "$1/ipv6-proxy" ]]
}

resolve_source() {
    # Sets source_dir, source_sha, source_ref. Local checkout only without --ref.
    if [[ -z $ref ]] && is_full_checkout "$script_dir"; then
        source_dir=$script_dir
        source_ref=local
        # sudo runs git as root on a user-owned checkout ("dubious ownership"):
        # trust this one directory for these two commands only, never globally.
        local git=(git -c "safe.directory=$script_dir" -C "$script_dir")
        source_sha=$("${git[@]}" rev-parse --verify 'HEAD^{commit}' 2>/dev/null) || source_sha=unknown
        if [[ $source_sha != unknown ]] && [[ -n $("${git[@]}" status --porcelain 2>/dev/null) ]]; then
            source_sha="$source_sha-dirty"
        fi
        if [[ $source_sha == unknown ]]; then
            say "Aviso: nao foi possivel ler o SHA de $script_dir (sem .git ou git ausente); VERSION registrara sha=unknown. Use --ref para fixar a revisao." >&2
        fi
        return
    fi
    source_ref=${ref:-main}
    source_dir=$work_dir/app
    git clone --quiet --filter=blob:none --no-checkout "$REPO_URL" "$source_dir"
    local candidate
    for candidate in "origin/$source_ref" "refs/tags/$source_ref" "$source_ref"; do
        if source_sha=$(git -C "$source_dir" rev-parse --quiet --verify "$candidate^{commit}"); then
            break
        fi
        source_sha=''
    done
    [[ -n $source_sha ]] || die "--ref $source_ref nao existe em $REPO_URL."
    git -C "$source_dir" checkout --quiet --detach "$source_sha"
    [[ $(git -C "$source_dir" rev-parse HEAD) == "$source_sha" ]] || die 'Checkout inconsistente do SHA resolvido.'
    is_full_checkout "$source_dir" || die "A revisao $source_sha nao contem a API (proxy_api/deploy/ipv6-proxy)."
}

# --- Detection and validation ---------------------------------------------------

detect_version() {
    # none | partial | main (aa29685) | ashford (bdfe9c7) | independent (per-proxy units)
    local path found=() missing=()
    for path in "$app_dir" "$env_file" "$unit_file" "$state_dir" "$threeproxy_file" "$cli_file"; do
        if [[ -e $path || -L $path ]]; then found+=("$path"); else missing+=("$path"); fi
    done
    if ((${#found[@]} == 0)); then
        echo none
    elif [[ ! -f $app_dir/proxy_api/app.py || ! -f $env_file || ! -f $unit_file ]]; then
        echo partial
    elif [[ -f $app_dir/proxy_api/supervisor.py ]]; then
        echo independent
    elif grep -q 'ashford.proxy/1' "$app_dir/proxy_api/app.py"; then
        echo ashford
    else
        echo main
    fi
}

describe_existing() {
    local path
    for path in "$app_dir" "$env_file" "$unit_file" "$state_dir" "$threeproxy_file" "$cli_file"; do
        if [[ -e $path || -L $path ]]; then printf '%s ' "$path"; fi
    done
}

legacy_cron_present() {
    # Consume the whole crontab: grep -q can SIGPIPE crontab under pipefail.
    [[ -d /root/proxyserver ]] || crontab -l 2>/dev/null | grep 'proxy-startup\.sh' >/dev/null
}

foreign_3proxy_pids() {
    # 3proxy processes that are neither our per-proxy units nor children of our API.
    local dir pid
    for dir in "$proc_dir"/[0-9]*; do
        [[ -r $dir/comm && $(<"$dir/comm") == 3proxy ]] || continue
        pid=${dir##*/}
        if ! grep -qE '/(ipv6-proxy-api\.service|system-ipv6\\x2dproxy\.slice/ipv6-proxy@[0-9]+\.service)$' \
                "$dir/cgroup" 2>/dev/null; then
            printf '%s\n' "$pid"
        fi
    done
}

api_cgroup_3proxy_count() {
    local dir count=0
    for dir in "$proc_dir"/[0-9]*; do
        [[ -r $dir/comm && $(<"$dir/comm") == 3proxy ]] || continue
        if grep -q '/ipv6-proxy-api\.service$' "$dir/cgroup" 2>/dev/null; then count=$((count + 1)); fi
    done
    echo "$count"
}

check_platform() {
    [[ $(uname -s) == Linux ]] || die 'Execute na VPS Linux, nao no macOS/Windows.'
    [[ $EUID == 0 ]] || die 'Execute com: sudo bash install.sh'
    [[ -f /etc/os-release ]] || die 'Distribuicao nao identificada.'
    local os
    # shellcheck disable=SC1091
    os=$(. /etc/os-release && printf '%s:%s' "${ID:-}" "${VERSION_ID:-}")
    case "$os" in
        ubuntu:22.04|ubuntu:24.04|ubuntu:26.04|debian:12|debian:13) ;;
        *) die "Sistema $os nao suportado. Use Ubuntu 22.04/24.04/26.04 ou Debian 12/13." ;;
    esac
    [[ -d /run/systemd/system ]] || die 'systemd deve estar ativo (use uma VPS/VM, nao container).'
}

check_foreign_3proxy() {
    local pids
    pids=$(foreign_3proxy_pids | tr '\n' ' ')
    [[ -z $pids ]] || die "3proxy de outro gerenciador em execucao (PIDs: $pids). Nada foi encerrado; desative-o antes de instalar."
}

ask_network() {
    local answer default_interface default_ipv4 addresses
    default_interface=$(ip -j -4 route show default | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r[0].get("dev", "") if r else "")')
    interface=${interface:-$default_interface}
    if [[ -z $subnet ]]; then
        have_tty || die 'Sem terminal: informe --subnet, --interface e --ipv4.'
        read -r -p 'Subnet IPv6 dedicada (ex.: 2001:db8:1234::/64): ' subnet </dev/tty
        read -r -p "Interface [$interface]: " answer </dev/tty
        interface=${answer:-$interface}
    fi
    [[ $interface =~ ^[a-zA-Z0-9_.-]{1,15}$ ]] || die "Interface invalida: '$interface'."
    ip link show dev "$interface" >/dev/null 2>&1 || die "Interface $interface nao existe (veja: ip -br link)."
    addresses=$(ip -j -4 addr show dev "$interface")
    default_ipv4=$(printf '%s' "$addresses" | python3 -c 'import json,sys; a=[i["local"] for d in json.load(sys.stdin) for i in d.get("addr_info",[]) if i["family"]=="inet"]; print(a[0] if a else "")')
    if [[ -z $ipv4 ]]; then
        ipv4=$default_ipv4
        if have_tty && [[ -t 0 ]]; then
            read -r -p "IPv4 local [$ipv4]: " answer </dev/tty
            ipv4=${answer:-$ipv4}
        fi
    fi
    validate_network "$subnet" "$ipv4" "$addresses"
}

validate_network() {
    python3 - "$1" "$2" "$3" <<'PY' || die 'Corrija a rede e execute novamente (docs/INSTALL.md).'
import ipaddress, json, sys
def fail(message):
    print(f"Erro: {message}", file=sys.stderr)
    sys.exit(1)
try:
    net = ipaddress.IPv6Network(sys.argv[1])
except ValueError:
    fail(f"subnet IPv6 invalida: {sys.argv[1]!r} (ex.: 2001:db8:1234::/64)")
if net.prefixlen > 126:
    fail("subnet precisa de prefixo <= 126 para haver enderecos de rotacao")
if net.is_multicast or net.is_link_local or net.is_loopback or net.is_unspecified:
    fail("use uma subnet roteada (nao multicast/link-local/loopback)")
try:
    ip = ipaddress.IPv4Address(sys.argv[2])
except ValueError:
    fail(f"IPv4 invalido: {sys.argv[2]!r}")
if ip.is_unspecified or ip.is_multicast:
    fail("IPv4 deve ser um endereco especifico")
local = [a["local"] for d in json.loads(sys.argv[3]) for a in d.get("addr_info", [])]
if str(ip) not in local:
    fail(f"IPv4 {ip} nao existe na interface (enderecos: {', '.join(local) or 'nenhum'})")
PY
}

check_api_port_free() {
    if ss -ltnH 'sport = :8000' 2>/dev/null | grep -q .; then
        die 'Porta 127.0.0.1:8000 ja esta em uso por outro processo; libere-a antes de instalar.'
    fi
}

confirm_migration() {
    local count=$1 from=$2 answer
    say ''
    say "ATENCAO: migracao da versao $from para um servico systemd por proxy."
    say "Os $count proxies atuais sao processos filhos da API e serao interrompidos UMA vez"
    say '(alguns segundos) enquanto passam a rodar como ipv6-proxy@<porta>. Depois disso,'
    say 'atualizacoes e reinicios da API nao interrompem mais os proxies.'
    if [[ $assume_yes == true ]]; then
        say 'Confirmado por --yes.'
        return
    fi
    have_tty || die 'Sem terminal para confirmar: execute novamente com --yes.'
    read -r -p 'Digite MIGRAR para continuar: ' answer </dev/tty
    [[ $answer == MIGRAR ]] || die 'Migracao cancelada; nada foi alterado.'
}

confirm_proxy_restarts() {
    # $1: ports whose rendered config changes ('?' = could not compute).
    local ports=$1 total=$2 answer count
    [[ -n $ports ]] || return 0
    say ''
    if [[ $ports == '?' ]]; then
        say "ATENCAO: nao foi possivel pre-calcular a configuracao gerada; a API pode reiniciar ate $total proxies."
    else
        count=$(wc -w <<<"$ports")
        say "ATENCAO: esta revisao muda a configuracao gerada de $count proxies (portas: $ports)."
        say "$count proxies serao reiniciados uma vez (um por vez), cada um com interrupcao de alguns segundos."
        say 'Os demais continuam sem interrupcao. Se algum falhar, a versao anterior e restaurada e'
        say 'somente os proxies ja reiniciados voltam a configuracao anterior (tambem um por vez).'
    fi
    if [[ $assume_yes == true ]]; then
        say 'Confirmado por --yes.'
        return
    fi
    have_tty || die 'Sem terminal para confirmar: execute novamente com --yes. Nada foi alterado.'
    read -r -p 'Digite REINICIAR para continuar: ' answer </dev/tty
    [[ $answer == REINICIAR ]] || die 'Atualizacao cancelada; nada foi alterado.'
}

# --- Staging and idempotent apply ----------------------------------------------

stage_release() {
    # Build the exact files to install under $stage; nothing outside it changes.
    stage=$work_dir/stage
    rm -rf -- "$stage"
    install -d -m 0755 "$stage/app/proxy_api" "$stage/units" "$stage/bin"
    install -m 0644 "$source_dir"/proxy_api/*.py "$stage/app/proxy_api/"
    install -m 0644 "$source_dir/requirements.txt" "$stage/app/requirements.txt"
    if [[ -d $source_dir/proxy_tui ]]; then
        install -d -m 0755 "$stage/app/proxy_tui"
        cp -R --no-preserve=ownership "$source_dir/proxy_tui/." "$stage/app/proxy_tui/"
        find "$stage/app/proxy_tui" -name __pycache__ -prune -exec rm -rf {} +
        chmod -R u=rwX,go=rX "$stage/app/proxy_tui"
    fi
    install -m 0644 "$source_dir/deploy/ipv6-proxy-api.service" "$stage/units/ipv6-proxy-api.service"
    install -m 0644 "$source_dir/deploy/ipv6-proxy@.service" "$stage/units/ipv6-proxy@.service"
    install -m 0755 "$source_dir/ipv6-proxy" "$stage/bin/ipv6-proxy"
    if [[ -f $source_dir/ipv6-proxy-tui ]]; then
        install -m 0755 "$source_dir/ipv6-proxy-tui" "$stage/bin/ipv6-proxy-tui"
    fi
}

tree_digest() {
    # Content + mode digest of a directory, ignoring caches and the venv.
    [[ -d $1 ]] || { echo absent; return; }
    (cd -- "$1" && find . -path ./.venv -prune -o -name __pycache__ -prune -o -type f -print0 \
        | sort -z | xargs -0 -r stat -c '%a %n' | sha256sum; \
        find . -path ./.venv -prune -o -name __pycache__ -prune -o -type f -print0 \
        | sort -z | xargs -0 -r sha256sum | sha256sum) | sha256sum | cut -d' ' -f1
}

file_differs() { ! cmp -s -- "$1" "$2"; }

plan_changes() {
    # Sets app_changed, api_unit_changed, template_changed, cli_changed, tui_changed, version_changed.
    local installed_app=$work_dir/installed-app
    rm -rf -- "$installed_app"
    mkdir -p "$installed_app"
    if [[ -d $app_dir/proxy_api ]]; then cp -a "$app_dir/proxy_api" "$installed_app/"; fi
    if [[ -d $app_dir/proxy_tui ]]; then cp -a "$app_dir/proxy_tui" "$installed_app/"; fi
    if [[ -f $app_dir/requirements.txt ]]; then cp -a "$app_dir/requirements.txt" "$installed_app/"; fi
    app_changed=false api_unit_changed=false template_changed=false cli_changed=false tui_changed=false
    [[ $(tree_digest "$installed_app") == "$(tree_digest "$stage/app")" && -x $app_dir/.venv/bin/python ]] || app_changed=true
    file_differs "$stage/units/ipv6-proxy-api.service" "$unit_file" && api_unit_changed=true
    file_differs "$stage/units/ipv6-proxy@.service" "$template_file" && template_changed=true
    file_differs "$stage/bin/ipv6-proxy" "$cli_file" && cli_changed=true
    if [[ -f $stage/bin/ipv6-proxy-tui ]] && file_differs "$stage/bin/ipv6-proxy-tui" "$tui_file"; then
        tui_changed=true
    fi
    version_changed=false
    [[ $(sed -n 's/^sha=//p' "$app_dir/VERSION" 2>/dev/null) == "$source_sha" ]] || version_changed=true
}

config_changes() {
    # Ports whose current proxies/<port>.cfg differs from what the staged release
    # renders: the API's reconciliation restarts exactly those. Prints '?' when
    # it cannot tell (caller then asks as if all could restart).
    python3 -B - "$stage/app" "$state_dir" "$env_file" <<'PY' 2>/dev/null || echo '?'
import json, pathlib, sys
from ipaddress import IPv4Address, IPv6Address
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from proxy_api.backend import render_config
state = pathlib.Path(sys.argv[2])
env = dict(line.split("=", 1) for line in pathlib.Path(sys.argv[3]).read_text().splitlines() if "=" in line)
settings = SimpleNamespace(bind_ipv4=IPv4Address(env["PROXY_BIND_IPV4"]))
changed = []
for item in json.loads((state / "state.json").read_text())["proxies"]:
    record = SimpleNamespace(**{"username": None, "password": None, **item})
    record.ipv6 = IPv6Address(item["ipv6"])
    path = state / "proxies" / f"{int(item['port'])}.cfg"
    if path.exists() and path.read_text() != render_config(settings, record):
        changed.append(str(record.port))
print(" ".join(changed))
PY
}

anything_changes() {
    [[ $app_changed == true || $api_unit_changed == true || $template_changed == true ||
       $cli_changed == true || $tui_changed == true || $version_changed == true ]]
}

replace_dir() {
    # Atomic-ish swap of one app subdirectory (removes files dropped upstream).
    local source=$1 destination=$2
    rm -rf -- "$destination.new"
    # Never keep the checkout owner's uid: these files run as root.
    cp -R --no-preserve=ownership "$source" "$destination.new"
    chown -R root:root "$destination.new"
    chmod -R u=rwX,go=rX "$destination.new"
    rm -rf -- "$destination.old"
    if [[ -e $destination ]]; then mv -- "$destination" "$destination.old"; fi
    mv -- "$destination.new" "$destination"
    rm -rf -- "$destination.old"
}

apply_release() {
    install -d -m 0755 "$app_dir" "$unit_dir" "$bin_dir"
    if [[ $app_changed == true ]]; then
        replace_dir "$stage/app/proxy_api" "$app_dir/proxy_api"
        if [[ -d $stage/app/proxy_tui ]]; then replace_dir "$stage/app/proxy_tui" "$app_dir/proxy_tui"; fi
        if [[ ! -x $app_dir/.venv/bin/python ]]; then
            python3 -m venv "$app_dir/.venv"
            run_pip=true
        fi
        if [[ ${run_pip:-false} == true ]] || file_differs "$stage/app/requirements.txt" "$app_dir/requirements.txt"; then
            "$app_dir/.venv/bin/pip" install --quiet --disable-pip-version-check -r "$stage/app/requirements.txt"
        fi
        install -m 0644 "$stage/app/requirements.txt" "$app_dir/requirements.txt"
    fi
    if [[ $api_unit_changed == true ]]; then install -m 0644 "$stage/units/ipv6-proxy-api.service" "$unit_file"; fi
    if [[ $template_changed == true ]]; then install -m 0644 "$stage/units/ipv6-proxy@.service" "$template_file"; fi
    if [[ $cli_changed == true ]]; then install_client "$stage/bin/ipv6-proxy" "$cli_file" ipv6-proxy-api-client; fi
    if [[ $tui_changed == true ]]; then install_client "$stage/bin/ipv6-proxy-tui" "$tui_file" ''; fi
    if [[ $version_changed == true ]]; then
        printf 'sha=%s\nref=%s\ndate=%s\n' "$source_sha" "$source_ref" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$app_dir/VERSION.tmp"
        chmod 0644 "$app_dir/VERSION.tmp"
        mv -f -- "$app_dir/VERSION.tmp" "$app_dir/VERSION"
    fi
}

install_client() {
    # Replace a client executable atomically; refuse to overwrite something not ours.
    local source=$1 destination=$2 marker=$3 temporary
    if [[ -e $destination || -L $destination ]]; then
        [[ -f $destination && ! -L $destination ]] || die "Destino inseguro: $destination"
        [[ $(stat -c '%U:%G:%a' "$destination") == root:root:755 ]] || die "Destino nao reconhecido: $destination"
        if [[ -n $marker ]]; then
            grep -q "^# $marker\$" "$destination" || die "Destino nao pertence a este projeto: $destination"
        fi
    fi
    temporary=$(mktemp "$(dirname -- "$destination")/.$(basename -- "$destination").XXXXXXXX")
    install -o root -g root -m 0755 "$source" "$temporary" || { rm -f -- "$temporary"; return 1; }
    mv -f -- "$temporary" "$destination"
}

# --- Backup and rollback --------------------------------------------------------

backup_installation() {
    # Never chmod the shared /var/backups: only our own subdirectory is 0700.
    [[ -d ${backup_root%/*} ]] || install -d -m 0755 "${backup_root%/*}"
    backup_dir=$backup_root/$(date -u +%Y%m%dT%H%M%SZ)-$$
    install -d -m 0700 "$backup_root"
    install -d -m 0700 "$backup_dir"
    local name path
    for name in app env api-unit template state cli tui 3proxy; do
        path=$(backup_target "$name")
        if [[ -e $path || -L $path ]]; then
            cp -a -- "$path" "$backup_dir/$name"
        else
            : >"$backup_dir/$name.absent"
        fi
    done
    say "Backup: $backup_dir"
}

prune_backups() {
    # Keep the $keep_backups newest (names sort by UTC date); they hold token and passwords.
    local old
    find "$backup_root" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*T*Z-*' -printf '%f\n' | sort -r \
        | tail -n +"$((keep_backups + 1))" | while read -r old; do
            rm -rf -- "${backup_root:?}/$old"
            say "Backup antigo removido: $backup_root/$old"
        done
}

backup_target() {
    case "$1" in
        app) echo "$app_dir" ;;
        env) echo "$env_file" ;;
        api-unit) echo "$unit_file" ;;
        template) echo "$template_file" ;;
        state) echo "$state_dir" ;;
        cli) echo "$cli_file" ;;
        tui) echo "$tui_file" ;;
        3proxy) echo "$threeproxy_file" ;;
    esac
}

restore_installation() {
    # Restores every item it can; one bad item never stops the others. Returns 1 if any failed.
    local source=$1 name path failed=0
    for name in app env api-unit template state cli tui 3proxy; do
        path=$(backup_target "$name")
        if [[ -e $source/$name || -L $source/$name ]]; then
            { rm -rf -- "$path.rollback" && cp -a -- "$source/$name" "$path.rollback" &&
              rm -rf -- "$path" && mv -- "$path.rollback" "$path"; } || {
                say "Rollback: falha ao restaurar $path de $source/$name." >&2; failed=1; }
        elif [[ -e $source/$name.absent ]]; then
            rm -rf -- "$path" || { say "Rollback: falha ao remover $path." >&2; failed=1; }
        else
            say "Rollback: backup incompleto em $source ($name); restaure $path manualmente (docs/MIGRATION.md)." >&2
            failed=1
        fi
    done
    return "$failed"
}

# --- systemd / API helpers --------------------------------------------------------

instance_ports() {
    systemctl list-units --all --plain --no-legend --type=service 'ipv6-proxy@*.service' 2>/dev/null \
        | sed -nE 's/^ipv6-proxy@([0-9]+)\.service.*/\1/p'
}

instance_snapshot() {
    # port pid config-hash, one line per instance in $1 (the ones active before).
    local port config
    for port in $1; do
        config=$(sha256sum <"$state_dir/proxies/$port.cfg" 2>/dev/null | cut -d' ' -f1) || config=none
        printf '%s %s %s\n' "$port" "$(systemctl show --property=MainPID --value "ipv6-proxy@$port.service")" "${config:-none}"
    done
}

restarted_unchanged() {
    # Ports whose config is unchanged between two snapshots but whose PID changed.
    # A port with no main PID before (inactive/failed) is never "restarted by us".
    awk 'NR == FNR { if ($2 != "" && $2 != 0) before[$1] = $2 " " $3; next }
         ($1 in before) && before[$1] != $2 " " $3 && substr(before[$1], index(before[$1], " ") + 1) == $3 { print $1 }' \
        "$1" "$2"
}

state_ports() {
    # Ports in the persisted state (state.json v2, or proxies.json before the API migrates it).
    python3 - "$state_dir" <<'PY'
import json, pathlib, sys
base = pathlib.Path(sys.argv[1])
for name in ("state.json", "proxies.json"):
    path = base / name
    if path.exists():
        data = json.loads(path.read_text())
        for item in data["proxies"] if isinstance(data, dict) else data:
            print(int(item["port"]))
        break
PY
}

write_curl_config() {
    # Keep the token out of argv, logs and the environment of child processes.
    local token
    token=$(sed -n 's/^PROXY_API_TOKEN=//p' "$env_file" | tail -n 1)
    [[ -n $token ]] || die "PROXY_API_TOKEN ausente em $env_file."
    curl_config=$work_dir/curl.conf
    printf 'header = "Authorization: Bearer %s"\n' "$token" >"$curl_config"
}

api() {
    curl --config "$curl_config" --noproxy '*' --silent --show-error --max-time 30 "$@"
}

api_deadline() {
    # The API reconciles every proxy before answering (up to ~5s each when one
    # must be (re)started), so the wait grows with the number of proxies.
    echo $((45 + 5 * $(state_ports 2>/dev/null | wc -l)))
}

wait_api() {
    # ?include=status answers 200 even with a fenced proxy (plain GET /proxies is
    # 503 then); older versions ignore the parameter. A proxy that is down and
    # unrelated to this update must not fail it.
    local attempt deadline=${1:-$(api_deadline)}
    for ((attempt = 0; attempt < deadline; attempt++)); do
        if api --fail --max-time 3 -o /dev/null "$API_URL/proxies?include=status" 2>/dev/null; then return 0; fi
        sleep 1
    done
    return 1
}

healthcheck() {
    # API answers authenticated; each required proxy ($1: ports, default all persisted)
    # is its own active unit; no 3proxy left in the API cgroup.
    local port ports=${1-$(state_ports)}
    wait_api || { say "Healthcheck: API nao respondeu 200 em GET /proxies?include=status em $(api_deadline)s." >&2; return 1; }
    for port in $ports; do
        if ! systemctl is-active --quiet "ipv6-proxy@$port.service"; then
            say "Healthcheck: ipv6-proxy@$port.service nao esta ativo." >&2
            return 1
        fi
    done
    if [[ $(api_cgroup_3proxy_count) != 0 ]]; then
        say 'Healthcheck: ainda ha 3proxy dentro do cgroup da API (arquitetura antiga).' >&2
        return 1
    fi
}

run_self_test() {
    local body port id password
    password=$(openssl rand -hex 16)
    printf '{"username":"selftest","password":"%s"}' "$password" >"$work_dir/self-test.json"
    body=$(api --fail -X POST -H 'Content-Type: application/json' --data "@$work_dir/self-test.json" "$API_URL/proxies") \
        || die 'Self-test: falha ao criar proxy de teste.'
    read -r port id < <(printf '%s' "$body" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["port"], d["id"])')
    local status='' attempt
    for ((attempt = 0; attempt < 10; attempt++)); do
        status=$(api --fail "$API_URL/proxies/$port" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])') || true
        [[ $status == running ]] && break
        sleep 1
    done
    api --fail -o /dev/null -X DELETE -H "If-Match: $id" "$API_URL/proxies/$port" \
        || die "Self-test: falha ao remover o proxy de teste na porta $port (remova com: sudo ipv6-proxy delete $port)."
    [[ $status == running ]] || die "Self-test: proxy de teste na porta $port nao ficou running (status: ${status:-?})."
    say "Self-test OK: proxy de teste na porta $port criado, ativo e removido."
}

reload_if_units_changed() {
    if [[ $api_unit_changed == true || $template_changed == true ]]; then systemctl daemon-reload; fi
}

# Transaction: between begin_tx and end_tx any non-zero exit of the main shell
# (set -e error, die, fail_tx) runs on_exit, which undoes the change. ERR only
# records the line: exit (die) never fires ERR, but always fires EXIT.
begin_tx() {
    tx=$1 tx_reason='' tx_line=''
    trap 'tx_line=$LINENO' ERR
    trap on_exit EXIT
}

end_tx() { tx=''; trap - ERR; }

fail_tx() { tx_reason=$1; exit 1; }

on_exit() {
    local rc=$? kind=${tx:-}
    # Only the main shell undoes: never a $(...) or pipeline subshell.
    if [[ $rc != 0 && -n $kind && $BASHPID == "$$" ]]; then
        tx=''
        trap - ERR
        set +e
        local reason=${tx_reason:-"erro (codigo $rc) na linha ${tx_line:-?}"}
        if [[ $kind == fresh ]]; then undo_fresh_install "$reason"; else rollback "$reason"; fi
    fi
    if [[ ${cleanup_work_dir:-false} == true ]]; then rm -rf -- "$work_dir"; fi
    exit "$rc"
}

step() {
    # Run one rollback step, report it, never abort the rest.
    local description=$1
    shift
    if "$@"; then say "  ok: $description" >&2; else say "  FALHOU: $description" >&2; rollback_failed=true; fi
}

rollback() {
    # Runs with set +e: every step is attempted and the old API is always started.
    local reason=$1 port
    rollback_failed=false
    say "Falha: $reason. Restaurando $backup_dir ..." >&2
    step 'parar a API nova' systemctl stop ipv6-proxy-api.service
    if [[ $migrating == true ]]; then
        # Undo the per-proxy units this migration created, one explicit instance at a time.
        for port in $(instance_ports); do
            step "desabilitar ipv6-proxy@$port" systemctl disable --now "ipv6-proxy@$port.service"
        done
    fi
    local reverted
    reverted=$(changed_configs "$backup_dir/state/proxies")
    step 'restaurar arquivos do backup' restore_installation "$backup_dir"
    step 'systemctl daemon-reload' systemctl daemon-reload
    if [[ $migrating == false ]]; then
        # The API adopts a proxy whose file matches: a proxy already restarted with
        # the new config must be restarted on the restored one, one at a time.
        for port in $reverted; do
            step "reiniciar ipv6-proxy@$port com a configuracao anterior" systemctl restart "ipv6-proxy@$port.service"
        done
    fi
    step 'iniciar a API anterior' systemctl start ipv6-proxy-api.service
    if [[ -f $env_file ]] && write_curl_config 2>/dev/null && wait_api; then
        say "Rollback concluido: versao anterior respondendo$([[ $rollback_failed == false ]] || echo ' (com passos que falharam acima)')." >&2
    else
        say "Rollback aplicado, mas a API nao respondeu. Veja: journalctl -u ipv6-proxy-api -n 100; manual: docs/MIGRATION.md" >&2
    fi
}

changed_configs() {
    # Ports whose live proxies/<port>.cfg differs from the copy in $1.
    local file port
    for file in "$state_dir"/proxies/*.cfg; do
        [[ -f $file ]] || continue
        port=${file##*/}
        port=${port%.cfg}
        [[ $port =~ ^[0-9]+$ && -f $1/$port.cfg ]] || continue
        cmp -s -- "$file" "$1/$port.cfg" || printf '%s ' "$port"
    done
}

undo_fresh_install() {
    # A failed clean install removes only what it created (paths absent before),
    # so the next run starts again from 'none' instead of a stuck 'partial'.
    local reason=$1 port path
    say "Falha: $reason. Desfazendo a instalacao incompleta ..." >&2
    rollback_failed=false
    if [[ -f $unit_file ]]; then step 'desabilitar a API' systemctl disable --now ipv6-proxy-api.service; fi
    for port in $(instance_ports); do
        step "desabilitar ipv6-proxy@$port" systemctl disable --now "ipv6-proxy@$port.service"
    done
    for path in "${fresh_created[@]}"; do
        step "remover $path" rm -rf -- "$path"
    done
    step 'systemctl daemon-reload' systemctl daemon-reload
    if [[ $rollback_failed == true ]]; then
        say "Limpeza incompleta (veja acima); remova os itens restantes e execute novamente. docs/INSTALL.md" >&2
    else
        say 'Nada da instalacao ficou no sistema; corrija a causa e execute o mesmo comando novamente.' >&2
    fi
}

# --- Modes ---------------------------------------------------------------------

install_clients_only() {
    local which=$1 source destination marker
    if [[ $which == cli ]]; then
        source=$script_dir/ipv6-proxy destination=$cli_file marker=ipv6-proxy-api-client
        [[ -f $source ]] || die '--cli-only requer checkout completo deste repositorio (install.sh e ipv6-proxy juntos); consulte docs/INSTALL.md.'
    else
        source=$script_dir/ipv6-proxy-tui destination=$tui_file marker=''
        [[ -f $source && -d $script_dir/proxy_tui && -f $script_dir/requirements.txt ]] \
            || die '--tui-only requer checkout completo com ipv6-proxy-tui e proxy_tui/; consulte docs/INSTALL.md.'
    fi
    [[ -d $app_dir && -f $env_file && -f $unit_file ]] || die "API existente nao reconhecida; use --$which-only somente apos a instalacao completa."
    if [[ $which == tui ]]; then
        [[ -x $app_dir/.venv/bin/pip ]] || die "venv da API ausente em $app_dir/.venv; rode o instalador completo."
        local pin
        pin=$(grep -E '^textual==' "$script_dir/requirements.txt") || die 'requirements.txt sem pin de textual.'
        "$app_dir/.venv/bin/pip" install --quiet --disable-pip-version-check "$pin"
        replace_dir "$script_dir/proxy_tui" "$app_dir/proxy_tui"
        find "$app_dir/proxy_tui" -name __pycache__ -prune -exec rm -rf {} +
    fi
    install_client "$source" "$destination" "$marker"
    if [[ $which == cli ]]; then say 'CLI instalado: sudo ipv6-proxy --help'; else say 'TUI instalada: sudo ipv6-proxy-tui'; fi
}

fresh_install() {
    ask_network
    check_api_port_free
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y ca-certificates curl git python3 python3-venv build-essential iproute2 openssl
    resolve_source
    stage_release
    git clone --quiet --depth 1 --branch "$THREEPROXY_TAG" https://github.com/3proxy/3proxy.git "$work_dir/3proxy"
    [[ $(git -C "$work_dir/3proxy" rev-parse HEAD) == "$THREEPROXY_SHA" ]] || die 'Revisao inesperada do 3proxy.'
    make -C "$work_dir/3proxy" -f Makefile.Linux -j "$(nproc)"
    plan_changes
    # From here on a failure removes everything this run created.
    local name path
    fresh_created=()
    for name in app env api-unit template state cli tui 3proxy; do
        path=$(backup_target "$name")
        [[ -e $path || -L $path ]] || fresh_created+=("$path")
    done
    begin_tx fresh
    apply_release
    install -m 0755 "$work_dir/3proxy/bin/3proxy" "$threeproxy_file"
    local token
    token=$(openssl rand -hex 32)
    cat >"$env_file" <<EOF
PROXY_IPV6_SUBNET=$subnet
PROXY_BIND_IPV4=$ipv4
PROXY_INTERFACE=$interface
PROXY_API_TOKEN=$token
PROXY_PORT_START=30000
PROXY_PORT_END=39999
PROXY_DATA_DIR=/var/lib/ipv6-proxy-api
PROXY_3PROXY_BINARY=/usr/local/bin/3proxy
PROXY_SUPERVISOR=systemd
EOF
    chmod 0600 "$env_file"
    systemctl daemon-reload
    systemctl enable --now ipv6-proxy-api.service
    write_curl_config
    healthcheck || fail_tx 'API nao ficou saudavel (journalctl -u ipv6-proxy-api -n 50)'
    end_tx
    finish "Instalacao concluida"
}

check_env_compatible() {
    # ipv6-proxy@.service hardcodes these paths; refuse before touching anything.
    local data_dir binary
    data_dir=$(sed -n 's/^PROXY_DATA_DIR=//p' "$env_file" | tail -n 1)
    binary=$(sed -n 's/^PROXY_3PROXY_BINARY=//p' "$env_file" | tail -n 1)
    [[ -z $data_dir || $data_dir == /var/lib/ipv6-proxy-api ]] \
        || die "PROXY_DATA_DIR=$data_dir nao suportado pelas unidades por proxy (use /var/lib/ipv6-proxy-api; docs/MIGRATION.md)."
    [[ -z $binary || $binary == /usr/local/bin/3proxy ]] \
        || die "PROXY_3PROXY_BINARY=$binary nao suportado pelas unidades por proxy (use /usr/local/bin/3proxy; docs/MIGRATION.md)."
}

update_install() {
    local version=$1 moved
    check_env_compatible
    resolve_source
    stage_release
    plan_changes
    migrating=false
    [[ $version == independent ]] || migrating=true
    if [[ $migrating == false ]] && ! anything_changes; then
        write_curl_config
        say "Nada a atualizar: revisao $source_sha ja instalada; nenhum servico foi tocado."
        if [[ $self_test == true ]]; then
            wait_api || die 'API nao responde. Execute: journalctl -u ipv6-proxy-api -n 50'
            run_self_test
        fi
        exit 0
    fi
    [[ $mode == update ]] || die "Instalacao existente com mudancas pendentes; para aplicar use --update. Instalacao preservada."
    if [[ $migrating == true ]]; then
        confirm_migration "$(state_ports | wc -l)" "$version"
    elif [[ $app_changed == true ]]; then
        confirm_proxy_restarts "$(config_changes)" "$(state_ports | wc -l)"
    fi
    backup_installation
    # From here on, any failure (error, die, healthcheck) restores the backup.
    begin_tx update
    apply_release
    reload_if_units_changed
    write_curl_config
    if [[ $migrating == true ]]; then
        # Stopping the old API stops its child 3proxy (KillMode=control-group): the one interruption.
        systemctl stop ipv6-proxy-api.service
        systemctl start ipv6-proxy-api.service
        healthcheck || fail_tx 'healthcheck apos migracao'
        rm -f -- "$state_dir/3proxy.cfg"
    elif [[ $app_changed == true || $api_unit_changed == true ]]; then
        # A proxy already down before the update is not this update's failure.
        local active_before
        active_before=$(systemctl list-units --plain --no-legend --type=service --state=active 'ipv6-proxy@*.service' \
            | sed -nE 's/^ipv6-proxy@([0-9]+)\.service.*/\1/p' | tr '\n' ' ')
        instance_snapshot "$active_before" >"$work_dir/instances.before"
        # Only the API restarts; ipv6-proxy@* are separate units. The API's
        # reconciliation restarts an instance only if its rendered config changed.
        systemctl restart ipv6-proxy-api.service
        healthcheck "$active_before" || fail_tx 'healthcheck apos atualizacao'
        instance_snapshot "$active_before" >"$work_dir/instances.after"
        moved=$(restarted_unchanged "$work_dir/instances.before" "$work_dir/instances.after" | tr '\n' ' ')
        [[ -z $moved ]] || fail_tx "proxies sem mudanca de config reiniciados durante a atualizacao: $moved"
    else
        wait_api || fail_tx 'API nao responde'
    fi
    if [[ $template_changed == true && $migrating == false ]]; then
        say 'Nota: ipv6-proxy@.service mudou; cada proxy adota a nova unidade no proximo reinicio dele (editar/rotacionar/reboot). Nenhum proxy foi reiniciado.'
    fi
    end_tx
    prune_backups
    finish "Atualizacao concluida"
}

finish() {
    # Outside the transaction: a self-test failure is reported, never rolled back.
    if [[ $self_test == true ]]; then run_self_test; fi
    say ''
    say "$1: revisao $source_sha (ref: $source_ref)."
    say "API: $API_URL/docs  |  versao em $app_dir/VERSION"
    say "Token em $env_file (leitura: sudo cat $env_file)."
    say 'CLI: sudo ipv6-proxy list   |   status: systemctl list-units "ipv6-proxy@*"'
    if [[ $source_sha =~ ^[0-9a-f]{40}$ ]]; then
        say "Reinstalar esta mesma revisao: curl -fsSLo install.sh $RAW_URL/$source_sha/install.sh && sudo bash install.sh --update --ref $source_sha"
    fi
    say 'Libere somente as portas dos proxies para clientes autorizados no firewall.'
}

main() {
    set_paths
    parse_args "$@"
    script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-.}")" && pwd)
    if [[ $mode == cli-only || $mode == tui-only ]]; then
        [[ $EUID == 0 ]] || die 'Execute com: sudo bash install.sh'
        install_clients_only "${mode%-only}"
        exit 0
    fi
    check_platform
    local version
    version=$(detect_version)
    if legacy_cron_present; then
        die 'Instalacao legada (cron proxy-startup.sh) detectada. Migre e remova o cron legado antes de instalar a API (docs/MIGRATION.md).'
    fi
    check_foreign_3proxy
    work_dir=$(mktemp -d /tmp/ipv6-proxy-install.XXXXXXXX)
    cleanup_work_dir=true
    trap on_exit EXIT
    case "$version:$mode" in
        none:install) fresh_install ;;
        none:update) die 'Nenhuma instalacao encontrada; execute sem --update.' ;;
        partial:*) die "Instalacao incompleta ou desconhecida encontrada: $(describe_existing). Instalacao preservada; consulte docs/INSTALL.md (secao 'Instalacao interrompida')." ;;
        main:install|ashford:install) die "Versao base ($version) detectada. Use --update para migrar (interrompe os proxies uma vez). Instalacao preservada." ;;
        *:install|*:update)
            [[ -z "$subnet$interface$ipv4" ]] || die 'Instalacao existente: --subnet/--interface/--ipv4 nao sao alterados (veja docs/INSTALL.md).'
            update_install "$version" ;;
    esac
}

[[ ${IPV6_PROXY_INSTALL_LIB:-} == 1 ]] || main "$@"
