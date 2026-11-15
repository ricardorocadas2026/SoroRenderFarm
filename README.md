# SoroRenderFarm

![SoroRenderFarm — render distribuído para Blender](banner.svg)

Idiomas: Português · [English](README.en.md)

Render distribuído em rede local para o Blender, num único ficheiro Python. O add-on divide uma imagem em pedaços, distribui o trabalho por vários computadores e reúne os resultados numa imagem final, com opção de Denoise.

**Versão 0.5.0** · Blender 3.6 ou mais recente · Windows como ambiente de referência

## Começar

- [Instruções completas em português](README.md#como-funciona) — instalação, configuração, utilização e resolução de problemas.
- [Full documentation in English](README.en.md)
- [Descarregar o add-on](soro_renderfarm.py)
- [Versões e atualizações](https://github.com/ricardorocadas2026/SoroRenderFarm/releases)

O add-on é instalado no Blender da máquina principal. Nos computadores de trabalho (slaves), execute o Worker com `py soro_renderfarm.py --worker`. Todos os computadores precisam da mesma versão do Blender, acesso à mesma pasta partilhada e ligação à rede local.

## Segurança

O Worker destina-se apenas a redes locais de confiança. Não tem autenticação; não exponha a porta 8765 à Internet. Consulte a documentação para os requisitos e limitações completos.

## Licença

© Soro Arquitetos · [MIT License](LICENSE)
