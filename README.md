# SRT Bedrock Translator

Ferramenta local para traduzir legendas `.srt` para portugues brasileiro usando LLMs do Amazon Bedrock via AWS CLI.

## Como abrir a UI

```bash
python3 srt_bedrock_translator.py ui
```

Abra `http://127.0.0.1:8765/`. A UI lista os `.srt` da pasta atual, mostra progresso, lote atual, modelo em uso, erros e log detalhado. Use `Testar Bedrock` antes de iniciar se quiser validar credenciais, regiao e modelos.

## Como traduzir pelo terminal

```bash
python3 srt_bedrock_translator.py translate "arquivo.srt"
```

Padroes importantes:

- AWS profile: `default`
- Regiao: `us-east-1`
- Modelo principal: `us.anthropic.claude-sonnet-4-6`
- Saida parcial: `*.pt-BR.EM_ANDAMENTO.srt`
- Saida final OK: `*.pt-BR.OK.srt`
- Saida com pendencias: `*.pt-BR.INCOMPLETO.srt`

## Retomada

O estado fica em `.srt_translator_jobs/<job_id>/` ao lado da legenda original. A cada lote o script salva:

- `state.json`
- `translations.json`
- `events.jsonl`
- `quality_report.json`
- legenda parcial
- sidecar `*.translator-state.json`

Se o processo parar, rode o mesmo comando de novo ou use a UI em `Iniciar ou retomar`. Se voce passar uma legenda `*.INCOMPLETO.srt` que tenha o sidecar ao lado, o script localiza a fonte original e retoma o trabalho pendente.

## Qualidade

O tradutor manda ao modelo:

- contexto anterior com traducao ja feita;
- lote atual, que e o unico que deve ser traduzido;
- proximo lote como contexto;
- guia de contexto do filme criado a partir do nome do arquivo e amostras da legenda;
- contrato JSON estrito, validado antes de aceitar o lote.

Tambem ha deteccao de recusa, resposta fora do contrato, possivel texto nao traduzido e variantes de nomes/grafias intencionais, como `Frued` vs `Freud`.

O QC local verifica:

- cues pendentes ou com erro;
- texto vazio;
- possivel recusa do modelo;
- possivel texto nao traduzido;
- tags simples desbalanceadas ou perdidas (`<i>`, `<b>`, `<u>`, `<font>`);
- marcadores musicais `♪` perdidos;
- mais de 2 linhas;
- linhas acima de 42 caracteres visiveis;
- velocidade de leitura acima de 17 cps.

Erros duros fazem o job tentar refazer os lotes afetados e impedem a saida `.OK.srt`. Avisos de legibilidade ficam no relatorio para revisao.

Para um segundo passe de revisao:

```bash
python3 srt_bedrock_translator.py translate "arquivo.srt" --polish-pass
```

## Bedrock

Nesta maquina, o profile `default` respondeu em `us-east-1` com:

- `us.anthropic.claude-sonnet-4-6`
- `us.anthropic.claude-haiku-4-5-20251001-v1:0`
- `amazon.nova-pro-v1:0`
- `amazon.nova-lite-v1:0`
- `mistral.mistral-large-3-675b-instruct`

Os modelos OpenAI GPT-5.6 no Bedrock apareceram na lista de inference profiles, mas retornaram `AccessDeniedException` nesta conta. Se quiser usa-los, va ao console do Amazon Bedrock em `Model access` ou fale com o suporte/comercial da AWS quando o console indicar que o modelo nao esta disponivel para a conta.

Para listar profiles uteis:

```bash
python3 srt_bedrock_translator.py list-models
```

Para testar tudo antes de traduzir:

```bash
python3 srt_bedrock_translator.py doctor
```

Para auditar uma legenda ja gerada:

```bash
python3 srt_bedrock_translator.py qc "arquivo.pt-BR.OK.srt" --source "arquivo-original.srt"
```

## Referencias usadas para os criterios

- AWS Bedrock: [Use an inference profile in model invocation](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-use.html)
- AWS Bedrock: [Request access to models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html)
- Library of Congress: [SubRip Subtitle format (SRT)](https://www.loc.gov/preservation/digital/formats/fdd/fdd000569.shtml)
- Netflix Partner Help Center: [Timed Text Style Guide: General Requirements](https://partnerhelp.netflixstudios.com/hc/en-us/articles/215758617-Timed-Text-Style-Guide-General-Requirements)
- Netflix Partner Help Center: [Timed Text Style Guide: Subtitle Templates](https://partnerhelp.netflixstudios.com/hc/en-us/articles/219375728-Timed-Text-Style-Guide-Subtitle-Templates)
