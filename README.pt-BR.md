# jellyfin-ondemand

**Um catálogo no Jellyfin que só baixa quando você dá play.**

[English](README.md)

O jellyfin-ondemand coloca filmes e séries das fontes de torrent que **você** configurar nas bibliotecas
do Jellyfin como itens `.strm` leves — pôster, metadados, busca e "continuar assistindo", tudo nativo.
Nada é baixado até alguém dar play. Aí o torrent começa no qBittorrent (sequencial, primeira e última
peça antes) e um pequeno gateway entrega o vídeo ao Jellyfin **enquanto baixa**. Cumprida a regra de seed
da fonte e sem ninguém assistindo, o torrent é apagado e o título volta a ser só catálogo.

- **Traga seus fornecedores.** Adaptadores genéricos para **Torznab** (Prowlarr/Jackett: centenas de
  indexadores sem programar), trackers **UNIT3D**, **feeds RSS no qBittorrent** e o **Internet Archive**
  (filmes em domínio público, para o primeiro teste). Ou escreva um fornecedor em ~40 linhas de Python.
- **Busca fora da biblioteca.** Uma seção na própria página de busca do Jellyfin mostra filmes e séries
  que você ainda não tem. Um toque adiciona o filme e já toca; em série, adiciona todas as temporadas
  disponíveis.
- **Séries bem montadas.** O sistema escolhe uma release por episódio entre todas as fontes, olhando a
  coerência da temporada, a qualidade da fonte, PROPER, pacote ou episódio avulso e seeds. A identidade
  é conferida na lista de episódios do TVmaze, e só os buracos das suas séries locais são preenchidos.
- **Respeita trackers privados.** O torrent privado nunca é abandonado pela metade. A regra de seed vem
  de cada fornecedor ("72 h OU ratio 1.0") e há filtro de freeleech. A passkey nunca vai para log, e só
  o qBittorrent baixa o `.torrent`.
- **O play vem primeiro.** Escrita do catálogo e trabalho pesado esperam ninguém estar assistindo. Os
  outros downloads ficam limitados enquanto um título do catálogo toca e ainda baixa.
- **Legenda no seu idioma** buscada no play (SubDL) e trocada sem reiniciar o vídeo.
- **Extras opcionais para o jellyfin-web:** qualidade adaptativa com escadas HLS pré-geradas, segmentos
  TS fora de casa (pulo exato na timeline) e troca de canal (zapping) na TV ao vivo.

Só a biblioteca padrão do Python, sem dependências. Usa uns 20 MB de RAM e roda num Celeron.

> **Aviso legal.** O projeto não traz nenhuma fonte de conteúdo. Quem escolhe os fornecedores é você,
> e cabe a você cumprir a lei e as regras de cada fonte. Veja o [DISCLAIMER.md](DISCLAIMER.md).

## Início rápido (Docker)

```bash
git clone https://github.com/NAbobo101/jellyfin-ondemand && cd jellyfin-ondemand
mkdir -p config data catalog downloads && touch catalog/.ondemand-marker
cp config.example.toml config/config.toml                 # edite: fornecedores, idioma da legenda…
cp secrets.example.toml config/secrets.toml && chmod 600 config/secrets.toml
docker compose -f docker/compose.example.yml up -d --build
```

Depois, uma vez só (detalhes em [docs/jellyfin-setup.md](docs/jellyfin-setup.md), em inglês):

1. Crie uma chave de API no Jellyfin e coloque-a em `config/secrets.toml`. Faça a WebUI do qBittorrent
   aceitar o container do ondemand: whitelist da sub-rede, ou usuário e senha nos segredos.
2. Adicione `/catalog/Movies` a uma biblioteca de Filmes, `/catalog/Movies 4K` a outra separada e
   `/catalog/Series` à de Séries. Desligue o **monitoramento em tempo real** dessas bibliotecas.
3. Em cada usuário, ligue **"Forçar transcodificação de mídia remota"** (os `.strm` exigem isso).
4. Instale o plugin [JavaScript Injector](https://github.com/n00bcodr/Jellyfin-JavaScript-Injector) e rode
   `JELLYFIN_URL=http://localhost:8096 JELLYFIN_API_KEY=… bash web/install-script.sh web/search.js "On demand: search"`.
   Os textos aparecem em português quando o Jellyfin está em português.
5. `docker compose -f docker/compose.example.yml exec ondemand python3 -m ondemand.cli preview` mostra o
   que os fornecedores colocariam no catálogo. Depois rode `… cli import`, ou espere o agendador.

## Fornecedores

```toml
[[providers]]
name = "meu-indexador"
type = "torznab"                  # torznab | unit3d | qbit_rss | archive_org | python
url = "http://prowlarr:9696/1/api"
private = true
seed = { min_hours = 0, min_ratio = 1.0 }
```

Os segredos ficam em `secrets.toml`, em `[providers.meu-indexador]`. Para escrever o seu, veja
[docs/writing-a-provider.md](docs/writing-a-provider.md).

## Licença

[GPL-3.0](LICENSE).
