#!/usr/bin/env python3
"""
SRT -> Portuguese (Brazil) subtitle translator using Amazon Bedrock through AWS CLI.

No third-party Python packages are required. The script provides:
- A CLI translator with resume support.
- A local web UI for choosing .srt files and watching logs/progress.
- Strict JSON contracts for LLM responses.
- Batch translation with previous/current/next context.
- Persistent state só interrupted jobs can be resumed.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import html
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


APP_NAME = "SRT Bedrock Translator"
LOCAL_CONFIG_NAME = "srt_translator.local.json"


def local_defaults() -> dict[str, Any]:
    """Le preferencias da maquina, para o repositorio não carregar dado de ninguem.

    Ordem: $SRT_TRANSLATOR_CONFIG, arquivo ao lado do script, depois ~/.config.
    O arquivo ao lado do script esta no .gitignore de proposito.
    """
    candidates = [
        os.environ.get("SRT_TRANSLATOR_CONFIG"),
        str(Path(__file__).resolve().parent / LOCAL_CONFIG_NAME),
        str(Path.home() / ".config" / "srt-bedrock-translator" / "config.json"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return {}


LOCAL_DEFAULTS = local_defaults()
DEFAULT_PROFILE = os.environ.get("AWS_PROFILE") or LOCAL_DEFAULTS.get("profile") or "default"
DEFAULT_REGION = os.environ.get("AWS_REGION") or LOCAL_DEFAULTS.get("region") or "us-east-1"
DEFAULT_MODELS = [
    "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.amazon.nova-pro-v1:0",
    "amazon.nova-pro-v1:0",
    "mistral.mistral-large-3-675b-instruct",
    "amazon.nova-lite-v1:0",
    "us.amazon.nova-lite-v1:0",
    "mistral.mistral-small-2402-v1:0",
    "meta.llama3-70b-instruct-v1:0",
]

# Quantos modelos distintos precisam concordar no mesmo texto suspeito para que a
# heurística de qualidade seja considerada errada e a tradução seja aceita.
SOFT_CONSENSUS_MODELS = 2

# Quanto a tradução pode ficar mais lenta de ler que a fonte antes de virar aviso.
# Medido num filme real: a expansao PT/EN tem mediana 0.97x e p90 1.19x, entao um
# limiar de 1.15x ficava abaixo da variacao normal e marcava 8% do filme sem motivo.
CPS_REGRESSION_RATIO = 1.35

# Silêncio que sugere troca de cena. Calibrado num filme real: com 4s e o lote já a 75%
# do alvo, os cortes no meio de diálogo caem um terço e o número de chamadas sobe só 5%.
SCENE_GAP_SECONDS = 4.0

# Bump quando as regras de QC mudarem, para relatórios antigos serem recalculados.
QUALITY_REPORT_VERSION = 4

TIME_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}:\d{2},\d{3}\s*-->\s*"
    r"\d{1,2}:\d{2}:\d{2},\d{3}(?:\s+.*)?$"
)
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

REFUSAL_RE = re.compile(
    r"("
    r"\b(?:as an ai|as a language model|como (?:modelo|uma ia|um assistente)|sou uma ia)\b|"
    r"\b(?:i(?:'m| am) sorry|sorry|desculpe|lamento)\b.{0,140}"
    r"\b(?:can't|cannot|can not|unable|not able|não posso|não posso|não consigo|não consigo)\b.{0,140}"
    r"\b(?:translate|traduzir|lyrics?|letras?|copyright|direitos autorais|request|pedido|policy|politica|política)\b|"
    r"\b(?:can't|cannot|can not|unable to|not able to|não posso|não posso|não consigo|não consigo)\b.{0,140}"
    r"\b(?:translate|traduzir|lyrics?|letras?|copyright|direitos autorais|request|pedido)\b|"
    r"\b(?:violates?|viola)\b.{0,140}\b(?:copyright|direitos autorais|policy|politica|política)\b"
    r")",
    re.IGNORECASE,
)
ENGLISH_STOPWORDS = {
    "the",
    "and",
    "you",
    "your",
    "what",
    "when",
    "where",
    "why",
    "who",
    "how",
    "this",
    "that",
    "with",
    "for",
    "from",
    "have",
    "has",
    "had",
    "are",
    "were",
    "was",
    "will",
    "would",
    "could",
    "should",
    "not",
    "don't",
    "didn't",
    "isn't",
    "can't",
    "just",
    "like",
    "about",
    "know",
    "think",
    "going",
    "want",
    "need",
}
MUSICAL_VOCABLES = {
    "ah",
    "awo",
    "awoo",
    "ay",
    "ayee",
    "ba",
    "bam",
    "bang",
    "be",
    "big",
    "cha",
    "da",
    "doo",
    "flash",
    "guli",
    "há",
    "hey",
    "hi",
    "kie",
    "kye",
    "la",
    "ma",
    "me",
    "mi",
    "mo",
    "mu",
    "na",
    "oh",
    "ooh",
    "pa",
    "ra",
    "ramalama",
    "sam",
    "sha",
    "ta",
    "um",
    "whoa",
    "yi",
    "yippie",
    "yay",
    "yeah",
}
COMMON_CAPITALIZED_WORDS = {
    "A",
    "An",
    "And",
    "Are",
    "As",
    "At",
    "But",
    "By",
    "For",
    "From",
    "He",
    "Her",
    "Hey",
    "His",
    "How",
    "I",
    "If",
    "In",
    "Is",
    "It",
    "Mate",
    "My",
    "No",
    "Of",
    "Oh",
    "On",
    "Or",
    "Our",
    "She",
    "Só",
    "The",
    "Then",
    "This",
    "To",
    "We",
    "What",
    "When",
    "Where",
    "Who",
    "Why",
    "You",
    "Your",
}
COMMON_NORMAL_WORDS = {
    *ENGLISH_STOPWORDS,
    "a",
    "an",
    "as",
    "at",
    "be",
    "been",
    "being",
    "can",
    "come",
    "did",
    "does",
    "feel",
    "felt",
    "find",
    "found",
    "get",
    "give",
    "good",
    "great",
    "guy",
    "guys",
    "he",
    "hello",
    "here",
    "hows",
    "its",
    "lets",
    "life",
    "live",
    "look",
    "love",
    "mate",
    "meet",
    "more",
    "okay",
    "only",
    "over",
    "really",
    "seen",
    "sent",
    "some",
    "sure",
    "take",
    "tell",
    "thank",
    "thanks",
    "there",
    "these",
    "they",
    "three",
    "through",
    "wait",
    "walk",
    "well",
    "were",
    "whos",
    "whoa",
    "wont",
    "youll",
    "youre",
    "youve",
}


# Preços por 1.000 tokens, em USD, colhidos da API de preços da AWS para us-east-1.
# A API não publica os Claude 4.x, então eles não aparecem aqui de propósito: é melhor a
# ferramenta dizer "preço não configurado" do que inventar um número e passar por oficial.
BUNDLED_PRICES: dict[str, dict[str, float]] = {
    "nova-pro": {"input": 0.0008, "output": 0.0032, "cache_read": 0.0002, "cache_write": 0.0},
    "nova-lite": {"input": 0.00006, "output": 0.00024, "cache_read": 0.000015, "cache_write": 0.0},
    "nova-micro": {"input": 0.000035, "output": 0.00014},
    "mistral-large-3": {"input": 0.0005, "output": 0.0015},
    "mistral-small": {"input": 0.001, "output": 0.003},
    "claude-3-haiku": {"input": 0.00025, "output": 0.00125},
}
BUNDLED_PRICES_DATE = "2026-09-05"

# Referência para o que a API de preços da AWS não publica. Valores por 1.000 tokens,
# convertidos da tabela oficial da Anthropic (US$/milhão dividido por mil). A própria
# Anthropic avisa que Bedrock é operado pela AWS e pode ter preço próprio, e que
# endpoint regional (o prefixo us.) costuma ter 10% de acréscimo sobre o global —
# por isso isto é referência, não preço final. Ajuste em srt_translator.local.json.
REFERENCE_PRICES_URL = "https://platform.claude.com/docs/en/about-claude/pricing"
REFERENCE_PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-4-6": {"input": 0.003, "output": 0.015, "cache_read": 0.0003, "cache_write": 0.00375},
    "claude-sonnet-4-5": {"input": 0.003, "output": 0.015, "cache_read": 0.0003, "cache_write": 0.00375},
    "claude-sonnet-5": {"input": 0.002, "output": 0.010, "cache_read": 0.0002, "cache_write": 0.0025},
    "claude-haiku-4-5": {"input": 0.001, "output": 0.005, "cache_read": 0.0001, "cache_write": 0.00125},
    "claude-opus-4-5": {"input": 0.005, "output": 0.025, "cache_read": 0.0005, "cache_write": 0.00625},
    "claude-opus-4-6": {"input": 0.005, "output": 0.025, "cache_read": 0.0005, "cache_write": 0.00625},
    "claude-opus-5": {"input": 0.005, "output": 0.025, "cache_read": 0.0005, "cache_write": 0.00625},
}
REFERENCE_PRICES_DATE = "2026-09-05"
BCB_PTAX_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
    "CotacaoDolarPeriodo(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
)
PRICE_KEYS = ("input", "output", "cache_read", "cache_write")
USAGE_TO_PRICE = {
    "inputTokens": "input",
    "outputTokens": "output",
    "cacheReadInputTokens": "cache_read",
    "cacheWriteInputTokens": "cache_write",
}


def normalize_model_key(model_id: str) -> str:
    """us.anthropic.claude-sonnet-4-6 -> claudesonnet46, para casar id com tabela."""
    corpo = re.sub(r"^(us|eu|apac|global)\.", "", str(model_id or ""))
    corpo = re.sub(r"^[a-z0-9]+\.", "", corpo)
    corpo = re.sub(r"-v\d+(:\d+)?$", "", corpo)
    return re.sub(r"[^a-z0-9]", "", corpo.lower())


def price_store_path() -> Path:
    return Path.home() / ".config" / "srt-bedrock-translator" / "prices.json"


def load_prices() -> dict[str, dict[str, Any]]:
    """Tabela de preços por modelo, com a origem de cada entrada.

    Ordem de precedência: preço definido pelo usuário, depois o que foi buscado na
    AWS, depois o instantâneo embutido. A origem viaja junto para a tela nunca
    mostrar uma estimativa com cara de número oficial.
    """
    tabela: dict[str, dict[str, Any]] = {}
    for chave, valores in REFERENCE_PRICES.items():
        tabela[normalize_model_key(chave)] = {
            **valores,
            "_fonte": f"referência Anthropic de {REFERENCE_PRICES_DATE}; a AWS pode cobrar diferente",
            "_referencia": True,
        }
    for chave, valores in BUNDLED_PRICES.items():
        tabela[normalize_model_key(chave)] = {**valores, "_fonte": f"instantâneo AWS de {BUNDLED_PRICES_DATE}"}
    buscados = load_json(price_store_path(), {})
    for chave, valores in (buscados.get("modelos") or {}).items():
        if isinstance(valores, dict):
            tabela[normalize_model_key(chave)] = {
                **{k: v for k, v in valores.items() if k in PRICE_KEYS},
                "_fonte": f"API de preços da AWS ({buscados.get('buscado_em', 'data desconhecida')})",
            }
    for chave, valores in (LOCAL_DEFAULTS.get("prices") or {}).items():
        if isinstance(valores, dict):
            tabela[normalize_model_key(chave)] = {
                **{k: float(v) for k, v in valores.items() if k in PRICE_KEYS},
                "_fonte": "definido por você em srt_translator.local.json",
            }
    return tabela


def load_exchange_rate() -> dict[str, Any] | None:
    """Cotação do dólar guardada em disco, com a data em que o Banco Central a publicou."""
    manual = LOCAL_DEFAULTS.get("usd_brl")
    if isinstance(manual, (int, float)) and manual > 0:
        return {"rate": float(manual), "date": "definida por você", "source": "srt_translator.local.json"}
    guardado = load_json(price_store_path(), {}).get("cambio")
    if isinstance(guardado, dict) and guardado.get("rate"):
        return guardado
    return None


def fetch_exchange_rate(timeout: int = 30) -> dict[str, Any] | None:
    """Busca a cotação PTAX de venda no Banco Central, fonte oficial no Brasil."""
    import urllib.request

    hoje = _dt.datetime.now()
    inicio = (hoje - _dt.timedelta(days=12)).strftime("%m-%d-%Y")
    fim = hoje.strftime("%m-%d-%Y")
    # OData do BCB recusa $ e @ percent-encoded, entao a query e montada a mao.
    params = (
        f"@dataInicial='{inicio}'&@dataFinalCotacao='{fim}'"
        "&$top=1&$orderby=dataHoraCotacao%20desc&$format=json"
        "&$select=cotacaoVenda,dataHoraCotacao"
    )
    try:
        with urllib.request.urlopen(f"{BCB_PTAX_URL}?{params}", timeout=timeout) as resp:
            dados = json.loads(resp.read().decode("utf-8"))
        item = (dados.get("value") or [])[0]
        return {
            "rate": float(item["cotacaoVenda"]),
            "date": str(item["dataHoraCotacao"])[:10],
            "source": "PTAX de venda, Banco Central do Brasil",
        }
    except Exception:
        return None


def price_for_model(model_id: str, tabela: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    alvo = normalize_model_key(model_id)
    if not alvo:
        return None
    if alvo in tabela:
        return tabela[alvo]
    candidatos = [chave for chave in tabela if chave and (chave in alvo or alvo in chave)]
    if not candidatos:
        return None
    return tabela[max(candidatos, key=len)]


def estimate_cost(
    usage_by_model: dict[str, Any],
    tabela: dict[str, dict[str, Any]],
    cambio: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Custo estimado por modelo. Modelo sem preço aparece com custo nulo e aviso."""
    linhas = []
    total = 0.0
    completo = True
    for modelo, uso in sorted((usage_by_model or {}).items()):
        preco = price_for_model(modelo, tabela)
        custo = 0.0
        if preco:
            for campo_uso, campo_preco in USAGE_TO_PRICE.items():
                valor = preco.get(campo_preco)
                if valor is None:
                    continue
                custo += (int(uso.get(campo_uso, 0) or 0) / 1000.0) * float(valor)
        else:
            completo = False
        linhas.append(
            {
                "model": modelo,
                "calls": int(uso.get("calls", 0) or 0),
                "input": int(uso.get("inputTokens", 0) or 0),
                "output": int(uso.get("outputTokens", 0) or 0),
                "cache_read": int(uso.get("cacheReadInputTokens", 0) or 0),
                "cache_write": int(uso.get("cacheWriteInputTokens", 0) or 0),
                "cost_usd": round(custo, 6) if preco else None,
                "cost_brl": round(custo * float(cambio["rate"]), 4) if (preco and cambio) else None,
                "price_source": preco.get("_fonte") if preco else None,
                "price_is_reference": bool(preco.get("_referencia")) if preco else False,
            }
        )
        total += custo
    return {
        "rows": linhas,
        "total_usd": round(total, 6),
        "total_brl": round(total * float(cambio["rate"]), 4) if cambio else None,
        "complete": completo,
        "exchange": cambio,
    }


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def safe_rel(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except Exception:
        return str(path)


def slugish(text: str, limit: int = 80) -> str:
    text = re.sub(r"[^\w.-]+", "_", text, flags=re.UNICODE).strip("._")
    return text[:limit] or "subtitle"


def read_text_with_encoding(path: Path) -> tuple[str, str, str]:
    raw = path.read_bytes()
    newline = "\r\n" if raw.count(b"\r\n") >= raw.count(b"\n") / 2 else "\n"
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc), enc, newline
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace"), "utf-8-replace", newline


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8-sig") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding, newline="")
    os.replace(tmp, path)


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


@dataclass
class SrtCue:
    id: int
    number: str
    timing: str
    text: str

    def as_prompt_item(self, include_time: bool = True) -> dict[str, Any]:
        item: dict[str, Any] = {"id": self.id, "text": self.text}
        if include_time:
            item["time"] = self.timing
            item["srt_index"] = self.number
        return item


@dataclass
class SrtDocument:
    path: Path
    cues: list[SrtCue]
    encoding: str
    newline: str

    @classmethod
    def load(cls, path: Path, max_cues: int | None = None) -> "SrtDocument":
        text, encoding, newline = read_text_with_encoding(path)
        cues = parse_srt(text)
        if max_cues is not None:
            cues = cues[: max(0, max_cues)]
        return cls(path=path, cues=cues, encoding=encoding, newline=newline)


@dataclass
class Batch:
    number: int
    cues: list[SrtCue]
    start_id: int
    end_id: int


@dataclass
class JobConfig:
    source_path: Path
    profile: str = DEFAULT_PROFILE
    region: str = DEFAULT_REGION
    models: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))
    batch_size: int = 28
    max_batch_chars: int = 4300
    context_batches: int = 1
    attempts_per_model: int = 3
    base_backoff: float = 3.0
    max_backoff: float = 120.0
    retry_forever: bool = True
    call_timeout: int = 240
    context_pass: bool = True
    polish_pass: bool = False
    retry_qc_issues: bool = True
    qc_repair_rounds: int = 2
    semantic_review: bool = False
    semantic_min_signals: int = 0
    semantic_autofix: bool = False
    semantic_budget: int = 100000
    semantic_sample_pct: float = 0.10
    max_lines: int = 2
    max_line_length: int = 42
    max_cps: float = 17.0
    max_cues: int | None = None
    output_path: Path | None = None
    job_root: Path | None = None
    force_new: bool = False


class JobStopped(Exception):
    pass


class ContractError(Exception):
    """Resposta fora do contrato.

    `soft=True` marca falhas que vem de heurística de qualidade (ex.: "parece não
    traduzido"), não de quebra estrutural. Heurística pode errar; por isso um erro
    soft nunca pode travar o lote para sempre. Ver `SoftContractError`.
    """

    def __init__(self, message: str, *, soft: bool = False, cue_ids: list[int] | None = None):
        super().__init__(message)
        self.soft = soft
        self.cue_ids = cue_ids or []


class SoftContractError(ContractError):
    """Falha apenas heurística: o payload e estruturalmente valido e utilizavel."""

    def __init__(self, message: str, *, cue_ids: list[int] | None = None, payload: dict[str, str] | None = None):
        super().__init__(message, soft=True, cue_ids=cue_ids)
        self.payload = payload or {}


