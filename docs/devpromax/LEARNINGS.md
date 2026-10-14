# Aprendizados — IPv6 Proxy Server Independente

- **Snapshots/evidências reescritos pela suíte geram conflitos em todo merge paralelo.** Testes não podem gravar no repo por padrão (usar tmp ou flag
  `UPDATE_SNAPSHOTS=1`/`E2E_WRITE_EVIDENCE=1`). Custou 3 rodadas de conflito (T17, T19, T20).
- **3proxy 0.9.4:** `auth none` ignora ACLs (use `auth iponly`); ACL de UDP ASSOCIATE só vê o endereço declarado (bloqueie `UDPASSOC`); SIGTERM leva ~5 s
  (use `KillSignal=SIGINT`, ~2 ms); UDPASSOC negado não responde erro (segura ~30 s).
- **Rich/Textual:** qualquer dado externo exibido precisa ser `Text()`/`markup=False` — `:1234:` vira emoji e `[bold]` vira markup.
- **bash `set -E` + `trap ERR`:** `die`/`exit` não dispara ERR; rollback precisa de `trap EXIT` com rc e guarda `BASHPID`; rollback com `set +e`.
- **shellcheck:** validar na versão do runner (0.9 no ubuntu-24.04), não só na local (0.11).
- **Revisor independente + segurança antes do QA final** encontraram 7 altos e 2 altos de segurança que os testes não pegavam (rollback, SSRF).
- **Ambiente:** host DPM sem root/docker/KVM — projetos de infra precisam de VM/runner autorizado no brief; sem isso o critério de instalação fica gated.
- **`dpm sync` pode deixar HEAD destacado** após rebase interrompido; conferir `git merge-base --is-ancestor <main> HEAD` antes de reportar.
- **VM descartável sem root/KVM:** QEMU extraído com `apt-get download` + `dpkg -x`, modo TCG, imagem cloud oficial + NoCloud, boot via kernel/initrd com
  timeout de dispositivos maior. Lento (~15 min/boot sob carga), mas achou 4 bugs reais (timeouts, fencing, toggle de segurança, lock do apt) que os testes em
  loopback não pegavam. Use sempre que houver systemd/instalador no escopo — candidato a skill.

## Gates web do `dpm project finish` em projeto CLI/TUI (2026-10-13)
- `finish` exige `dpm e2e run`/`dpm perf check` contra URL de produção sempre que há features em `docs/features.yaml`, mesmo para TUI sem deploy. Também procura `docs/DESIGN.md` fora do repo (copiar de repo/docs).
- Pendência pós-entrega: DevProMax precisa de flag "sem web" no plano para pular esses gates (liberado pelo Hermes na #168, não aplicado).
