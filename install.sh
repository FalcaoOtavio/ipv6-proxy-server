#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

usage() {
    cat <<'EOF'
Instala IPv6 Proxy Server API e o cliente CLI no Ubuntu 22.04+ / Debian 12+.

  sudo bash install.sh
  sudo bash install.sh --subnet 2001:db8:1234::/64 --interface eth0 --ipv4 192.0.2.10
  sudo bash install.sh --cli-only

Sem argumentos, pergunta a subnet e sugere interface/IPv4 locais.
--cli-only requer checkout completo; instala/atualiza apenas o CLI em uma API existente.
A subnet deve ser dedicada e roteada pelo provedor. Nao configura NDP/firewall.
Instalacoes existentes sao preservadas; o modo normal nao faz atualizacoes.
EOF
}
die() { printf 'Erro: %s\n' "$*" >&2; exit 1; }
subnet='' interface='' ipv4='' cli_only=false
while (($#)); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --cli-only) cli_only=true; shift ;;
        --subnet|--interface|--ipv4)
            if (($# < 2)) || [[ -z "$2" || "$2" == --* ]]; then
                die "Falta valor para $1"
            fi
            case "$1" in
                --subnet) subnet=$2 ;;
                --interface) interface=$2 ;;
                --ipv4) ipv4=$2 ;;
            esac
            shift 2 ;;
        *) die "Opcao desconhecida: $1 (use --help)" ;;
    esac
done

[[ $(uname -s) == Linux ]] || die 'Execute na VPS Linux, nao no macOS/Windows.'
[[ $EUID == 0 ]] || die 'Execute com sudo bash install.sh.'
app_dir=/opt/ipv6-proxy-server
env_file=/etc/ipv6-proxy-api.env
unit_file=/etc/systemd/system/ipv6-proxy-api.service
cli_file=/usr/local/bin/ipv6-proxy
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if [[ $cli_only == true ]]; then
    [[ -f "$script_dir/ipv6-proxy" ]] || die '--cli-only requer checkout completo deste repositorio (install.sh e ipv6-proxy juntos); consulte docs/INSTALL.md.'
    [[ -z "$subnet$interface$ipv4" ]] || die '--cli-only nao aceita --subnet, --interface ou --ipv4.'
    [[ -d "$app_dir" && -f "$env_file" && -f "$unit_file" ]] || die 'API existente nao reconhecida; use --cli-only somente apos a instalacao completa.'
    if [[ -e "$cli_file" || -L "$cli_file" ]]; then
        [[ -f "$cli_file" && ! -L "$cli_file" ]] || die "Destino inseguro: $cli_file"
        [[ $(stat -c '%U:%G:%a' "$cli_file") == root:root:755 ]] || die "Destino nao reconhecido: $cli_file"
        grep -q '^# ipv6-proxy-api-client$' "$cli_file" || die "Destino nao pertence a este projeto: $cli_file"
    fi
    temporary=$(mktemp /usr/local/bin/.ipv6-proxy.XXXXXXXX)
    trap 'rm -f -- "${temporary:-}"' EXIT
    install -o root -g root -m 0755 "$script_dir/ipv6-proxy" "$temporary"
    mv -f -- "$temporary" "$cli_file"
    printf 'CLI instalado: sudo ipv6-proxy --help\n'
    exit 0
fi

[[ -f /etc/os-release ]] || die 'Distribuicao nao identificada.'
# shellcheck disable=SC1091
source /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
    ubuntu:22.04|ubuntu:24.04|ubuntu:26.04|debian:12|debian:13) ;;
    *) die 'Use Ubuntu 22.04/24.04/26.04 ou Debian 12/13.' ;;
esac
[[ -d /run/systemd/system ]] || die 'systemd deve estar ativo (use uma VPS/VM).'
for path in "$app_dir" "$env_file" "$unit_file" /var/lib/ipv6-proxy-api /usr/local/bin/3proxy "$cli_file"; do
    [[ ! -e "$path" && ! -L "$path" ]] || die "Ja existe: $path. Instalacao preservada; consulte docs/INSTALL.md."
done
# Consume the whole crontab: grep -q can SIGPIPE crontab under pipefail.
if [[ -d /root/proxyserver ]] || crontab -l 2>/dev/null | grep 'proxy-startup\.sh' >/dev/null; then
    die 'Instalacao legada detectada. Migre e remova o cron legado antes de instalar a API.'
fi
if pgrep -x 3proxy >/dev/null; then
    die '3proxy ja esta em execucao. Desative o gerenciador legado antes de instalar.'
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl git python3 python3-venv build-essential iproute2 openssl
default_interface=$(ip -j -4 route show default | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r[0].get("dev", "") if r else "")')
interface=${interface:-$default_interface}
if [[ -z "$subnet" ]]; then
    [[ -r /dev/tty ]] || die 'Informe --subnet, --interface e --ipv4 para instalacao sem terminal.'
    read -r -p 'Subnet IPv6 dedicada (ex.: 2001:db8:1234::/64): ' subnet </dev/tty
    read -r -p "Interface [$interface]: " answer </dev/tty
    interface=${answer:-$interface}