class BedrockCallError(Exception):
    def __init__(self, message: str, *, retryable: bool = True, unavailable_model: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.unavailable_model = unavailable_model


class JsonLogger:
    def __init__(self, job_dir: Path | None = None, echo: bool = True):
        self.job_dir = job_dir
        self.echo = echo
        self._lock = threading.RLock()
        self.memory: list[dict[str, Any]] = []
        self.max_memory = 400
        if job_dir:
            job_dir.mkdir(parents=True, exist_ok=True)
            self.events_path = job_dir / "events.jsonl"
        else:
            self.events_path = None

    def bind(self, job_dir: Path) -> None:
        with self._lock:
            self.job_dir = job_dir
            self.events_path = job_dir / "events.jsonl"
            job_dir.mkdir(parents=True, exist_ok=True)

    def event(self, level: str, message: str, **data: Any) -> None:
        item = {"ts": utc_now(), "level": level.upper(), "message": message, **data}
        with self._lock:
            self.memory.append(item)
            self.memory = self.memory[-self.max_memory :]
            if self.events_path:
                with self.events_path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        if self.echo:
            extras = ""
            if data:
                small = {
                    k: v
                    for k, v in data.items()
                    if k
                    in {
                        "batch",
                        "total_batches",
                        "model",
                        "attempt",
                        "done",
                        "total",
                        "status",
                        "sleep",
                        "error",
                        "max_tokens",
                    }
                }
                if small:
                    if "error" in small and isinstance(small["error"], str) and len(small["error"]) > 240:
                        small["error"] = small["error"][:240] + "..."
                    extras = " " + json.dumps(small, ensure_ascii=False)
            print(f"[{item['ts']}] {level.upper():7} {message}{extras}", flush=True)

    def tail(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            if self.events_path and self.events_path.exists():
                lines = self.events_path.read_text(encoding="utf-8", errors="replace").splitlines()
                out = []
                for line in lines[-limit:]:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        out.append({"ts": "", "level": "INFO", "message": line})
                return out
            return list(self.memory[-limit:])


def parse_srt(text: str) -> list[SrtCue]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks = re.split(r"\n{2,}", normalized.strip())
    cues: list[SrtCue] = []
    fallback_id = 1
    for raw in blocks:
        lines = raw.split("\n")
        if not lines:
            continue
        while lines and not lines[0].strip():
            lines.pop(0)
        if not lines:
            continue

        number = ""
        timing_idx = 0
        if len(lines) >= 2 and lines[0].strip().isdigit() and TIME_RE.match(lines[1]):
            number = lines[0].strip()
            timing_idx = 1
        else:
            for idx, line in enumerate(lines[:3]):
                if TIME_RE.match(line):
                    timing_idx = idx
                    if idx > 0 and lines[idx - 1].strip().isdigit():
                        number = lines[idx - 1].strip()
                    break
            else:
                continue

        timing = lines[timing_idx].strip()
        body = "\n".join(lines[timing_idx + 1 :]).strip("\n")
        cues.append(SrtCue(id=fallback_id, number=number or str(fallback_id), timing=timing, text=body))
        fallback_id += 1
    return cues


def render_srt(
    cues: list[SrtCue],
    translations: dict[str, dict[str, Any]],
    newline: str = "\r\n",
    include_pending_markers: bool = True,
) -> str:
    blocks = []
    for cue in cues:
        record = translations.get(str(cue.id))
        text = ""
        if record and record.get("status") == "ok" and str(record.get("text", "")).strip():
            text = normalize_subtitle_text(str(record["text"]))
        elif record and record.get("status") in {"needs_review", "qc_error"} and str(record.get("text", "")).strip():
            text = (
                f"[REVISAO_NECESSARIA id={cue.id} lote={record.get('batch', '?')}]\n"
                + normalize_subtitle_text(str(record["text"]))
            )
        elif record and record.get("status") == "error":
            text = (
                f"[ERRO_TRADUCAO id={cue.id} lote={record.get('batch', '?')}]\n"
                + cue.text
            )
        elif include_pending_markers:
            text = f"[TRADUCAO_PENDENTE id={cue.id}]\n{cue.text}"
        else:
            text = cue.text
        blocks.append(newline.join([cue.number, cue.timing, text]))
    return (newline + newline).join(blocks) + newline


def normalize_subtitle_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"^\s*```(?:json|text)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.IGNORECASE)
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def parse_time_ms(value: str) -> int:
    m = re.match(r"^\s*(\d{1,2}):(\d{2}):(\d{2}),(\d{3})", value)
    if not m:
        raise ValueError(f"Timecode inválido: {value}")
    hh, mm, ss, ms = map(int, m.groups())
    return ((hh * 60 + mm) * 60 + ss) * 1000 + ms


def cue_duration_seconds(cue: SrtCue) -> float:
    left, right = cue.timing.split("-->", 1)
    end = right.strip().split()[0]
    duration_ms = max(1, parse_time_ms(end) - parse_time_ms(left.strip()))
    return duration_ms / 1000.0


def visible_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("♪", "")
    text = re.sub(r"\s+", " ", text)
    return html.unescape(text).strip()


def line_lengths(text: str) -> list[int]:
    return [len(visible_text(line)) for line in normalize_subtitle_text(text).split("\n")]


def simple_tag_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in re.finditer(r"</?\s*([a-zA-Z][\w:-]*)\b[^>]*>", text):
        token = match.group(0)
        name = match.group(1).lower()
        key = f"/{name}" if token.startswith("</") else name
        counts[key] = counts.get(key, 0) + 1
    return counts


def unbalanced_tags(text: str) -> list[str]:
    counts = simple_tag_counts(text)
    issues = []
    for tag in ("i", "b", "u", "font"):
        if counts.get(tag, 0) != counts.get(f"/{tag}", 0):
            issues.append(tag)
    return issues


def missing_source_tags(source: str, translated: str) -> list[str]:
    source_counts = simple_tag_counts(source)
    translated_counts = simple_tag_counts(translated)
    missing = []
    for key, count in source_counts.items():
        if key.lstrip("/") not in {"i", "b", "u", "font"}:
            continue
        if translated_counts.get(key, 0) < count:
            missing.append(key)
    return missing


def smart_break_plain(text: str, max_line_length: int) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    if len(visible_text(text)) <= max_line_length:
        return text
    target = len(text) // 2
    candidates = []
    for idx, ch in enumerate(text):
        if ch == " ":
            score = abs(idx - target)
            if idx <= max_line_length + 8:
                score -= 6
            candidates.append((score, idx))
        elif ch in ",.;:!?":
            idx2 = idx + 1
            if idx2 < len(text) and text[idx2 : idx2 + 1] == " ":
                score = abs(idx2 - target) - 10
                candidates.append((score, idx2))
    if not candidates:
        return text
    # Pontuacao ganha peso no score, mas não pode ganhar de caber na linha: sem este
    # filtro o corte "natural" era escolhido mesmo deixando um lado estourado.
    viaveis = [
        (score, idx)
        for score, idx in candidates
        if len(visible_text(text[:idx].strip())) <= max_line_length
        and len(visible_text(text[idx:].strip())) <= max_line_length
    ]
    _, split = min(viaveis or candidates)
    left = text[:split].strip()
    right = text[split:].strip()
    if not left or not right:
        return text
    return left + "\n" + right


def apply_subtitle_formatting(text: str, max_line_length: int, max_lines: int) -> str:
    text = normalize_subtitle_text(text)
    lines = text.split("\n")
    lengths = line_lengths(text)
    if len(lines) <= max_lines and all(length <= max_line_length for length in lengths):
        return text

    def melhor(candidato: str) -> bool:
        novas = line_lengths(candidato)
        if len(novas) > max_lines:
            return False
        if max(novas) <= max_line_length:
            return True
        # Corrigir excesso de linhas vale uma folga pequena no comprimento: legenda de
        # 3 linhas cobre a imagem, enquanto 1 ou 2 caracteres a mais não incomodam.
        if len(lines) > max_lines:
            return max(novas) <= max_line_length * 1.1
        return max(novas) < max(lengths)

    # Legenda de dois falantes usa hifen por linha. Juntar as linhas colaria as falas
    # de duas pessoas numa só, entao esse caso fica intocado de proposito.
    if any(line.lstrip().startswith("-") for line in lines):
        return text

    unida = " ".join(line.strip() for line in lines if line.strip())
    if "<" not in text and ">" not in text:
        candidate = smart_break_plain(unida, max_line_length)
        return candidate if melhor(candidate) else text

    # Um único par de tags envolvendo o texto todo pode ser refluido por dentro.
    wrapper = re.match(r"^(<([ibu])>)(.*)(</\2>)$", text, flags=re.IGNORECASE | re.DOTALL)
    if wrapper and "<" not in wrapper.group(3) and ">" not in wrapper.group(3):
        miolo = " ".join(part.strip() for part in wrapper.group(3).split("\n") if part.strip())
        inner = smart_break_plain(miolo, max_line_length)
        candidate = wrapper.group(1) + inner + wrapper.group(4)
        return candidate if melhor(candidate) else text
    return text


def cue_quality_issues(
    cue: SrtCue,
    record: dict[str, Any] | None,
    *,
    max_lines: int,
    max_line_length: int,
    max_cps: float,
    protected_tokens: list[str] | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    protected_tokens = protected_tokens or []
    status = (record or {}).get("status")
    text = normalize_subtitle_text(str((record or {}).get("text", "")))
    if status in {None, "", "pending"}:
        issues.append({"severity": "pending", "code": "pending", "message": "Tradução ainda pendente."})
        if not text:
            return issues
    elif status != "ok":
        issues.append({"severity": "error", "code": "not_ok", "message": f"Status atual: {status or 'pending'}."})
        if not text:
            return issues
    if not text:
        return [{"severity": "error", "code": "empty", "message": "Tradução vazia."}]
    if "TRADUCAO_PENDENTE" in text or "ERRO_TRADUCAO" in text:
        issues.append({"severity": "error", "code": "marker_in_text", "message": "Marcador técnico apareceu no texto."})
    if text_has_refusal(text):
        issues.append({"severity": "error", "code": "refusal", "message": "Texto parece recusa do modelo."})
    if looks_untranslated(cue.text, text):
        # Se modelos independentes já devolveram este mesmo texto, a heurística perde a
        # última palavra: vira aviso para revisão, não erro que bloqueia o .OK.srt.
        if (record or {}).get("review_flag") == "consenso_heuristica":
            issues.append(
                {
                    "severity": "warning",
                    "code": "untranslated_consensus",
                    "message": "Texto identico a fonte, aceito por consenso entre modelos; confira manualmente.",
                    "models": (record or {}).get("review_models", []),
                }
            )
        else:
            issues.append({"severity": "error", "code": "looks_untranslated", "message": "Texto parece não traduzido."})
    missing_tokens = [token for token in protected_tokens if token not in text]
    if missing_tokens:
        issues.append({"severity": "error", "code": "protected_token_missing", "message": "Token protegido ausente.", "tokens": missing_tokens})
    if unbalanced_tags(text):
        issues.append({"severity": "error", "code": "unbalanced_tags", "message": "Tags HTML simples desbalanceadas.", "tags": unbalanced_tags(text)})
    missing_tags = missing_source_tags(cue.text, text)
    if missing_tags:
        issues.append({"severity": "error", "code": "source_tag_missing", "message": "Tag presente na fonte sumiu na tradução.", "tags": missing_tags})
    source_notes = cue.text.count("♪")
    translated_notes = text.count("♪")
    if source_notes and not translated_notes:
        issues.append({"severity": "error", "code": "music_marker_missing", "message": "Legenda musical perdeu o marcador musical."})
    elif source_notes >= 2 and translated_notes < 2:
        issues.append({"severity": "warning", "code": "music_marker_partial", "message": "Legenda musical deveria manter notas no início e no fim."})
    lengths = line_lengths(text)
    if len(lengths) > max_lines:
        issues.append({"severity": "warning", "code": "too_many_lines", "message": f"{len(lengths)} linhas; alvo: {max_lines}.", "line_count": len(lengths)})
    long_lines = [length for length in lengths if length > max_line_length]
    if long_lines:
        issues.append({"severity": "warning", "code": "long_line", "message": f"Linha acima de {max_line_length} caracteres.", "lengths": long_lines})
    try:
        duration = cue_duration_seconds(cue)
        cps = len(visible_text(text)) / duration
        source_cps = len(visible_text(cue.text)) / duration
        if cps > max_cps:
            # Muitas legendas comerciais já passam do limite na própria fonte. Cobrar o
            # limite absoluto marcaria metade do filme e esconderia o que importa: se a
            # tradução ficou mais lenta de ler do que o original.
            if cps > source_cps * CPS_REGRESSION_RATIO:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "high_cps",
                        "message": f"Velocidade de leitura acima de {max_cps:.1f} cps e pior que a fonte.",
                        "cps": round(cps, 2),
                        "source_cps": round(source_cps, 2),
                    }
                )
            else:
                issues.append(
                    {
                        "severity": "info",
                        "code": "high_cps_inherited",
                        "message": f"Acima de {max_cps:.1f} cps, mas a legenda original já era assim.",
                        "cps": round(cps, 2),
                        "source_cps": round(source_cps, 2),
                    }
                )
    except Exception as exc:
        issues.append({"severity": "warning", "code": "duration_parse", "message": f"Não consegui calcular CPS: {exc}"})
    return issues


def build_quality_report(
    cues: list[SrtCue],
    translations: dict[str, dict[str, Any]],
    *,
    max_lines: int,
    max_line_length: int,
    max_cps: float,
    protected_tokens_by_id: dict[int, list[str]] | None = None,
    glossary: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    protected_tokens_by_id = protected_tokens_by_id or {}
    conflitos = glossary_conflicts(cues, translations, glossary or {}) if glossary else {}
    cues_em_conflito: dict[int, list[dict[str, Any]]] = {}
    for origem, dados in conflitos.items():
        for cue_id in dados["cues"]:
            cues_em_conflito.setdefault(cue_id, []).append({"termo": origem, "esperado": dados["esperado"]})
    cue_reports = []
    counts = {"error": 0, "warning": 0}
    error_cue_ids: set[int] = set()
    warning_cue_ids: set[int] = set()
    pending_cue_ids: set[int] = set()
    for cue in cues:
        issues = cue_quality_issues(
            cue,
            translations.get(str(cue.id)),
            max_lines=max_lines,
            max_line_length=max_line_length,
            max_cps=max_cps,
            protected_tokens=protected_tokens_by_id.get(cue.id, []),
        )
        for conflito in cues_em_conflito.get(cue.id, []):
            issues.append(
                {
                    "severity": "warning",
                    "code": "glossary_gender",
                    "message": (
                        f"O termo {conflito['termo']} foi fixado como {conflito['esperado']}, "
                        "mas aqui apareceu na outra forma de gênero."
                    ),
                    "esperado": conflito["esperado"],
                }
            )
        if issues:
            severities = {item["severity"] for item in issues}
            codes = {item.get("code") for item in issues}
            cue_status = (translations.get(str(cue.id)) or {}).get("status")
            is_pending = "pending" in codes and cue_status in {None, "", "pending"}
            if is_pending:
                pending_cue_ids.add(cue.id)
            elif "error" in severities:
                error_cue_ids.add(cue.id)
            if "warning" in severities:
                warning_cue_ids.add(cue.id)
            for item in issues:
                if not is_pending and item["severity"] in counts:
                    counts[item["severity"]] += 1
            cue_reports.append(
                {
                    "id": cue.id,
                    "srt_index": cue.number,
                    "time": cue.timing,
                    "issues": issues,
                }
            )
    total = len(cues)
    ok = total - len(error_cue_ids) - len(pending_cue_ids)
    return {
        "created_at": utc_now(),
        "report_version": QUALITY_REPORT_VERSION,
        "thresholds": {
            "max_lines": max_lines,
            "max_line_length": max_line_length,
            "max_cps": max_cps,
        },
        "summary": {
            "total_cues": total,
            "ok_cues": ok,
            "error_cues": len(error_cue_ids),
            "pending_cues": len(pending_cue_ids),
            "warning_cues": len(warning_cue_ids),
            "error_count": counts["error"],
            "warning_count": counts["warning"],
        },
        "error_cue_ids": sorted(error_cue_ids),
        "pending_cue_ids": sorted(pending_cue_ids),
        "warning_cue_ids": sorted(warning_cue_ids),
        "cues": cue_reports,
    }


def silence_before(cue: SrtCue, anterior: SrtCue | None) -> float:
    """Segundos de silêncio entre o fim da fala anterior e o início desta."""
    if anterior is None:
        return 0.0
    try:
        fim = parse_time_ms(anterior.timing.split("-->")[1].strip())
        inicio = parse_time_ms(cue.timing.split("-->")[0].strip())
    except Exception:
        return 0.0
    return max(0.0, (inicio - fim) / 1000.0)


SEMANTIC_NEGATION = re.compile(r"\b(not|never|no|nothing|none|neither|nor|cannot)\b|n't", re.IGNORECASE)
SEMANTIC_CONTRAST = re.compile(r"\b(but|although|though|yet|however|unless|except|instead|rather)\b", re.IGNORECASE)
SEMANTIC_MODAL = re.compile(r"\b(if|would|could|should|might|must|unless|suppose)\b", re.IGNORECASE)
SEMANTIC_NUMBER = re.compile(r"\d|\b(one|two|three|four|five|first|second|third|hundred|thousand|million)\b", re.IGNORECASE)

# Frases com que o juiz recua da propria acusacao. Vistas na pratica: ele marcou
# "errado" e explicou que o sentido estava preservado. Sem isso, retraduziriamos
# em cima de uma traducao boa.
JUDGE_HEDGES = (
    "sentido preservado",
    "sentido esta preservado",
    "sentido está preservado",
    "aceitavel",
    "aceitável",
    "funciona",
    "esta correto",
    "está correto",
    "ok —",
    "ok -",
    "apos reanalise",
    "após reanálise",
    "reanalisando",
)


def semantic_risk_signals(source: str, translation: str) -> list[str]:
    """Marcas que costumam acompanhar erro de sentido que o QC estrutural nao ve.

    Calibrado contra erros reais achados por um juiz num filme: perda de termo
    tecnico veio com negacao, distincao colapsada veio com contraste e modal, e
    termo inflado veio com expansao de tamanho.
    """
    limpo_en = visible_text(source)
    limpo_pt = visible_text(translation)
    sinais: list[str] = []
    if SEMANTIC_NEGATION.search(limpo_en):
        sinais.append("negacao")
    if SEMANTIC_CONTRAST.search(limpo_en):
        sinais.append("contraste")
    if SEMANTIC_MODAL.search(limpo_en):
        sinais.append("modal")
    if SEMANTIC_NUMBER.search(limpo_en):
        sinais.append("numero")
    if len(limpo_en) > 12 and limpo_pt:
        razao = len(limpo_pt) / len(limpo_en)
        if razao > 1.4:
            sinais.append("expandiu")
        elif razao < 0.65:
            sinais.append("encurtou")
    return sinais


def select_for_semantic_review(
    cues: list[SrtCue],
    translations: dict[str, dict[str, Any]],
    *,
    min_signals: int = 1,
    budget: int = 600,
    always: set[int] | None = None,
    sample_pct: float = 0.10,
    seed: str = "",
) -> tuple[list[int], int]:
    """Escolhe quais falas mandar ao juiz: as de risco mais uma amostra aleatória.

    Só os sinais de risco deixam um buraco de cobertura: uma inversão total de
    sentido que mantenha o tamanho e não use negação passa despercebida. A amostra
    aleatória dá um piso de cobertura e, de quebra, uma taxa de erro não enviesada.
    Devolve (ids, quantos vieram por risco).
    """
    always = always or set()
    elegiveis: list[tuple[int, int]] = []
    for cue in cues:
        rec = translations.get(str(cue.id))
        if not isinstance(rec, dict) or rec.get("status") != "ok":
            continue
        if not str(rec.get("text", "")).strip():
            continue
        n = len(semantic_risk_signals(cue.text, str(rec["text"])))
        if cue.id in always:
            n += 10
        elegiveis.append((n, cue.id))

    por_risco = sorted((par for par in elegiveis if par[0] >= min_signals), key=lambda i: (-i[0], i[1]))
    escolhidos = [cue_id for _, cue_id in por_risco[:budget]]
    quantos_risco = len(escolhidos)

    sobra = budget - len(escolhidos)
    if sample_pct > 0 and sobra > 0:
        restantes = [cue_id for _, cue_id in elegiveis if cue_id not in set(escolhidos)]
        alvo = min(sobra, int(len(elegiveis) * sample_pct))
        if alvo > 0 and restantes:
            sorteio = random.Random(f"{seed}:{len(elegiveis)}")
            escolhidos += sorteio.sample(restantes, min(alvo, len(restantes)))
    return sorted(set(escolhidos)), quantos_risco


def judge_verdict_is_actionable(item: dict[str, Any], texto_atual: str) -> bool:
    """So aceita acusacao decidida, com alternativa concreta e sem recuo no texto."""
    if str(item.get("veredito", "")).lower() != "errado":
        return False
    sugestao = str(item.get("sugestao", "")).strip()
    if not sugestao or normalize_subtitle_text(sugestao) == normalize_subtitle_text(texto_atual):
        return False
    explicacao = str(item.get("porque", "")).lower()
    if any(h in explicacao for h in JUDGE_HEDGES):
        return False
    return True


def make_batches(
    cues: list[SrtCue],
    batch_size: int,
    max_chars: int,
    scene_gap: float = SCENE_GAP_SECONDS,
) -> list[Batch]:
    """Agrupa as falas em lotes, preferindo fechar numa pausa da cena.

    O corte a cada N falas cai com frequência no meio de um diálogo, justamente
    onde o contexto mais importa. Quando o lote já está perto do tamanho alvo e
    aparece um silêncio longo, ele fecha ali: a troca de assunto vira a fronteira.
    """
    batches: list[Batch] = []
    current: list[SrtCue] = []
    chars = 0
    minimo_para_cortar = max(4, int(batch_size * 0.75))

    def fechar() -> None:
        nonlocal current, chars
        batches.append(Batch(len(batches) + 1, current, current[0].id, current[-1].id))
        current = []
        chars = 0

    anterior: SrtCue | None = None
    for cue in cues:
        cue_len = len(cue.text) + len(cue.timing) + 32
        estourou = current and (len(current) >= batch_size or chars + cue_len > max_chars)
        pausa_boa = (
            current
            and len(current) >= minimo_para_cortar
            and silence_before(cue, anterior) >= scene_gap
        )
        if estourou or pausa_boa:
            fechar()
        current.append(cue)
        chars += cue_len
        anterior = cue
    if current:
        fechar()
    return batches


def file_signature(path: Path) -> str:
    st = path.stat()
    h = hashlib.sha1()
    h.update(str(path.resolve()).encode("utf-8", errors="replace"))
    h.update(str(st.st_size).encode())
    h.update(str(st.st_mtime_ns).encode())
    try:
        with path.open("rb") as fh:
            h.update(fh.read(65536))
    except OSError:
        pass
    return h.hexdigest()[:20]


def infer_movie_title(path: Path) -> dict[str, Any]:
    candidates = [path.stem, path.parent.name]
    cleaned = []
    for candidate in candidates:
        name = re.sub(r"\[[^\]]+\]|\([^\)]*YTS[^\)]*\)", " ", candidate, flags=re.IGNORECASE)
        name = name.replace(".", " ").replace("_", " ")
        name = re.sub(r"\b(2160p|1080p|720p|4k|web|webrip|x265|x264|10bit|aac5 1|aac|yts|gg|bz|bluray|h264|h265|srt|subtitles|english|sdh|en)\b", " ", name, flags=re.IGNORECASE)
        name = re.sub(r"\s+", " ", name).strip(" -._")
        if name:
            cleaned.append(name)
    text = cleaned[0] if cleaned else path.stem
    year = None
    m = YEAR_RE.search(" ".join(candidates))
    if m:
        year = m.group(1)
    title = YEAR_RE.sub("", text).strip()
    title = re.sub(r"\s+", " ", title)
    return {"title_guess": title or path.stem, "year_guess": year, "source_filename": path.name}


FIM_DE_VALOR_JSON = re.compile(r'\s*(?:\}\s*[,\]]|,\s*")')


def repair_json_text_values(raw: str) -> str:
    """Escapa aspas soltas dentro dos valores de "text".

    Legenda cita fala o tempo todo, e de vez em quando o modelo devolve a aspa
    interna sem escapar, o que quebra o JSON inteiro por causa de um caractere.
    Percorre cada valor até o fechamento real (aspa seguida de , ou }) e escapa
    o que estiver no meio.
    """
    out = []
    i = 0
    abertura = re.compile(r'"text"\s*:\s*"')
    while True:
        match = abertura.search(raw, i)
        if not match:
            out.append(raw[i:])
            break
        out.append(raw[i : match.end()])
        j = match.end()
        valor = []
        while j < len(raw):
            ch = raw[j]
            if ch == "\\" and j + 1 < len(raw):
                valor.append(raw[j : j + 2])
                j += 2
                continue
            if ch == '"':
                # Uma aspa só fecha o valor se o que vem depois só pode ser estrutura:
                # fim do objeto seguido de , ou ], ou virgula seguida de outra chave.
                # Sem esse rigor, texto como "supostos", Yancy? seria lido como fim.
                if FIM_DE_VALOR_JSON.match(raw, j + 1):
                    break
                valor.append('\\"')
                j += 1
                continue
            if ch == "\n":
                valor.append("\\n")
                j += 1
                continue
            valor.append(ch)
            j += 1
        out.append("".join(valor))
        i = j
    return "".join(out)


def json_error_context(raw: str, exc: json.JSONDecodeError, span: int = 90) -> str:
    pos = getattr(exc, "pos", 0) or 0
    início = max(0, pos - span)
    return f"...{raw[início:pos]}>>>AQUI>>>{raw[pos : pos + span]}..."


def extract_json_object(text: str) -> Any:
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as primeiro_erro:
        start = raw.find("{")
        end = raw.rfind("}")
        recorte = raw[start : end + 1] if start >= 0 and end > start else raw
        try:
            return json.loads(recorte)
        except json.JSONDecodeError as erro:
            # Antes de queimar uma chamada nova, tenta consertar o caso comum de
            # aspa não escapada. Só aceita se o resultado virar JSON valido.
            try:
                return json.loads(repair_json_text_values(recorte))
            except json.JSONDecodeError:
                pass
            raise ContractError(
                f"JSON inválido: {erro.msg} na posicao {erro.pos}. Trecho: {json_error_context(recorte, erro)}"
            ) from primeiro_erro


def validate_context_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ContractError("Contexto não e objeto JSON.")
    required = ["title_guess", "source_language", "tone", "style_guide_ptbr", "names_and_terms", "continuity_notes"]
    missing = [key for key in required if key not in payload]
    if missing:
        raise ContractError(f"Contexto sem campos obrigatorios: {missing}.")
    if not isinstance(payload.get("style_guide_ptbr"), list) or not payload["style_guide_ptbr"]:
        raise ContractError("Contexto sem style_guide_ptbr útil.")
    if not isinstance(payload.get("names_and_terms"), list):
        raise ContractError("Contexto names_and_terms inválido.")
    if not isinstance(payload.get("continuity_notes"), list):
        raise ContractError("Contexto continuity_notes inválido.")
    return payload


def glossary_from_context(context: Any) -> list[dict[str, str]]:
    """Termos com traducao fixada e genero, extraidos do guia do filme."""
    entradas = []
    if not isinstance(context, dict):
        return entradas
    for item in context.get("names_and_terms") or []:
        if not isinstance(item, dict):
            continue
        origem = str(item.get("source", "")).strip()
        destino = str(item.get("ptbr", "")).strip()
        if not origem or not destino:
            continue
        genero = str(item.get("gender", "n")).strip().lower()[:1]
        entradas.append(
            {
                "source": origem,
                "ptbr": destino,
                "gender": genero if genero in {"f", "m", "n"} else "n",
                "note": str(item.get("note", ""))[:160],
            }
        )
    return entradas[:40]


def gender_variants(term: str) -> list[str]:
    """Formas do mesmo termo que diferem so na desinencia de genero.

    Serve para pegar o caso em que a mesma personagem recebe Meritissimo num
    trecho e Meritissima em outro: sao a mesma palavra, generos diferentes.
    """
    saida = []
    for de, para in (("o", "a"), ("a", "o")):
        if term.endswith(de) and len(term) > 3:
            saida.append(term[:-1] + para)
    return saida


def glossary_conflicts(
    cues: list[SrtCue],
    translations: dict[str, dict[str, Any]],
    glossary: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    """Onde o filme usou a variante de genero errada de um termo ja decidido."""
    achados: dict[str, dict[str, Any]] = {}
    for entrada in glossary:
        esperado = entrada["ptbr"]
        variantes = gender_variants(esperado)
        if not variantes:
            continue
        origem_re = re.compile(r"\b" + re.escape(entrada["source"]) + r"\b", re.IGNORECASE)
        certos, errados = 0, []
        for cue in cues:
            rec = translations.get(str(cue.id))
            if not isinstance(rec, dict) or rec.get("status") != "ok":
                continue
            texto = str(rec.get("text", ""))
            if not origem_re.search(cue.text):
                continue
            if re.search(r"\b" + re.escape(esperado) + r"\b", texto, re.IGNORECASE):
                certos += 1
            elif any(re.search(r"\b" + re.escape(v) + r"\b", texto, re.IGNORECASE) for v in variantes):
                errados.append(cue.id)
        if errados:
            achados[entrada["source"]] = {
                "esperado": esperado,
                "cues": errados,
                "certos": certos,
            }
    return achados


def context_is_usable(context: Any) -> bool:
    if not isinstance(context, dict):
        return False
    if context.get("_fallback"):
        return False
    notes = " ".join(str(item) for item in context.get("continuity_notes", []))
    if "Context pass falhou" in notes:
        return False
    try:
        validate_context_payload(context)
        return True
    except ContractError:
        return False


def text_has_refusal(text: str) -> bool:
    return bool(REFUSAL_RE.search(text or ""))


def looks_like_repeated_name_or_token(text: str) -> bool:
    cleaned = strip_tags(text)
    words = re.findall(r"[A-Za-z][A-Za-z'.-]*", cleaned)
    if not words:
        return False
    lowered = [word.lower().strip("'-.") for word in words]
    unique = {word for word in lowered if word}
    if len(words) > 1 and 1 <= len(unique) <= 2:
        return all(
            original[:1].isupper()
            and lowered_word not in ENGLISH_STOPWORDS
            and len(lowered_word) >= 2
            for original, lowered_word in zip(words, lowered)
        )
    if len(words) == 1:
        original = words[0]
        lowered_word = lowered[0]
        return original[:1].isupper() and lowered_word not in ENGLISH_STOPWORDS and len(lowered_word) >= 2
    return False


def looks_like_musical_vocable_line(source: str) -> bool:
    if "♪" not in source:
        return False
    words = re.findall(r"[a-z']{1,}", strip_tags(source).lower())
    if not words or len(words) > 8:
        return False
    vocable_hits = sum(
        1
        for word in words
        if word.strip("'") in MUSICAL_VOCABLES or len(word.strip("'")) <= 2
    )
    return vocable_hits / len(words) >= 0.5


def looks_untranslated(source: str, translated: str) -> bool:
    source_plain = re.sub(r"<[^>]+>", "", source).strip()
    translated_plain = re.sub(r"<[^>]+>", "", translated).strip()
    src = source_plain.lower()
    out = translated_plain.lower()
    if not src or not out:
        return False
    src_words = re.findall(r"[a-z']{2,}", src)
    if src == out and looks_like_repeated_name_or_token(source_plain):
        return False
    if src == out and looks_like_musical_vocable_line(source_plain):
        return False
    if src == out and len(src_words) >= 3 and any(word in ENGLISH_STOPWORDS for word in src_words):
        return True
    if src == out and len(src_words) >= 4 and re.search(r"[a-z]{4,}", src):
        return True
    words = re.findall(r"[a-z']{2,}", out)
    if len(words) < 7:
        return False
    english_hits = sum(1 for word in words if word in ENGLISH_STOPWORDS)
    return english_hits / max(1, len(words)) > 0.38 and english_hits >= 5


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def capitalized_tokens(text: str) -> list[str]:
    tokens = re.findall(r"\b[A-Z][A-Za-z][A-Za-z'.-]*\b", strip_tags(text))
    return [token.strip("'\".,!?;:") for token in tokens if token not in COMMON_CAPITALIZED_WORDS]


def edit_distance(a: str, b: str, limit: int = 2) -> int:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
            row_min = min(row_min, cur[-1])
        if row_min > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def detect_spelling_variant_tokens(cues: list[SrtCue]) -> set[str]:
    tokens: dict[str, str] = {}
    for cue in cues:
        for token in capitalized_tokens(cue.text):
            normalized = re.sub(r"[^a-z]", "", token.lower())
            if len(normalized) >= 4:
                tokens.setdefault(token, normalized)
    protected: set[str] = set()
    items = list(tokens.items())
    for idx, (token, lower) in enumerate(items):
        for other, other_lower in items[idx + 1 :]:
            if token == other:
                continue
            if lower == other_lower:
                continue
            if lower in COMMON_NORMAL_WORDS or other_lower in COMMON_NORMAL_WORDS:
                continue
            if len(lower) != len(other_lower):
                continue
            if sorted(lower) == sorted(other_lower) and edit_distance(lower, other_lower, limit=2) <= 2:
                protected.add(token)
                protected.add(other)
    return protected


def protected_tokens_for_batch(job: "TranslatorJob", batch: Batch) -> dict[int, list[str]]:
    protected = getattr(job, "protected_variant_tokens", set())
    out: dict[int, list[str]] = {}
    if not protected:
        return out
    for cue in batch.cues:
        tokens = [token for token in capitalized_tokens(cue.text) if token in protected]
        if tokens:
            out[cue.id] = sorted(set(tokens))
    return out


def validate_translation_payload(
    payload: Any,
    batch: Batch,
    protected_tokens_by_id: dict[int, list[str]] | None = None,
) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ContractError("Resposta não e um objeto JSON.")
    translations = payload.get("translations")
    if not isinstance(translations, list):
        raise ContractError("Campo 'translations' ausente ou inválido.")
    expected_ids = {cue.id for cue in batch.cues}
    got: dict[int, str] = {}
    for item in translations:
        if not isinstance(item, dict):
            raise ContractError("Item de tradução não e objeto.")
        if "id" not in item or "text" not in item:
            raise ContractError("Item sem id/text.")
        try:
            cue_id = int(item["id"])
        except Exception as exc:
            raise ContractError(f"ID inválido em item: {item!r}") from exc
        if cue_id not in expected_ids:
            raise ContractError(f"ID inesperado: {cue_id}.")
        text = normalize_subtitle_text(str(item["text"]))
        if not text:
            raise ContractError(f"Tradução vazia para id {cue_id}.")
        if TIME_RE.match(text.split("\n", 1)[0] or ""):
            raise ContractError(f"Tradução contem linha de timestamp no id {cue_id}.")
        if text_has_refusal(text):
            raise ContractError(f"Tradução parece recusa no id {cue_id}.")
        source = next(cue.text for cue in batch.cues if cue.id == cue_id)
        if unbalanced_tags(text):
            raise ContractError(f"Tags desbalanceadas no id {cue_id}: {unbalanced_tags(text)}.")
        missing_tags = missing_source_tags(source, text)
        if missing_tags:
            raise ContractError(f"Tags da fonte ausentes no id {cue_id}: {missing_tags}.")
        if source.count("♪") and text.count("♪") == 0:
            raise ContractError(f"Marcador musical ausente no id {cue_id}.")
        got[cue_id] = text
    missing = sorted(expected_ids - set(got))
    if missing:
        raise ContractError(f"IDs faltando: {missing[:12]}.")
    protected_tokens_by_id = protected_tokens_by_id or {}
    for cue_id, tokens in protected_tokens_by_id.items():
        translated = got.get(cue_id, "")
        missing_tokens = [token for token in tokens if token and token not in translated]
        if missing_tokens:
            raise ContractError(f"Tokens protegidos ausentes no id {cue_id}: {missing_tokens}.")
    normalized = {str(k): v for k, v in got.items()}
    # Checagem heurística por último: o payload já esta estruturalmente correto aqui,
    # entao a falha vira soft e carrega o payload para permitir aceitacao por consenso.
    cue_by_id = {cue.id: cue for cue in batch.cues}
    suspicious = [
        cue_id
        for cue_id, text in got.items()
        if looks_untranslated(cue_by_id[cue_id].text, text)
    ]
    if suspicious:
        raise SoftContractError(
            f"Possível texto não traduzido nos IDs: {sorted(suspicious)[:12]}.",
            cue_ids=sorted(suspicious),
            payload=normalized,
        )
    return normalized


class BedrockClient:
    def __init__(self, profile: str, region: str, timeout: int, logger: JsonLogger):
        self.profile = profile
        self.region = region
        self.timeout = timeout
        self.logger = logger
        self.aws_path = shutil.which("aws") or "aws"

    def converse(
        self,
        model_id: str,
        system_text: str,
        user_text: str,
        *,
        max_tokens: int,
        temperature: float = 0.2,
        tool_config: dict[str, Any] | None = None,
        cache_prefix: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if cache_prefix:
            # Tudo antes do cachePoint e reaproveitado entre chamadas a ~10% do custo.
            # Ordem de renderizacao: tools, system, messages -- por isso o ponto fica
            # logo depois da parte estavel do prompt do usuario.
            conteudo = [
                {"text": cache_prefix},
                {"cachePoint": {"type": "default"}},
                {"text": user_text},
            ]
        else:
            conteudo = [{"text": user_text}]
        messages = [{"role": "user", "content": conteudo}]
        cmd = [
            self.aws_path,
            "bedrock-runtime",
            "converse",
            "--profile",
            self.profile,
            "--region",
            self.region,
            "--model-id",
            model_id,
            "--system",
            json.dumps([{"text": system_text}], ensure_ascii=False),
            "--messages",
            json.dumps(messages, ensure_ascii=False),
            "--inference-config",
            json.dumps(
                {
                    "maxTokens": max_tokens,
                    "temperature": temperature,
                },
                ensure_ascii=False,
            ),
            "--output",
            "json",
        ]
        if tool_config:
            cmd += ["--tool-config", json.dumps(tool_config, ensure_ascii=False)]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BedrockCallError(f"Timeout após {self.timeout}s chamando {model_id}.") from exc
        except FileNotFoundError as exc:
            raise BedrockCallError("AWS CLI não encontrado no PATH.", retryable=False) from exc

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            unavailable = is_unavailable_model_error(err)
            retryable = not unavailable and is_retryable_cli_error(err)
            raise BedrockCallError(err[:4000] or f"AWS CLI saiu com código {proc.returncode}.", retryable=retryable, unavailable_model=unavailable)

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise BedrockCallError(f"Resposta da AWS não era JSON: {proc.stdout[:1000]}", retryable=True) from exc

        content = data.get("output", {}).get("message", {}).get("content", [])
        chunks = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if "toolUse" in block:
                # A API ja devolve o argumento como objeto validado contra o schema.
                # Serializar aqui deixa validacao, reparo e QC funcionando sem mudanca.
                entrada = (block.get("toolUse") or {}).get("input")
                if entrada is not None:
                    chunks.append(json.dumps(entrada, ensure_ascii=False))
            elif "text" in block:
                chunks.append(str(block["text"]))
        text = "\n".join(chunks).strip()
        if not text:
            raise BedrockCallError(f"Modelo {model_id} retornou sem bloco text.", retryable=True)
        meta = {
            "usage": data.get("usage", {}),
            "metrics": data.get("metrics", {}),
            "stopReason": data.get("stopReason"),
        }
        return text, meta


def make_llm_client(profile: str, region: str, timeout: int, logger: JsonLogger) -> Any:
    """Ponto único de troca de provedor de LLM.

    Para usar uma chave de API em vez do Bedrock, devolva aqui uma classe com o
    mesmo `converse(model_id, system_text, user_text, *, max_tokens, temperature)`
    retornando `(texto, meta)`. Ver a secao "Usar com outra LLM" no README.
    """
    return BedrockClient(profile, region, timeout, logger)


TRANSLATION_TOOL_NAME = "entregar_traducoes"


def translation_tool_config() -> dict[str, Any]:
    """Contrato de resposta como ferramenta, em vez de JSON pedido em prosa.

    A API valida o schema e devolve objeto pronto, o que elimina a classe de falha
    em que o modelo escapa mal uma aspa dentro da fala e quebra o lote inteiro.
    """
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": TRANSLATION_TOOL_NAME,
                    # Texto fixo de proposito: o toolConfig e renderizado antes do system,
                    # entao qualquer variacao por lote invalidaria todo o prefixo em cache.
                    "description": (
                        "Entrega a traducao em portugues brasileiro das legendas do lote atual. "
                        "Devolva exatamente um item para cada id pedido, sem faltar nem sobrar."
                    ),
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "translations": {
                                    "type": "array",
                                    "description": "Uma entrada por legenda do lote, na ordem dos ids.",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "integer", "description": "O id da legenda."},
                                            "text": {
                                                "type": "string",
                                                "description": "A legenda em pt-BR, com as quebras de linha originais.",
                                            },
                                        },
                                        "required": ["id", "text"],
                                    },
                                }
                            },
                            "required": ["translations"],
                        }
                    },
                }
            }
        ],
        "toolChoice": {"tool": {"name": TRANSLATION_TOOL_NAME}},
    }


def is_tool_unsupported_error(err: str) -> bool:
    lower = (err or "").lower()
    return "doesn't support tool use" in lower or "does not support tool use" in lower or "toolconfig" in lower


def is_unavailable_model_error(err: str) -> bool:
    lower = err.lower()
    return any(
        needle in lower
        for needle in (
            "not available for this account",
            "access denied",
            "resource not found",
            "is marked by provider as legacy",
            "on-demand throughput isn't supported",
            "on-demand throughput isn’t supported",
            "you don't have access",
            "you do not have access",
        )
    )


def is_retryable_cli_error(err: str) -> bool:
    lower = err.lower()
    if not err:
        return True
    if any(
        needle in lower
        for needle in (
            "throttl",
            "too many requests",
            "rate exceeded",
            "timeout",
            "timed out",
            "serviceunavailable",
            "internalserver",
            "connection",
            "temporarily unavailable",
            "model timeout",
            "modelstreamerror",
        )
    ):
        return True
    if any(needle in lower for needle in ("validationexception", "accessdenied", "not available for this account")):
        return False
    return True


