# Migracao para proxies independentes

Vale para instalacoes da versao base: `aa29685` (main do fork) e `bdfe9c7`
(branch `ashford/isolated-mutations`). Nelas todos os 3proxy sao processos
filhos da API, dentro do cgroup de `ipv6-proxy-api.service` (um processo unico
em aa29685, um por porta em bdfe9c7). Na versao nova cada proxy e a unidade
`ipv6-proxy@<porta>.service`.

## O que acontece

**A migracao interrompe todos os proxies uma unica vez**, por alguns segundos:
parar a API antiga encerra os 3proxy filhos dela. O instalador anuncia isso,
mostra quantos proxies serao afetados e exige confirmacao (digitar `MIGRAR` no
terminal, ou `--yes` sem terminal).

```bash
REF=<sha-ou-tag-aprovado>
curl -fsSLo install-ipv6-proxy.sh "https://raw.githubusercontent.com/FalcaoOtavio/ipv6-proxy-server/$REF/install.sh"
sudo bash install-ipv6-proxy.sh --update --ref "$REF"          # pergunta
sudo bash install-ipv6-proxy.sh --update --ref "$REF" --yes    # automacao
```

Passos:

1. Detecta a versao instalada (aa29685, bdfe9c7 ou nova) e confere que
   `PROXY_DATA_DIR`/`PROXY_3PROXY_BINARY` estao nos caminhos padrao.
2. Backup completo em `/var/backups/ipv6-proxy/<data>-<pid>/` (0700): app,
   `/etc/ipv6-proxy-api.env`, `/var/lib/ipv6-proxy-api`, unidades, CLI/TUI e
   binario 3proxy.
3. Instala a nova revisao (codigo, venv, `ipv6-proxy-api.service`,
   `ipv6-proxy@.service`, CLI/TUI) e grava `VERSION`.
4. Para a API antiga (interrupcao) e inicia a nova. Na partida, a API:
   - converte `proxies.json` (aa29685) em `state.json` v2, gerando o `id`
     imutavel de cada proxy e guardando `proxies.json.v1-backup`;
   - cria e habilita uma unidade `ipv6-proxy@<porta>` por proxy do estado, com
     configuracao propria `proxies/<porta>.cfg` (0600);
   - mantem os IPv6 ja atribuidos (journal `owned-addresses.json`).
5. Healthcheck: API autenticada, cada porta do estado ativa como unidade
   propria e nenhum 3proxy restante no cgroup da API. Remove o
   `3proxy.cfg` unico antigo.

Portas, IPv6, credenciais e owners sao preservados. O token nao muda.

## Rollback automatico

Se qualquer passo falhar, o instalador:

1. para a API nova e desabilita, uma a uma, as unidades `ipv6-proxy@<porta>`
   criadas;
2. restaura app, env, estado, unidades, CLI/TUI e 3proxy do backup;
3. recarrega o systemd e inicia a API anterior, que volta a subir seus 3proxy.

Cada passo e tentado mesmo se o anterior falhar e aparece como `ok` ou
`FALHOU`; a API anterior e sempre iniciada. Se algum passo falhar, conclua com
o rollback manual abaixo.

Os proxies voltam a funcionar na versao anterior (nova interrupcao curta).

## Rollback manual

Use um backup de `/var/backups/ipv6-proxy/` (o mais recente e o anterior a
ultima atualizacao; atualizacoes antigas usavam `/var/backups/ipv6-proxy-<data>-<pid>`).
Cada item do backup ou existe (copie de volta) ou aparece como `<nome>.absent`
(o item nao existia: remova-o). O trecho abaixo trata os dois casos para todos
os itens: app, env, unidades, estado, CLI, TUI e binario 3proxy.

```bash
B=/var/backups/ipv6-proxy/<data>-<pid>
sudo systemctl stop ipv6-proxy-api
for unit in $(systemctl list-units --all --plain --no-legend 'ipv6-proxy@*.service' | cut -d' ' -f1); do
    sudo systemctl disable --now "$unit"   # um proxy por vez, nome explicito
done
restore() {  # item-do-backup destino
    sudo rm -rf "$2"
    if [ -e "$B/$1" ]; then sudo cp -a "$B/$1" "$2"
    elif [ ! -e "$B/$1.absent" ]; then echo "backup sem $1: verifique $2 manualmente" >&2; fi
}
restore app      /opt/ipv6-proxy-server
restore env      /etc/ipv6-proxy-api.env
restore api-unit /etc/systemd/system/ipv6-proxy-api.service
restore template /etc/systemd/system/ipv6-proxy@.service
restore state    /var/lib/ipv6-proxy-api
restore cli      /usr/local/bin/ipv6-proxy
restore tui      /usr/local/bin/ipv6-proxy-tui
restore 3proxy   /usr/local/bin/3proxy
sudo systemctl daemon-reload
sudo systemctl start ipv6-proxy-api
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $(sudo sed -n 's/^PROXY_API_TOKEN=//p' /etc/ipv6-proxy-api.env)" \
    'http://127.0.0.1:8000/proxies?include=status'   # 200 = API anterior respondendo
```

Na versao base (aa29685/bdfe9c7) a API anterior sobe os proprios 3proxy ao
iniciar. Numa instalacao ja independente, a API restaurada recria as unidades
`ipv6-proxy@<porta>` do estado restaurado.

## Depois da migracao

Atualizacoes seguintes reiniciam apenas a API; os proxies so reiniciam se a
configuracao gerada deles mudar. Veja [INSTALL.md](INSTALL.md#atualizar).

## Limitacoes

- `PROXY_DATA_DIR` diferente de `/var/lib/ipv6-proxy-api` ou
  `PROXY_3PROXY_BINARY` diferente de `/usr/local/bin/3proxy`: o instalador
  recusa migrar. Mova os dados/binario para os caminhos padrao antes.
- O instalador legado por cron (`ipv6-proxy-server.sh`) nao e migrado.
- A validacao em systemd real (instalacao, upgrade, reboot) e feita na rodada
  de VM (tests/e2e); os testes locais usam stubs.