fi
[[ "$interface" =~ ^[a-zA-Z0-9_.-]{1,15}$ ]] || die 'Interface invalida.'
addresses=$(ip -j -4 addr show dev "$interface")
default_ipv4=$(printf '%s' "$addresses" | python3 -c 'import json,sys; a=[i["local"] for d in json.load(sys.stdin) for i in d.get("addr_info",[]) if i["family"]=="inet"]; print(a[0] if a else "")')
if [[ -z "$ipv4" ]]; then
    ipv4=$default_ipv4
    if [[ -t 0 ]]; then
        read -r -p "IPv4 local [$ipv4]: " answer </dev/tty
        ipv4=${answer:-$ipv4}
    fi
fi
python3 - "$subnet" "$ipv4" "$addresses" <<'PY'
import ipaddress, json, sys
net = ipaddress.IPv6Network(sys.argv[1])
assert net.prefixlen <= 126, 'Subnet deve ter prefixo <= 126'
assert not net.is_multicast and not net.is_link_local, 'Use uma subnet roteada'
ip = ipaddress.IPv4Address(sys.argv[2])
assert not ip.is_unspecified and not ip.is_multicast, 'IPv4 invalido'
assert str(ip) in [a['local'] for d in json.loads(sys.argv[3]) for a in d.get('addr_info', [])], 'IPv4 deve existir na interface'
PY

work_dir=$(mktemp -d /tmp/ipv6-proxy-install.XXXXXXXX)
trap 'rm -rf -- "$work_dir"' EXIT
trap 'printf "Falha na linha %s. Consulte a mensagem acima e docs/INSTALL.md.\n" "$LINENO" >&2' ERR
# A downloaded standalone installer has no adjacent CLI; clone the full source.
if [[ -f "$script_dir/proxy_api/app.py" && -f "$script_dir/deploy/ipv6-proxy-api.service" && -f "$script_dir/ipv6-proxy" ]]; then
    source_dir=$script_dir
else
    git clone --depth 1 --branch main https://github.com/FalcaoOtavio/ipv6-proxy-server.git "$work_dir/app"
    source_dir=$work_dir/app
fi
git clone --depth 1 --branch 0.9.4 https://github.com/3proxy/3proxy.git "$work_dir/3proxy"
[[ $(git -C "$work_dir/3proxy" rev-parse HEAD) == 346880eb2e73836385e5f7648f738efcece580ac ]] || die 'Revisao inesperada do 3proxy.'
make -C "$work_dir/3proxy" -f Makefile.Linux -j "$(nproc)"

install -d -m 0755 "$app_dir" "$app_dir/proxy_api"
install -m 0644 "$source_dir"/proxy_api/*.py "$app_dir/proxy_api/"
install -m 0644 "$source_dir/requirements.txt" "$app_dir/requirements.txt"
python3 -m venv "$app_dir/.venv"
"$app_dir/.venv/bin/pip" install -r "$app_dir/requirements.txt"
install -m 0755 "$work_dir/3proxy/bin/3proxy" /usr/local/bin/3proxy
install -o root -g root -m 0755 "$source_dir/ipv6-proxy" "$cli_file"
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
EOF
chmod 0600 "$env_file"
install -m 0644 "$source_dir/deploy/ipv6-proxy-api.service" "$unit_file"
systemctl daemon-reload
systemctl enable --now ipv6-proxy-api
# Keep the token out of command-line arguments and process listings.
printf 'header = "Authorization: Bearer %s"\n' "$token" >"$work_dir/curl.conf"
ready=false
for ((attempt=0; attempt<30; attempt++)); do
    if curl --config "$work_dir/curl.conf" --noproxy '*' --fail --silent --max-time 2 http://127.0.0.1:8000/proxies > /dev/null; then
        ready=true
        break
    fi
    sleep 1
done
[[ "$ready" == true ]] || die 'API nao iniciou. Execute: journalctl -u ipv6-proxy-api -n 50'
printf '\nInstalacao concluida! API: http://127.0.0.1:8000/docs\n'
printf 'Token salvo em %s (leitura: sudo cat %s).\n' "$env_file" "$env_file"
printf 'CLI: sudo ipv6-proxy list (use --help para ver os comandos).\n'
printf 'Status: sudo systemctl status ipv6-proxy-api\n'
printf 'Acesso remoto: ssh usuario@IP_PUBLICO "sudo ipv6-proxy list"\n'
printf 'Libere somente as portas dos proxies para clientes autorizados no firewall.\n'
