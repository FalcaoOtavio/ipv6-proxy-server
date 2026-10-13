# ADR 0007: SOCKS5 UDP ASSOCIATE bloqueado com proteção anti-SSRF ligada

- Data: 2026-10-12
- Status: aceito

## Contexto
Reauditoria T22 (N1, ALTO): a ACL do 3proxy 0.9.4 só verifica o endereço declarado no UDP ASSOCIATE; declarando um IPv6 público, o relay entrega datagramas a [::1] (provado com 3proxy real). Além disso o ASSOCIATE padrão (0.0.0.0) fica sem resposta com o bloqueio ligado, então a capacidade socks5_udp_associate anunciada (contrato ashford) não funciona em produção.

## Decisão
Com PROXY_BLOCK_PRIVATE_DESTINATIONS=true (padrão): regra 'deny * * * * UDPASSOC' no 3proxy e /capabilities deixa de anunciar socks5_udp_associate (capabilities = conjunto comprovado). Só com o bloqueio desligado (uso explícito, documentado como inseguro) UDP é anunciado. Defesa em profundidade: IPAddressDeny= de faixas IPv6 internas (::1, fc00::/7, fe80::/10, ::ffff:0:0/96, ff00::/8, subnet própria) na unit ipv6-proxy@ — validação real só em systemd (T14).

## Alternativas
Manter UDP anunciado com SSRF via relay (inaceitável); confiar só em IPAddressDeny (não testável neste host e ausente no modo local); patch no 3proxy (fora do escopo).

## Consequências
Clientes que dependam de UDP (ex.: Ashford) devem consultar /capabilities; mudança informada ao Hermes. Contrato HTTP inalterado; só o conjunto anunciado muda conforme configuração.
