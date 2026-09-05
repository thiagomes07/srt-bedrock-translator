# SRT Bedrock Translator

Traduz legendas `.srt` para português brasileiro usando os modelos do Amazon Bedrock.

Não é tradução linha a linha: a legenda é traduzida em blocos, cada bloco enxerga o
anterior e o seguinte, e um guia do filme gerado no início mantém nomes, tom e formas de
tratamento coerentes do começo ao fim. Um filme de duas horas leva alguns minutos.

O que ele resolve, além de traduzir:

- **Retoma de onde parou.** Fechou o notebook, caiu a internet, matou o processo: o
  progresso está em disco e nada é retraduzido nem cobrado duas vezes.
- **Não desiste.** Erro de rede, limite de uso ou resposta quebrada entram em nova
  tentativa com espera crescente, e caem para o próximo modelo da fila se preciso.
- **Diz na cara se deu certo.** O arquivo final sai com `OK` ou `INCOMPLETO` no nome, e
  uma legenda incompleta pode ser reenviada para completar só o que faltou.
- **Confere o resultado.** Um controle de qualidade local checa itálico quebrado, símbolo
  de música perdido, texto que ficou em inglês, linha longa demais e velocidade de leitura.
- **Interface web** para escolher a legenda, acompanhar o log ao vivo e retomar trabalhos,
  com explicação de cada campo na própria tela.

Sem dependências: só a biblioteca padrão do Python e o AWS CLI.

---

## O que você precisa antes

| Requisito | Como conferir |
|---|---|
| Python 3.10 ou mais novo | `python3 --version` |
| AWS CLI configurado | `aws configure list-profiles` |
| Acesso liberado a algum modelo do Bedrock | veja abaixo |

O acesso aos modelos **não vem liberado por padrão**. No console da AWS, abra
**Amazon Bedrock → Model access** e peça acesso aos modelos que quiser usar. A liberação
costuma ser imediata. Sem isso, toda chamada volta com `AccessDeniedException`.

## Configurar (uma vez)

Copie o exemplo e ajuste com o seu perfil do AWS CLI:

```bash
cp srt_translator.local.json.example srt_translator.local.json
```

```json
{
  "profile": "default",
  "region": "us-east-1"
}
```

Esse arquivo não vai para o git. Se preferir, use as variáveis `AWS_PROFILE` e
`AWS_REGION` — elas têm prioridade sobre ele.

Confirme que está tudo de pé antes de gastar tempo com um filme inteiro:

```bash
python3 srt_bedrock_translator.py doctor
```

Ele faz uma chamada mínima a cada modelo da lista e diz quais responderam.

## Como abrir a UI

```bash
python3 srt_bedrock_translator.py ui
```

Abra `http://127.0.0.1:8765/`.

Para trabalhar com as legendas de outra pasta, aponte para ela:

```bash
python3 srt_bedrock_translator.py ui --base "/caminho/da/pasta/do/filme"
```

Se a porta estiver ocupada, use `--port 8766`.

Na tela: escolha a legenda no primeiro campo, clique em **Testar Bedrock** se for a
primeira vez do dia, e depois em **Iniciar ou retomar**. Todo campo, botão e número tem um
ícone **ⓘ** ao lado que explica o que faz e mostra um exemplo — inclusive dizendo quais
você pode ignorar (quase todos).

Enquanto roda você acompanha progresso, bloco atual, modelo em uso, log colorido e as
falas sendo traduzidas em tempo real. Ao terminar, a tela mostra o nome e a pasta do
arquivo final, com botão de copiar o caminho.

## Como traduzir pelo terminal

```bash
python3 srt_bedrock_translator.py translate "arquivo.srt"
```

Para a melhor qualidade possível, com uma segunda passada de revisão (dobra o tempo e o
custo):

```bash
python3 srt_bedrock_translator.py translate "arquivo.srt" --polish-pass
```

Opções mais usadas:

| Opção | Para quê |
|---|---|
| `--profile` / `--region` | sobrescrevem a configuração local |
| `--models a,b,c` | define a fila de modelos |
| `--batch-size 20` | blocos menores, quando as respostas vêm quebradas |
| `--max-cues 20` | traduz só as primeiras legendas, para testar rápido |
| `--polish-pass` | segunda passada de revisão |

Veja tudo com `python3 srt_bedrock_translator.py translate --help`.

## Os arquivos que ele gera

Tudo fica na mesma pasta da legenda original.

