# Changelog

Todas as alterações notáveis deste projeto ficam registadas neste ficheiro.
Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).

## 0.5.0 — primeira versão pública (junho de 2026)

Publicação inicial do SoroRenderFarm.

- **Add-on para o Blender (máquina principal)**: separador "SoroFarm" na barra lateral da vista 3D, com gestão de slaves (adicionar por IP, pausar, reiniciar, remover), render distribuído por pedaços (1–64), composição final dos EXR em `final.png`/`final.exr` e botão de Denoise.
- **Worker para os slaves**: `python soro_renderfarm.py --worker`, sem dependências externas (só biblioteca standard do Python), deteção automática do Blender (ou `BLENDER_PATH`), arranque automático com o Windows (`--install-startup`) e registo em `worker.log`.
- **Comunicação**: HTTP na rede local, porta 8765, apenas com endereços privados. Resultados na pasta partilhada, em `SoroFarm/<data_hora>/`.