class TranslatorJob:
    def __init__(self, config: JobConfig, logger: JsonLogger | None = None):
        self.config = config
        self.source_path = config.source_path.expanduser().resolve()
        self.signature = file_signature(self.source_path)
        self.job_root = config.job_root or (self.source_path.parent / ".srt_translator_jobs")
        self.job_id = self.signature
        if config.max_cues is not None:
            self.job_id = f"{self.job_id}-first{config.max_cues}"
        if config.force_new:
            self.job_id = f"{self.job_id}-{_dt.datetime.now().strftime('%Y%m%d%H%M%S')}"
        self.job_dir = self.job_root / self.job_id
        self.logger = logger or JsonLogger(self.job_dir)
        self.logger.bind(self.job_dir)
        self.state_path = self.job_dir / "state.json"
        self.config_path = self.job_dir / "config.json"
        self.translations_path = self.job_dir / "translations.json"
        self.context_path = self.job_dir / "context.json"
        self.stop_path = self.job_dir / "STOP"
        self._stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.doc: SrtDocument | None = None
        self.batches: list[Batch] = []
        self.translations: dict[str, dict[str, Any]] = load_json(self.translations_path, {})
        self.unavailable_models: set[str] = set()
        self.models_without_tools: set[str] = set()
        self.protected_variant_tokens: set[str] = set()

    def request_stop(self) -> None:
        self._stop_event.set()
        self.stop_path.write_text("stop requested " + utc_now(), encoding="utf-8")
        self.logger.event("WARN", "Parada solicitada pelo usuário.")

    def stopped(self) -> bool:
        return self._stop_event.is_set() or self.stop_path.exists()

    def init_or_load(self) -> dict[str, Any]:
        self.doc = SrtDocument.load(self.source_path, max_cues=self.config.max_cues)
        self.batches = make_batches(self.doc.cues, self.config.batch_size, self.config.max_batch_chars)
        self.protected_variant_tokens = detect_spelling_variant_tokens(self.doc.cues)
        existing = load_json(self.state_path, {})
        if self.config.force_new and existing:
            existing = {}
        if not existing:
            output_base = self.config.output_path or self.source_path.with_name(self.source_path.stem + ".pt-BR.srt")
            state = {
                "job_id": self.job_id,
                "status": "created",
                "source_path": str(self.source_path),
                "source_signature": self.signature,
                "output_base": str(output_base),
                "partial_output_path": str(output_base.with_name(output_base.stem + ".EM_ANDAMENTO.srt")),
                "success_output_path": str(output_base.with_name(output_base.stem + ".OK.srt")),
                "incomplete_output_path": str(output_base.with_name(output_base.stem + ".INCOMPLETO.srt")),
                "quality_report_path": str(self.job_dir / "quality_report.json"),
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "profile": self.config.profile,
                "region": self.config.region,
                "models": self.config.models,
                "batch_size": self.config.batch_size,
                "max_batch_chars": self.config.max_batch_chars,
                "context_batches": self.config.context_batches,
                "attempts_per_model": self.config.attempts_per_model,
                "retry_forever": self.config.retry_forever,
                "context_pass": self.config.context_pass,
                "polish_pass": self.config.polish_pass,
                "retry_qc_issues": self.config.retry_qc_issues,
                "qc_repair_rounds": self.config.qc_repair_rounds,
                "semantic_review": self.config.semantic_review,
                "max_lines": self.config.max_lines,
                "max_line_length": self.config.max_line_length,
                "max_cps": self.config.max_cps,
                "encoding": self.doc.encoding,
                "newline": repr(self.doc.newline),
                "total_cues": len(self.doc.cues),
                "total_batches": len(self.batches),
                "completed_batches": [],
                "failed_batches": [],
                "current": None,
                "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0},
                "last_error": None,
                "movie": infer_movie_title(self.source_path),
            }
            atomic_write_json(self.config_path, config_to_json(self.config))
            self.save_state(state)
            self.logger.event("INFO", "Novo trabalho criado.", total=len(self.doc.cues), total_batches=len(self.batches))
            return state

        existing["updated_at"] = utc_now()
        existing["profile"] = self.config.profile or existing.get("profile", DEFAULT_PROFILE)
        existing["region"] = self.config.region or existing.get("region", DEFAULT_REGION)
        existing["models"] = self.config.models or existing.get("models", DEFAULT_MODELS)
        existing["batch_size"] = self.config.batch_size
        existing["max_batch_chars"] = self.config.max_batch_chars
        existing["context_batches"] = self.config.context_batches
        existing["retry_forever"] = self.config.retry_forever
        existing["attempts_per_model"] = self.config.attempts_per_model
        existing["context_pass"] = self.config.context_pass
        existing["polish_pass"] = self.config.polish_pass
        existing["retry_qc_issues"] = self.config.retry_qc_issues
        existing["qc_repair_rounds"] = self.config.qc_repair_rounds
        existing["max_lines"] = self.config.max_lines
        existing["max_line_length"] = self.config.max_line_length
        existing["max_cps"] = self.config.max_cps
        existing["quality_report_path"] = str(self.job_dir / "quality_report.json")
        existing["total_cues"] = len(self.doc.cues)
        existing["total_batches"] = len(self.batches)
        existing["completed_batches"] = sorted(
            batch.number for batch in self.batches if self.batch_done(batch)
        )
        if isinstance(existing.get("current"), dict):
            existing["current"]["total_batches"] = len(self.batches)
        atomic_write_json(self.config_path, config_to_json(self.config))
        self.save_state(existing)
        self.logger.event("INFO", "Trabalho existente carregado para retomada.", total=len(self.doc.cues), total_batches=len(self.batches))
        return existing

    def save_state(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now()
        atomic_write_json(self.state_path, state)

    def save_translations(self) -> None:
        atomic_write_json(self.translations_path, self.translations)

    def write_sidecar(self, output_path: Path) -> None:
        sidecar = output_path.with_name(output_path.name + ".translator-state.json")
        data = {
            "job_id": self.job_id,
            "job_dir": str(self.job_dir),
            "source_path": str(self.source_path),
            "state_path": str(self.state_path),
            "translations_path": str(self.translations_path),
            "written_at": utc_now(),
        }
        atomic_write_json(sidecar, data)

    def write_output(self, state: dict[str, Any], final: bool = False) -> Path:
        assert self.doc is not None
        report = self.write_quality_report(state)
        missing = self.missing_ids()
        failed = [k for k, v in self.translations.items() if v.get("status") == "error"]
        hard_qc_errors = report.get("summary", {}).get("error_cues", 0)
        if final:
            path_key = "success_output_path" if not missing and not failed and not hard_qc_errors else "incomplete_output_path"
        else:
            path_key = "partial_output_path"
        output_path = Path(state[path_key])
        text = render_srt(
            self.doc.cues,
            self.translations,
            newline=self.doc.newline,
            include_pending_markers=True,
        )
        atomic_write_text(output_path, text, encoding="utf-8-sig")
        self.write_sidecar(output_path)
        state["last_written_output"] = str(output_path)
        if final:
            self.cleanup_superseded_outputs(state, output_path)
        self.save_state(state)
        return output_path

    def cleanup_superseded_outputs(self, state: dict[str, Any], keep: Path) -> None:
        """Remove a saída parcial e a variante final antiga deste mesmo job.

        Sem isso a pasta do filme acumula .EM_ANDAMENTO.srt, .INCOMPLETO.srt e
        .OK.srt lado a lado e não da para saber qual e a legenda boa.
        """
        candidates = [
            Path(state[key])
            for key in ("partial_output_path", "success_output_path", "incomplete_output_path")
            if state.get(key)
        ]
        for path in candidates:
            if path == keep or not path.exists():
                continue
            sidecar = path.with_name(path.name + ".translator-state.json")
            # Só apaga o que este job escreveu: o sidecar tem que apontar para este job_id.
            owner = load_json(sidecar, {}).get("job_id") if sidecar.exists() else None
            if owner != self.job_id:
                self.logger.event(
                    "WARN",
                    "Arquivo antigo com nome de saída não pertence a este trabalho; mantendo.",
                    error=str(path),
                )
                continue
            for target in (path, sidecar):
                try:
                    target.unlink()
                except OSError as exc:
                    self.logger.event("WARN", "Não consegui remover saída antiga.", error=f"{target}: {exc}")
            self.logger.event("INFO", "Saída antiga removida para não confundir com a legenda final.", error=str(path))

    def try_write_output(self, state: dict[str, Any], final: bool = False) -> Path | None:
        try:
            return self.write_output(state, final=final)
        except Exception as exc:
            write_error = f"{exc.__class__.__name__}: {exc}"
            previous = str(state.get("last_error") or "").strip()
            state["last_error"] = (previous + " | " if previous else "") + f"Falha escrevendo arquivo SRT: {write_error}"
            state["output_write_error"] = write_error
            state["traceback"] = traceback.format_exc()[-6000:]
            self.save_state(state)
            self.logger.event("ERROR", "Falha escrevendo arquivo SRT.", error=write_error[:1000])
            return None

    def write_quality_report(self, state: dict[str, Any]) -> dict[str, Any]:
        assert self.doc is not None
        report = build_quality_report(
            self.doc.cues,
            self.translations,
            max_lines=self.config.max_lines,
            max_line_length=self.config.max_line_length,
            max_cps=self.config.max_cps,
            protected_tokens_by_id=self.protected_tokens_for_all_cues(),
            glossary=glossary_from_context(load_json(self.context_path, {})),
        )
        path = self.job_dir / "quality_report.json"
        atomic_write_json(path, report)
        state["quality_report_path"] = str(path)
        state["quality"] = report["summary"]
        return report

    def protected_tokens_for_all_cues(self) -> dict[int, list[str]]:
        assert self.doc is not None
        protected = self.protected_variant_tokens
        out: dict[int, list[str]] = {}
        if not protected:
            return out
        for cue in self.doc.cues:
            tokens = [token for token in capitalized_tokens(cue.text) if token in protected]
            if tokens:
                out[cue.id] = sorted(set(tokens))
        return out

    def missing_ids(self) -> list[int]:
        assert self.doc is not None
        return [
            cue.id
            for cue in self.doc.cues
            if self.translations.get(str(cue.id), {}).get("status") != "ok"
        ]

    def run(self) -> dict[str, Any]:
        state = self.init_or_load()
        if self.stop_path.exists():
            self.stop_path.unlink()
        state["status"] = "running"
        state["last_error"] = None
        self.save_state(state)
        client = make_llm_client(self.config.profile, self.config.region, self.config.call_timeout, self.logger)
        try:
            if self.config.context_pass:
                self.ensure_context_pack(client, state)
            self.translate_all(client, state, polish=False)
            if self.config.retry_qc_issues:
                self.repair_quality_issues(client, state)
            if self.config.semantic_review:
                confirmados = self.semantic_review(client, state)
                if confirmados and self.config.semantic_autofix:
                    mudou = self.repair_semantic_cues(client, state, confirmados)
                    self.write_quality_report(state)
                    self.save_state(state)
                    self.logger.event(
                        "INFO",
                        f"Revisão de sentido concluída: {mudou} de {len(confirmados)} falas mudaram de fato.",
                        cue_ids=confirmados[:30],
                    )
            if self.config.polish_pass and not self.missing_ids():
                self.translate_all(client, state, polish=True)
                if self.config.retry_qc_issues:
                    self.repair_quality_issues(client, state)
            missing = self.missing_ids()
            failed = [k for k, v in self.translations.items() if v.get("status") == "error"]
            final_path = self.write_output(state, final=True)
            hard_qc_errors = int((state.get("quality") or {}).get("error_cues") or 0)
            if missing or failed or hard_qc_errors:
                state["status"] = "incomplete"
                state["last_error"] = f"{len(missing)} legendas pendentes; {len(failed)} com erro; {hard_qc_errors} cues com erro de QC."
                self.logger.event("ERROR", "Trabalho finalizado com pendencias.", done=len(self.doc.cues) - len(missing), total=len(self.doc.cues), status="incomplete", error=state["last_error"])
            else:
                state["status"] = "complete"
                state["last_error"] = None
                self.logger.event("INFO", "Trabalho finalizado com sucesso.", done=len(self.doc.cues), total=len(self.doc.cues), status="complete")
            review_ids = sorted(
                {int(cue_id) for cue_id, rec in self.translations.items() if rec.get("review_flag")}
            )
            state["review_cue_ids"] = review_ids
            if review_ids:
                self.logger.event(
                    "WARN",
                    f"{len(review_ids)} legendas foram aceitas por consenso entre modelos e merecem uma conferida.",
                    cue_ids=review_ids[:40],
                )
            state["final_output_path"] = str(final_path)
            self.save_state(state)
            return state
        except JobStopped:
            state["status"] = "stopped"
            state["last_error"] = "Parado pelo usuário."
            self.try_write_output(state, final=False)
            self.save_state(state)
            self.logger.event("WARN", "Trabalho parado; pode ser retomado depois.", status="stopped")
            return state
        except Exception as exc:
            state["status"] = "failed"
            state["last_error"] = f"{exc.__class__.__name__}: {exc}"
            state["traceback"] = traceback.format_exc()[-6000:]
            self.try_write_output(state, final=False)
            self.save_state(state)
            self.logger.event("ERROR", "Trabalho falhou.", status="failed", error=str(exc)[:1000])
            raise

    def ensure_context_pack(self, client: BedrockClient, state: dict[str, Any]) -> None:
        if self.context_path.exists():
            context = load_json(self.context_path, {})
            if context_is_usable(context):
                state["context"] = context
                self.save_state(state)
                return
            self.logger.event("WARN", "Contexto existente não passou na validação; vou recriá-lo.", error="contexto ausente, genérico ou truncado")
        assert self.doc is not None
        self.logger.event("INFO", "Criando contexto do filme a partir do nome e de amostras da legenda.")
        system = (
            "Você prepara guias de tradução audiovisual para português brasileiro. "
            "Use apenas os dados fornecidos. Se algo não estiver claro, marque como inferencia ou desconhecido. "
            "Retorne somente JSON valido, curto, sem markdown, sem texto antes ou depois."
        )
        samples = collect_samples(self.doc.cues)
        prompt = {
            "task": "Prepare um guia curto e prático para traduzir esta legenda SRT para português brasileiro natural e contextualizado.",
            "movie_metadata_from_path": infer_movie_title(self.source_path),
            "rules": [
                "Não invente sinopse externa.",
                "Identifique nomes recorrentes, relacoes aparentes, tom, registro e escolhas de tratamento apenas quando a amostra permitir.",
                "Para CADA pessoa em names_and_terms preencha gender com f, m ou n. Portugues exige concordancia que o ingles nao marca: sem isso a mesma personagem vira ora masculina ora feminina ao longo do filme.",
                "Inclua tambem as formas de tratamento recorrentes como termo: Your Honor, Counselor, sir, ma'am, Doctor. Fixe a versao pt-BR e o genero de quem recebe o tratamento.",
                "Se a amostra nao permitir deduzir o genero, use n; nao invente.",
                "Inclua no máximo 8 orientacoes praticas para músicas legendadas, palavroes, humor, ironia e continuidade.",
                "Use strings curtas. Limite names_and_terms a 20 itens e continuity_notes a 10 itens.",
                "Se houver dúvida, prefira diretrizes conservadoras.",
            ],
            "response_format_required": (
                '{"title_guess":"...","year_guess":"...","source_language":"...",'
                '"tone":"...","style_guide_ptbr":["..."],'
                '"names_and_terms":[{"source":"...","ptbr":"...","gender":"f|m|n","note":"..."}],'
                '"continuity_notes":["..."]}'
            ),
            "subtitle_samples": samples,
        }
        try:
            text, meta, model, _outcome = self.call_with_fallback(
                client,
                state,
                system,
                json.dumps(prompt, ensure_ascii=False, indent=2),
                max_tokens=4200,
                temperature=0.15,
                stage="context",
                max_cycles=2,
            )
            context = extract_json_object(text)
            context = validate_context_payload(context)
            context["_model"] = model
        except Exception as exc:
            context = {
                "title_guess": infer_movie_title(self.source_path).get("title_guess"),
                "year_guess": infer_movie_title(self.source_path).get("year_guess"),
                "source_language": "unknown",
                "tone": "unknown",
                "style_guide_ptbr": [
                    "Traduzir para português brasileiro natural.",
                    "Preservar nomes próprios e continuidade entre lotes.",
                    "Adaptar músicas legendadas como legenda audiovisual, mantendo notas musicais quando existirem.",
                ],
                "names_and_terms": [],
                "continuity_notes": [f"Context pass falhou: {exc}"],
                "_fallback": True,
            }
            self.logger.event("WARN", "Não consegui criar contexto via LLM; usando guia genérico.", error=str(exc)[:500])
        atomic_write_json(self.context_path, context)
        state["context"] = context
        self.add_usage(state, meta if "meta" in locals() else {}, locals().get("model", ""))
        self.save_state(state)
        self.logger.event("INFO", "Contexto preparado.", status="ok")

    def translate_all(self, client: BedrockClient, state: dict[str, Any], *, polish: bool) -> None:
        assert self.doc is not None
        completed = set(state.get("completed_batches", []))
        stage_name = "polimento" if polish else "tradução"
        for batch in self.batches:
            if self.stopped():
                raise JobStopped()
            if not polish and self.batch_done(batch):
                if batch.number not in completed:
                    completed.add(batch.number)
                    state["completed_batches"] = sorted(completed)
                    self.save_state(state)
                continue
            if polish and self.batch_polished(batch):
                continue
            state["current"] = {
                "stage": stage_name,
                "batch": batch.number,
                "total_batches": len(self.batches),
                "start_id": batch.start_id,
                "end_id": batch.end_id,
            }
            self.save_state(state)
            self.logger.event(
                "INFO",
                f"Iniciando {stage_name} do lote {batch.number}/{len(self.batches)}.",
                batch=batch.number,
                total_batches=len(self.batches),
                done=self.count_done(),
                total=len(self.doc.cues),
            )
            try:
                translations, model, outcome = self.translate_batch(client, state, batch, polish=polish)
                now = utc_now()
                soft_ids = {str(cue_id) for cue_id in (outcome.get("soft_cue_ids") or [])}
                for cue_id, text in translations.items():
                    prior = self.translations.get(cue_id, {})
                    text = apply_subtitle_formatting(text, self.config.max_line_length, self.config.max_lines)
                    record = {
                        **prior,
                        "text": text,
                        "status": "ok",
                        "batch": batch.number,
                        "model": model,
                        "polished": bool(polish) or bool(prior.get("polished")),
                        "updated_at": now,
                    }
                    if cue_id in soft_ids:
                        # Aceito por consenso entre modelos: não e erro duro, mas fica
                        # sinalizado para o relatório de QC e para revisão humana.
                        record["review_flag"] = "consenso_heuristica"
                        record["review_reason"] = str(outcome.get("reason", ""))[:400]
                        record["review_models"] = outcome.get("models_agreeing") or []
                    else:
                        record.pop("review_flag", None)
                        record.pop("review_reason", None)
                        record.pop("review_models", None)
                    self.translations[cue_id] = record
                self.save_translations()
                if not polish:
                    completed.add(batch.number)
                    state["completed_batches"] = sorted(completed)
                self.write_quality_report(state)
                self.save_state(state)
                self.logger.event(
                    "INFO",
                    "Traduções do lote persistidas; atualizando SRT parcial.",
                    batch=batch.number,
                    done=self.count_done(),
                    total=len(self.doc.cues),
                )
                self.write_output(state, final=False)
                self.logger.event(
                    "INFO",
                    f"Lote {batch.number}/{len(self.batches)} concluído.",
                    batch=batch.number,
                    model=model,
                    done=self.count_done(),
                    total=len(self.doc.cues),
                )
            except JobStopped:
                raise
            except Exception as exc:
                if self.config.retry_forever:
                    raise
                self.mark_batch_error(batch, str(exc))
                failed = set(state.get("failed_batches", []))
                failed.add(batch.number)
                state["failed_batches"] = sorted(failed)
                state["last_error"] = str(exc)
                self.save_translations()
                self.write_output(state, final=False)
                self.save_state(state)
                self.logger.event("ERROR", f"Lote {batch.number} ficou com erro e o trabalho continuou.", batch=batch.number, error=str(exc)[:600])
        state["current"] = None
        self.save_state(state)

    def judge_pairs(
        self,
        client: BedrockClient,
        state: dict[str, Any],
        pares: list[dict[str, Any]],
        *,
        modelos: list[str] | None = None,
    ) -> dict[int, dict[str, Any]]:
        """Manda pares ao juiz e devolve o veredito por id."""
        if not pares:
            return {}
        system, prompt = build_judge_prompt(self, pares)
        modelos_originais = self.config.models
        if modelos:
            self.config.models = modelos
        try:
            texto, meta, _modelo, _out = self.call_with_fallback(
                client,
                state,
                system,
                prompt,
                max_tokens=min(12000, 220 * len(pares) + 800),
                temperature=0.1,
                stage="review",
                max_cycles=2,
                tool_config=judge_tool_config(),
            )
        finally:
            self.config.models = modelos_originais
        self.add_usage(state, meta, _modelo)
        payload = extract_json_object(texto)
        itens = payload.get("itens") if isinstance(payload, dict) else None
        if not isinstance(itens, list):
            raise ContractError("Resposta do juiz sem lista itens.")
        saida: dict[int, dict[str, Any]] = {}
        for item in itens:
            if isinstance(item, dict) and "id" in item:
                try:
                    saida[int(item["id"])] = item
                except Exception:
                    continue
        return saida

    def semantic_review(self, client: BedrockClient, state: dict[str, Any]) -> list[int]:
        """Julga o sentido das falas de risco e confirma as acusacoes antes de agir.

        Um unico juiz erra muito: medindo numa amostra real, metade dos apontamentos
        era ruido, e ele chegou a marcar errado e explicar que o sentido estava certo.
        Por isso a acusacao so vale se sobreviver ao filtro de recuo e for confirmada
        por um segundo modelo, no mesmo espirito do consenso ja usado na traducao.
        """
        assert self.doc is not None
        report = load_json(self.job_dir / "quality_report.json", {})
        marcados = {
            int(cue_id)
            for cue_id, rec in self.translations.items()
            if isinstance(rec, dict) and rec.get("review_flag")
        } | set(report.get("warning_cue_ids", []) or [])
        alvos, por_risco = select_for_semantic_review(
            self.doc.cues,
            self.translations,
            min_signals=self.config.semantic_min_signals,
            budget=self.config.semantic_budget,
            always=marcados,
            sample_pct=self.config.semantic_sample_pct,
            seed=self.job_id,
        )
        if not alvos:
            return []
        por_id = {cue.id: cue for cue in self.doc.cues}
        self.logger.event(
            "INFO",
            f"Revisão de sentido: {len(alvos)} de {len(self.doc.cues)} falas "
            f"({por_risco} por sinal de risco, {len(alvos) - por_risco} por amostragem).",
            total=len(self.doc.cues),
        )
        acusados: list[tuple[int, dict[str, Any]]] = []
        lote = 40
        for inicio in range(0, len(alvos), lote):
            if self.stopped():
                raise JobStopped()
            fatia = alvos[inicio : inicio + lote]
            pares = [
                {
                    "id": cue_id,
                    "en": por_id[cue_id].text,
                    "pt": str(self.translations[str(cue_id)].get("text", "")),
                }
                for cue_id in fatia
                if str(cue_id) in self.translations
            ]
            try:
                vereditos = self.judge_pairs(client, state, pares)
            except Exception as exc:
                # Revisão é um extra: falhar aqui não pode derrubar uma tradução pronta.
                self.logger.event("WARN", "Não consegui avaliar este bloco de sentido; sigo sem ele.", error=str(exc)[:400])
                continue
            for cue_id, item in vereditos.items():
                atual = str(self.translations.get(str(cue_id), {}).get("text", ""))
                if judge_verdict_is_actionable(item, atual):
                    acusados.append((cue_id, item))
        if not acusados:
            self.logger.event("INFO", "Revisão de sentido não encontrou erro que justifique refazer.")
            return []

        # Segunda opinião, de preferência com outro provedor.
        outros = [mo for mo in self.config.models if mo.split(".")[0:2] != self.config.models[0].split(".")[0:2]]
        segunda = self.judge_pairs(
            client,
            state,
            [
                {"id": cue_id, "en": por_id[cue_id].text, "pt": str(self.translations[str(cue_id)].get("text", ""))}
                for cue_id, _ in acusados
            ],
            modelos=outros or self.config.models,
        )
        confirmados: list[int] = []
        for cue_id, item in acusados:
            atual = str(self.translations.get(str(cue_id), {}).get("text", ""))
            outro = segunda.get(cue_id)
            if outro and judge_verdict_is_actionable(outro, atual):
                rec = self.translations[str(cue_id)]
                rec["semantic_note"] = str(item.get("porque", ""))[:400]
                rec["semantic_category"] = str(item.get("categoria", ""))[:60]
                rec["review_flag"] = "sentido_suspeito"
                if self.config.semantic_autofix:
                    rec["text_before_review"] = atual
                    rec["status"] = "needs_review"
                confirmados.append(cue_id)
        self.save_translations()
        self.logger.event(
            "WARN" if confirmados else "INFO",
            f"Revisão de sentido: {len(acusados)} acusações, {len(confirmados)} confirmadas por um segundo modelo.",
            cue_ids=confirmados[:30],
        )
        state["semantic_review_cue_ids"] = confirmados
        self.save_state(state)
        return confirmados

    def repair_semantic_cues(self, client: BedrockClient, state: dict[str, Any], cue_ids: list[int]) -> int:
        """Refaz só as falas acusadas, sem tocar nas vizinhas que estavam boas."""
        assert self.doc is not None
        por_id = {cue.id: cue for cue in self.doc.cues}
        indice = {cue.id: i for i, cue in enumerate(self.doc.cues)}
        mudou = 0
        for inicio in range(0, len(cue_ids), 10):
            if self.stopped():
                raise JobStopped()
            grupo = [por_id[c] for c in cue_ids[inicio : inicio + 10] if c in por_id]
            if not grupo:
                continue
            # vizinhas de cada alvo, como leitura
            vizinhos: dict[int, dict[str, Any]] = {}
            for cue in grupo:
                i = indice[cue.id]
                for j in range(max(0, i - 2), min(len(self.doc.cues), i + 3)):
                    vz = self.doc.cues[j]
                    if vz.id in {c.id for c in grupo}:
                        continue
                    vizinhos[vz.id] = {
                        "id": vz.id,
                        "original_ingles": vz.text,
                        "ptbr": self.translations.get(str(vz.id), {}).get("text", ""),
                    }
            system, prompt = build_semantic_fix_prompt(self, grupo, [vizinhos[k] for k in sorted(vizinhos)])
            sintetico = Batch(0, grupo, grupo[0].id, grupo[-1].id)
            protegidos = protected_tokens_for_batch(self, sintetico)
            try:
                texto, meta, modelo, _out = self.call_with_fallback(
                    client,
                    state,
                    system,
                    prompt,
                    max_tokens=estimate_max_tokens(sintetico),
                    temperature=0.15,
                    stage="fix",
                    max_cycles=2,
                    validator=lambda raw: validate_translation_payload(extract_json_object(raw), sintetico, protegidos),
                    tool_config=translation_tool_config(),
                )
            except Exception as exc:
                self.logger.event("WARN", "Não consegui refazer este grupo de falas; mantenho a tradução atual.", error=str(exc)[:400])
                for cue in grupo:
                    self.translations[str(cue.id)]["status"] = "ok"
                continue
            self.add_usage(state, meta, modelo)
            novos = validate_translation_payload(extract_json_object(texto), sintetico, protegidos)
            agora = utc_now()
            for cue_id, texto_novo in novos.items():
                rec = self.translations.get(cue_id, {})
                antes = rec.get("text_before_review") or rec.get("text", "")
                formatado = apply_subtitle_formatting(texto_novo, self.config.max_line_length, self.config.max_lines)
                rec.update({"text": formatado, "status": "ok", "model": modelo, "updated_at": agora})
                if normalize_subtitle_text(antes) != normalize_subtitle_text(formatado):
                    rec["review_flag"] = "sentido_refeito"
                    rec["text_before_review"] = antes
                    mudou += 1
                else:
                    rec.pop("text_before_review", None)
                self.translations[cue_id] = rec
            self.save_translations()
        return mudou

    def registrar_mudancas_da_revisao(self, cue_ids: list[int]) -> None:
        """Guarda o antes e o depois para a revisão poder ser conferida e desfeita."""
        mudou = 0
        for cue_id in cue_ids:
            rec = self.translations.get(str(cue_id))
            if not isinstance(rec, dict):
                continue
            antes = rec.get("text_before_review")
            if antes and normalize_subtitle_text(antes) != normalize_subtitle_text(str(rec.get("text", ""))):
                rec["review_flag"] = "sentido_refeito"
                mudou += 1
            else:
                rec.pop("text_before_review", None)
        self.save_translations()
        self.logger.event(
            "INFO",
            f"Revisão de sentido concluída: {mudou} de {len(cue_ids)} falas mudaram de fato.",
            cue_ids=cue_ids[:30],
        )

    def repair_quality_issues(self, client: BedrockClient, state: dict[str, Any]) -> None:
        assert self.doc is not None
        for repair_round in range(1, self.config.qc_repair_rounds + 1):
            report = self.write_quality_report(state)
            issue_ids = [
                cue_id
                for cue_id in report.get("error_cue_ids", [])
                if self.translations.get(str(cue_id), {}).get("status") == "ok"
            ]
            if not issue_ids:
                self.save_state(state)
                return
            now = utc_now()
            for cue_id in issue_ids:
                rec = self.translations.get(str(cue_id), {})
                self.translations[str(cue_id)] = {
                    **rec,
                    "status": "needs_review",
                    "qc_retry_round": repair_round,
                    "updated_at": now,
                }
            self.save_translations()
            self.logger.event(
                "WARN",
                f"QC encontrou {len(issue_ids)} cues traduzidos com erro duro; refazendo lotes afetados.",
                done=self.count_done(),
                total=len(self.doc.cues),
                error=f"round={repair_round}; ids={issue_ids[:20]}",
            )
            self.translate_all(client, state, polish=False)
        report = self.write_quality_report(state)
        remaining = [
            cue_id
            for cue_id in report.get("error_cue_ids", [])
            if self.translations.get(str(cue_id), {}).get("status") == "ok"
        ]
        if remaining:
            now = utc_now()
            for cue_id in remaining:
                rec = self.translations.get(str(cue_id), {})
                self.translations[str(cue_id)] = {
                    **rec,
                    "status": "qc_error",
                    "updated_at": now,
                }
            self.save_translations()
            self.write_quality_report(state)
            self.logger.event("ERROR", "Alguns cues continuaram com erro de QC após as rodadas de reparo.", error=f"ids={remaining[:30]}")

    def batch_done(self, batch: Batch) -> bool:
        return all(self.translations.get(str(cue.id), {}).get("status") == "ok" for cue in batch.cues)

    def batch_polished(self, batch: Batch) -> bool:
        return all(self.translations.get(str(cue.id), {}).get("polished") for cue in batch.cues)

    def count_done(self) -> int:
        assert self.doc is not None
        return len(self.doc.cues) - len(self.missing_ids())

    def mark_batch_error(self, batch: Batch, error: str) -> None:
        now = utc_now()
        for cue in batch.cues:
            self.translations[str(cue.id)] = {
                "text": cue.text,
                "status": "error",
                "batch": batch.number,
                "error": error[:1000],
                "updated_at": now,
            }

    def translate_batch(self, client: BedrockClient, state: dict[str, Any], batch: Batch, *, polish: bool) -> tuple[dict[str, str], str, dict[str, Any]]:
        builder = build_polish_prompt if polish else build_translation_prompt
        system, prompt, cache_prefix = builder(self, batch)
        protected = protected_tokens_for_batch(self, batch)
        max_tokens = estimate_max_tokens(batch, polish=polish)
        text, meta, model, outcome = self.call_with_fallback(
            client,
            state,
            system,
            prompt,
            max_tokens=max_tokens,
            temperature=0.18 if not polish else 0.12,
            stage="polish" if polish else "translate",
            batch=batch.number,
            validator=lambda raw: validate_translation_payload(extract_json_object(raw), batch, protected),
            prompt_builder=lambda feedback: builder(self, batch, feedback=feedback),
            tool_config=translation_tool_config(),
            cache_prefix=cache_prefix,
        )
        self.add_usage(state, meta, model)
        if outcome.get("soft_accepted") and outcome.get("payload"):
            return dict(outcome["payload"]), model, outcome
        payload = extract_json_object(text)
        translations = validate_translation_payload(payload, batch, protected)
        return translations, model, outcome

    def call_with_fallback(
        self,
        client: BedrockClient,
        state: dict[str, Any],
        system: str,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
        stage: str,
        batch: int | None = None,
        validator: Any | None = None,
        max_cycles: int | None = None,
        prompt_builder: Any | None = None,
        tool_config: dict[str, Any] | None = None,
        cache_prefix: str | None = None,
    ) -> tuple[str, dict[str, Any], str, dict[str, Any]]:
        cycle = 0
        last_error = ""
        feedback = ""
        current_max_tokens = max_tokens
        outcome: dict[str, Any] = {"soft_accepted": False, "soft_cue_ids": [], "payload": None, "reason": ""}
        soft_records: list[dict[str, Any]] = []
        hard_failures = 0
        while True:
            cycle += 1
            cycle_soft_only = True
            for model in self.config.models:
                if model in self.unavailable_models:
                    continue
                for attempt in range(1, self.config.attempts_per_model + 1):
                    if self.stopped():
                        raise JobStopped()
                    raw_excerpt = ""
                    state["current"] = {
                        **(state.get("current") or {}),
                        "stage": stage,
                        "batch": batch,
                        "model": model,
                        "attempt": attempt,
                        "cycle": cycle,
                        "soft_failures": len(soft_records),
                    }
                    self.save_state(state)
                    self.logger.event(
                        "INFO",
                        f"Chamando Bedrock: {model}.",
                        batch=batch,
                        model=model,
                        attempt=attempt,
                        max_tokens=current_max_tokens,
                        retry_feedback=bool(feedback),
                    )
                    # Reenviar o prompt identico depois de uma falha faz o modelo repetir
                    # a mesma resposta. Cada retry carrega o motivo da recusa anterior.
                    call_system, call_prompt, call_prefix = system, prompt, cache_prefix
                    if feedback and prompt_builder is not None:
                        try:
                            call_system, call_prompt, call_prefix = prompt_builder(feedback)
                        except Exception:
                            call_system, call_prompt, call_prefix = system, prompt, cache_prefix
                    try:
                        raw, meta = self.converse_com_ou_sem_ferramenta(
                            client,
                            model,
                            call_system,
                            call_prompt,
                            max_tokens=current_max_tokens,
                            temperature=temperature,
                            tool_config=tool_config,
                            cache_prefix=call_prefix,
                        )
                        raw_excerpt = raw[:800]
                        if meta.get("stopReason") == "max_tokens":
                            raise ContractError("Resposta cortada pelo limite max_tokens.")
                        if validator is not None:
                            validator(raw)
                        elif text_has_refusal(raw):
                            raise ContractError("Resposta parece recusa, não tradução no contrato.")
                        self.logger.event("INFO", "Resposta validada.", batch=batch, model=model, attempt=attempt)
                        return raw, meta, model, outcome
                    except BedrockCallError as exc:
                        last_error = str(exc)
                        feedback = ""
                        hard_failures += 1
                        cycle_soft_only = False
                        state["last_error"] = last_error
                        self.save_state(state)
                        if exc.unavailable_model:
                            self.unavailable_models.add(model)
                            self.logger.event("WARN", "Modelo indisponível para esta conta ou forma de chamada; tentando outro.", model=model, error=last_error[:700])
                            break
                        self.logger.event("WARN", "Erro chamando modelo.", batch=batch, model=model, attempt=attempt, error=last_error[:700])
                        if not exc.retryable:
                            break
                    except (ContractError, json.JSONDecodeError) as exc:
                        last_error = str(exc)
                        is_soft = isinstance(exc, ContractError) and exc.soft
                        state["last_error"] = last_error
                        self.save_state(state)
                        if is_soft:
                            soft_records.append(
                                {
                                    "model": model,
                                    "cue_ids": tuple(getattr(exc, "cue_ids", []) or []),
                                    "payload": getattr(exc, "payload", None),
                                    "raw": raw_excerpt,
                                    "meta": locals().get("meta") or {},
                                    "reason": last_error,
                                }
                            )
                            feedback = (
                                f"A resposta anterior foi recusada pela validação automática: {last_error} "
                                "Traduza ou adapte esses IDs para português brasileiro de verdade. "
                                "Se e somente se o texto for vocalizacao musical sem sentido lexical "
                                "(refrao de sílabas, onomatopeia, scat), repita exatamente o mesmo texto: "
                                "isso e aceito e não e considerado erro."
                            )
                        else:
                            hard_failures += 1
                            cycle_soft_only = False
                            feedback = f"A resposta anterior foi recusada: {last_error} Corrija e devolva o contrato JSON exato."
                        self.logger.event(
                            "WARN",
                            "Resposta fora do contrato; vou retentar." if not is_soft else "Heurística de qualidade recusou a resposta; vou retentar com feedback.",
                            batch=batch,
                            model=model,
                            attempt=attempt,
                            soft=is_soft,
                            error=(last_error + (f" | resposta={raw_excerpt}" if raw_excerpt else ""))[:1200],
                        )
                        if "max_tokens" in last_error:
                            current_max_tokens = min(12000, max(current_max_tokens + 1000, int(current_max_tokens * 1.5)))
                        accepted = self.soft_consensus_record(soft_records)
                        if accepted is not None:
                            models_agreeing = sorted({rec["model"] for rec in soft_records if rec["cue_ids"] == accepted["cue_ids"]})
                            outcome.update(
                                {
                                    "soft_accepted": True,
                                    "soft_cue_ids": list(accepted["cue_ids"]),
                                    "payload": accepted["payload"],
                                    "reason": accepted["reason"],
                                    "models_agreeing": models_agreeing,
                                }
                            )
                            self.logger.event(
                                "WARN",
                                "Modelos independentes devolveram o mesmo texto nesses IDs; aceitando por consenso "
                                "e marcando para revisão em vez de travar o lote.",
                                batch=batch,
                                cue_ids=list(accepted["cue_ids"])[:20],
                                models=models_agreeing,
                                error=accepted["reason"][:400],
                            )
                            return accepted.get("raw", ""), accepted.get("meta") or {}, accepted["model"], outcome
                    sleep = min(self.config.max_backoff, self.config.base_backoff * (2 ** (attempt - 1)))
                    sleep = sleep + random.uniform(0, min(2.0, sleep * 0.2))
                    self.sleep_or_stop(sleep)
            if cycle_soft_only and soft_records:
                # Um ciclo inteiro de modelos só falhou na heurística: o payload e valido,
                # entao seguir tentando só queima tokens. Aceita e marca para revisão.
                accepted = soft_records[-1]
                outcome.update(
                    {
                        "soft_accepted": True,
                        "soft_cue_ids": list(accepted["cue_ids"]),
                        "payload": accepted["payload"],
                        "reason": accepted["reason"],
                        "models_agreeing": sorted({rec["model"] for rec in soft_records}),
                    }
                )
                self.logger.event(
                    "WARN",
                    "Ciclo completo de modelos falhou apenas na heurística de qualidade; "
                    "aceitando a tradução e marcando os IDs para revisão.",
                    batch=batch,
                    cue_ids=list(accepted["cue_ids"])[:20],
                    error=accepted["reason"][:400],
                )
                return accepted.get("raw", ""), accepted.get("meta") or {}, accepted["model"], outcome
            if len(self.unavailable_models) >= len(self.config.models):
                raise RuntimeError(
                    "Todos os modelos configurados ficaram indisponiveis para esta conta/região. "
                    "Verifique acesso no console do Amazon Bedrock em Model access, ou troque a lista de modelos."
                )
            if max_cycles is not None and cycle >= max_cycles:
                raise RuntimeError(f"Falha após {cycle} ciclo(s) de modelos. Último erro: {last_error}")
            if self.config.retry_forever:
                sleep = min(self.config.max_backoff, self.config.base_backoff * (2 ** min(cycle, 6)))
                self.logger.event(
                    "WARN",
                    "Todos os modelos falharam neste ciclo; retomando a fila após backoff.",
                    batch=batch,
                    sleep=round(sleep, 1),
                )
                self.sleep_or_stop(sleep)
                continue
            raise RuntimeError(f"Falha após tentar todos os modelos. Último erro: {last_error}")

    def converse_com_ou_sem_ferramenta(
        self,
        client: BedrockClient,
        model: str,
        system_text: str,
        user_text: str,
        *,
        max_tokens: int,
        temperature: float,
        tool_config: dict[str, Any] | None,
        cache_prefix: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """Usa o contrato por ferramenta quando o modelo aceita.

        Modelo que nao suporta ferramenta e descoberto na primeira tentativa e
        anotado, para nao pagar a descoberta de novo; a chamada e refeita na hora
        com o contrato em texto, entao a tentativa nao e desperdicada.
        """
        usar = bool(tool_config) and model not in self.models_without_tools
        try:
            return client.converse(
                model,
                system_text,
                user_text,
                max_tokens=max_tokens,
                temperature=temperature,
                tool_config=tool_config if usar else None,
                cache_prefix=cache_prefix,
            )
        except BedrockCallError as exc:
            if not usar or not is_tool_unsupported_error(str(exc)):
                raise
            self.models_without_tools.add(model)
            self.logger.event(
                "INFO",
                "Modelo nao aceita contrato por ferramenta; usando o contrato em texto para ele.",
                model=model,
            )
        return client.converse(
            model,
            system_text,
            user_text,
            max_tokens=max_tokens,
            temperature=temperature,
            cache_prefix=cache_prefix,
        )

    @staticmethod
    def soft_consensus_record(soft_records: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Aceita quando modelos independentes concordam no mesmo texto suspeito.

        Se dois provedores diferentes devolvem exatamente os mesmos IDs marcados pela
        heurística, a evidencia aponta para a heurística errada, não para o modelo.
        """
        by_ids: dict[tuple, set[str]] = {}
        for rec in soft_records:
            if not rec.get("payload"):
                continue
            by_ids.setdefault(rec["cue_ids"], set()).add(str(rec["model"]))
        for ids, models in by_ids.items():
            if len(models) >= SOFT_CONSENSUS_MODELS:
                for rec in reversed(soft_records):
                    if rec["cue_ids"] == ids and rec.get("payload"):
                        return rec
        return None

    def sleep_or_stop(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            if self.stopped():
                raise JobStopped()
            time.sleep(min(1.0, end - time.time()))

    def add_usage(self, state: dict[str, Any], meta: dict[str, Any], model: str = "") -> None:
        usage = meta.get("usage") if isinstance(meta, dict) else None
        if not isinstance(usage, dict):
            return
        if model:
            por_modelo = state.setdefault("usage_by_model", {})
            atual = por_modelo.setdefault(
                model,
                {"calls": 0, "inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0},
            )
            atual["calls"] = int(atual.get("calls", 0)) + 1
            for key in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens"):
                try:
                    atual[key] = int(atual.get(key, 0)) + int(usage.get(key, 0) or 0)
                except Exception:
                    pass
        total = state.setdefault(
            "usage",
            {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0},
        )
        for key in ("inputTokens", "outputTokens", "totalTokens", "cacheReadInputTokens", "cacheWriteInputTokens"):
            try:
                total[key] = int(total.get(key, 0)) + int(usage.get(key, 0))
            except Exception:
                pass


def config_to_json(config: JobConfig) -> dict[str, Any]:
    out = {k: v for k, v in config.__dict__.items()}
    for key in ("source_path", "output_path", "job_root"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    return out


def collect_samples(cues: list[SrtCue], per_window: int = 18) -> list[dict[str, Any]]:
    if not cues:
        return []
    anchors = [0, max(0, len(cues) // 3), max(0, (len(cues) * 2) // 3), max(0, len(cues) - per_window)]
    seen: set[int] = set()
    samples: list[dict[str, Any]] = []
    for anchor in anchors:
        for cue in cues[anchor : anchor + per_window]:
            if cue.id in seen:
                continue
            seen.add(cue.id)
            samples.append(cue.as_prompt_item(include_time=True))
    return samples[:72]


def prompt_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def adjacent_batches(job: TranslatorJob, batch: Batch) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prev_items: list[dict[str, Any]] = []
    next_items: list[dict[str, Any]] = []
    idx = batch.number - 1
    for b in job.batches[max(0, idx - job.config.context_batches) : idx]:
        for cue in b.cues:
            rec = job.translations.get(str(cue.id), {})
            item = cue.as_prompt_item(include_time=True)
            if rec.get("status") == "ok":
                item["ptbr"] = rec.get("text", "")
            prev_items.append(item)
    for b in job.batches[idx + 1 : idx + 1 + job.config.context_batches]:
        next_items.extend(cue.as_prompt_item(include_time=True) for cue in b.cues)
    return prev_items, next_items


def build_translation_prompt(job: TranslatorJob, batch: Batch, *, feedback: str = "") -> tuple[str, str]:
    context = load_json(job.context_path, {})
    prev_items, next_items = adjacent_batches(job, batch)
    protected = protected_tokens_for_batch(job, batch)
    system = (
        "Você e um tradutor/adaptador senior de legendas audiovisuais para português brasileiro. "
        "A tarefa e traduzir trechos de um arquivo SRT fornecido pelo usuário. "
        "Traduza de forma contextual, idiomatica e natural para público brasileiro, sem literalismo duro. "
        "Preserve sentido, subtexto, humor, ironia, intensidade de palavroes, nomes próprios, tags como <i> e quebras de linha quando ajudarem a leitura. "
        "Preserve erros deliberados, nomes escritos errado, mal-entendidos e autocorrecoes quando eles sustentarem uma piada ou informação posterior. "
        "Quando houver música legendada, trate como legenda audiovisual: adapte o sentido para pt-BR, mantenha simbolos musicais como ♪, e não devolva o texto original sem traduzir. "
        "Não inclua timestamps, comentarios, markdown ou explicacoes. "
        "A resposta deve comecar com {\"translations\": e terminar com }. "
        "Retorne somente JSON valido no contrato pedido."
    )
    estavel = {
        "response_format_required": 'Retorne exatamente: {"translations":[{"id":1,"text":"texto em pt-BR"}]}. A chave de topo deve ser translations.',
        "movie_context": context,
        "glossario_decidido_use_exatamente": glossary_from_context(context),
        "batching_instructions": [
            "CONTEXTO_ANTERIOR pode trazer source e ptbr já traduzido; use para manter continuidade.",
            "LOTE_ATUAL e o único bloco que deve ser traduzido e retornado.",
            "CONTEXTO_SEGUINTE existe apenas para desambiguar o LOTE_ATUAL.",
            "Retorne exatamente um item por id do LOTE_ATUAL, sem IDs extras e sem omitir nenhum.",
            f"Use no máximo {job.config.max_lines} linhas por cue sempre que possível.",
            f"Procure manter cada linha com até {job.config.max_line_length} caracteres visiveis.",
            f"Procure manter velocidade de leitura confortavel, alvo até {job.config.max_cps:.1f} caracteres por segundo.",
            "Se a fala tiver duas pessoas com hifen, preserve essa estrutura quando natural.",
            "Para SDH/som entre colchetes, traduza o conteudo do colchete para pt-BR quando for descritivo: [laughs] -> [ri].",
            "Se houver idioma em colchetes, use forma natural em pt-BR: [speaks French] -> [fala frances].",
            "Se houver tags HTML simples, preserve as tags ao redor do texto equivalente.",
            "Tokens listados em protected_tokens_by_id devem ser copiados exatamente; eles podem ser nomes, grafias intencionais ou piadas.",
            "glossario_decidido_use_exatamente fixa a traducao e o genero de cada termo. Use a forma exata listada, inclusive a desinencia de genero, mesmo que o ingles nao marque genero. Uma personagem tratada por Meritissima em um trecho nao pode virar Meritissimo em outro.",
            "Concordancia de genero: o portugues exige o que o ingles omite. Quem fala define obrigado ou obrigada; quem e descrito define cansado ou cansada. Use o gender do glossario e o contexto anterior para decidir, e mantenha a escolha estavel.",
            "Não resuma, não censure, não explique.",
        ],
    }
    volatil = {
        "protected_tokens_by_id": protected,
        "previous_context_source_and_ptbr": prev_items,
        "current_batch_translate_this": [cue.as_prompt_item(include_time=True) for cue in batch.cues],
        "next_context_source_only": next_items,
    }
    notas = {
        cue.id: job.translations[str(cue.id)]["semantic_note"]
        for cue in batch.cues
        if str(cue.id) in job.translations and job.translations[str(cue.id)].get("semantic_note")
    }
    if notas:
        volatil["revisao_de_sentido_corrija_estes"] = notas
    if feedback:
        volatil["retry_feedback_fix_this_first"] = feedback
    # A parte estavel e identica em todos os lotes do filme, entao vira prefixo de cache.
    return system, prompt_json(volatil), prompt_json(estavel)


def build_polish_prompt(job: TranslatorJob, batch: Batch, *, feedback: str = "") -> tuple[str, str]:
    context = load_json(job.context_path, {})
    prev_items, next_items = adjacent_batches(job, batch)
    protected = protected_tokens_for_batch(job, batch)
    current = []
    for cue in batch.cues:
        rec = job.translations.get(str(cue.id), {})
        current.append(
            {
                "id": cue.id,
                "time": cue.timing,
                "source": cue.text,
                "ptbr_draft": rec.get("text", cue.text),
            }
        )
    system = (
        "Você e revisor senior de legendas em português brasileiro. "
        "Revise apenas o lote atual para naturalidade, contexto, concisao de legenda e consistencia. "
        "Preserve IDs e não inclua comentarios. A resposta deve comecar com {\"translations\": e terminar com }. Retorne somente JSON valido."
    )
    payload = {
        "response_format_required": 'Retorne exatamente: {"translations":[{"id":1,"text":"texto revisado em pt-BR"}]}. A chave de topo deve ser translations.',
        "movie_context": context,
        "rules": [
            "Melhore frases duras ou literais.",
            "Mantenha timing de legenda: frases curtas e legiveis.",
            f"Use no máximo {job.config.max_lines} linhas por cue sempre que possível.",
            f"Procure manter cada linha com até {job.config.max_line_length} caracteres visiveis e até {job.config.max_cps:.1f} cps.",
            "Preserve nomes, tags, hifens de dialogo e simbolos musicais.",
            "Tokens listados em protected_tokens_by_id devem continuar exatamente iguais.",
            "Retorne exatamente os IDs do lote atual.",
        ],
        "protected_tokens_by_id": protected,
        "glossario_decidido_use_exatamente": glossary_from_context(context),
        "previous_context_source_and_ptbr": prev_items,
        "current_batch_review_this": current,
        "next_context_source_only": next_items,
    }
    if feedback:
        payload["retry_feedback_fix_this_first"] = feedback
    return system, prompt_json(payload), None


JUDGE_TOOL_NAME = "avaliar_traducoes"


def judge_tool_config() -> dict[str, Any]:
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": JUDGE_TOOL_NAME,
                    "description": "Devolve a avaliacao de sentido de cada par original/traducao.",
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "itens": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "id": {"type": "integer"},
                                            "veredito": {"type": "string", "enum": ["ok", "suspeito", "errado"]},
                                            "categoria": {"type": "string"},
                                            "porque": {"type": "string"},
                                            "sugestao": {
                                                "type": "string",
                                                "description": "So quando veredito for errado: a legenda corrigida em pt-BR.",
                                            },
                                        },
                                        "required": ["id", "veredito"],
                                    },
                                }
                            },
                            "required": ["itens"],
                        }
                    },
                }
            }
        ],
        "toolChoice": {"tool": {"name": JUDGE_TOOL_NAME}},
    }


def build_judge_prompt(job: "TranslatorJob", pares: list[dict[str, Any]]) -> tuple[str, str]:
    context = load_json(job.context_path, {})
    system = (
        "Voce revisa legendas ja traduzidas para portugues brasileiro. "
        "Seu trabalho NAO e traduzir nem melhorar estilo: e apontar apenas onde o SENTIDO "
        "esta errado, perdido ou distorcido em relacao ao original. "
        "Ignore preferencia estilistica, sinonimo aceitavel e a concisao propria de legenda. "
        "Se o sentido esta preservado, responda ok e siga; nao invente problema. "
        "Use errado somente quando tiver certeza e souber escrever a legenda corrigida; "
        "na duvida use suspeito."
    )
    payload = {
        "movie_context": context,
        "criterios": [
            "sentido_invertido: a traducao afirma o contrario do original.",
            "termo_errado: termo tecnico ou juridico trocado por outro que muda o sentido.",
            "omissao: parte relevante do sentido sumiu.",
            "adicao: a traducao afirma algo que o original nao diz.",
            "registro_errado: formalidade ou tratamento incompativel com a cena.",
            "Quando marcar errado, sugestao deve conter a legenda inteira corrigida, "
            "respeitando as quebras de linha e as tags do original.",
        ],
        "pares_para_avaliar": pares,
    }
    return system, prompt_json(payload)


def build_semantic_fix_prompt(
    job: "TranslatorJob",
    alvos: list[SrtCue],
    vizinhanca: list[dict[str, Any]],
) -> tuple[str, str]:
    """Pede a correção só das falas acusadas, com as vizinhas como leitura.

    Refazer o lote inteiro para consertar uma fala re-sorteia dezenas de traduções
    que estavam boas. Aqui as vizinhas entram como contexto imutável.
    """
    context = load_json(job.context_path, {})
    system = (
        "Voce e tradutor senior de legendas para portugues brasileiro. "
        "Um revisor apontou erro de sentido em legendas especificas. "
        "Corrija APENAS as legendas pedidas, uma por id, mantendo o estilo do restante. "
        "As legendas vizinhas sao so contexto: nao as devolva nem as altere. "
        "Se ao ler o original voce concluir que a traducao atual ja esta correta, "
        "devolva ela mesma sem mudanca."
    )
    corrigir = []
    for cue in alvos:
        rec = job.translations.get(str(cue.id), {})
        corrigir.append(
            {
                "id": cue.id,
                "time": cue.timing,
                "original_ingles": cue.text,
                "traducao_atual": rec.get("text", ""),
                "critica_do_revisor": rec.get("semantic_note", ""),
                "categoria": rec.get("semantic_category", ""),
            }
        )
    payload = {
        "movie_context": context,
        "glossario_decidido_use_exatamente": glossary_from_context(context),
        "regras": [
            f"Use no maximo {job.config.max_lines} linhas e ate {job.config.max_line_length} caracteres por linha.",
            "Preserve tags como <i> e os simbolos musicais do original.",
            "Preserve a estrutura de dois falantes com hifen quando existir.",
            "Devolva exatamente um item para cada id em legendas_para_corrigir.",
        ],
        "contexto_ao_redor_nao_devolver": vizinhanca,
        "legendas_para_corrigir": corrigir,
    }
    return system, prompt_json(payload)


def estimate_max_tokens(batch: Batch, *, polish: bool = False) -> int:
    chars = sum(len(cue.text) for cue in batch.cues)
    return max(1800, min(9000, int(chars * 1.6) + len(batch.cues) * 70 + (1200 if polish else 1800)))


def discover_resume_source(path: Path) -> Path:
    sidecar = path.with_name(path.name + ".translator-state.json")
    if sidecar.exists():
        data = load_json(sidecar, {})
        src = data.get("source_path")
        if src and Path(src).exists():
            return Path(src)
    return path


def build_config_from_args(args: argparse.Namespace) -> JobConfig:
    source = discover_resume_source(Path(args.input).expanduser()) if getattr(args, "input", None) else Path.cwd()
    models = parse_models(args.models) if getattr(args, "models", None) else list(DEFAULT_MODELS)
    return JobConfig(
        source_path=source,
        profile=getattr(args, "profile", DEFAULT_PROFILE),
        region=getattr(args, "region", DEFAULT_REGION),
        models=models,
        batch_size=getattr(args, "batch_size", 28),
        max_batch_chars=getattr(args, "max_batch_chars", 4300),
        context_batches=getattr(args, "context_batches", 1),
        attempts_per_model=getattr(args, "attempts_per_model", 3),
        base_backoff=getattr(args, "base_backoff", 3.0),
        max_backoff=getattr(args, "max_backoff", 120.0),
        retry_forever=not getattr(args, "no_retry_forever", False),
        call_timeout=getattr(args, "call_timeout", 240),
        context_pass=not getattr(args, "no_context_pass", False),
        polish_pass=getattr(args, "polish_pass", False),
        retry_qc_issues=not getattr(args, "no_retry_qc_issues", False),
        qc_repair_rounds=getattr(args, "qc_repair_rounds", 2),
        semantic_review=bool(getattr(args, "semantic_review", False)),
        semantic_autofix=bool(getattr(args, "semantic_autofix", False)),
        semantic_min_signals=int(getattr(args, "semantic_min_signals", 0) or 0),
        semantic_budget=int(getattr(args, "semantic_budget", 100000) or 100000),
        semantic_sample_pct=float(getattr(args, "semantic_sample_pct", 0.10) or 0.0),
        max_lines=getattr(args, "max_lines", 2),
        max_line_length=getattr(args, "max_line_length", 42),
        max_cps=getattr(args, "max_cps", 17.0),
        max_cues=getattr(args, "max_cues", None),
        output_path=Path(args.output).expanduser().resolve() if getattr(args, "output", None) else None,
        job_root=Path(args.job_root).expanduser().resolve() if getattr(args, "job_root", None) else None,
        force_new=getattr(args, "force_new", False),
    )


def parse_models(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [part.strip() for part in re.split(r"[,;\n]", str(value)) if part.strip()]


RUNNERS: dict[str, TranslatorJob] = {}
RUNNERS_LOCK = threading.RLock()


def runner_alive(job_id: Any) -> bool:
    if not job_id:
        return False
    with RUNNERS_LOCK:
        runner = RUNNERS.get(str(job_id))
        if not runner:
            return False
        thread = getattr(runner, "thread", None)
        if thread and thread.is_alive():
            return True
        RUNNERS.pop(str(job_id), None)
        return False


def list_srt_files(base: Path) -> list[dict[str, Any]]:
    out = []
    def sort_key(path: Path) -> tuple[int, int, int, str]:
        rel = safe_rel(path, base)
        name = path.name.lower()
        depth = len(Path(rel).parts)
        sdh = 1 if re.search(r"(\b|[._-])sdh(\b|[._-])", name) else 0
        return (depth, sdh, len(rel), rel.lower())

    for path in sorted(base.rglob("*.srt"), key=sort_key):
        if ".srt_translator_jobs" in path.parts:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append(
            {
                "path": str(path.resolve()),
                "name": safe_rel(path, base),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
            }
        )
    return out[:500]


def job_index_path(base: Path) -> Path:
    return base / ".srt_translator_jobs" / "known_jobs.json"


def register_job_dir(base: Path, job_dir: Path) -> None:
    """Anota o job para a UI achar mesmo quando a legenda esta fora da pasta base.

    O diretório de estado nasce ao lado do .srt de origem. Sem este indice, uma
    legenda escolhida pelo campo de caminho absoluto virava um trabalho que roda
    mas não aparece na tela: sem status, sem log e sem botao de parar.
    """
    try:
        path = job_index_path(base)
        path.parent.mkdir(parents=True, exist_ok=True)
        known = [str(item) for item in load_json(path, []) if isinstance(item, str)]
        entry = str(job_dir.resolve())
        if entry in known:
            return
        known.append(entry)
        atomic_write_json(path, known[-200:])
    except Exception:
        pass


def known_job_dirs(base: Path) -> list[Path]:
    dirs: list[Path] = []
    for item in load_json(job_index_path(base), []):
        if not isinstance(item, str):
            continue
        path = Path(item)
        if (path / "state.json").exists():
            dirs.append(path)
    with RUNNERS_LOCK:
        for runner in RUNNERS.values():
            job_dir = getattr(runner, "job_dir", None)
            if job_dir and (Path(job_dir) / "state.json").exists():
                dirs.append(Path(job_dir))
    return dirs


def browse_directory(alvo: str | None, base: Path) -> dict[str, Any]:
    """Lista pastas e legendas de um diretório, para o seletor de arquivos da UI.

    Só leitura. O servidor escuta em 127.0.0.1 e o campo de caminho absoluto já
    permitia apontar para qualquer lugar, então isto não amplia o alcance: só
    troca digitar o caminho por navegar até ele.
    """
    caminho = Path(alvo).expanduser() if alvo else base
    try:
        caminho = caminho.resolve()
    except OSError:
        caminho = base
    if caminho.is_file():
        caminho = caminho.parent
    if not caminho.is_dir():
        # Todo caminho válido volta resolvido; o de fallback também, senão a mesma
        # pasta apareceria com dois nomes conforme o jeito de chegar nela
        # (/tmp e /private/tmp no macOS).
        caminho = base if base.is_dir() else Path.home()
        try:
            caminho = caminho.resolve()
        except OSError:
            pass

    pastas: list[dict[str, str]] = []
    legendas: list[dict[str, Any]] = []
    erro = None
    try:
        for item in sorted(caminho.iterdir(), key=lambda i: i.name.lower()):
            if item.name.startswith("."):
                continue
            try:
                if item.is_dir():
                    pastas.append({"name": item.name, "path": str(item)})
                elif item.suffix.lower() == ".srt":
                    legendas.append(
                        {
                            "name": item.name,
                            "path": str(item),
                            "size": item.stat().st_size,
                            "traduzida": bool(re.search(r"\.(OK|INCOMPLETO|EM_ANDAMENTO)\.srt$", item.name)),
                        }
                    )
            except OSError:
                continue
    except PermissionError:
        erro = "Sem permissão para ler esta pasta."
    except OSError as exc:
        erro = f"Não consegui ler esta pasta: {exc}"

    atalhos = []
    for rotulo, destino in (
        ("Pasta atual da UI", base),
        ("Filmes", Path.home() / "Movies"),
        ("Downloads", Path.home() / "Downloads"),
        ("Área de trabalho", Path.home() / "Desktop"),
        ("Pasta pessoal", Path.home()),
    ):
        if destino.is_dir():
            atalhos.append({"label": rotulo, "path": str(destino.resolve())})

    return {
        "path": str(caminho),
        "parent": str(caminho.parent) if caminho.parent != caminho else None,
        "dirs": pastas,
        "files": legendas,
        "shortcuts": atalhos,
        "error": erro,
    }


def load_jobs(base: Path) -> list[dict[str, Any]]:
    state_paths: dict[str, Path] = {}
    for state_path in base.rglob(".srt_translator_jobs/*/state.json"):
        state_paths[str(state_path.parent.resolve())] = state_path
    for job_dir in known_job_dirs(base):
        state_paths.setdefault(str(job_dir.resolve()), job_dir / "state.json")
    ordered = sorted(
        state_paths.values(),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    jobs = []
    for state_path in ordered:
        state = load_json(state_path, {})
        if not state:
            continue
        state["state_path"] = str(state_path)
        state["job_dir"] = str(state_path.parent)
        jobs.append(compact_state(state))
    return jobs[:100]


def compact_state(state: dict[str, Any]) -> dict[str, Any]:
    total = int(state.get("total_cues") or 0)
    translations = load_json(Path(state.get("job_dir", "")) / "translations.json", {}) if state.get("job_dir") else {}
    quality = load_json(Path(state.get("job_dir", "")) / "quality_report.json", {}) if state.get("job_dir") else {}
    if state.get("job_dir"):
        quality = ensure_quality_report_current(Path(state["job_dir"]), state, translations, quality)
    done = sum(1 for rec in translations.values() if isinstance(rec, dict) and rec.get("status") == "ok")
    current = state.get("current") or {}
    quality_summary = quality.get("summary", state.get("quality", {})) if isinstance(quality, dict) else state.get("quality", {})
    stored_status = state.get("status")
    is_alive = runner_alive(state.get("job_id"))
    stale_running = stored_status == "running" and not is_alive
    return {
        "job_id": state.get("job_id"),
        "status": "stalled" if stale_running else stored_status,
        "stored_status": stored_status,
        "runner_alive": is_alive,
        "stale_running": stale_running,
        "source_path": state.get("source_path"),
        "output": state.get("final_output_path") or state.get("last_written_output"),
        "last_error": state.get("last_error") or ("Processo de tradução não esta ativo; use Retomar para continuar." if stale_running else None),
        "updated_at": state.get("updated_at"),
        "total_cues": total,
        "done_cues": done if total and done <= total else done,
        "pending_cues": quality_summary.get("pending_cues", max(0, total - done) if total else 0),
        "total_batches": state.get("total_batches"),
        "completed_batches": len(state.get("completed_batches", [])),
        "current": current,
        "usage": state.get("usage", {}),
        "quality": quality_summary,
        "review_cues": sum(1 for rec in translations.values() if isinstance(rec, dict) and rec.get("review_flag")),
        "stuck_batch": bool(
            stored_status == "running"
            and is_alive
            and (int(current.get("cycle") or 0) >= 2 or int(current.get("soft_failures") or 0) >= 3)
        ),
    }


def ensure_quality_report_current(
    job_dir: Path,
    state: dict[str, Any],
    translations: dict[str, Any],
    quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quality = quality or {}
    # Relatório gravado por uma versao anterior dos critérios e recalculado, senao a UI
    # mostra números que não batem com as regras atuais.
    if isinstance(quality, dict) and quality.get("report_version") == QUALITY_REPORT_VERSION:
        return quality
    source = state.get("source_path")
    if not source or not Path(source).exists():
        return quality or {}
    try:
        doc = SrtDocument.load(Path(source))
        protected = detect_spelling_variant_tokens(doc.cues)
        protected_by_id = {
            cue.id: sorted({token for token in capitalized_tokens(cue.text) if token in protected})
            for cue in doc.cues
        }
        report = build_quality_report(
            doc.cues,
            translations,
            max_lines=int(state.get("max_lines") or 2),
            max_line_length=int(state.get("max_line_length") or 42),
            max_cps=float(state.get("max_cps") or 17.0),
            protected_tokens_by_id=protected_by_id,
        )
        atomic_write_json(job_dir / "quality_report.json", report)
        return report
    except Exception:
        return quality or {}


def find_job_dir(base: Path, job_id: str) -> Path | None:
    for state_path in base.rglob(".srt_translator_jobs/*/state.json"):
        if state_path.parent.name == job_id:
            return state_path.parent
    for job_dir in known_job_dirs(base):
        if job_dir.name == job_id:
            return job_dir
    direct = base / ".srt_translator_jobs" / job_id
    return direct if direct.exists() else None


def state_for_job_dir(job_dir: Path) -> dict[str, Any]:
    state = load_json(job_dir / "state.json", {})
    state["job_dir"] = str(job_dir)
    stored_status = state.get("status")
    is_alive = runner_alive(state.get("job_id") or job_dir.name)
    stale_running = stored_status == "running" and not is_alive
    state["stored_status"] = stored_status
    state["runner_alive"] = is_alive
    state["stale_running"] = stale_running
    if stale_running:
        state["status"] = "stalled"
        if not state.get("last_error"):
            state["last_error"] = "Processo de tradução não esta ativo; use Retomar para continuar."
    translations = load_json(job_dir / "translations.json", {})
    state["done_cues"] = sum(1 for rec in translations.values() if isinstance(rec, dict) and rec.get("status") == "ok")
    quality = load_json(job_dir / "quality_report.json", {})
    quality = ensure_quality_report_current(job_dir, state, translations, quality)
    if quality:
        state["quality"] = quality.get("summary", {})
        state["error_cues"] = quality.get("summary", {}).get("error_cues", 0)
        state["pending_cues"] = quality.get("summary", {}).get("pending_cues", 0)
        state["warning_cues"] = quality.get("summary", {}).get("warning_cues", 0)
        state["quality_report_path"] = str(job_dir / "quality_report.json")
    else:
        state["error_cues"] = sum(1 for rec in translations.values() if isinstance(rec, dict) and rec.get("status") == "error")
        state["pending_cues"] = max(0, int(state.get("total_cues") or 0) - int(state.get("done_cues") or 0))
        state["warning_cues"] = 0
    # translations.json e a fonte da verdade: um retry posterior limpa a flag, entao
    # derivar daqui evita mostrar cue que já foi corrigido.
    review_ids = sorted(
        int(cue_id)
        for cue_id, rec in translations.items()
        if isinstance(rec, dict) and rec.get("review_flag")
    )
    state["review_cue_ids"] = review_ids
    state["review_cues"] = len(review_ids)
    current = state.get("current") or {}
    state["stuck_batch"] = bool(
        stored_status == "running"
        and is_alive
        and (int(current.get("cycle") or 0) >= 2 or int(current.get("soft_failures") or 0) >= 3)
    )
    state["cost"] = estimate_cost(state.get("usage_by_model") or {}, load_prices(), load_exchange_rate())
    state["log_tail"] = JsonLogger(job_dir, echo=False).tail(240)
    state["preview"] = preview_current(job_dir, state, translations)
    state["compare"] = compare_recent(state, translations)
    return state


_DOC_CACHE: dict[str, tuple[float, "SrtDocument"]] = {}
_DOC_CACHE_LOCK = threading.Lock()


def load_source_doc_cached(path: Path) -> "SrtDocument | None":
    """A UI faz polling a cada 2,5s; reparsear a legenda inteira toda vez e desperdicio."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    with _DOC_CACHE_LOCK:
        hit = _DOC_CACHE.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        doc = SrtDocument.load(path)
    except Exception:
        return None
    with _DOC_CACHE_LOCK:
        _DOC_CACHE[key] = (mtime, doc)
        if len(_DOC_CACHE) > 8:
            _DOC_CACHE.pop(next(iter(_DOC_CACHE)))
    return doc


def compare_recent(state: dict[str, Any], translations: dict[str, Any], limit: int | None = 60) -> list[dict[str, Any]]:
    """Falas já traduzidas, com fonte e tradução lado a lado.

    `limit` recorta as últimas N para o acompanhamento ao vivo; `None` devolve o
    filme inteiro, usado pelo endpoint sob demanda para não inflar cada polling.
    """
    src = state.get("source_path")
    if not src:
        return []
    doc = load_source_doc_cached(Path(src))
    if doc is None:
        return []
    done = [
        (cue, rec)
        for cue in doc.cues
        if isinstance(rec := translations.get(str(cue.id)), dict)
        and rec.get("status") == "ok"
        and str(rec.get("text", "")).strip()
    ]
    return [
        {
            "id": cue.id,
            "time": cue.timing,
            "source": cue.text,
            "translation": str(rec.get("text", "")),
            "review": bool(rec.get("review_flag")),
        }
        for cue, rec in (done if limit is None else done[-limit:])
    ]


def preview_current(job_dir: Path, state: dict[str, Any], translations: dict[str, Any]) -> dict[str, Any]:
    src = state.get("source_path")
    current = state.get("current") or {}
    if not src or not Path(src).exists():
        return {}
    doc = load_source_doc_cached(Path(src))
    if doc is None:
        return {}
    batch_no = current.get("batch")
    if not batch_no:
        return {}
    batches = make_batches(doc.cues, int(state.get("batch_size") or 28), int(state.get("max_batch_chars") or 4300))
    if not (1 <= int(batch_no) <= len(batches)):
        return {}
    batch = batches[int(batch_no) - 1]
    items = []
    for cue in batch.cues[:8]:
        items.append(
            {
                "id": cue.id,
                "time": cue.timing,
                "source": cue.text,
                "translation": translations.get(str(cue.id), {}).get("text", ""),
                "status": translations.get(str(cue.id), {}).get("status", "pending"),
            }
        )
    return {"batch": batch_no, "items": items}


class UIHandler(BaseHTTPRequestHandler):
    server_version = "SRTTranslator/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    @property
    def base(self) -> Path:
        return self.server.base_path  # type: ignore[attr-defined]

    def send_json(self, data: Any, status: int = 200) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_html(self, text: str) -> None:
        raw = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(raw or "{}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(render_ui_html())
            return
        if parsed.path == "/api/files":
            self.send_json({"files": list_srt_files(self.base)})
            return
        if parsed.path == "/api/jobs":
            self.send_json({"jobs": load_jobs(self.base)})
            return
        if parsed.path == "/api/job":
            qs = parse_qs(parsed.query)
            job_id = (qs.get("id") or [""])[0]
            job_dir = find_job_dir(self.base, job_id)
            if not job_dir:
                self.send_json({"error": "job not found"}, 404)
                return
            self.send_json({"job": state_for_job_dir(job_dir)})
            return
        if parsed.path == "/api/browse":
            qs = parse_qs(parsed.query)
            self.send_json(browse_directory((qs.get("path") or [""])[0] or None, self.base))
            return
        if parsed.path == "/api/compare":
            qs = parse_qs(parsed.query)
            job_id = (qs.get("id") or [""])[0]
            job_dir = find_job_dir(self.base, job_id)
            if not job_dir:
                self.send_json({"error": "job not found"}, 404)
                return
            state = load_json(job_dir / "state.json", {})
            translations = load_json(job_dir / "translations.json", {})
            items = compare_recent(state, translations, limit=None)
            self.send_json({"items": items, "total": len(items)})
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/start":
                data = self.read_json()
                source = Path(data.get("path", "")).expanduser().resolve()
                if not source.exists():
                    self.send_json({"error": "Arquivo não encontrado."}, 400)
                    return
                cfg = JobConfig(
                    source_path=discover_resume_source(source),
                    profile=data.get("profile") or DEFAULT_PROFILE,
                    region=data.get("region") or DEFAULT_REGION,
                    models=parse_models(data.get("models") or ",".join(DEFAULT_MODELS)),
                    batch_size=int(data.get("batch_size") or 28),
                    max_batch_chars=int(data.get("max_batch_chars") or 4300),
                    context_batches=int(data.get("context_batches") or 1),
                    attempts_per_model=int(data.get("attempts_per_model") or 3),
                    retry_forever=bool(data.get("retry_forever", True)),
                    context_pass=bool(data.get("context_pass", True)),
                    polish_pass=bool(data.get("polish_pass", False)),
                    retry_qc_issues=bool(data.get("retry_qc_issues", True)),
                    qc_repair_rounds=int(data.get("qc_repair_rounds") or 2),
                    semantic_review=bool(data.get("semantic_review", False)),
                    semantic_autofix=bool(data.get("semantic_autofix", False)),
                    max_lines=int(data.get("max_lines") or 2),
                    max_line_length=int(data.get("max_line_length") or 42),
                    max_cps=float(data.get("max_cps") or 17.0),
                    call_timeout=int(data.get("call_timeout") or 240),
                    force_new=bool(data.get("force_new", False)),
                )
                job = TranslatorJob(cfg)
                register_job_dir(self.base, job.job_dir)
                start_job_thread(job)
                self.send_json({"job_id": job.job_id})
                return
            if parsed.path == "/api/resume":
                data = self.read_json()
                job_id = data.get("job_id")
                job_dir = find_job_dir(self.base, str(job_id))
                if not job_dir:
                    self.send_json({"error": "job not found"}, 404)
                    return
                state = load_json(job_dir / "state.json", {})
                source = Path(state.get("source_path", ""))
                cfg = JobConfig(
                    source_path=source,
                    profile=data.get("profile") or state.get("profile") or DEFAULT_PROFILE,
                    region=data.get("region") or state.get("region") or DEFAULT_REGION,
                    models=parse_models(data.get("models") or ",".join(state.get("models") or DEFAULT_MODELS)),
                    batch_size=int(data.get("batch_size") or state.get("batch_size") or 28),
                    max_batch_chars=int(data.get("max_batch_chars") or state.get("max_batch_chars") or 4300),
                    context_batches=int(data.get("context_batches") or state.get("context_batches") or 1),
                    attempts_per_model=int(data.get("attempts_per_model") or state.get("attempts_per_model") or 3),
                    retry_forever=bool(data.get("retry_forever", state.get("retry_forever", True))),
                    context_pass=bool(data.get("context_pass", state.get("context_pass", True))),
                    polish_pass=bool(data.get("polish_pass", state.get("polish_pass", False))),
                    retry_qc_issues=bool(data.get("retry_qc_issues", state.get("retry_qc_issues", True))),
                    qc_repair_rounds=int(data.get("qc_repair_rounds") or state.get("qc_repair_rounds") or 2),
                    semantic_review=bool(data.get("semantic_review", False)),
                    semantic_autofix=bool(data.get("semantic_autofix", False)),
                    max_lines=int(data.get("max_lines") or state.get("max_lines") or 2),
                    max_line_length=int(data.get("max_line_length") or state.get("max_line_length") or 42),
                    max_cps=float(data.get("max_cps") or state.get("max_cps") or 17.0),
                    call_timeout=int(data.get("call_timeout") or 240),
                    job_root=job_dir.parent,
                )
                job = TranslatorJob(cfg)
                register_job_dir(self.base, job.job_dir)
                start_job_thread(job)
                self.send_json({"job_id": job.job_id})
                return
            if parsed.path == "/api/stop":
                data = self.read_json()
                job_id = str(data.get("job_id") or "")
                with RUNNERS_LOCK:
                    runner = RUNNERS.get(job_id)
                if runner:
                    runner.request_stop()
                    self.send_json({"ok": True, "message": "Parada solicitada."})
                else:
                    job_dir = find_job_dir(self.base, job_id)
                    if job_dir:
                        (job_dir / "STOP").write_text("stop requested " + utc_now(), encoding="utf-8")
                    self.send_json({"ok": True, "message": "Sinal de parada gravado."})
                return
            if parsed.path == "/api/doctor":
                data = self.read_json()
                profile = data.get("profile") or DEFAULT_PROFILE
                region = data.get("region") or DEFAULT_REGION
                models = parse_models(data.get("models") or ",".join(DEFAULT_MODELS))
                client = make_llm_client(profile, region, int(data.get("call_timeout") or 60), JsonLogger(echo=False))
                results = []
                for model in models:
                    try:
                        raw, meta = client.converse(
                            model,
                            "Responda somente JSON valido.",
                            'Retorne exatamente {"ok":true}.',
                            max_tokens=40,
                            temperature=0.1,
                        )
                        payload = extract_json_object(raw)
                        results.append({"model": model, "ok": payload.get("ok") is True, "response": raw[:200], "meta": meta})
                    except Exception as exc:
                        results.append({"model": model, "ok": False, "error": str(exc)[:1000]})
                self.send_json({"results": results, "ok_count": sum(1 for r in results if r.get("ok"))})
                return
        except Exception as exc:
            self.send_json({"error": str(exc), "traceback": traceback.format_exc()[-2000:]}, 500)
            return
        self.send_json({"error": "not found"}, 404)


def start_job_thread(job: TranslatorJob) -> None:
    def target() -> None:
        try:
            job.run()
        except Exception as exc:
            try:
                job.logger.event(
                    "ERROR",
                    "Runner terminou com exceção não tratada.",
                    status="failed",
                    error=f"{exc.__class__.__name__}: {exc}"[:1000],
                )
            except Exception:
                pass
        finally:
            with RUNNERS_LOCK:
                if RUNNERS.get(job.job_id) is job:
                    RUNNERS.pop(job.job_id, None)

    thread = threading.Thread(target=target, name=f"srt-job-{job.job_id}", daemon=True)
    job.thread = thread
    with RUNNERS_LOCK:
        existing = RUNNERS.get(job.job_id)
        existing_thread = getattr(existing, "thread", None) if existing else None
        if existing_thread and existing_thread.is_alive():
            return
        RUNNERS[job.job_id] = job
    thread.start()


UI_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SRT Bedrock Translator</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --ink: #20242b;
      --muted: #69707d;
      --line: #d9dee7;
      --blue: #1f6feb;
      --green: #16833a;
      --red: #bf3030;
      --amber: #986400;
      --violet: #6842c2;
      --shadow: 0 10px 30px rgba(34, 40, 49, .08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    header {
      padding: 14px 24px;
      border-bottom: 1px solid var(--line);
      background: #fff;
      position: sticky;
      top: 0;
      z-index: 5;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    h1 { margin: 0; font-size: 19px; font-weight: 750; }
    .header-hint { font-size: 12px; color: var(--muted); }
    main {
      display: grid;
      grid-template-columns: minmax(320px, 440px) 1fr;
      gap: 16px;
      padding: 16px;
      max-width: 1560px;
      margin: 0 auto;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      min-width: 0;
    }
    .panel-head {
      padding: 13px 16px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .panel-head h2 { margin: 0; font-size: 15px; display: flex; align-items: center; gap: 6px; }
    .panel-body { padding: 16px; }
    label { display: block; font-size: 12px; font-weight: 650; color: var(--muted); margin: 12px 0 6px; }
    .field-label { display: flex; align-items: center; gap: 5px; }
    select, input, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      font: inherit;
      color: var(--ink);
      background: #fff;
    }
    select:focus, input:focus, textarea:focus {
      outline: 2px solid rgba(31,111,235,.35);
      outline-offset: 1px;
      border-color: var(--blue);
    }
    textarea {
      min-height: 92px;
      resize: vertical;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
    }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 16px; }
    .action-wrap { display: flex; align-items: center; gap: 4px; }
    button {
      border: 1px solid transparent;
      border-radius: 6px;
      padding: 9px 12px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      background: #eef2f7;
      color: var(--ink);
    }
    button:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
    button.primary { background: var(--blue); color: white; }
    button.danger { background: #fff1f1; color: var(--red); border-color: #efc5c5; }
    button:disabled { opacity: .45; cursor: not-allowed; }
    button.busy { position: relative; color: transparent !important; }
    button.busy::after {
      content: "";
      position: absolute;
      inset: 0;
      margin: auto;
      width: 14px; height: 14px;
      border: 2px solid rgba(255,255,255,.45);
      border-top-color: #fff;
      border-radius: 50%;
      animation: spin .7s linear infinite;
    }
    button.busy:not(.primary)::after { border-color: rgba(32,36,43,.25); border-top-color: var(--ink); }
    @keyframes spin { to { transform: rotate(360deg); } }
    .toggle-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
      color: var(--muted);
      font-size: 13px;
    }
    .toggle-row input { width: auto; flex: none; }
    .toggle-row span { flex: 1; }

    /* ---- botão de ajuda e popover ---- */
    .info {
      width: 16px; height: 16px;
      min-width: 16px;
      padding: 0;
      border-radius: 50%;
      border: 1px solid var(--line);
      background: #f2f5f9;
      color: var(--muted);
      font-size: 11px;
      font-weight: 800;
      font-style: italic;
      line-height: 1;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      cursor: help;
    }
    .info:hover, .info.open {
      background: var(--blue);
      border-color: var(--blue);
      color: #fff;
    }
    #help {
      position: fixed;
      z-index: 60;
      width: 330px;
      max-width: calc(100vw - 24px);
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 10px;
      box-shadow: 0 16px 44px rgba(20,26,34,.22);
      padding: 13px 14px;
      font-size: 13px;
      line-height: 1.5;
      display: none;
    }
    #help.show { display: block; }
    #help h4 { margin: 0 0 6px; font-size: 13px; font-weight: 800; }
    #help p { margin: 0 0 8px; color: #3c434e; }
    #help .ex {
      background: #f5f8fc;
      border-left: 3px solid var(--blue);
      border-radius: 0 6px 6px 0;
      padding: 8px 10px;
      font-size: 12px;
      color: #2c333d;
    }
    #help .ex b { color: var(--blue); display: block; margin-bottom: 2px; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
    #help code {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 11.5px;
      background: #eaeff6;
      padding: 1px 4px;
      border-radius: 3px;
      overflow-wrap: anywhere;
    }
    #help .sample {
      display: block;
      margin: 7px 0;
      padding: 7px 9px;
      background: #11161c;
      color: #e9eef4;
      border-radius: 5px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.5;
      text-align: center;
    }
    #help .tip {
      margin-top: 8px;
      font-size: 12px;
      color: #2f5d38;
      background: #f0faf3;
      border: 1px solid #cbe7d4;
      border-radius: 6px;
      padding: 7px 9px;
    }
    #help .tip b { color: #16833a; }
    #help .pin-hint { margin: 8px 0 0; font-size: 11px; color: var(--muted); }

    /* ---- seletor de arquivo ---- */
    .path-row { display: flex; gap: 6px; }
    .path-row input { flex: 1; }
    .path-row button { white-space: nowrap; }
    #navOverlay {
      position: fixed; inset: 0; z-index: 70;
      background: rgba(20, 26, 34, .45);
      display: flex; align-items: center; justify-content: center;
      padding: 24px;
    }
    #navOverlay[hidden] { display: none; }
    #navBox {
      background: #fff; border-radius: 12px; width: 720px; max-width: 100%;
      max-height: 82vh; display: flex; flex-direction: column;
      box-shadow: 0 24px 60px rgba(20,26,34,.3);
    }
    .nav-head { display: flex; align-items: center; justify-content: space-between; padding: 14px 16px; border-bottom: 1px solid var(--line); }
    .nav-head button { background: none; border: none; font-size: 20px; color: var(--muted); cursor: pointer; padding: 0 4px; }
    .nav-atalhos { display: flex; gap: 6px; flex-wrap: wrap; padding: 10px 16px 0; }
    .nav-atalhos button { padding: 4px 10px; font-size: 12px; font-weight: 600; background: #eef2f7; }
    .nav-caminho {
      padding: 10px 16px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px; color: var(--muted); overflow-wrap: anywhere;
    }
    .nav-lista { flex: 1; overflow: auto; border-top: 1px solid var(--line); }
    .nav-item {
      display: flex; align-items: center; gap: 10px; width: 100%;
      padding: 9px 16px; border: 0; border-bottom: 1px solid #f2f4f8;
      background: #fff; cursor: pointer; text-align: left; font: inherit; font-size: 13px;
    }
    .nav-item:hover { background: #f5f8fd; }
    .nav-item .ic { width: 18px; text-align: center; }
    .nav-item .nome { flex: 1; overflow-wrap: anywhere; }
    .nav-item .tag { font-size: 11px; color: var(--amber); font-weight: 700; }
    .nav-item .tam { font-size: 11px; color: var(--muted); }
    .nav-item.srt .nome { font-weight: 650; }
    .nav-rodape { padding: 10px 16px; border-top: 1px solid var(--line); font-size: 12px; color: var(--muted); min-height: 20px; }

    /* ---- toasts ---- */
    #toasts {
      position: fixed;
      top: 14px;
      right: 14px;
      z-index: 80;
      display: flex;
      flex-direction: column;
      gap: 9px;
      width: 380px;
      max-width: calc(100vw - 28px);
    }
    .toast {
      background: #fff;
      border: 1px solid var(--line);
      border-left: 4px solid var(--muted);
      border-radius: 8px;
      box-shadow: 0 12px 32px rgba(20,26,34,.18);
      padding: 11px 13px;
      animation: slidein .18s ease;
    }
    @keyframes slidein { from { transform: translateX(14px); opacity: 0; } }
    .toast.success { border-left-color: var(--green); }
    .toast.error { border-left-color: var(--red); }
    .toast.warn { border-left-color: var(--amber); }
    .toast.info { border-left-color: var(--blue); }
    .toast .t-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
    .toast strong { font-size: 13px; }
    .toast .t-body { font-size: 12.5px; color: #464d58; margin-top: 3px; line-height: 1.45; overflow-wrap: anywhere; }
    .toast .t-close {
      background: none;
      border: none;
      color: var(--muted);
      font-size: 16px;
      line-height: 1;
      padding: 0 2px;
      cursor: pointer;
      font-weight: 700;
    }
    .toast .t-actions { margin-top: 8px; display: flex; gap: 6px; }
    .toast .t-actions button { padding: 5px 9px; font-size: 12px; }

    /* ---- status ---- */
    .status-grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(104px, 1fr));
      gap: 10px;
      padding: 16px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 11px;
      background: #fbfcfe;
    }
    .metric .value { font-size: 19px; font-weight: 800; margin-bottom: 2px; }
    .metric .label { font-size: 11.5px; color: var(--muted); display: flex; align-items: center; gap: 4px; }
    .bar { height: 10px; background: #e7ebf1; border-radius: 99px; overflow: hidden; margin: 0 16px 8px; }
    .bar > div { height: 100%; width: 0%; background: linear-gradient(90deg, var(--blue), var(--green)); transition: width .25s ease; }
    .eta { margin: 0 16px 14px; font-size: 12px; color: var(--muted); }

    /* ---- cartão de resultado ---- */
    .result {
      margin: 0 16px 14px;
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 14px;
      display: none;
    }
    .result.show { display: block; }
    .result.ok { border-color: #b9dfc6; background: #f2fdf6; }
    .result.warn { border-color: #f0d99f; background: #fffaf0; }
    .result.bad { border-color: #efc5c5; background: #fff5f5; }
    .result h3 { margin: 0 0 4px; font-size: 15px; display: flex; align-items: center; gap: 7px; }
    .result .sub { font-size: 12.5px; color: #4a515c; margin-bottom: 10px; }
    .filebox {
      display: flex;
      align-items: center;
      gap: 8px;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 8px 10px;
      margin-bottom: 8px;
    }
    .filebox .fb-main { min-width: 0; flex: 1; }
    .filebox .fb-name {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12.5px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .filebox .fb-dir { font-size: 11px; color: var(--muted); overflow-wrap: anywhere; margin-top: 2px; }
    .filebox button { padding: 6px 10px; font-size: 12px; white-space: nowrap; }
    .result ul { margin: 8px 0 0; padding-left: 18px; font-size: 12.5px; color: #444b56; line-height: 1.6; }

    /* ---- caminhos ---- */
    .custo { width: 100%; border-collapse: collapse; font-size: 12.5px; }
    .custo th, .custo td { padding: 6px 8px; border-bottom: 1px solid #eef1f6; text-align: right; }
    .custo th { font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); background: #f7f9fc; }
    .custo th:first-child, .custo td:first-child { text-align: left; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .custo tfoot td { font-weight: 800; border-bottom: 0; border-top: 1px solid var(--line); }
    .custo .semPreco { color: var(--amber); font-weight: 700; }
    .custo .brl { font-size: 11.5px; color: var(--muted); }
    .custo .ref { font-size: 10.5px; color: var(--amber); }
    .custo-nota { font-size: 11.5px; color: var(--muted); margin-top: 6px; line-height: 1.5; }
    .paths { display: grid; gap: 7px; }
    .pathline { display: flex; align-items: baseline; gap: 8px; font-size: 12px; }
    .pathline .pk { color: var(--muted); font-weight: 700; min-width: 74px; flex: none; }
    .pathline .pv {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 11.5px;
      overflow-wrap: anywhere;
      flex: 1;
    }
    .pathline .copy {
      padding: 2px 7px;
      font-size: 11px;
      font-weight: 700;
      background: #eef2f7;
      border-radius: 4px;
      flex: none;
    }
    .alert-line {
      margin-top: 10px;
      font-size: 12.5px;
      color: var(--red);
      background: #fff4f4;
      border: 1px solid #f2d2d2;
      border-radius: 6px;
      padding: 8px 10px;
      overflow-wrap: anywhere;
      display: none;
    }
    .alert-line.show { display: block; }

    /* ---- log e preview ---- */
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 0 16px 16px; }
    .sub-head { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; font-size: 12px; font-weight: 700; color: var(--muted); }
    .log {
      height: 400px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #101418;
      padding: 10px 12px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
      line-height: 1.5;
    }
    .log .ln { color: #cfd8e3; white-space: pre-wrap; overflow-wrap: anywhere; padding: 1px 0; }
    .log .ln .ts { color: #6d7a89; }
    .log .ln.WARN { color: #ffcf70; }
    .log .ln.ERROR { color: #ff9a9a; }
    .log .ln.good { color: #8ee0a8; }
    .preview {
      height: 400px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 12px;
    }
    .cue { border-bottom: 1px solid var(--line); padding: 9px 0; }
    .cue:last-child { border-bottom: 0; }
    .cue .meta { color: var(--muted); font-size: 11.5px; margin-bottom: 5px; }
    .cue .tag { font-weight: 700; }
    .cue .tag.ok { color: var(--green); }
    .cue .tag.pending { color: var(--amber); }
    .cue pre { margin: 4px 0 0; white-space: pre-wrap; font: 12.5px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow-wrap: anywhere; }
    .cue pre.src { color: var(--muted); }
    .compare-head {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 6px;
      font-size: 12px;
      font-weight: 700;
      color: var(--muted);
      flex-wrap: wrap;
    }
    .compare-head .mini, .compare-head .mini label {
      display: flex;
      align-items: center;
      gap: 5px;
      font-weight: 600;
      font-size: 12px;
      color: var(--muted);
      margin: 0;
    }
    .compare-head .mini input[type=checkbox] { width: auto; }
    .compare-head .mini input[type=search] { width: 190px; padding: 4px 8px; font-size: 12px; }
    .compare-head .mini select { width: auto; padding: 4px 8px; font-size: 12px; }
    .compare-head #cmpCount { font-weight: 700; color: var(--muted); }
    .compare-head .spacer { flex: 1; }
    .compare {
      height: 300px;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }
    .cmp-legend {
      position: sticky;
      top: 0;
      z-index: 1;
      display: grid;
      grid-template-columns: 86px 1fr 1fr;
      gap: 12px;
      padding: 8px 12px;
      background: #f4f7fb;
      border-bottom: 1px solid var(--line);
      font-size: 11px;
      font-weight: 800;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: .04em;
    }
    .cmp-row {
      display: grid;
      grid-template-columns: 86px 1fr 1fr;
      gap: 12px;
      padding: 9px 12px;
      border-bottom: 1px solid #eef1f6;
      font-size: 12.5px;
      line-height: 1.45;
    }
    .cmp-row:last-child { border-bottom: 0; }
    .cmp-row:nth-child(even) { background: #fcfdff; }
    .cmp-row.review { background: #fffaf0; }
    .cmp-id { color: var(--muted); font-size: 11px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .cmp-id .flag { display: block; color: var(--amber); font-weight: 800; margin-top: 3px; }
    .cmp-src, .cmp-pt { white-space: pre-wrap; overflow-wrap: anywhere; }
    .cmp-src { color: #6b7280; }
    .cmp-pt { color: var(--ink); }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 2px 9px;
      border-radius: 99px;
      font-size: 12px;
      font-weight: 700;
      border: 1px solid var(--line);
      background: #fff;
    }
    .pill.complete { color: var(--green); border-color: #b9dfc6; background: #f0fff5; }
    .pill.running { color: var(--blue); border-color: #bfd3ff; background: #f2f6ff; }
    .pill.failed, .pill.incomplete { color: var(--red); border-color: #efc5c5; background: #fff1f1; }
    .pill.stopped, .pill.stalled { color: var(--amber); border-color: #f0d99f; background: #fff8e6; }
    .jobs { display: grid; gap: 8px; max-height: 250px; overflow: auto; }
    .job { border: 1px solid var(--line); border-radius: 8px; padding: 10px; cursor: pointer; background: #fff; }
    .job:hover { border-color: #b9c6d8; }
    .job.active { border-color: var(--blue); box-shadow: 0 0 0 2px rgba(31, 111, 235, .12); }
    .job strong { display: block; font-size: 12.5px; overflow-wrap: anywhere; margin-bottom: 6px; }
    .small { font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
    .empty { font-size: 12.5px; color: var(--muted); padding: 6px 0; }
    @média (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      .status-grid { grid-template-columns: repeat(2, 1fr); }
      .split { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>SRT Bedrock Translator</h1>
    <span class="header-hint">Passe o mouse no <span class="info" style="cursor:default">i</span> de cada item para entender o que ele faz.</span>
  </header>
  <div id="toasts" aria-live="polite"></div>
  <div id="help" role="tooltip"></div>
  <div id="navOverlay" hidden>
    <div id="navBox" role="dialog" aria-label="Escolher legenda">
      <div class="nav-head">
        <strong>Escolher legenda</strong>
        <button id="navFechar" aria-label="Fechar">&times;</button>
      </div>
      <div class="nav-atalhos" id="navAtalhos"></div>
      <div class="nav-caminho" id="navCaminho"></div>
      <div class="nav-lista" id="navLista"></div>
      <div class="nav-rodape"><span id="navAviso"></span></div>
    </div>
  </div>
  <main>
    <section>
      <div class="panel-head">
        <h2>Entrada <button class="info" data-help="painelEntrada" aria-label="Ajuda">i</button></h2>
        <div class="action-wrap">
          <button id="refresh">Atualizar</button>
          <button class="info" data-help="refresh" aria-label="Ajuda sobre Atualizar">i</button>
        </div>
      </div>
      <div class="panel-body">
        <label class="field-label" for="file">Legenda encontrada <button class="info" data-help="file" aria-label="Ajuda">i</button></label>
        <select id="file"></select>

        <label class="field-label" for="path">Ou escolha outra legenda <button class="info" data-help="path" aria-label="Ajuda">i</button></label>
        <div class="path-row">
          <input id="path" placeholder="/caminho/arquivo.srt">
          <button id="procurar" type="button">Procurar...</button>
        </div>

        <div class="row">
          <div>
            <label class="field-label" for="profile">AWS profile <button class="info" data-help="profile" aria-label="Ajuda">i</button></label>
            <input id="profile" value="__DEFAULT_PROFILE__">
          </div>
          <div>
            <label class="field-label" for="region">Região <button class="info" data-help="region" aria-label="Ajuda">i</button></label>
            <input id="region" value="__DEFAULT_REGION__">
          </div>
        </div>

        <label class="field-label" for="models">Modelos em ordem de fallback <button class="info" data-help="models" aria-label="Ajuda">i</button></label>
        <textarea id="models"></textarea>

        <div class="row">
          <div>
            <label class="field-label" for="batchSize">Legendas por lote <button class="info" data-help="batchSize" aria-label="Ajuda">i</button></label>
            <input id="batchSize" type="number" min="8" max="120" value="28">
          </div>
          <div>
            <label class="field-label" for="batchChars">Caracteres por lote <button class="info" data-help="batchChars" aria-label="Ajuda">i</button></label>
            <input id="batchChars" type="number" min="1500" max="16000" value="4300">
          </div>
        </div>
        <div class="row">
          <div>
            <label class="field-label" for="attempts">Tentativas por modelo <button class="info" data-help="attempts" aria-label="Ajuda">i</button></label>
            <input id="attempts" type="number" min="1" max="12" value="3">
          </div>
          <div>
            <label class="field-label" for="timeout">Timeout por chamada <button class="info" data-help="timeout" aria-label="Ajuda">i</button></label>
            <input id="timeout" type="number" min="60" max="900" value="240">
          </div>
        </div>
        <div class="row">
          <div>
            <label class="field-label" for="maxLines">Máximo de linhas <button class="info" data-help="maxLines" aria-label="Ajuda">i</button></label>
            <input id="maxLines" type="number" min="1" max="4" value="2">
          </div>
          <div>
            <label class="field-label" for="lineLength">Caracteres por linha <button class="info" data-help="lineLength" aria-label="Ajuda">i</button></label>
            <input id="lineLength" type="number" min="24" max="60" value="42">
          </div>
        </div>
        <div class="row">
          <div>
            <label class="field-label" for="maxCps">CPS máximo <button class="info" data-help="maxCps" aria-label="Ajuda">i</button></label>
            <input id="maxCps" type="number" min="8" max="30" step="0.5" value="17">
          </div>
          <div>
            <label class="field-label" for="qcRounds">Rodadas de reparo QC <button class="info" data-help="qcRounds" aria-label="Ajuda">i</button></label>
            <input id="qcRounds" type="number" min="0" max="6" value="2">
          </div>
        </div>

        <label class="toggle-row"><input id="retryForever" type="checkbox" checked> <span>Retentar até concluir ou parar manualmente</span> <button class="info" data-help="retryForever" aria-label="Ajuda">i</button></label>
        <label class="toggle-row"><input id="retryQc" type="checkbox" checked> <span>Refazer automaticamente cues com erro duro de QC</span> <button class="info" data-help="retryQc" aria-label="Ajuda">i</button></label>
        <label class="toggle-row"><input id="contextPass" type="checkbox" checked> <span>Criar guia de contexto antes de traduzir</span> <button class="info" data-help="contextPass" aria-label="Ajuda">i</button></label>
        <label class="toggle-row"><input id="semanticReview" type="checkbox"> <span>Revisar o sentido com um segundo modelo</span> <button class="info" data-help="semanticReview" aria-label="Ajuda">i</button></label>
        <label class="toggle-row"><input id="polishPass" type="checkbox"> <span>Rodar passe final de revisão</span> <button class="info" data-help="polishPass" aria-label="Ajuda">i</button></label>
        <label class="toggle-row"><input id="forceNew" type="checkbox"> <span>Criar trabalho novo mesmo se já existir estado</span> <button class="info" data-help="forceNew" aria-label="Ajuda">i</button></label>

        <div class="actions">
          <div class="action-wrap">
            <button id="doctor">Testar Bedrock</button>
            <button class="info" data-help="doctor" aria-label="Ajuda">i</button>
          </div>
          <div class="action-wrap">
            <button class="primary" id="start">Iniciar ou retomar</button>
            <button class="info" data-help="start" aria-label="Ajuda">i</button>
          </div>
          <div class="action-wrap">
            <button id="resume">Retomar selecionado</button>
            <button class="info" data-help="resumeBtn" aria-label="Ajuda">i</button>
          </div>
          <div class="action-wrap">
            <button class="danger" id="stop">Parar</button>
            <button class="info" data-help="stop" aria-label="Ajuda">i</button>
          </div>
        </div>
      </div>
      <div class="panel-head">
        <h2>Trabalhos <button class="info" data-help="jobs" aria-label="Ajuda">i</button></h2>
      </div>
      <div class="panel-body">
        <div id="jobs" class="jobs"></div>
      </div>
    </section>

    <section>
      <div class="panel-head">
        <h2>Status <button class="info" data-help="status" aria-label="Ajuda">i</button></h2>
        <span id="statusPill" class="pill">sem trabalho</span>
      </div>
      <div class="status-grid">
        <div class="metric"><div class="value" id="mProgress">0%</div><div class="label">progresso <button class="info" data-help="mProgress" aria-label="Ajuda">i</button></div></div>
        <div class="metric"><div class="value" id="mBatch">-</div><div class="label">lote <button class="info" data-help="mBatch" aria-label="Ajuda">i</button></div></div>
        <div class="metric"><div class="value" id="mModel">-</div><div class="label">modelo atual <button class="info" data-help="mModel" aria-label="Ajuda">i</button></div></div>
        <div class="metric"><div class="value" id="mErrors">0</div><div class="label">erros QC <button class="info" data-help="mErrors" aria-label="Ajuda">i</button></div></div>
        <div class="metric"><div class="value" id="mReview">0</div><div class="label">revisar <button class="info" data-help="mReview" aria-label="Ajuda">i</button></div></div>
      </div>
      <div class="bar"><div id="barFill"></div></div>
      <div class="eta" id="eta"></div>

      <div class="result" id="result"></div>

      <div class="panel-body" id="custoWrap" hidden>
        <div class="sub-head">Consumo por modelo <button class="info" data-help="custo" aria-label="Ajuda">i</button></div>
        <div id="custo"></div>
      </div>
      <div class="panel-body">
        <div class="sub-head">Arquivos deste trabalho <button class="info" data-help="arquivos" aria-label="Ajuda">i</button></div>
        <div class="paths" id="paths"></div>
        <div class="alert-line" id="lastError"></div>
      </div>

      <div class="split">
        <div>
          <div class="sub-head">Log <button class="info" data-help="log" aria-label="Ajuda">i</button></div>
          <div id="log" class="log"></div>
        </div>
        <div>
          <div class="sub-head">Lote atual <button class="info" data-help="preview" aria-label="Ajuda">i</button></div>
          <div id="preview" class="preview"></div>
        </div>
      </div>
      <div class="panel-body" style="padding-top:0;">
        <div class="compare-head">
          <span>Comparar tradução <button class="info" data-help="comparar" aria-label="Ajuda">i</button></span>
          <span class="spacer"></span>
          <span class="mini"><select id="cmpScope"><option value="live">últimas 60 (ao vivo)</option><option value="all">filme inteiro</option></select><button class="info" data-help="cmpScope" aria-label="Ajuda">i</button></span>
          <span class="mini"><input id="cmpSearch" type="search" placeholder="buscar no texto..." autocomplete="off"><button class="info" data-help="cmpSearch" aria-label="Ajuda">i</button></span>
          <span class="mini" id="wrapFollow"><label><input type="checkbox" id="cmpFollow" checked> acompanhar</label><button class="info" data-help="cmpFollow" aria-label="Ajuda">i</button></span>
          <span class="mini" id="wrapReview"><label><input type="checkbox" id="cmpReview"> só revisar</label><button class="info" data-help="cmpReview" aria-label="Ajuda">i</button></span>
          <span class="mini" id="cmpCount"></span>
        </div>
        <div id="compare" class="compare"></div>
      </div>
    </section>
  </main>
  <script>
    const HELP = {
      painelEntrada: {
        t: "Painel de entrada",
        p: "É aqui que você escolhe a legenda e ajusta como ela vai ser traduzida. O uso normal é curto: confira a legenda no primeiro campo e clique no botão azul. Todo o resto já vem com valor bom.",
        e: "Os campos estão em ordem de importancia: primeiro QUAL legenda, depois QUAL conta da AWS, depois QUAIS modelos, é só no fim as regras de formatação da legenda.",
        d: "Não precisa mexer em nada para comecar."
      },
      refresh: {
        t: "Atualizar",
        p: "Faz a página olhar de novo para a pasta do filme e para a lista de trabalhos. É só uma releitura: nada é traduzido, iniciado ou apagado por causa deste botão.",
        e: "Você deixou esta página aberta e, no Finder, copiou a legenda de outro filme para a pasta. Ela não vai aparecer sozinha no campo de cima, porque a lista foi montada quando a página abriu. Clique em Atualizar e ela aparece.",
        d: "Só quando você mexeu nos arquivos da pasta por fora."
      },
      file: {
        t: "Legenda encontrada",
        p: "Lista os arquivos .srt que estão na pasta que o servidor está olhando. É daqui que você escolhe o que traduzir.",
        e: "Se a pasta do filme tem <code>Filme.srt</code> e <code>Subs/Filme.en.SDH.srt</code>, o primeiro da lista será o <code>Filme.srt</code>. A versão SDH fica embaixo de proposito: ela é feita para surdos e traz descrições de som como <code>[porta batendo]</code>, que nem todo mundo quer na legenda.",
        d: "Escolha aqui. Só use o campo de baixo se a legenda estiver em outra pasta."
      },
      path: {
        t: "Ou escolha outra legenda",
        p: "Para traduzir uma legenda que não está na pasta acima, clique em <b>Procurar</b> e navegue até ela. O que estiver aqui manda, e a escolha do campo de cima é ignorada.",
        e: "O botão abre um navegador de pastas com atalhos para Filmes, Downloads e sua pasta pessoal. Legendas que a própria ferramenta já gerou aparecem marcadas como <b>já traduzida</b>, para você não escolher a saída no lugar do original por engano.<br><br>Também dá para colar o caminho direto no campo. Atalho útil: no Finder, clique no arquivo e aperte Cmd+Option+C para copiar o caminho completo.<br><br>Legenda de outra pasta funciona normalmente: o trabalho aparece na lista, com log e botão de parar, e os arquivos são gravados ao lado dela.",
        d: "Use Procurar para trocar de filme sem reiniciar a UI."
      },
      profile: {
        t: "AWS profile",
        p: "Diz qual conta da AWS usar. É o apelido que o AWS CLI guardou no seu computador quando você fez login, e não seu e-mail nem sua senha.",
        e: "Para ver os apelidos que existem nesta máquina, rode no terminal <code>aws configure list-profiles</code>. O que está preenchido aqui é o que já funcionou.",
        d: "Já está certo. Só mude se for usar outra conta AWS."
      },
      region: {
        t: "Região",
        p: "Em qual data center da AWS o pedido vai cair. Isso importa porque a lista de modelos disponíveis muda de região para região.",
        e: "Em <code>us-east-1</code>, que fica na Virginia, os modelos Claude e Nova responderam nesta conta. Se você trocar para <code>sa-east-1</code>, que é São Paulo, é bem provável que esses modelos nem existam lá e todos falhem de uma vez.",
        d: "Deixe us-east-1."
      },
      models: {
        t: "Modelos em ordem de fallback",
        p: "A fila de modelos, um por linha. Ele sempre tenta o de cima primeiro; se aquele não der conta, desce para o próximo. É uma escada de reserva.",
        e: "Pense num revezamento: o Claude Sonnet é o titular, porque traduz melhor. Se ele engasgar num trecho, o Haiku entra. Se o Haiku também engasgar, a Nova entra. Ter mais de um modelo aqui tem um segundo efeito importante: quando dois modelos diferentes devolvem exatamente o mesmo texto, o sistema entende que quem está errado é a suspeita dele, e aceita a tradução em vez de ficar insistindo para sempre.",
        d: "Não precisa mexer. Tirar modelos daqui só enfraquece a rede de segurança."
      },
      batchSize: {
        t: "Legendas por lote",
        p: "A legenda não vai inteira de uma vez: ela é traduzida em blocos. Este número diz quantas falas vão em cada bloco. Bloco maior faz o modelo enxergar mais da cena de uma vez, o que ajuda no contexto, mas deixa a resposta longa e mais sujeita a vir cortada pela metade.",
        e: "Com 28, o filme que você acabou de traduzir (2435 falas) virou 87 blocos, cada um cobrindo cerca de um minuto e meio de filme. Se o log encher de linha amarela em varios blocos seguidos, baixar este número para 20 costuma resolver.",
        d: "Não precisa mexer."
      },
      batchChars: {
        t: "Caracteres por lote",
        p: "Um segundo limite para o bloco, agora contando letras em vez de falas. Vale o que estourar primeiro. Serve para uma cena de falas muito longas não gerar um bloco gigante.",
        e: "Numa cena de conversa rápida, o bloco fecha com as 28 falas normais. Numa cena em que um personagem da um discurso, ele pode fechar com 18 falas, porque já bateu os 4300 caracteres antes de chegar em 28.",
        d: "Não precisa mexer."
      },
      attempts: {
        t: "Tentativas por modelo",
        p: "Quantas vezes insistir com o mesmo modelo antes de chamar o próximo da fila. Não é repetição burra: cada nova tentativa vai com o motivo da recusa anterior escrito dentro do pedido.",
        e: "Com 3: o Sonnet tenta, a resposta e recusada, e na segunda tentativa ele recebe junto um recado do tipo <i>sua resposta anterior foi recusada porque as falas 12 e 13 ficaram sem traduzir</i>. Se as 3 tentativas falharem, a vez passa para o Haiku.",
        d: "Não precisa mexer."
      },
      timeout: {
        t: "Timeout por chamada",
        p: "Quanto tempo esperar por uma resposta antes de considerar que ela se perdeu no caminho. Bloco grande e modelo lento demoram mais.",
        e: "240 segundos, ou seja 4 minutos, é bem folgado: na prática um bloco de 28 falas volta em 10 a 15 segundos. Se você baixar para 60, um bloco mais pesado pode ser cortado no meio e refeito a toa.",
        d: "Não precisa mexer."
      },
      maxLines: {
        t: "Máximo de linhas",
        p: "Quantas linhas uma legenda pode ocupar na tela. Duas é o padrão do cinema e da TV: com três ou mais, a legenda cobre a imagem e o olho se perde.",
        e: "Uma fala longa fica assim, quebrada em duas:<span class='sample'>Você sempre diz isso<br>quando algo está errado.</span>Com 1 linha, essa mesma fala viraria uma tira única atravessando a tela inteira.",
        d: "Deixe 2."
      },
      lineLength: {
        t: "Caracteres por linha",
        p: "O tamanho máximo de cada linha, contando só o que aparece na tela. 42 é o número que os estúdios usam em português: é mais ou menos o que o olho lê de uma vez, sem precisar varrer a tela.",
        e: "A fala <code>Você sempre diz isso quando algo está errado.</code> tem 44 caracteres e estoura o limite, então ela é quebrada num ponto natural da frase:<span class='sample'>Você sempre diz isso<br>quando algo está errado.</span>",
        d: "Deixe 42."
      },
      maxCps: {
        t: "CPS máximo",
        p: "Velocidade de leitura: quantas letras aparecem na tela por segundo. Acima de mais ou menos 17, a legenda some antes de você terminar de ler. Mas aqui este número não é cobrado no seco: ele é sempre comparado com a legenda original.",
        e: "Por que comparado? Porque nesta legenda em inglês, 48 por cento das falas JÁ passavam de 17. Cobrando o número puro, metade do filme viraria aviso e o problema de verdade sumiria no meio do barulho. Então a regra é: original a 25 e tradução a 26 não gera aviso, porque o filme já era corrido ali. Original a 15 e tradução a 30 gera aviso, porque quem deixou pesado foi a tradução.",
        d: "Deixe 17."
      },
      qcRounds: {
        t: "Rodadas de reparo QC",
        p: "Terminada a tradução, ele passa um pente fino em tudo. Se achar erro grave numa fala, refaz o bloco inteiro daquela fala. Este número e quantas vezes ele pode tentar consertar antes de desistir.",
        e: "Uma fala voltou com o itálico aberto e não fechado, tipo <code>&lt;i&gt;Ola.</code> sem o <code>&lt;/i&gt;</code>. Isso bagunca a exibição, então ele refaz o bloco. Se depois de 2 rodadas continuar quebrado, ele para de gastar chamada e entrega o arquivo com INCOMPLETO no nome, avisando você.",
        d: "Deixe 2."
      },
      retryForever: {
        t: "Retentar até concluir ou parar manualmente",
        p: "Ligado, ele nunca desiste sozinho de um bloco: espera um pouco, tenta de novo, espera mais um pouco, tenta de novo, até dar certo ou até você clicar em Parar. Desligado, um bloco que falha é abandonado e a tradução segue sem ele.",
        e: "É a diferença entre voltar e achar o filme inteiro traduzido, ou voltar e achar um buraco de 30 falas bem no meio da cena mais importante. Caso tipico: a AWS comeca a limitar suas chamadas porque você usou muito. Ligado, ele espera a limitação passar e continua sozinho.",
        d: "Deixe ligado."
      },
      retryQc: {
        t: "Refazer cues com erro duro de QC",
        p: "No fim de tudo, refaz automaticamente os blocos que tiverem alguma fala reprovada no pente fino.",
        e: "Desligue apenas se você quiser ver o que o modelo devolveu cru, sem nenhuma correção depois. Isso serve para comparar modelos entre si, não para traduzir um filme de verdade.",
        d: "Deixe ligado."
      },
      contextPass: {
        t: "Criar guia de contexto antes de traduzir",
        p: "Antes de comecar, ele le amostras do filme inteiro e escreve para si mesmo um resumo: quem são os personagens, que tom o filme tem, como as pessoas se tratam. Esse resumo vai junto em todos os blocos.",
        e: "É o que impede o problema classico de tradução em pedacos: o personagem tratar alguém por você no começo do filme e por tu no fim, ou um apelido virar uma coisa no bloco 3 e outra coisa no bloco 40. Custa uma chamada a mais, no começo, uma vez só.",
        d: "Deixe ligado."
      },
      semanticReview: {
        t: "Revisar o sentido com um segundo modelo",
        p: "Depois de traduzir, um segundo modelo relê os pares original/tradução procurando erro de significado — o tipo de erro escrito em português impecável, que nenhuma outra checagem enxerga. Vem desligado, e a razão está no exemplo abaixo.",
        e: "<b>Medido num filme real, em 400 falas:</b> o juiz apontou 17, sobraram 8 depois dos filtros, e 3 foram confirmadas por um segundo modelo. Dessas 3, uma era melhoria de verdade, uma era discutível e <b>uma pioraria a legenda</b>: ele quis trocar <i>Discorde à vontade</i> por <i>Implore à vontade</i>, sem perceber que a fala anterior tinha sido traduzida como <i>Discordo respeitosamente</i> e que o trocadilho se perderia.<br><br>Custa cerca de 40% a mais no filme inteiro. Ligue quando a legenda for para valer e você mesmo for conferir a lista; as falas apontadas aparecem no contador <b>revisar</b>. Ela só relata: para deixá-la reescrever sozinha é preciso <code>--semantic-autofix</code>, e aí o risco daquela terceira correção é seu.",
        d: "Deixe desligado no uso normal. O ganho medido não paga os 40%."
      },
      polishPass: {
        t: "Rodar passe final de revisão",
        p: "Uma segunda passada por tudo. Na primeira ele traduz; nesta, ele rele o que já traduziu e melhora. Praticamente dobra o tempo e o custo.",
        e: "É o passe que pega deslize de sentido. Nesta legenda, por exemplo, <code>chat room</code> saiu como <i>grupo de e-mail</i>. Esse tipo de erro passa batido na validação automática, porque está em português correto e bem escrito; só uma releitura pega.",
        d: "Ligue quando a legenda for pra valer e o tempo não importar."
      },
      forceNew: {
        t: "Criar trabalho novo mesmo se já existir estado",
        p: "Joga fora todo o progresso salvo daquela legenda e comeca do zero.",
        e: "Cuidado com este: se você já traduziu 80 por cento e marcar aqui, esses 80 por cento são refeitos e cobrados de novo. Só faz sentido quando você mudou algo grande, como trocar a lista de modelos, e quer o filme inteiro no critério novo.",
        d: "Deixe desmarcado."
      },
      doctor: {
        t: "Testar Bedrock",
        p: "Um teste rápido de porta, antes de comecar pra valer. Ele manda um pedido mínimo, literalmente pedindo a palavra OK, para cada modelo da lista, usando a conta e a região preenchidas aqui.",
        e: "Serve para você não descobrir depois de 20 minutos que a credencial estava errada. Ele responde três perguntas: a conta funciona? a região responde? esta conta tem permissão neste modelo? Se algum voltar <code>AccessDeniedException</code>, e questão de permissão: entre no console da AWS, va em Amazon Bedrock, Model access, e libere o modelo. O que ele NÃO faz e avaliar qualidade de tradução: ele só testa se a porta abre.",
        d: "Vale clicar antes do primeiro filme do dia."
      },
      start: {
        t: "Iniciar ou retomar",
        p: "O botão principal, e ele decide sozinho entre comecar e continuar: se já existe trabalho para aquela legenda, retoma exatamente de onde parou; se não existe, cria um novo.",
        e: "Você traduziu 60 por cento ontem e fechou o notebook. Hoje escolhe a mesma legenda e clica aqui: ele reconhece os blocos que já estavam prontos e comeca do seguinte, sem retraduzir nem cobrar de novo pelo que já estava feito. Clicar duas vezes sem querer também não duplica nada.",
        d: "É o botão que você vai usar em 9 de cada 10 vezes."
      },
      resumeBtn: {
        t: "Retomar selecionado",
        p: "Continua o trabalho que estiver marcado na lista Trabalhos, aqui embaixo. A diferença para o botão azul e que este ignora o campo de legenda la em cima e vai pelo cartão que você escolheu na lista.",
        e: "Serve principalmente quando a lista mostra um trabalho com status <code>stalled</code>, que quer dizer: o progresso está salvo, mas o programa que estava traduzindo morreu. Clique no cartão dele e depois neste botão.",
        d: "Use quando quiser continuar um trabalho específico da lista."
      },
      stop: {
        t: "Parar",
        p: "Pede para parar. Não é um corte seco: ele deixa a chamada que já está no ar terminar, grava tudo em disco é só então para.",
        e: "Por isso pode levar uns 10 segundos até o status virar <code>stopped</code>, e isso é normal. Nada do que já foi traduzido se perde: depois é só clicar em Iniciar ou retomar e ele volta do mesmo ponto.",
        d: "Pode usar sem medo."
      },
      jobs: {
        t: "Trabalhos",
        p: "Todo trabalho que já foi iniciado, com o quanto cada um andou. Clique num cartão para ver o status, o log e as falas dele no painel da direita.",
        e: "Os status querem dizer: <code>running</code> traduzindo agora &middot; <code>stopped</code> você mandou parar &middot; <code>stalled</code> o progresso está salvo mas ninguém está traduzindo, porque o programa caiu &middot; <code>complete</code> terminou limpo &middot; <code>incomplete</code> terminou faltando coisa &middot; <code>failed</code> deu erro.",
        d: "Clique num cartão para acompanhar aquele trabalho."
      },
      status: {
        t: "Status",
        p: "O resumo do trabalho selecionado. Ele se atualiza sozinho a cada 2,5 segundos, então você não precisa recarregar a página nem ficar clicando em nada.",
        e: "Duas situações merecem sua atenção. <code>stalled</code> significa que ninguém está traduzindo e você precisa clicar em Retomar selecionado. E <code>lote insistindo</code> significa que um bloco já passou por todos os modelos uma vez e voltou ao começo da fila: vale abrir o log e ver do que ele está reclamando.",
        d: "Só olhe. Não há nada para configurar aqui."
      },
      mProgress: {
        t: "Progresso",
        p: "Quanto do filme já tem tradução aprovada. Conta falas, e não blocos: uma fala só entra nesta conta depois de passar por toda a validação.",
        e: "1736 falas prontas de um total de 2435 aparecem aqui como 71 por cento.",
        d: ""
      },
      mBatch: {
        t: "Lote",
        p: "Qual bloco está sendo traduzido agora e quantos blocos o filme tem no total.",
        e: "<code>63/87</code> quer dizer que ele está no bloco 63 de 87. Quando não há nada rodando, aparece só o total, tipo <code>-/87</code>.",
        d: ""
      },
      mModel: {
        t: "Modelo atual",
        p: "Qual modelo está atendendo neste momento.",
        e: "Se aparecer <code>nova-pro</code> aqui quando o primeiro da sua lista é o <code>claude-sonnet-4-6</code>, e sinal de que o Sonnet falhou naquele bloco e a reserva entrou. Acontecer de vez em quando é normal; acontecer o tempo todo e motivo para olhar o log.",
        d: ""
      },
      mErrors: {
        t: "Erros QC",
        p: "Falas reprovadas por erro grave: texto vazio, o modelo se recusando a traduzir, itálico quebrado, ou o simbolo de música que sumiu. Enquanto este número não for zero, o arquivo final sai com INCOMPLETO no nome.",
        e: "Este contador não inclui os avisos leves, como linha comprida ou leitura rápida demais. Aviso não impede o arquivo de sair como OK; erro grave impede.",
        d: ""
      },
      mReview: {
        t: "Revisar",
        p: "Falas que o sistema achou suspeitas de não terem sido traduzidas, mas que dois modelos diferentes devolveram exatamente iguais. Quando isso acontece, quem provavelmente está errado é a suspeita, e não os modelos.",
        e: "Refrão de música como <code>Guli guli guli guli ram sam sam</code> é para ficar igual mesmo: não existe tradução disso. A fala entra nesta conta para você dar uma conferida depois, mas não impede o arquivo de sair como OK.",
        d: ""
      },
      custo: {
        t: "Consumo por modelo",
        p: "Quantas chamadas cada modelo atendeu, quantos tokens gastou e quanto isso custa em dólar. A entrada aparece separada do que veio do cache, que é cobrado por volta de um décimo.",
        e: "O custo é <b>estimativa</b>, e cada linha diz de onde veio o preço. Parte vem da API de preços da AWS, buscada pelo comando <code>refresh-prices</code>. A AWS não publica os Claude 4.x, então para eles usamos a <a href='https://platform.claude.com/docs/en/about-claude/pricing'>tabela oficial da Anthropic</a> — essas linhas aparecem marcadas como <b>referência</b>, porque a Anthropic avisa que a AWS opera o Bedrock e pode cobrar diferente, e que endpoint regional (o prefixo <code>us.</code>) costuma ter 10% de acréscimo.<br><br>Os valores em real usam a cotação PTAX de venda do Banco Central, buscada junto com os preços. Para fixar preço ou câmbio, escreva em <code>srt_translator.local.json</code>:<br><code>{\"usd_brl\": 5.20, \"prices\": {\"us.anthropic.claude-sonnet-4-6\": {\"input\": 0.003, \"output\": 0.015}}}</code><br><br>Preços por mil tokens. Se faltar o preço de algum modelo, o total aparece com um sinal de mais para avisar que está incompleto.",
        d: "Só olhe. Para o total ficar completo, informe o preço dos modelos que faltam."
      },
      arquivos: {
        t: "Arquivos gerados",
        p: "Tudo é gravado na mesma pasta onde está a legenda original. Nada vai para lugar escondido do sistema. São três coisas:",
        e: "<b>1. A legenda traduzida.</b> O final do nome conta o que aconteceu: <code>.pt-BR.EM_ANDAMENTO.srt</code> enquanto trabalha, <code>.pt-BR.OK.srt</code> quando terminou limpo, <code>.pt-BR.INCOMPLETO.srt</code> quando faltou algo. É um .srt comum: da para arrastar direto no VLC. Quando termina, ele apaga sozinho as versões antigas, para não sobrar três arquivos parecidos te confundindo.<br><br><b>2. Um arquivo <code>.translator-state.json</code></b> colado na legenda traduzida. É a etiqueta dela. É por causa desse arquivo que você pode arrastar uma legenda INCOMPLETO de volta aqui é o sistema reconhecer de qual trabalho ela veio, em vez de comecar do zero.<br><br><b>3. Uma pasta <code>.srt_translator_jobs</code></b>, que fica escondida por comecar com ponto. Dentro dela ficam o progresso salvo, o log completo e o <code>quality_report.json</code>, que lista fala por fala tudo que foi apontado.",
        d: "Para assistir o filme, você só precisa do arquivo .OK.srt."
      },
      log: {
        t: "Log",
        p: "O que está acontecendo, em ordem e com hora. Também fica salvo em disco, então você não perde nada ao fechar a página.",
        e: "Um ciclo saudável se repete assim: <i>Iniciando tradução do lote N</i>, <i>Chamando Bedrock</i>, <i>Resposta validada</i>, <i>Lote N concluído</i>. As cores ajudam: cinza e rotina, verde e coisa concluída, amarelo e aviso (ele vai tentar de novo sozinho) e vermelho e erro. Amarelo repetido no mesmo bloco quer dizer que a validação está recusando as respostas; o motivo vem escrito no fim da linha.",
        d: "Só olhe quando algo parecer travado."
      },
      cmpScope: {
        t: "Escopo da lista",
        p: "Escolhe se você está acompanhando o trabalho acontecer ou revisando o que já ficou pronto.",
        e: "<b>Últimas 60 (ao vivo)</b> mostra só o trecho mais recente e vai se atualizando conforme cada bloco fecha: é o modo de assistir a tradução sair.<br><br><b>Filme inteiro</b> carrega todas as falas já traduzidas de uma vez, para você navegar e revisar do começo ao fim. Num filme de 1770 falas isso é bem mais pesado, então ele só e buscado quando você pede, e não a cada atualização da tela.",
        d: "Rodando, use ao vivo. Terminado, use filme inteiro."
      },
      cmpSearch: {
        t: "Buscar no texto",
        p: "Filtra a lista por um trecho de texto, procurando ao mesmo tempo no original em inglês e na tradução em português.",
        e: "Digite o nome de um personagem, por exemplo <code>Aaron</code>, e veja de uma vez todas as falas em que ele aparece: da para conferir se o nome é o tratamento ficaram consistentes no filme inteiro. Também serve para uma expressão específica que você quer saber como foi resolvida.",
        d: "Combine com o escopo filme inteiro para revisar de verdade."
      },
      cmpFollow: {
        t: "Acompanhar",
        p: "Mantem a lista grudada na fala traduzida mais recente, rolando sozinha conforme o trabalho avanca. Só aparece enquanto existe tradução em andamento: com o trabalho terminado, não há nada novo chegando para acompanhar.",
        e: "Se você rolar para cima para ler alguma coisa, ele se desmarca sozinho para não arrastar a tela debaixo do seu olho. Quando você volta ao fim da lista, ele se remarca. Pedir <b>filme inteiro</b> também desliga, porque quem pediu tudo quer navegar.",
        d: "Deixe ligado enquanto assiste a tradução acontecer."
      },
      cmpReview: {
        t: "Só revisar",
        p: "Mostra apenas as falas que ficaram marcadas como <b>revisar</b>, escondendo todo o resto. Só aparece quando existe pelo menos uma; o número ao lado diz quantas são.",
        e: "São as falas em que a validação achou que o texto não tinha sido traduzido, mas dois modelos diferentes devolveram exatamente igual ao original. Quase sempre e refrão de música ou onomatopeia, que devem mesmo ficar iguais. Como a dúvida existe, elas ficam separadas para você bater o olho.<br><br>Se o contador não aparece, nenhuma fala precisou disso e não há o que revisar.",
        d: "Marque para conferir só essas, no fim do trabalho."
      },
      comparar: {
        t: "Comparar tradução",
        p: "As falas já traduzidas em três colunas: número e tempo na estreita da esquerda, o texto original no meio e o português à direita. É aqui que você julga se a tradução está boa, sem precisar abrir o arquivo.",
        e: "<b>Últimas 60 (ao vivo)</b> acompanha o trabalho acontecendo: a lista se atualiza a cada lote que fecha e gruda na fala mais recente. <b>Filme inteiro</b> carrega todas as falas já traduzidas para você navegar e revisar do começo ao fim.<br><br>A busca filtra pelo texto nos dois idiomas, então da para procurar um nome ou uma expressão e ver como ela ficou em todas as vezes que aparece. <b>Só revisar</b> mostra apenas as falas que dois modelos devolveram iguais ao original, que são as únicas que pedem olho humano.<br><br>Se você rolar para cima para ler algo, o acompanhamento automático desliga sozinho e volta quando você chegar no fim de novo.",
        d: "Só olhe. É o melhor lugar para julgar se a tradução está boa."
      },
      preview: {
        t: "Lote atual",
        p: "As falas do bloco que está sendo traduzido agora. Em cinza o texto original e, embaixo, a tradução assim que ela chega.",
        e: "É onde você ve a qualidade saindo em tempo real, sem precisar abrir o arquivo no meio do caminho para espiar.",
        d: ""
      }
    };

    const defaultModels = __DEFAULT_MODELS__;
    let selectedJob = null;
    let lastStatus = {};
    let lastDone = null;
    let rateSamples = [];
    document.querySelector("#models").value = defaultModels.join("\n");

    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }

    /* ---------- popover de ajuda ---------- */
    const helpBox = document.querySelector("#help");
    let pinned = null;
    let hideTimer = null;

    function renderHelp(key) {
      const h = HELP[key];
      if (!h) return false;
      helpBox.innerHTML = `<h4>${escapeHtml(h.t)}</h4><p>${h.p}</p>` +
        (h.e ? `<div class="ex"><b>Na prática</b>${h.e}</div>` : "") +
        (h.d ? `<div class="tip"><b>Precisa mexer?</b> ${h.d}</div>` : "") +
        `<p class="pin-hint">Clique no i para fixar. Esc fecha.</p>`;
      return true;
    }
    function placeHelp(anchor) {
      helpBox.classList.add("show");
      const a = anchor.getBoundingClientRect();
      const b = helpBox.getBoundingClientRect();
      let left = a.left + a.width / 2 - b.width / 2;
      left = Math.max(10, Math.min(left, window.innerWidth - b.width - 10));
      let top = a.bottom + 8;
      if (top + b.height > window.innerHeight - 10) {
        top = a.top - b.height - 8;
        if (top < 10) top = Math.max(10, window.innerHeight - b.height - 10);
      }
      helpBox.style.left = left + "px";
      helpBox.style.top = top + "px";
    }
    function openHelp(anchor, key) {
      if (!renderHelp(key)) return;
      placeHelp(anchor);
    }
    function closeHelp() {
      helpBox.classList.remove("show");
      if (pinned) { pinned.classList.remove("open"); pinned = null; }
    }
    document.addEventListener("mouseover", ev => {
      const btn = ev.target.closest(".info[data-help]");
      if (!btn || pinned) return;
      clearTimeout(hideTimer);
      openHelp(btn, btn.dataset.help);
    });
    document.addEventListener("mouseout", ev => {
      const btn = ev.target.closest(".info[data-help]");
      if (!btn || pinned) return;
      hideTimer = setTimeout(() => { if (!pinned) helpBox.classList.remove("show"); }, 160);
    });
    helpBox.addEventListener("mouseenter", () => clearTimeout(hideTimer));
    helpBox.addEventListener("mouseleave", () => { if (!pinned) helpBox.classList.remove("show"); });
    document.addEventListener("click", ev => {
      const btn = ev.target.closest(".info[data-help]");
      if (btn) {
        ev.preventDefault();
        ev.stopPropagation();
        if (pinned === btn) { closeHelp(); return; }
        if (pinned) pinned.classList.remove("open");
        pinned = btn;
        btn.classList.add("open");
        openHelp(btn, btn.dataset.help);
        return;
      }
      if (!ev.target.closest("#help")) closeHelp();
    });
    document.addEventListener("keydown", ev => { if (ev.key === "Escape") closeHelp(); });
    window.addEventListener("resize", closeHelp);

    /* ---------- toasts ---------- */
    function toast(title, body, kind, opts) {
      opts = opts || {};
      const box = document.querySelector("#toasts");
      const el = document.createElement("div");
      el.className = "toast " + (kind || "info");
      el.innerHTML = `<div class="t-head"><strong>${escapeHtml(title)}</strong>
          <button class="t-close" aria-label="Fechar">&times;</button></div>
        ${body ? `<div class="t-body">${escapeHtml(body)}</div>` : ""}
        ${opts.copy ? `<div class="t-actions"><button data-copy="1">Copiar caminho</button></div>` : ""}`;
      el.querySelector(".t-close").onclick = () => el.remove();
      const cp = el.querySelector("[data-copy]");
      if (cp) cp.onclick = () => copyText(opts.copy, cp);
      box.appendChild(el);
      const ms = opts.timeout === 0 ? 0 : (opts.timeout || (kind === "error" ? 14000 : 7000));
      if (ms) setTimeout(() => el.remove(), ms);
      return el;
    }
    async function copyText(text, btn) {
      try {
        await navigator.clipboard.writeText(text);
        if (btn) { const old = btn.textContent; btn.textContent = "Copiado!"; setTimeout(() => btn.textContent = old, 1600); }
      } catch (e) {
        toast("Não consegui copiar", "O navegador bloqueou o acesso a área de transferência. Selecione o texto manualmente.", "warn");
      }
    }
    function busy(sel, on) {
      const b = document.querySelector(sel);
      if (!b) return;
      b.classList.toggle("busy", !!on);
      b.disabled = !!on;
    }

    /* ---------- seletor de legenda ---------- */
    const navOverlay = document.querySelector("#navOverlay");
    let navPastaAtual = null;

    function tamanhoLegivel(bytes) {
      if (!bytes) return "";
      return bytes > 1024 * 1024
        ? (bytes / 1024 / 1024).toFixed(1) + " MB"
        : Math.max(1, Math.round(bytes / 1024)) + " KB";
    }
    async function navegar(caminho) {
      const dados = await api("/api/browse" + (caminho ? "?path=" + encodeURIComponent(caminho) : ""));
      navPastaAtual = dados.path;
      document.querySelector("#navCaminho").textContent = dados.path;
      document.querySelector("#navAtalhos").innerHTML = (dados.shortcuts || [])
        .map(a => `<button data-ir="${escapeHtml(a.path)}">${escapeHtml(a.label)}</button>`).join("");
      const partes = [];
      if (dados.parent) {
        partes.push(`<button class="nav-item" data-ir="${escapeHtml(dados.parent)}">
          <span class="ic">&#8617;</span><span class="nome">Subir um nível</span></button>`);
      }
      for (const d of dados.dirs) {
        partes.push(`<button class="nav-item" data-ir="${escapeHtml(d.path)}">
          <span class="ic">&#128193;</span><span class="nome">${escapeHtml(d.name)}</span></button>`);
      }
      for (const f of dados.files) {
        partes.push(`<button class="nav-item srt" data-escolher="${escapeHtml(f.path)}">
          <span class="ic">&#127916;</span><span class="nome">${escapeHtml(f.name)}</span>
          ${f.traduzida ? "<span class='tag'>já traduzida</span>" : ""}
          <span class="tam">${tamanhoLegivel(f.size)}</span></button>`);
      }
      document.querySelector("#navLista").innerHTML =
        partes.join("") || "<div class='empty' style='padding:16px'>Nada aqui. Suba um nível ou use um atalho.</div>";
      document.querySelector("#navAviso").textContent =
        dados.error || `${dados.dirs.length} pasta(s), ${dados.files.length} legenda(s) nesta pasta.`;
    }
    function abrirNavegador() {
      navOverlay.hidden = false;
      const partida = document.querySelector("#path").value.trim() || document.querySelector("#file").value || "";
      navegar(partida || null).catch(e => toast("Não consegui abrir a pasta", e.message, "error"));
    }
    function fecharNavegador() { navOverlay.hidden = true; }
    document.querySelector("#procurar").onclick = abrirNavegador;
    document.querySelector("#navFechar").onclick = fecharNavegador;
    navOverlay.addEventListener("click", ev => { if (ev.target === navOverlay) fecharNavegador(); });
    document.addEventListener("keydown", ev => { if (ev.key === "Escape" && !navOverlay.hidden) fecharNavegador(); });
    document.querySelector("#navBox").addEventListener("click", ev => {
      const ir = ev.target.closest("[data-ir]");
      if (ir) { navegar(ir.dataset.ir).catch(e => toast("Não consegui abrir a pasta", e.message, "error")); return; }
      const escolher = ev.target.closest("[data-escolher]");
      if (escolher) {
        const caminho = escolher.dataset.escolher;
        document.querySelector("#path").value = caminho;
        fecharNavegador();
        toast("Legenda selecionada", caminho.split("/").pop() + " — clique em Iniciar ou retomar para traduzir.", "success");
      }
    });

    /* ---------- api ---------- */
    async function api(path, opts) {
      let res, data;
      try {
        res = await fetch(path, opts);
      } catch (e) {
        throw new Error("Não consegui falar com o servidor local. Ele ainda está rodando no terminal?");
      }
      try { data = await res.json(); } catch (e) { throw new Error("Resposta inválida do servidor (HTTP " + res.status + ")."); }
      if (!res.ok) throw new Error(data.error || ("Erro HTTP " + res.status));
      return data;
    }
    function formConfig() {
      const manual = document.querySelector("#path").value.trim();
      const selected = document.querySelector("#file").value;
      return {
        path: manual || selected,
        profile: document.querySelector("#profile").value.trim(),
        region: document.querySelector("#region").value.trim(),
        models: document.querySelector("#models").value,
        batch_size: Number(document.querySelector("#batchSize").value),
        max_batch_chars: Number(document.querySelector("#batchChars").value),
        attempts_per_model: Number(document.querySelector("#attempts").value),
        call_timeout: Number(document.querySelector("#timeout").value),
        max_lines: Number(document.querySelector("#maxLines").value),
        max_line_length: Number(document.querySelector("#lineLength").value),
        max_cps: Number(document.querySelector("#maxCps").value),
        qc_repair_rounds: Number(document.querySelector("#qcRounds").value),
        retry_forever: document.querySelector("#retryForever").checked,
        retry_qc_issues: document.querySelector("#retryQc").checked,
        context_pass: document.querySelector("#contextPass").checked,
        semantic_review: document.querySelector("#semanticReview").checked,
        polish_pass: document.querySelector("#polishPass").checked,
        force_new: document.querySelector("#forceNew").checked
      };
    }

    /* ---------- ações ---------- */
    async function refreshFiles() {
      const data = await api("/api/files");
      const select = document.querySelector("#file");
      const keep = select.value;
      select.innerHTML = "";
      for (const f of data.files) {
        const opt = document.createElement("option");
        opt.value = f.path;
        opt.textContent = f.name;
        select.appendChild(opt);
      }
      if (keep) select.value = keep;
      return data.files.length;
    }
    async function refreshJobs() {
      const data = await api("/api/jobs");
      const box = document.querySelector("#jobs");
      box.innerHTML = "";
      if (!data.jobs.length) {
        box.innerHTML = "<div class='empty'>Nenhum trabalho ainda. Escolha uma legenda e clique em Iniciar ou retomar.</div>";
      }
      for (const job of data.jobs) {
        const div = document.createElement("div");
        div.className = "job" + (job.job_id === selectedJob ? " active" : "");
        div.onclick = () => { selectedJob = job.job_id; refreshJob(); refreshJobs(); };
        const pct = job.total_cues ? Math.round((job.done_cues || 0) * 100 / job.total_cues) : 0;
        div.innerHTML = `<strong>${escapeHtml(shortPath(job.source_path || ""))}</strong>
          <span class="pill ${escapeHtml(job.status || "")}">${escapeHtml(job.status || "-")}</span>
          <div class="small">${pct}% &middot; ${job.done_cues || 0}/${job.total_cues || 0} legendas &middot; ${escapeHtml(job.updated_at || "")}</div>`;
        box.appendChild(div);
        announceStatus(job);
      }
      if (!selectedJob && data.jobs[0]) selectedJob = data.jobs[0].job_id;
      syncButtons(data.jobs);
    }
    function syncButtons(jobs) {
      const cur = (jobs || []).find(j => j.job_id === selectedJob);
      const running = cur && cur.status === "running";
      document.querySelector("#stop").disabled = !running;
      document.querySelector("#resume").disabled = !selectedJob || running;
    }
    function announceStatus(job) {
      const prev = lastStatus[job.job_id];
      lastStatus[job.job_id] = job.status;
      if (prev === undefined || prev === job.status) return;
      const name = shortPath(job.source_path || "");
      if (job.status === "complete") {
        toast("Tradução concluída", `${name} — arquivo pronto em ${fileName(job.output)}`, "success", {copy: job.output, timeout: 0});
      } else if (job.status === "incomplete") {
        toast("Terminou com pendencias", `${name} — saiu como ${fileName(job.output)}. Repasse esse arquivo e clique em Iniciar ou retomar para ele completar o que faltou.`, "warn", {timeout: 0});
      } else if (job.status === "failed") {
        toast("O trabalho falhou", job.last_error || name, "error", {timeout: 0});
      } else if (job.status === "stalled") {
        toast("Processo parou sozinho", `${name} — o progresso está salvo. Clique em Retomar selecionado para continuar.`, "warn", {timeout: 0});
      }
    }
    async function startJob() {
      const cfg = formConfig();
      if (!cfg.path) { toast("Escolha uma legenda", "Nenhum arquivo .srt selecionado nem caminho informado.", "warn"); return; }
      busy("#start", true);
      try {
        const data = await api("/api/start", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(cfg)});
        selectedJob = data.job_id;
        lastStatus[data.job_id] = "running";
        toast("Trabalho iniciado", `${fileName(cfg.path)} — acompanhe pelo log abaixo. Pode fechar esta aba: o servidor continua traduzindo.`, "success");
      } finally { busy("#start", false); }
      // refresh separado: se ele falhar, o trabalho JÁ comecou e dizer
      // "não consegui iniciar" seria mentira.
      try { await refreshJobs(); await refreshJob(); }
      catch (e) { toast("Trabalho rodando, mas a tela não atualizou", e.message, "warn"); }
    }
    async function resumeJob() {
      if (!selectedJob) { toast("Nenhum trabalho selecionado", "Clique num cartão na lista Trabalhos primeiro.", "warn"); return; }
      const cfg = formConfig();
      cfg.job_id = selectedJob;
      busy("#resume", true);
      try {
        const data = await api("/api/resume", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(cfg)});
        selectedJob = data.job_id;
        lastStatus[data.job_id] = "running";
        toast("Trabalho retomado", "Ele continua do ponto em que parou; nada já traduzido será refeito.", "success");
      } finally { busy("#resume", false); }
      try { await refreshJobs(); await refreshJob(); }
      catch (e) { toast("Trabalho rodando, mas a tela não atualizou", e.message, "warn"); }
    }
    async function stopJob() {
      if (!selectedJob) return;
      busy("#stop", true);
      try {
        await api("/api/stop", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({job_id: selectedJob})});
        toast("Parada solicitada", "A chamada em andamento vai terminar antes de parar. O progresso já está salvo.", "info");
        await refreshJob();
      } finally { busy("#stop", false); }
    }
    async function runDoctor() {
      const cfg = formConfig();
      busy("#doctor", true);
      const pending = toast("Testando Bedrock...", "Fazendo uma chamada mínima para cada modelo da lista.", "info", {timeout: 0});
      try {
        const data = await api("/api/doctor", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(cfg)});
        pending.remove();
        const bad = data.results.filter(r => !r.ok);
        if (data.ok_count === 0) {
          toast("Nenhum modelo respondeu", `Confira o profile (${cfg.profile}) e a região (${cfg.region}). Primeiro erro: ` + ((bad[0] && bad[0].error) || "").slice(0, 220), "error", {timeout: 0});
        } else if (bad.length) {
          toast(`${data.ok_count} de ${data.results.length} modelos OK`, "Sem acesso: " + bad.map(r => shortModel(r.model)).join(", ") + ". Da para traduzir assim mesmo; libere os demais em Amazon Bedrock, Model access, se quiser mais fallback.", "warn", {timeout: 0});
        } else {
          toast("Bedrock pronto", `Todos os ${data.results.length} modelos responderam com o profile ${cfg.profile} em ${cfg.region}.`, "success");
        }
      } finally { pending.remove(); busy("#doctor", false); }
    }
    async function refreshJob() {
      if (!selectedJob) return;
      const data = await api("/api/job?id=" + encodeURIComponent(selectedJob));
      renderJob(data.job);
    }

    /* ---------- render ---------- */
    function fileName(p) { return (p || "").split("/").pop() || "-"; }
    function dirName(p) { const a = (p || "").split("/"); a.pop(); return a.join("/"); }
    function shortPath(p) { if (!p) return ""; const parts = p.split("/"); return parts.slice(-2).join("/"); }
    function shortModel(m) { return (m || "").replace(/^global\./, "").replace(/^us\./, ""); }

    function estimateEta(job) {
      const done = job.done_cues || 0, total = job.total_cues || 0;
      if (job.status !== "running" || !total) { rateSamples = []; lastDone = done; return ""; }
      const now = Date.now();
      if (lastDone !== null && done > lastDone) {
        rateSamples.push({t: now, d: done});
        if (rateSamples.length > 12) rateSamples.shift();
      }
      lastDone = done;
      if (rateSamples.length < 2) return `${done} de ${total} legendas traduzidas. Calculando tempo restante...`;
      const a = rateSamples[0], b = rateSamples[rateSamples.length - 1];
      const perSec = (b.d - a.d) / Math.max(1, (b.t - a.t) / 1000);
      if (perSec <= 0) return `${done} de ${total} legendas traduzidas.`;
      const secs = Math.round((total - done) / perSec);
      const mins = Math.floor(secs / 60);
      const label = mins >= 1 ? `${mins} min ${secs % 60}s` : `${secs}s`;
      return `${done} de ${total} legendas traduzidas &middot; faltam cerca de ${label}`;
    }

    function pathLine(key, value, help) {
      if (!value) return "";
      return `<div class="pathline"><span class="pk">${escapeHtml(key)}</span>
        <span class="pv">${escapeHtml(value)}</span>
        <button class="copy" data-path="${escapeHtml(value)}">copiar</button></div>`;
    }

    function renderResult(job) {
      const box = document.querySelector("#result");
      const out = job.final_output_path || job.last_written_output || "";
      const q = job.quality || {};
      if (job.status === "complete") {
        box.className = "result show ok";
        box.innerHTML = `<h3>Tradução concluída</h3>
          <div class="sub">${job.total_cues || 0} legendas traduzidas, nenhum erro duro de QC. O arquivo está pronto para usar.</div>
          <div class="filebox"><div class="fb-main"><div class="fb-name">${escapeHtml(fileName(out))}</div>
            <div class="fb-dir">${escapeHtml(dirName(out))}</div></div>
            <button data-path="${escapeHtml(out)}">Copiar caminho</button></div>
          <ul>
            <li>É um SRT normal em UTF-8: abra no VLC pelo menu Legenda, Adicionar arquivo de legenda.</li>
            <li>Mesma quantidade de legendas e mesmos tempos do original, então sincroniza igual.</li>
            ${job.review_cues ? `<li><b>${job.review_cues}</b> legendas foram aceitas por consenso entre modelos e valem uma conferida.</li>` : ""}
            ${q.warning_cues ? `<li>${q.warning_cues} avisos de legibilidade no relatório de qualidade. Avisos não bloqueiam o arquivo.</li>` : ""}
          </ul>`;
      } else if (job.status === "incomplete") {
        box.className = "result show warn";
        box.innerHTML = `<h3>Terminou com pendencias</h3>
          <div class="sub">${job.last_error || "Sobraram legendas sem tradução aceita."}</div>
          <div class="filebox"><div class="fb-main"><div class="fb-name">${escapeHtml(fileName(out))}</div>
            <div class="fb-dir">${escapeHtml(dirName(out))}</div></div>
            <button data-path="${escapeHtml(out)}">Copiar caminho</button></div>
          <ul>
            <li>O sufixo <b>INCOMPLETO</b> no nome é o aviso de que faltou coisa.</li>
            <li>As legendas que faltaram ficam marcadas dentro do arquivo com <code>[TRADUCAO_PENDENTE]</code>.</li>
            <li>Para completar: deixe esta legenda selecionada e clique em <b>Iniciar ou retomar</b>. Ele refaz só o que faltou e, ao terminar, troca o arquivo por um <code>.OK.srt</code>.</li>
          </ul>`;
      } else if (job.status === "failed") {
        box.className = "result show bad";
        box.innerHTML = `<h3>O trabalho falhou</h3>
          <div class="sub">${escapeHtml(job.last_error || "Erro não identificado.")}</div>
          <ul><li>Clique em <b>Testar Bedrock</b> para checar credencial, região e acesso aos modelos.</li>
          <li>Nada do que já foi traduzido se perdeu: depois de resolver, use <b>Retomar selecionado</b>.</li></ul>`;
      } else if (job.status === "stalled") {
        box.className = "result show warn";
        box.innerHTML = `<h3>O processo parou sozinho</h3>
          <div class="sub">O progresso está salvo em disco, mas ninguém está traduzindo agora. Normalmente o servidor foi encerrado.</div>
          <ul><li>Clique em <b>Retomar selecionado</b> para continuar de onde parou.</li></ul>`;
      } else {
        box.className = "result";
        box.innerHTML = "";
      }
    }

    function renderJob(job) {
      const done = job.done_cues || 0;
      const total = job.total_cues || 0;
      const pct = total ? Math.round(done * 100 / total) : 0;
      document.querySelector("#mProgress").textContent = pct + "%";
      document.querySelector("#barFill").style.width = pct + "%";
      const cur = job.current || {};
      document.querySelector("#mBatch").textContent = cur.batch ? `${cur.batch}/${job.total_batches || "?"}` : (job.total_batches ? `-/${job.total_batches}` : "-");
      document.querySelector("#mModel").textContent = cur.model ? shortModel(cur.model) : "-";
      document.querySelector("#mErrors").textContent = job.error_cues || 0;
      document.querySelector("#mReview").textContent = job.review_cues || 0;

      const pill = document.querySelector("#statusPill");
      pill.textContent = job.stuck_batch ? `${job.status || "-"} · lote insistindo` : (job.status || "-");
      pill.className = "pill " + (job.status || "");
      document.querySelector("#eta").innerHTML = estimateEta(job);

      renderResult(job);

      const q = job.quality || {};
      const usage = job.usage || {};
      document.querySelector("#paths").innerHTML =
        pathLine("Original", job.source_path) +
        pathLine("Traduzida", job.final_output_path || job.last_written_output) +
        pathLine("Relatório", job.quality_report_path) +
        `<div class="pathline"><span class="pk">Números</span><span class="pv">${done}/${total} traduzidas &middot; ${job.pending_cues || q.pending_cues || 0} pendentes &middot; ${job.error_cues || 0} erros &middot; ${job.warning_cues || q.warning_cues || 0} avisos${usage.totalTokens ? ` &middot; ${Number(usage.totalTokens).toLocaleString("pt-BR")} tokens` : ""}${usage.cacheReadInputTokens ? ` (${Number(usage.cacheReadInputTokens).toLocaleString("pt-BR")} reaproveitados do cache)` : ""}</span></div>`;

      renderCusto(job.cost);
      const err = document.querySelector("#lastError");
      const showErr = job.last_error && job.status !== "complete";
      err.className = "alert-line" + (showErr ? " show" : "");
      err.textContent = showErr ? job.last_error : "";

      const log = document.querySelector("#log");
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
      log.innerHTML = (job.log_tail || []).map(e => {
        const lvl = e.level || "INFO";
        const good = /concluído|sucesso|validada|pronto/i.test(e.message || "") ? " good" : "";
        return `<div class="ln ${lvl}${good}"><span class="ts">${escapeHtml((e.ts || "").slice(11, 19))}</span> ${escapeHtml(e.message || "")}${escapeHtml(formatEvent(e))}</div>`;
      }).join("");
      if (atBottom) log.scrollTop = log.scrollHeight;

      const preview = document.querySelector("#preview");
      const items = (job.preview && job.preview.items) || [];
      preview.innerHTML = items.length ? items.map(renderCue).join("") : "<div class='empty'>Sem lote ativo no momento.</div>";
      renderCompare(job);
    }
    let escopoTocado = false;    // o usuário escolheu o escopo na mão?
    let escopoAjustadoPara = null;
    let fullCompare = null;      // lista completa, buscada sob demanda
    let fullCompareDone = -1;    // quantas falas existiam quando ela foi buscada

    async function ensureFullCompare(job) {
      const done = job.done_cues || 0;
      if (fullCompare && fullCompareDone === done) return fullCompare;
      const data = await api("/api/compare?id=" + encodeURIComponent(job.job_id));
      fullCompare = data.items || [];
      fullCompareDone = done;
      return fullCompare;
    }

    function renderCompare(job) {
      // Controle que não faz nada no estado atual só atrapalha: acompanhar sem trabalho
      // rodando não tem o que seguir, e filtrar por revisão sem nenhuma marcada esvazia
      // a lista sem motivo.
      const rodando = job.status === "running";
      const revisar = job.review_cues || 0;
      const wrapFollow = document.querySelector("#wrapFollow");
      const wrapReview = document.querySelector("#wrapReview");
      wrapFollow.hidden = !rodando;
      wrapReview.hidden = revisar === 0;
      wrapReview.querySelector("label").childNodes[1].nodeValue = ` só revisar (${revisar})`;
      if (!rodando) document.querySelector("#cmpFollow").checked = false;
      if (revisar === 0) document.querySelector("#cmpReview").checked = false;
      const seletor = document.querySelector("#cmpScope");
      // Ao abrir um trabalho que já terminou, o útil é a lista inteira. Isso é feito
      // uma vez por trabalho e nunca sobrescreve uma escolha manual, nem puxa o
      // usuário para fora da visão ao vivo quando o trabalho acaba de terminar.
      if (!escopoTocado && escopoAjustadoPara !== job.job_id) {
        escopoAjustadoPara = job.job_id;
        if (!rodando && (job.done_cues || 0) > 0) seletor.value = "all";
      }
      const scope = seletor.value;
      if (scope === "all") {
        // busca a lista inteira uma vez é só refaz quando o número de falas muda,
        // para não mandar o filme todo a cada polling
        ensureFullCompare(job)
          .then(items => paintCompare(items, job, true))
          .catch(() => paintCompare(job.compare || [], job, false));
        return;
      }
      fullCompare = null;
      fullCompareDone = -1;
      paintCompare(job.compare || [], job, false);
    }

    function paintCompare(all, job, isFull) {
      const onlyReview = document.querySelector("#cmpReview").checked;
      const term = document.querySelector("#cmpSearch").value.trim().toLowerCase();
      let list = onlyReview ? all.filter(i => i.review) : all;
      if (term) {
        list = list.filter(i =>
          (i.source || "").toLowerCase().includes(term) ||
          (i.translation || "").toLowerCase().includes(term));
      }
      const box = document.querySelector("#compare");
      const follow = document.querySelector("#cmpFollow").checked && !term;
      document.querySelector("#cmpCount").textContent =
        list.length ? `${list.length} de ${all.length} falas` : "";
      if (!list.length) {
        box.innerHTML = "<div class='empty' style='padding:12px'>" +
          (term ? "Nada encontrado para essa busca."
                : onlyReview ? "Nenhuma fala marcada para revisão."
                : "Nada traduzido ainda. As falas aparecem aqui conforme cada lote fecha.") +
          "</div>";
        return;
      }
      box.innerHTML = "<div class='cmp-legend'><span>fala</span><span>original</span><span>português</span></div>" +
        list.map(i => `<div class="cmp-row${i.review ? " review" : ""}">
          <div class="cmp-id">#${i.id}<span style="display:block">${escapeHtml((i.time || "").slice(0, 8))}</span>${i.review ? "<span class='flag'>revisar</span>" : ""}</div>
          <div class="cmp-src">${escapeHtml(i.source || "")}</div>
          <div class="cmp-pt">${escapeHtml(i.translation || "")}</div>
        </div>`).join("");
      if (follow) box.scrollTop = box.scrollHeight;
    }

    function renderCusto(custo) {
      const wrap = document.querySelector("#custoWrap");
      const linhas = (custo && custo.rows) || [];
      wrap.hidden = linhas.length === 0;
      if (!linhas.length) return;
      const n = v => Number(v || 0).toLocaleString("pt-BR");
      const semPreco = linhas.filter(r => r.cost_usd === null).map(r => r.model);
      const fontes = [...new Set(linhas.map(r => r.price_source).filter(Boolean))];
      const cambio = custo.exchange;
      const brl = v => v === null || v === undefined
        ? ""
        : `<div class="brl">R$ ${Number(v).toLocaleString("pt-BR", {minimumFractionDigits: 2, maximumFractionDigits: 2})}</div>`;
      document.querySelector("#custo").innerHTML =
        `<table class="custo"><thead><tr>
           <th>modelo</th><th>chamadas</th><th>entrada</th><th>cache lido</th><th>saída</th><th>custo estimado</th>
         </tr></thead><tbody>` +
        linhas.map(r => `<tr>
            <td>${escapeHtml(shortModel(r.model))}</td>
            <td>${n(r.calls)}</td>
            <td>${n(r.input)}</td>
            <td>${n(r.cache_read)}</td>
            <td>${n(r.output)}</td>
            <td>${r.cost_usd === null
                  ? "<span class='semPreco'>sem preço</span>"
                  : "US$ " + r.cost_usd.toFixed(5) + brl(r.cost_brl) + (r.price_is_reference ? "<div class='ref'>referência</div>" : "")}</td>
          </tr>`).join("") +
        `</tbody><tfoot><tr><td>total</td><td colspan="4"></td>
           <td>US$ ${Number(custo.total_usd || 0).toFixed(5)}${custo.complete ? "" : "+"}${brl(custo.total_brl)}</td></tr></tfoot></table>` +
        `<div class="custo-nota">${
          semPreco.length
            ? `<b>Estimativa incompleta.</b> Sem preço para ${semPreco.map(escapeHtml).join(", ")} — rode <code>refresh-prices</code> ou defina em <code>srt_translator.local.json</code>. `
            : ""
        }${fontes.length ? "Origem do preço: " + fontes.map(escapeHtml).join("; ") + ". " : ""}${
          cambio ? `Convertido a R$ ${Number(cambio.rate).toFixed(4)} por dólar (${escapeHtml(cambio.source)}${cambio.date ? ", " + escapeHtml(cambio.date) : ""}).`
                 : "Sem cotação do dólar: rode <code>refresh-prices</code> para ver os valores em real."
        }</div>`;
    }
    function renderCue(item) {
      const st = item.status || "";
      return `<div class="cue">
        <div class="meta">#${item.id} &middot; ${escapeHtml(item.time || "")} &middot; <span class="tag ${escapeHtml(st)}">${escapeHtml(st)}</span></div>
        <pre class="src">${escapeHtml(item.source || "")}</pre>
        ${item.translation ? `<pre>${escapeHtml(item.translation)}</pre>` : ""}
      </div>`;
    }
    function formatEvent(e) {
      const parts = [];
      if (e.batch) parts.push("lote=" + e.batch);
      if (e.model) parts.push("modelo=" + shortModel(e.model));
      if (e.attempt) parts.push("tentativa=" + e.attempt);
      if (e.sleep) parts.push("sleep=" + e.sleep + "s");
      if (e.error) parts.push("erro=" + String(e.error).slice(0, 220));
      return parts.length ? "  | " + parts.join(" ") : "";
    }

    document.addEventListener("click", ev => {
      const b = ev.target.closest("[data-path]");
      if (b) copyText(b.dataset.path, b);
    });
    document.querySelector("#refresh").onclick = async () => {
      busy("#refresh", true);
      try {
        const n = await refreshFiles(); await refreshJobs(); await refreshJob();
        toast("Lista atualizada", `${n} legenda(s) encontradas na pasta base. Nenhuma tradução foi alterada.`, "info", {timeout: 4000});
      } catch (e) { toast("Falha ao atualizar", e.message, "error"); }
      finally { busy("#refresh", false); }
    };
    document.querySelector("#cmpReview").onchange = () => refreshJob().catch(() => {});
    document.querySelector("#cmpScope").onchange = () => {
      escopoTocado = true;
      // trocar de escopo desliga o acompanhamento: quem pediu o filme inteiro quer navegar
      if (document.querySelector("#cmpScope").value === "all") document.querySelector("#cmpFollow").checked = false;
      refreshJob().catch(() => {});
    };
    let buscaTimer = null;
    document.querySelector("#cmpSearch").oninput = () => {
      clearTimeout(buscaTimer);
      buscaTimer = setTimeout(() => refreshJob().catch(() => {}), 220);
    };
    document.querySelector("#compare").addEventListener("scroll", ev => {
      const box = ev.target;
      const noFim = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
      const follow = document.querySelector("#cmpFollow");
      // rolar para trás significa que o usuário quer ler algo; não arraste a tela dele
      if (!noFim && follow.checked) follow.checked = false;
      if (noFim && !follow.checked) follow.checked = true;
    });
    document.querySelector("#start").onclick = () => startJob().catch(err => toast("Não consegui iniciar", err.message, "error"));
    document.querySelector("#resume").onclick = () => resumeJob().catch(err => toast("Não consegui retomar", err.message, "error"));
    document.querySelector("#stop").onclick = () => stopJob().catch(err => toast("Não consegui parar", err.message, "error"));
    document.querySelector("#doctor").onclick = () => runDoctor().catch(err => toast("Teste do Bedrock falhou", err.message, "error"));

    (async function boot() {
      try {
        await refreshFiles(); await refreshJobs(); await refreshJob();
      } catch (e) { toast("Falha ao carregar", e.message, "error"); }
      setInterval(() => { refreshJobs().then(refreshJob).catch(() => {}); }, 2500);
    })();
  </script>
</body>
</html>
"""


def render_ui_html() -> str:
    """Injeta os padroes desta maquina na pagina, para o HTML não guardar nada pessoal."""
    return (
        UI_HTML.replace("__DEFAULT_PROFILE__", html.escape(DEFAULT_PROFILE, quote=True))
        .replace("__DEFAULT_REGION__", html.escape(DEFAULT_REGION, quote=True))
        .replace("__DEFAULT_MODELS__", json.dumps(DEFAULT_MODELS, ensure_ascii=False))
    )


def serve_ui(args: argparse.Namespace) -> None:
    base = Path(args.base or Path.cwd()).expanduser().resolve()
    host = args.host
    port = args.port if args.port else find_free_port(host, 8765)
    httpd = ThreadingHTTPServer((host, port), UIHandler)
    httpd.base_path = base  # type: ignore[attr-defined]
    url = f"http://{host}:{port}/"
    print(f"{APP_NAME} UI rodando em {url}")
    print(f"Base: {base}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando UI.")


def find_free_port(host: str, preferred: int) -> int:
    for port in [preferred, *range(preferred + 1, preferred + 30)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError("Não encontrei porta livre para a UI.")


def list_models(args: argparse.Namespace) -> int:
    cmd = [
        shutil.which("aws") or "aws",
        "bedrock",
        "list-inference-profiles",
        "--profile",
        args.profile,
        "--region",
        args.region,
        "--type-equals",
        "SYSTEM_DEFINED",
        "--output",
        "json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout, file=sys.stderr)
        return proc.returncode
    data = json.loads(proc.stdout)
    for item in data.get("inferenceProfileSummaries", []):
        ident = item.get("inferenceProfileId", "")
        name = item.get("inferenceProfileName", "")
        if re.search(r"claude|nova|gpt|mistral|llama", ident + " " + name, re.IGNORECASE):
            print(f"{ident}\t{name}\t{item.get('status', '')}")
    return 0


def doctor(args: argparse.Namespace) -> int:
    logger = JsonLogger(echo=True)
    print(f"Python: {sys.version.split()[0]}")
    aws = shutil.which("aws")
    print(f"AWS CLI: {aws or 'não encontrado'}")
    if not aws:
        return 1
    ident = subprocess.run(
        [aws, "sts", "get-caller-identity", "--profile", args.profile, "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if ident.returncode != 0:
        print(ident.stderr or ident.stdout, file=sys.stderr)
        return ident.returncode
    print("Identidade AWS:", ident.stdout.strip())
    client = make_llm_client(args.profile, args.region, args.call_timeout, logger)
    ok_models = []
    bad_models = []
    system = "Responda somente JSON valido."
    prompt = 'Retorne exatamente {"ok":true}.'
    for model in parse_models(args.models):
        try:
            raw, meta = client.converse(model, system, prompt, max_tokens=40, temperature=0.1)
            data = extract_json_object(raw)
            if data.get("ok") is True:
                ok_models.append(model)
                print(f"OK modelo: {model}")
            else:
                bad_models.append((model, f"resposta inesperada: {raw[:200]}"))
                print(f"FALHA modelo: {model} resposta inesperada")
        except Exception as exc:
            bad_models.append((model, str(exc)))
            print(f"FALHA modelo: {model}: {str(exc)[:500]}")
    print(f"\nResumo doctor: {len(ok_models)} modelos OK, {len(bad_models)} com falha.")
    if bad_models:
        print("Se a falha for AccessDeniedException, confira Model access no console do Amazon Bedrock.")
    return 0 if ok_models else 2


def refresh_prices_cli(args: argparse.Namespace) -> int:
    """Busca preços na API da AWS e guarda o que ela souber informar."""
    aws = shutil.which("aws") or "aws"
    print(f"Consultando a API de preços da AWS para {args.region}...")
    cmd = [
        aws, "pricing", "get-products", "--service-code", "AmazonBedrock",
        "--region", "us-east-1", "--profile", args.profile,
        "--filters", f"Type=TERM_MATCH,Field=regionCode,Value={args.region}",
        "--page-size", "100", "--output", "json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.call_timeout or 300)
    if proc.returncode != 0:
        erro = (proc.stderr or proc.stdout).strip()[:600]
        print("Não consegui consultar a API de preços.")
        print(erro)
        print("\nIsso costuma ser falta da permissão pricing:GetProducts. A ferramenta segue")
        print("funcionando com o instantâneo embutido; você também pode definir preços à mão")
        print("em srt_translator.local.json, na chave prices.")
        return 1
    categorias = {
        "input tokens": "input",
        "output tokens": "output",
        "prompt cache read input tokens": "cache_read",
        "prompt cache write input tokens": "cache_write",
    }
    penaliza = ("flex", "priority", "batch", "latency", "provisioned", "custom-model", "commit")
    melhor: dict[str, dict[str, tuple[float, int]]] = {}
    dados = json.loads(proc.stdout)
    for bruto in dados.get("PriceList", []):
        item = json.loads(bruto) if isinstance(bruto, str) else bruto
        attrs = item.get("product", {}).get("attributes", {})
        categoria = categorias.get(str(attrs.get("inferenceType", "")).strip().lower())
        modelo = attrs.get("model")
        if not categoria or not modelo:
            continue
        uso = str(attrs.get("usagetype", "")).lower()
        nota = sum(1 for termo in penaliza if termo in uso)
        for termo in item.get("terms", {}).get("OnDemand", {}).values():
            for dim in termo.get("priceDimensions", {}).values():
                try:
                    valor = float(dim.get("pricePerUnit", {}).get("USD", 0))
                except Exception:
                    continue
                atual = melhor.setdefault(modelo, {}).get(categoria)
                if atual is None or nota < atual[1]:
                    melhor[modelo][categoria] = (valor, nota)
    tabela = {modelo: {c: round(v[0], 8) for c, v in cats.items()} for modelo, cats in melhor.items() if cats}
    destino = price_store_path()
    destino.parent.mkdir(parents=True, exist_ok=True)
    anterior = load_json(destino, {})
    cambio = fetch_exchange_rate()
    atomic_write_json(
        destino,
        {
            "buscado_em": utc_now()[:10],
            "regiao": args.region,
            "modelos": tabela,
            "cambio": cambio or anterior.get("cambio"),
        },
    )
    print(f"Guardei preços de {len(tabela)} modelos em {destino}")
    if cambio:
        print(f"Câmbio: US$ 1,00 = R$ {cambio['rate']:.4f} ({cambio['source']}, {cambio['date']})")
    else:
        print("Não consegui buscar a cotação do dólar no Banco Central; o total sai só em dólar.")
    faltando = []
    for modelo in parse_models(args.models) if getattr(args, "models", None) else DEFAULT_MODELS:
        if not price_for_model(modelo, load_prices()):
            faltando.append(modelo)
    if faltando:
        print("\nA AWS não publica preço para estes modelos da sua fila:")
        for modelo in faltando:
            print(f"  {modelo}")
        print("\nConsulte https://aws.amazon.com/bedrock/pricing/ e defina em srt_translator.local.json:")
        print(json.dumps({"prices": {faltando[0]: {"input": 0.003, "output": 0.015}}}, indent=2, ensure_ascii=False))
    return 0


def qc_cli(args: argparse.Namespace) -> int:
    translated = Path(args.translated).expanduser().resolve()
    source = Path(args.source).expanduser().resolve() if args.source else discover_resume_source(translated)
    if not source.exists():
        print("Fonte não encontrada. Passe --source /caminho/original.srt", file=sys.stderr)
        return 1
    src_doc = SrtDocument.load(source)
    tr_doc = SrtDocument.load(translated)
    translations: dict[str, dict[str, Any]] = {}
    for idx, cue in enumerate(src_doc.cues):
        if idx < len(tr_doc.cues):
            translations[str(cue.id)] = {"status": "ok", "text": tr_doc.cues[idx].text}
        else:
            translations[str(cue.id)] = {"status": "error", "text": "", "error": "cue ausente na tradução"}
    protected = detect_spelling_variant_tokens(src_doc.cues)
    protected_by_id = {
        cue.id: sorted({token for token in capitalized_tokens(cue.text) if token in protected})
        for cue in src_doc.cues
    }
    report = build_quality_report(
        src_doc.cues,
        translations,
        max_lines=args.max_lines,
        max_line_length=args.max_line_length,
        max_cps=args.max_cps,
        protected_tokens_by_id=protected_by_id,
    )
    if args.output:
        atomic_write_json(Path(args.output).expanduser().resolve(), report)
    summary = report["summary"]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if len(src_doc.cues) != len(tr_doc.cues):
        print(f"AVISO: fonte tem {len(src_doc.cues)} cues; tradução tem {len(tr_doc.cues)} cues.")
    for cue_report in report["cues"][:20]:
        issue_text = "; ".join(f"{i['severity']}:{i['code']}" for i in cue_report["issues"])
        print(f"#{cue_report['id']} {cue_report['time']} {issue_text}")
    if len(report["cues"]) > 20:
        print(f"... mais {len(report['cues']) - 20} cues com avisos/erros no relatório.")
    return 2 if summary["error_cues"] else 0


def self_test() -> int:
    sample = """1
00:00:01,000 --> 00:00:03,000
Hello.

2
00:00:03,100 --> 00:00:05,000
<i>How are you?</i>
"""
    cues = parse_srt(sample)
    assert len(cues) == 2
    batches = make_batches(cues, 1, 999)
    assert len(batches) == 2
    payload = {"translations": [{"id": 1, "text": "Ola."}]}
    assert validate_translation_payload(payload, batches[0]) == {"1": "Ola."}
    rendered = render_srt(cues, {"1": {"status": "ok", "text": "Ola."}}, "\n")
    assert "TRADUCAO_PENDENTE id=2" in rendered
    music = Batch(1, [SrtCue(1, "1", "00:00:01,000 --> 00:00:03,000", "♪ Hello ♪")], 1, 1)
    try:
        validate_translation_payload({"translations": [{"id": 1, "text": "Ola"}]}, music)
        raise AssertionError("music marker validation failed")
    except ContractError:
        pass
    assert not looks_untranslated("♪ Guli guli guli guli ram sam sam ♪", "♪ Guli guli guli guli ram sam sam ♪")
    assert not looks_untranslated("♪ Hi kye yay, yippie yi kye yay ♪", "♪ Hi kye yay, yippie yi kye yay ♪")
    assert not looks_untranslated("♪ Awoo awoo ayee kie chi' ♪", "♪ Awoo awoo ayee kie chi' ♪")
    assert looks_untranslated("♪ I love you baby ♪", "♪ I love you baby ♪")
    typo_doc = parse_srt("""1
00:00:01,000 --> 00:00:02,000
Sigmund "Frued."

2
00:00:02,100 --> 00:00:03,000
Freud.
""")
    assert detect_spelling_variant_tokens(typo_doc) == {"Frued", "Freud"}
    report = build_quality_report(
        cues,
        {"1": {"status": "ok", "text": "Ola."}, "2": {"status": "ok", "text": "<i>Como vai?</i>"}},
        max_lines=2,
        max_line_length=42,
        max_cps=17.0,
    )
    assert report["summary"]["error_cues"] == 0

    # Falha de heurística precisa ser soft e carregar o payload, para que o lote possa
    # ser aceito por consenso em vez de retentar para sempre.
    vocable_cues = [
        SrtCue(id=1, number=1, timing="00:00:01,000 --> 00:00:03,000", text="Hello there, my friend."),
        SrtCue(id=2, number=2, timing="00:00:03,000 --> 00:00:09,000", text="♪ Zoop bidoo wappa dinga ♪"),
    ]
    vocable_batch = Batch(number=1, cues=vocable_cues, start_id=1, end_id=2)
    try:
        validate_translation_payload(
            {"translations": [{"id": 1, "text": "Olá, meu amigo."}, {"id": 2, "text": "♪ Zoop bidoo wappa dinga ♪"}]},
            vocable_batch,
        )
        raise AssertionError("heurística deveria ter recusado")
    except SoftContractError as exc:
        assert exc.soft and exc.cue_ids == [2] and exc.payload["1"] == "Olá, meu amigo."
    try:
        validate_translation_payload({"translations": [{"id": 1, "text": "Olá."}]}, vocable_batch)
        raise AssertionError("IDs faltando deveria ter recusado")
    except ContractError as exc:
        assert not exc.soft, "quebra estrutural não pode virar soft"

    # Consenso exige modelos distintos concordando nos mesmos IDs.
    rec_a = {"model": "modelo-a", "cue_ids": (2,), "payload": {"2": "x"}, "reason": "r", "raw": "", "meta": {}}
    rec_b = {"model": "modelo-b", "cue_ids": (2,), "payload": {"2": "y"}, "reason": "r", "raw": "", "meta": {}}
    assert TranslatorJob.soft_consensus_record([rec_a, rec_b]) is not None
    assert TranslatorJob.soft_consensus_record([rec_a, dict(rec_a)]) is None
    assert TranslatorJob.soft_consensus_record([rec_a, {**rec_b, "cue_ids": (3,)}]) is None

    # Aceito por consenso vira aviso no QC, não erro duro.
    flagged = {"status": "ok", "text": "♪ Zoop bidoo wappa dinga ♪", "review_flag": "consenso_heuristica"}
    issues_flagged = cue_quality_issues(vocable_cues[1], flagged, max_lines=2, max_line_length=42, max_cps=17.0)
    assert not any(item["severity"] == "error" for item in issues_flagged)
    issues_plain = cue_quality_issues(
        vocable_cues[1],
        {"status": "ok", "text": "♪ Zoop bidoo wappa dinga ♪"},
        max_lines=2,
        max_line_length=42,
        max_cps=17.0,
    )
    assert any(item["code"] == "looks_untranslated" for item in issues_plain)

    # CPS e cobrado como regressao contra a fonte: legenda comercial já costuma passar do
    # limite, e marcar metade do filme esconderia o que a tradução de fato piorou.
    slow_source = SrtCue(id=1, number="1", timing="00:00:00,000 --> 00:00:04,000", text="Hi.")
    verbose = {"status": "ok", "text": "Uma frase muito comprida que ninguem consegue ler nesse tempo todo aqui."}
    codes_regression = {
        item["code"]: item["severity"]
        for item in cue_quality_issues(slow_source, verbose, max_lines=2, max_line_length=200, max_cps=17.0)
    }
    assert codes_regression.get("high_cps") == "warning", codes_regression
    fast_source = SrtCue(id=2, number="2", timing="00:00:00,000 --> 00:00:01,000", text="This line is already far too fast to read.")
    matched = {"status": "ok", "text": "Essa linha já era rápida demais na fonte."}
    codes_inherited = {
        item["code"]: item["severity"]
        for item in cue_quality_issues(fast_source, matched, max_lines=2, max_line_length=200, max_cps=17.0)
    }
    assert codes_inherited.get("high_cps_inherited") == "info", codes_inherited
    assert "high_cps" not in codes_inherited, codes_inherited
    inherited_report = build_quality_report([fast_source], {"2": matched}, max_lines=2, max_line_length=200, max_cps=17.0)
    assert inherited_report["summary"]["warning_cues"] == 0, inherited_report["summary"]
    assert inherited_report["report_version"] == QUALITY_REPORT_VERSION

    # Contrato por ferramenta: schema exige um item por id, com id e texto.
    tool = translation_tool_config()
    schema = tool["tools"][0]["toolSpec"]["inputSchema"]["json"]
    assert tool["toolChoice"]["tool"]["name"] == TRANSLATION_TOOL_NAME
    assert schema["properties"]["translations"]["items"]["required"] == ["id", "text"]
    assert is_tool_unsupported_error("This model doesn't support tool use.")
    assert not is_tool_unsupported_error("The maximum tokens you requested exceeds the model limit")

    # Glossario com genero: o guia fixa a forma e o QC cobra a coerencia.
    contexto = {
        "names_and_terms": [
            {"source": "Your Honor", "ptbr": "Meritíssima", "gender": "f", "note": "a juíza"},
            {"source": "Vail", "ptbr": "Vail", "gender": "m"},
            {"source": "sem ptbr", "ptbr": "", "gender": "f"},
        ]
    }
    gloss = glossary_from_context(contexto)
    assert len(gloss) == 2, gloss
    assert gloss[0]["gender"] == "f"
    assert gender_variants("Meritíssima") == ["Meritíssimo"]
    assert gender_variants("Vail") == []

    juiza_cues = [
        SrtCue(id=1, number="1", timing="00:00:01,000 --> 00:00:03,000", text="Objection, Your Honor."),
        SrtCue(id=2, number="2", timing="00:00:03,000 --> 00:00:05,000", text="Yes, Your Honor."),
        SrtCue(id=3, number="3", timing="00:00:05,000 --> 00:00:07,000", text="Thank you."),
    ]
    juiza_tr = {
        "1": {"status": "ok", "text": "Objeção, Meritíssima."},
        "2": {"status": "ok", "text": "Sim, Meritíssimo."},
        "3": {"status": "ok", "text": "Obrigado."},
    }
    conflitos = glossary_conflicts(juiza_cues, juiza_tr, gloss)
    assert conflitos["Your Honor"]["cues"] == [2], conflitos
    assert conflitos["Your Honor"]["certos"] == 1
    relatorio_gen = build_quality_report(
        juiza_cues, juiza_tr, max_lines=2, max_line_length=42, max_cps=99.0, glossary=gloss
    )
    codigos = {i["code"] for c in relatorio_gen["cues"] for i in c["issues"]}
    assert "glossary_gender" in codigos, codigos
    # sem glossario o comportamento antigo se mantem
    sem_gloss = build_quality_report(juiza_cues, juiza_tr, max_lines=2, max_line_length=42, max_cps=99.0)
    assert not any(i["code"] == "glossary_gender" for c in sem_gloss["cues"] for i in c["issues"])

    # Lotes fecham em pausa de cena sem perder nem reordenar falas.
    cena_cues = []
    t = 0
    for i in range(1, 25):
        gap = 6000 if i == 22 else 200   # um silêncio longo perto do fim
        t += gap
        cena_cues.append(
            SrtCue(
                id=i,
                number=str(i),
                timing=f"00:00:{t // 1000:02d},{t % 1000:03d} --> 00:00:{(t + 1500) // 1000:02d},{(t + 1500) % 1000:03d}",
                text=f"Fala {i}.",
            )
        )
        t += 1500
    lotes = make_batches(cena_cues, batch_size=24, max_chars=99999)
    assert [c.id for b in lotes for c in b.cues] == [c.id for c in cena_cues], "falas perdidas ou reordenadas"
    assert len(lotes) == 2 and lotes[0].cues[-1].id == 21, [(b.start_id, b.end_id) for b in lotes]
    # sem pausa longa, o comportamento antigo se mantém
    sem_pausa = make_batches(cena_cues, batch_size=24, max_chars=99999, scene_gap=10**9)
    assert len(sem_pausa) == 1, [(b.start_id, b.end_id) for b in sem_pausa]

    # Revisão de sentido: sinais de risco calibrados contra erros reais.
    assert "negacao" in semantic_risk_signals("I have no authority to deal.", "Não tenho autoridade para isso.")
    assert "contraste" in semantic_risk_signals("I could've been mistaken, but I wasn't wrong.", "x")
    assert "expandiu" in semantic_risk_signals("first-degree murder", "homicídio doloso em primeiro grau")
    assert semantic_risk_signals("Hello there my friend.", "Olá, meu amigo.") == []

    risco_cues = [
        SrtCue(id=1, number="1", timing="00:00:01,000 --> 00:00:03,000", text="I have no authority to deal."),
        SrtCue(id=2, number="2", timing="00:00:03,000 --> 00:00:05,000", text="Hello there my friend."),
    ]
    risco_tr = {"1": {"status": "ok", "text": "Não tenho autoridade para isso."}, "2": {"status": "ok", "text": "Olá, meu amigo."}}
    assert select_for_semantic_review(risco_cues, risco_tr, sample_pct=0)[0] == [1]
    assert select_for_semantic_review(risco_cues, risco_tr, always={2}, sample_pct=0)[0] == [1, 2]

    # O juiz recuando da própria acusação não pode disparar retradução.
    atual = "Não tenho autoridade para isso."
    assert judge_verdict_is_actionable(
        {"veredito": "errado", "porque": "perdeu o sentido de negociar", "sugestao": "Não tenho autoridade para negociar."}, atual
    )
    assert not judge_verdict_is_actionable(
        {"veredito": "errado", "porque": "Após reanálise, sentido preservado.", "sugestao": "outra coisa"}, atual
    ), "recuo do juiz deveria bloquear a ação"
    assert not judge_verdict_is_actionable({"veredito": "suspeito", "porque": "x", "sugestao": "y"}, atual)
    assert not judge_verdict_is_actionable({"veredito": "errado", "porque": "x", "sugestao": atual}, atual)
    assert not judge_verdict_is_actionable({"veredito": "errado", "porque": "x"}, atual)

    # A amostragem cobre o buraco dos sinais: inversão de sentido do mesmo tamanho
    # e sem negação não gera sinal nenhum.
    invisivel = semantic_risk_signals("You print any of this, I'll sue your ass.", "Sim, eu fiz isso e tenho orgulho.")
    assert invisivel == [], invisivel
    muitos = [
        SrtCue(id=i, number=str(i), timing="00:00:01,000 --> 00:00:03,000", text="Plain line here.")
        for i in range(1, 51)
    ]
    muitos_tr = {str(i): {"status": "ok", "text": "Linha simples aqui."} for i in range(1, 51)}
    so_risco, n_risco = select_for_semantic_review(muitos, muitos_tr, sample_pct=0)
    assert so_risco == [] and n_risco == 0
    com_amostra, n_risco2 = select_for_semantic_review(muitos, muitos_tr, sample_pct=0.2, seed="job")
    assert n_risco2 == 0 and len(com_amostra) == 10, (n_risco2, len(com_amostra))
    # a amostra é estável entre retomadas do mesmo trabalho
    assert select_for_semantic_review(muitos, muitos_tr, sample_pct=0.2, seed="job")[0] == com_amostra
    assert select_for_semantic_review(muitos, muitos_tr, sample_pct=0.2, seed="outro")[0] != com_amostra

    # Revisão total é o padrão; o usuário barateia subindo o limiar.
    padrao = JobConfig(source_path=Path("x.srt"))
    assert padrao.semantic_review is False, "revisão de sentido não deve vir ligada"
    assert padrao.semantic_autofix is False, "reescrita automática não deve vir ligada"
    assert padrao.semantic_min_signals == 0, "quando ligada, cobre o filme todo"
    # com limiar 0 tudo entra, com limiar 1 só o que tem sinal
    todos, _ = select_for_semantic_review(risco_cues, risco_tr, min_signals=0, sample_pct=0)
    assert todos == [1, 2], todos
    poucos, _ = select_for_semantic_review(risco_cues, risco_tr, min_signals=1, sample_pct=0)
    assert poucos == [1], poucos

    # Preço: casamento de id, cálculo com cache e honestidade sobre o que falta.
    assert normalize_model_key("us.amazon.nova-pro-v1:0") == "novapro"
    assert normalize_model_key("mistral.mistral-large-3-675b-instruct") == "mistrallarge3675binstruct"
    tabela_teste = {"novapro": {"input": 0.0008, "output": 0.0032, "cache_read": 0.0002, "_fonte": "teste"}}
    assert price_for_model("us.amazon.nova-pro-v1:0", tabela_teste) is not None
    assert price_for_model("us.anthropic.claude-sonnet-4-6", tabela_teste) is None
    uso = {
        "us.amazon.nova-pro-v1:0": {"calls": 2, "inputTokens": 10000, "outputTokens": 1000, "cacheReadInputTokens": 5000},
        "us.anthropic.claude-sonnet-4-6": {"calls": 1, "inputTokens": 1000, "outputTokens": 100},
    }
    custo = estimate_cost(uso, tabela_teste)
    nova = next(r for r in custo["rows"] if "nova" in r["model"])
    esperado = 10000 / 1000 * 0.0008 + 1000 / 1000 * 0.0032 + 5000 / 1000 * 0.0002
    assert abs(nova["cost_usd"] - esperado) < 1e-9, (nova["cost_usd"], esperado)
    claude = next(r for r in custo["rows"] if "claude" in r["model"])
    assert claude["cost_usd"] is None and claude["price_source"] is None
    assert custo["complete"] is False, "total precisa se declarar incompleto quando falta preço"
    assert abs(custo["total_usd"] - esperado) < 1e-6
    assert estimate_cost({}, tabela_teste)["complete"] is True

    # Conversão para real e marcação de preço de referência.
    tabela_ref = {
        "novapro": {"input": 0.001, "output": 0.002, "_fonte": "oficial"},
        "claudesonnet46": {"input": 0.003, "output": 0.015, "_fonte": "referência", "_referencia": True},
    }
    cambio_teste = {"rate": 5.0, "date": "2026-09-04", "source": "teste"}
    uso_brl = {
        "us.amazon.nova-pro-v1:0": {"calls": 1, "inputTokens": 1000, "outputTokens": 1000},
        "us.anthropic.claude-sonnet-4-6": {"calls": 1, "inputTokens": 1000, "outputTokens": 0},
    }
    c_brl = estimate_cost(uso_brl, tabela_ref, cambio_teste)
    nova_l = next(r for r in c_brl["rows"] if "nova" in r["model"])
    assert abs(nova_l["cost_usd"] - 0.003) < 1e-9
    assert abs(nova_l["cost_brl"] - 0.015) < 1e-9, nova_l["cost_brl"]
    assert nova_l["price_is_reference"] is False
    claude_l = next(r for r in c_brl["rows"] if "claude" in r["model"])
    assert claude_l["price_is_reference"] is True, "preço de referência precisa se declarar"
    assert abs(c_brl["total_brl"] - c_brl["total_usd"] * 5.0) < 1e-9
    # sem câmbio, some o real mas o dólar continua
    sem_cambio = estimate_cost(uso_brl, tabela_ref, None)
    assert sem_cambio["total_brl"] is None and sem_cambio["total_usd"] > 0
    assert all(r["cost_brl"] is None for r in sem_cambio["rows"])
    # todo modelo da fila padrão tem preço
    tabela_real = load_prices()
    assert all(price_for_model(mo, tabela_real) for mo in DEFAULT_MODELS[:4]), "fila padrão sem preço"

    # Navegador de pastas: lista, marca saída da ferramenta e não quebra em caminho ruim.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        raiz = Path(tmp)
        (raiz / "sub").mkdir()
        (raiz / ".oculta").mkdir()
        (raiz / "filme.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nOi.\n", encoding="utf-8")
        (raiz / "filme.pt-BR.OK.srt").write_text("x", encoding="utf-8")
        (raiz / "leiame.txt").write_text("x", encoding="utf-8")
        listagem = browse_directory(str(raiz), raiz)
        nomes_dir = {d["name"] for d in listagem["dirs"]}
        nomes_srt = {f["name"] for f in listagem["files"]}
        assert nomes_dir == {"sub"}, nomes_dir           # pasta oculta fica de fora
        assert nomes_srt == {"filme.srt", "filme.pt-BR.OK.srt"}, nomes_srt  # só .srt
        marcadas = {f["name"]: f["traduzida"] for f in listagem["files"]}
        assert marcadas["filme.pt-BR.OK.srt"] is True, "saída da ferramenta precisa vir marcada"
        assert marcadas["filme.srt"] is False
        assert listagem["shortcuts"], "atalhos ausentes"
        # apontar para um arquivo abre a pasta dele; caminho inexistente cai na base
        assert browse_directory(str(raiz / "filme.srt"), raiz)["path"] == str(raiz.resolve())
        assert browse_directory(str(raiz / "nao" / "existe"), raiz)["path"] == str(raiz.resolve())

    print("self-test ok")
    return 0


def translate_cli(args: argparse.Namespace) -> int:
    cfg = build_config_from_args(args)
    logger = JsonLogger(echo=True)
    job = TranslatorJob(cfg, logger=logger)
    try:
        state = job.run()
    except KeyboardInterrupt:
        job.request_stop()
        return 130
    except Exception as exc:
        print(f"Falha: {exc}", file=sys.stderr)
        return 1
    print("\nResultado:")
    print(f"status: {state.get('status')}")
    print(f"saída: {state.get('final_output_path') or state.get('last_written_output')}")
    if state.get("last_error"):
        print(f"último erro: {state.get('last_error')}")
    return 0 if state.get("status") == "complete" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=APP_NAME)
    sub = parser.add_subparsers(dest="cmd")

    p_translate = sub.add_parser("translate", help="traduz um arquivo .srt")
    p_translate.add_argument("input", help="arquivo .srt de entrada ou saída incompleta com sidecar")
    add_common_job_args(p_translate)
    p_translate.set_defaults(func=translate_cli)

    p_ui = sub.add_parser("ui", help="abre a UI local")
    p_ui.add_argument("--host", default="127.0.0.1")
    p_ui.add_argument("--port", type=int, default=8765)
    p_ui.add_argument("--base", default=str(Path.cwd()), help="pasta base para listar .srt e trabalhos")
    p_ui.set_defaults(func=serve_ui)

    p_precos = sub.add_parser("refresh-prices", help="busca precos do Bedrock na API da AWS")
    p_precos.add_argument("--profile", default=DEFAULT_PROFILE)
    p_precos.add_argument("--region", default=DEFAULT_REGION)
    p_precos.add_argument("--models", default=None)
    p_precos.add_argument("--call-timeout", type=int, default=300)
    p_precos.set_defaults(func=refresh_prices_cli)

    p_models = sub.add_parser("list-models", help="lista inference profiles úteis do Bedrock")
    p_models.add_argument("--profile", default=DEFAULT_PROFILE)
    p_models.add_argument("--region", default=DEFAULT_REGION)
    p_models.set_defaults(func=list_models)

    p_doctor = sub.add_parser("doctor", help="testa AWS CLI, identidade e modelos Bedrock")
    p_doctor.add_argument("--profile", default=DEFAULT_PROFILE)
    p_doctor.add_argument("--region", default=DEFAULT_REGION)
    p_doctor.add_argument("--models", default=",".join(DEFAULT_MODELS))
    p_doctor.add_argument("--call-timeout", type=int, default=60)
    p_doctor.set_defaults(func=doctor)

    p_qc = sub.add_parser("qc", help="audita uma legenda traduzida")
    p_qc.add_argument("translated", help="arquivo .srt traduzido")
    p_qc.add_argument("--source", default=None, help="arquivo .srt original; se omitido, tenta sidecar")
    p_qc.add_argument("--output", default=None, help="salva relatório JSON")
    p_qc.add_argument("--max-lines", type=int, default=2)
    p_qc.add_argument("--max-line-length", type=int, default=42)
    p_qc.add_argument("--max-cps", type=float, default=17.0)
    p_qc.set_defaults(func=qc_cli)

    p_test = sub.add_parser("self-test", help="testes rapidos sem chamar LLM")
    p_test.set_defaults(func=lambda args: self_test())
    return parser


def add_common_job_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS), help="lista separada por virgula, ponto e virgula ou linha")
    parser.add_argument("--batch-size", type=int, default=28)
    parser.add_argument("--max-batch-chars", type=int, default=4300)
    parser.add_argument("--context-batches", type=int, default=1)
    parser.add_argument("--attempts-per-model", type=int, default=3)
    parser.add_argument("--base-backoff", type=float, default=3.0)
    parser.add_argument("--max-backoff", type=float, default=120.0)
    parser.add_argument("--no-retry-forever", action="store_true", help="marca erro em vez de retentar indefinidamente")
    parser.add_argument("--call-timeout", type=int, default=240)
    parser.add_argument("--no-context-pass", action="store_true")
    parser.add_argument("--polish-pass", action="store_true", help="roda um segundo passe de revisão")
    parser.add_argument("--no-retry-qc-issues", action="store_true", help="não refaz automaticamente cues que falham no QC duro")
    parser.add_argument("--semantic-review", dest="semantic_review", action="store_true", help="liga a revisao de sentido por LLM: um segundo modelo procura erro de significado. Custa ~40%% a mais e, medido num filme real, aponta pouca coisa util")
    parser.add_argument("--semantic-autofix", dest="semantic_autofix", action="store_true", help="alem de relatar, reescreve as falas acusadas. Use com cuidado: numa medicao, uma das tres correcoes confirmadas piorava a legenda")
    parser.add_argument("--semantic-min-signals", type=int, default=0, help="sinais de risco para entrar na revisao de sentido")
    parser.add_argument("--semantic-sample-pct", type=float, default=0.10, help="fracao sorteada alem das falas de risco")
    parser.add_argument("--semantic-budget", type=int, default=100000, help="teto de falas enviadas a revisao de sentido")
    parser.add_argument("--qc-repair-rounds", type=int, default=2)
    parser.add_argument("--max-lines", type=int, default=2)
    parser.add_argument("--max-line-length", type=int, default=42)
    parser.add_argument("--max-cps", type=float, default=17.0)
    parser.add_argument("--max-cues", type=int, default=None, help="debug: traduz só os N primeiros cues")
    parser.add_argument("--output", default=None)
    parser.add_argument("--job-root", default=None)
    parser.add_argument("--force-new", action="store_true")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        args = parser.parse_args(["ui"])
    result = args.func(args)
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