| Arquivo | O que é |
|---|---|
| `*.pt-BR.EM_ANDAMENTO.srt` | parcial, enquanto trabalha |
| `*.pt-BR.OK.srt` | pronto, sem erro grave |
| `*.pt-BR.INCOMPLETO.srt` | terminou faltando coisa |
| `*.translator-state.json` | etiqueta que liga a legenda ao trabalho que a gerou |
| `.srt_translator_jobs/` | progresso, log e relatório de qualidade |

A legenda traduzida é um `.srt` comum em UTF-8, com a mesma quantidade de legendas e os
mesmos tempos do original — sincroniza igual e abre direto no player. Ao terminar, as
versões antigas daquele trabalho são apagadas, para não sobrar arquivo parecido
confundindo qual é o bom.

## Retomar um trabalho

Três caminhos, todos equivalentes:

- Na UI, selecione a legenda e clique em **Iniciar ou retomar**.
- No terminal, rode o mesmo comando `translate` de novo.
- Passe a legenda `*.INCOMPLETO.srt` como entrada: pelo arquivo de etiqueta ao lado dela,
  ele reconhece o trabalho de origem e completa só o que faltou.

Se a interface mostrar `stalled`, significa que o progresso está salvo mas o processo
morreu. Clique em **Retomar selecionado**.

## Comandos auxiliares

```bash
python3 srt_bedrock_translator.py doctor       # testa credencial, região e acesso aos modelos
python3 srt_bedrock_translator.py list-models  # lista os modelos visíveis na sua conta
python3 srt_bedrock_translator.py self-test    # testes internos, sem chamar a AWS
python3 srt_bedrock_translator.py qc "traduzida.srt" --source "original.srt"
```

## Como a qualidade é garantida

Cada bloco vai ao modelo com o bloco anterior já traduzido, o bloco seguinte como
contexto, o guia do filme e um formato de resposta obrigatório. A resposta só é aceita
depois de passar por validação.

O controle de qualidade separa dois níveis:

- **Erro grave** — texto vazio, recusa do modelo, tag `<i>` desbalanceada, símbolo `♪`
  perdido, trecho que ficou em inglês. Enquanto existir, o arquivo sai como `INCOMPLETO`.
- **Aviso** — mais de duas linhas, linha acima de 42 caracteres, leitura rápida demais.
  Fica registrado no relatório, mas não bloqueia o arquivo.

A velocidade de leitura é medida **contra a legenda original**, não contra um número fixo.
Muita legenda comercial já passa do limite confortável na fonte; cobrar o número absoluto
marcaria metade do filme e esconderia o problema real. O aviso só aparece quando a
tradução ficou mais de 15% mais lenta de ler do que o original já era.

### Quando a heurística erra: consenso entre modelos

"Parece não traduzido" é heurística, e heurística erra. Refrão de música
(`♪ Guli guli guli guli ram sam sam ♪`), onomatopeia e nome repetido são texto que deve
mesmo ficar igual ao original.

Para isso não travar o trabalho:

1. a falha de heurística é tratada como *leve*: a resposta é estruturalmente válida, só o
   texto é suspeito;
2. cada nova tentativa leva no pedido o motivo da recusa anterior e quais falas corrigir;
3. se dois modelos diferentes devolvem exatamente as mesmas falas suspeitas, a tradução é
   aceita e essas falas ficam marcadas como **revisar**;
4. falas aceitas por consenso viram aviso, não erro grave, então não bloqueiam o `OK`.

Quebra estrutural nunca é aceita por consenso: resposta ilegível, fala faltando ou
sobrando, recusa explícita, tag desbalanceada e símbolo de música perdido continuam
sendo erro grave.

## Custo

O custo é o do Bedrock, cobrado por token na sua conta AWS — a ferramenta em si é
gratuita. Como ordem de grandeza: um filme de cerca de 2.400 legendas consumiu por volta
de 800 mil tokens no total, entre entrada e saída, usando Claude Sonnet como modelo
principal. `--polish-pass` praticamente dobra isso. Consulte a tabela de preços da AWS
para o valor no seu caso.

## Referências usadas para os critérios

- AWS Bedrock: [Use an inference profile in model invocation](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-use.html)
- AWS Bedrock: [Request access to models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html)
- Library of Congress: [SubRip Subtitle format (SRT)](https://www.loc.gov/preservation/digital/formats/fdd/fdd000569.shtml)
- Netflix Partner Help Center: [Timed Text Style Guide: General Requirements](https://partnerhelp.netflixstudios.com/hc/en-us/articles/215758617-Timed-Text-Style-Guide-General-Requirements)
- Netflix Partner Help Center: [Timed Text Style Guide: Subtitle Templates](https://partnerhelp.netflixstudios.com/hc/en-us/articles/219375728-Timed-Text-Style-Guide-Subtitle-Templates)
