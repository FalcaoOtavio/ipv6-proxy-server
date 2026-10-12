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
2. Backup completo em `/var/backups/ipv6-proxy-<data>-<pid>/` (0700): app,
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

Os proxies voltam a funcionar na versao anterior (nova interrupcao curta).

## Rollback manual

Use um backup listado em `/var/backups/` (o mais recente e o anterior a ultima
atualizacao):

```bash
B=/var/backups/ipv6-proxy-<data>-<pid>
sudo systemctl stop ipv6-proxy-api
for unit in $(systemctl list-units --all --plain --no-legend 'ipv6-proxy@*.service' | cut -d' ' -f1); do
    sudo systemctl disable --now "$unit"   # um proxy por vez, nome explicito
done
sudo rm -rf /opt/ipv6-proxy-server /var/lib/ipv6-proxy-api
sudo cp -a "$B/app" /opt/ipv6-proxy-server
sudo cp -a "$B/state" /var/lib/ipv6-proxy-api
sudo cp -a "$B/env" /etc/ipv6-proxy-api.env
sudo cp -a "$B/api-unit" /etc/systemd/system/ipv6-proxy-api.service
[ -e "$B/template" ] && sudo cp -a "$B/template" /etc/systemd/system/ipv6-proxy@.service \
    || sudo rm -f /etc/systemd/system/ipv6-proxy@.service
sudo cp -a "$B/cli" /usr/local/bin/ipv6-proxy
sudo systemctl daemon-reload
sudo systemctl start ipv6-proxy-api
```

Arquivos `<nome>.absent` no backup indicam que o item nao existia; nesse caso
remova-o em vez de copiar.

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
