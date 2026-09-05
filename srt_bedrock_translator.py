#!/usr/bin/env python3
"""
SRT -> Portuguese (Brazil) subtitle translator using Amazon Bedrock through AWS CLI.

No third-party Python packages are required. The script provides:
- A CLI translator with resume support.
- A local web UI for choosing .srt files and watching logs/progress.
- Strict JSON contracts for LLM responses.
- Batch translation with previous/current/next context.
- Persistent state so interrupted jobs can be resumed.
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
DEFAULT_PROFILE = "default"
DEFAULT_REGION = "us-east-1"
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
# heuristica de qualidade seja considerada errada e a traducao seja aceita.
SOFT_CONSENSUS_MODELS = 2

# Quanto a traducao pode ficar mais lenta de ler que a fonte antes de virar aviso.
CPS_REGRESSION_RATIO = 1.15

# Bump quando as regras de QC mudarem, para relatorios antigos serem recalculados.
QUALITY_REPORT_VERSION = 2

TIME_RE = re.compile(
    r"^\s*\d{1,2}:\d{2}:\d{2},\d{3}\s*-->\s*"
    r"\d{1,2}:\d{2}:\d{2},\d{3}(?:\s+.*)?$"
)
YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")

REFUSAL_RE = re.compile(
    r"("
    r"\b(?:as an ai|as a language model|como (?:modelo|uma ia|um assistente)|sou uma ia)\b|"
    r"\b(?:i(?:'m| am) sorry|sorry|desculpe|lamento)\b.{0,140}"
    r"\b(?:can't|cannot|can not|unable|not able|nao posso|não posso|nao consigo|não consigo)\b.{0,140}"
    r"\b(?:translate|traduzir|lyrics?|letras?|copyright|direitos autorais|request|pedido|policy|politica|política)\b|"
    r"\b(?:can't|cannot|can not|unable to|not able to|nao posso|não posso|nao consigo|não consigo)\b.{0,140}"
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
    "ha",
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
    "So",
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

    `soft=True` marca falhas que vem de heuristica de qualidade (ex.: "parece nao
    traduzido"), nao de quebra estrutural. Heuristica pode errar; por isso um erro
    soft nunca pode travar o lote para sempre. Ver `SoftContractError`.
    """

    def __init__(self, message: str, *, soft: bool = False, cue_ids: list[int] | None = None):
        super().__init__(message)
        self.soft = soft
        self.cue_ids = cue_ids or []


class SoftContractError(ContractError):
    """Falha apenas heuristica: o payload e estruturalmente valido e utilizavel."""

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
        raise ValueError(f"Timecode invalido: {value}")
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
    _, split = min(candidates)
    left = text[:split].strip()
    right = text[split:].strip()
    if not left or not right:
        return text
    return left + "\n" + right


def apply_subtitle_formatting(text: str, max_line_length: int, max_lines: int) -> str:
    text = normalize_subtitle_text(text)
    lines = text.split("\n")
    if len(lines) == 1 and line_lengths(text)[0] > max_line_length:
        wrapper = re.match(r"^(<([ibu])>)(.*)(</\2>)$", text, flags=re.IGNORECASE | re.DOTALL)
        if wrapper and "<" not in wrapper.group(3) and ">" not in wrapper.group(3):
            inner = smart_break_plain(wrapper.group(3), max_line_length)
            if len(inner.split("\n")) <= max_lines:
                return wrapper.group(1) + inner + wrapper.group(4)
        if "<" not in text and ">" not in text:
            candidate = smart_break_plain(text, max_line_length)
            if len(candidate.split("\n")) <= max_lines:
                return candidate
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
        issues.append({"severity": "pending", "code": "pending", "message": "Traducao ainda pendente."})
        if not text:
            return issues
    elif status != "ok":
        issues.append({"severity": "error", "code": "not_ok", "message": f"Status atual: {status or 'pending'}."})
        if not text:
            return issues
    if not text:
        return [{"severity": "error", "code": "empty", "message": "Traducao vazia."}]
    if "TRADUCAO_PENDENTE" in text or "ERRO_TRADUCAO" in text:
        issues.append({"severity": "error", "code": "marker_in_text", "message": "Marcador tecnico apareceu no texto."})
    if text_has_refusal(text):
        issues.append({"severity": "error", "code": "refusal", "message": "Texto parece recusa do modelo."})
    if looks_untranslated(cue.text, text):
        # Se modelos independentes ja devolveram este mesmo texto, a heuristica perde a
        # ultima palavra: vira aviso para revisao, nao erro que bloqueia o .OK.srt.
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
            issues.append({"severity": "error", "code": "looks_untranslated", "message": "Texto parece nao traduzido."})
    missing_tokens = [token for token in protected_tokens if token not in text]
    if missing_tokens:
        issues.append({"severity": "error", "code": "protected_token_missing", "message": "Token protegido ausente.", "tokens": missing_tokens})
    if unbalanced_tags(text):
        issues.append({"severity": "error", "code": "unbalanced_tags", "message": "Tags HTML simples desbalanceadas.", "tags": unbalanced_tags(text)})
    missing_tags = missing_source_tags(cue.text, text)
    if missing_tags:
        issues.append({"severity": "error", "code": "source_tag_missing", "message": "Tag presente na fonte sumiu na traducao.", "tags": missing_tags})
    source_notes = cue.text.count("♪")
    translated_notes = text.count("♪")
    if source_notes and not translated_notes:
        issues.append({"severity": "error", "code": "music_marker_missing", "message": "Legenda musical perdeu o marcador musical."})
    elif source_notes >= 2 and translated_notes < 2:
        issues.append({"severity": "warning", "code": "music_marker_partial", "message": "Legenda musical deveria manter notas no inicio e no fim."})
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
            # Muitas legendas comerciais ja passam do limite na propria fonte. Cobrar o
            # limite absoluto marcaria metade do filme e esconderia o que importa: se a
            # traducao ficou mais lenta de ler do que o original.
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
                        "message": f"Acima de {max_cps:.1f} cps, mas a legenda original ja era assim.",
                        "cps": round(cps, 2),
                        "source_cps": round(source_cps, 2),
                    }
                )
    except Exception as exc:
        issues.append({"severity": "warning", "code": "duration_parse", "message": f"Nao consegui calcular CPS: {exc}"})
    return issues


def build_quality_report(
    cues: list[SrtCue],
    translations: dict[str, dict[str, Any]],
    *,
    max_lines: int,
    max_line_length: int,
    max_cps: float,
    protected_tokens_by_id: dict[int, list[str]] | None = None,
) -> dict[str, Any]:
    protected_tokens_by_id = protected_tokens_by_id or {}
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


def make_batches(cues: list[SrtCue], batch_size: int, max_chars: int) -> list[Batch]:
    batches: list[Batch] = []
    current: list[SrtCue] = []
    chars = 0
    for cue in cues:
        cue_len = len(cue.text) + len(cue.timing) + 32
        if current and (len(current) >= batch_size or chars + cue_len > max_chars):
            batches.append(Batch(len(batches) + 1, current, current[0].id, current[-1].id))
            current = []
            chars = 0
        current.append(cue)
        chars += cue_len
    if current:
        batches.append(Batch(len(batches) + 1, current, current[0].id, current[-1].id))
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


def extract_json_object(text: str) -> Any:
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw, flags=re.IGNORECASE)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise


def validate_context_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ContractError("Contexto nao e objeto JSON.")
    required = ["title_guess", "source_language", "tone", "style_guide_ptbr", "names_and_terms", "continuity_notes"]
    missing = [key for key in required if key not in payload]
    if missing:
        raise ContractError(f"Contexto sem campos obrigatorios: {missing}.")
    if not isinstance(payload.get("style_guide_ptbr"), list) or not payload["style_guide_ptbr"]:
        raise ContractError("Contexto sem style_guide_ptbr util.")
    if not isinstance(payload.get("names_and_terms"), list):
        raise ContractError("Contexto names_and_terms invalido.")
    if not isinstance(payload.get("continuity_notes"), list):
        raise ContractError("Contexto continuity_notes invalido.")
    return payload


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
        raise ContractError("Resposta nao e um objeto JSON.")
    translations = payload.get("translations")
    if not isinstance(translations, list):
        raise ContractError("Campo 'translations' ausente ou invalido.")
    expected_ids = {cue.id for cue in batch.cues}
    got: dict[int, str] = {}
    for item in translations:
        if not isinstance(item, dict):
            raise ContractError("Item de traducao nao e objeto.")
        if "id" not in item or "text" not in item:
            raise ContractError("Item sem id/text.")
        try:
            cue_id = int(item["id"])
        except Exception as exc:
            raise ContractError(f"ID invalido em item: {item!r}") from exc
        if cue_id not in expected_ids:
            raise ContractError(f"ID inesperado: {cue_id}.")
        text = normalize_subtitle_text(str(item["text"]))
        if not text:
            raise ContractError(f"Traducao vazia para id {cue_id}.")
        if TIME_RE.match(text.split("\n", 1)[0] or ""):
            raise ContractError(f"Traducao contem linha de timestamp no id {cue_id}.")
        if text_has_refusal(text):
            raise ContractError(f"Traducao parece recusa no id {cue_id}.")
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
    # Checagem heuristica por ultimo: o payload ja esta estruturalmente correto aqui,
    # entao a falha vira soft e carrega o payload para permitir aceitacao por consenso.
    cue_by_id = {cue.id: cue for cue in batch.cues}
    suspicious = [
        cue_id
        for cue_id, text in got.items()
        if looks_untranslated(cue_by_id[cue_id].text, text)
    ]
    if suspicious:
        raise SoftContractError(
            f"Possivel texto nao traduzido nos IDs: {sorted(suspicious)[:12]}.",
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
    ) -> tuple[str, dict[str, Any]]:
        messages = [{"role": "user", "content": [{"text": user_text}]}]
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
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BedrockCallError(f"Timeout apos {self.timeout}s chamando {model_id}.") from exc
        except FileNotFoundError as exc:
            raise BedrockCallError("AWS CLI nao encontrado no PATH.", retryable=False) from exc

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            unavailable = is_unavailable_model_error(err)
            retryable = not unavailable and is_retryable_cli_error(err)
            raise BedrockCallError(err[:4000] or f"AWS CLI saiu com codigo {proc.returncode}.", retryable=retryable, unavailable_model=unavailable)

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise BedrockCallError(f"Resposta da AWS nao era JSON: {proc.stdout[:1000]}", retryable=True) from exc

        content = data.get("output", {}).get("message", {}).get("content", [])
        chunks = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
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
        self.protected_variant_tokens: set[str] = set()

    def request_stop(self) -> None:
        self._stop_event.set()
        self.stop_path.write_text("stop requested " + utc_now(), encoding="utf-8")
        self.logger.event("WARN", "Parada solicitada pelo usuario.")

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
                "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
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
        """Remove a saida parcial e a variante final antiga deste mesmo job.

        Sem isso a pasta do filme acumula .EM_ANDAMENTO.srt, .INCOMPLETO.srt e
        .OK.srt lado a lado e nao da para saber qual e a legenda boa.
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
            # So apaga o que este job escreveu: o sidecar tem que apontar para este job_id.
            owner = load_json(sidecar, {}).get("job_id") if sidecar.exists() else None
            if owner != self.job_id:
                self.logger.event(
                    "WARN",
                    "Arquivo antigo com nome de saida nao pertence a este trabalho; mantendo.",
                    error=str(path),
                )
                continue
            for target in (path, sidecar):
                try:
                    target.unlink()
                except OSError as exc:
                    self.logger.event("WARN", "Nao consegui remover saida antiga.", error=f"{target}: {exc}")
            self.logger.event("INFO", "Saida antiga removida para nao confundir com a legenda final.", error=str(path))

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
        client = BedrockClient(self.config.profile, self.config.region, self.config.call_timeout, self.logger)
        try:
            if self.config.context_pass:
                self.ensure_context_pack(client, state)
            self.translate_all(client, state, polish=False)
            if self.config.retry_qc_issues:
                self.repair_quality_issues(client, state)
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
            state["last_error"] = "Parado pelo usuario."
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
            self.logger.event("WARN", "Contexto existente nao passou na validacao; vou recria-lo.", error="contexto ausente, generico ou truncado")
        assert self.doc is not None
        self.logger.event("INFO", "Criando contexto do filme a partir do nome e de amostras da legenda.")
        system = (
            "Voce prepara guias de traducao audiovisual para portugues brasileiro. "
            "Use apenas os dados fornecidos. Se algo nao estiver claro, marque como inferencia ou desconhecido. "
            "Retorne somente JSON valido, curto, sem markdown, sem texto antes ou depois."
        )
        samples = collect_samples(self.doc.cues)
        prompt = {
            "task": "Prepare um guia curto e pratico para traduzir esta legenda SRT para portugues brasileiro natural e contextualizado.",
            "movie_metadata_from_path": infer_movie_title(self.source_path),
            "rules": [
                "Nao invente sinopse externa.",
                "Identifique nomes recorrentes, relacoes aparentes, tom, registro e escolhas de tratamento apenas quando a amostra permitir.",
                "Inclua no maximo 8 orientacoes praticas para musicas legendadas, palavroes, humor, ironia e continuidade.",
                "Use strings curtas. Limite names_and_terms a 20 itens e continuity_notes a 10 itens.",
                "Se houver duvida, prefira diretrizes conservadoras.",
            ],
            "response_format_required": (
                '{"title_guess":"...","year_guess":"...","source_language":"...",'
                '"tone":"...","style_guide_ptbr":["..."],'
                '"names_and_terms":[{"source":"...","ptbr":"...","note":"..."}],'
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
                    "Traduzir para portugues brasileiro natural.",
                    "Preservar nomes proprios e continuidade entre lotes.",
                    "Adaptar musicas legendadas como legenda audiovisual, mantendo notas musicais quando existirem.",
                ],
                "names_and_terms": [],
                "continuity_notes": [f"Context pass falhou: {exc}"],
                "_fallback": True,
            }
            self.logger.event("WARN", "Nao consegui criar contexto via LLM; usando guia generico.", error=str(exc)[:500])
        atomic_write_json(self.context_path, context)
        state["context"] = context
        self.add_usage(state, meta if "meta" in locals() else {})
        self.save_state(state)
        self.logger.event("INFO", "Contexto preparado.", status="ok")

    def translate_all(self, client: BedrockClient, state: dict[str, Any], *, polish: bool) -> None:
        assert self.doc is not None
        completed = set(state.get("completed_batches", []))
        stage_name = "polimento" if polish else "traducao"
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
                        # Aceito por consenso entre modelos: nao e erro duro, mas fica
                        # sinalizado para o relatorio de QC e para revisao humana.
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
                    "Traducoes do lote persistidas; atualizando SRT parcial.",
                    batch=batch.number,
                    done=self.count_done(),
                    total=len(self.doc.cues),
                )
                self.write_output(state, final=False)
                self.logger.event(
                    "INFO",
                    f"Lote {batch.number}/{len(self.batches)} concluido.",
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
            self.logger.event("ERROR", "Alguns cues continuaram com erro de QC apos as rodadas de reparo.", error=f"ids={remaining[:30]}")

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
        system, prompt = builder(self, batch)
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
        )
        self.add_usage(state, meta)
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
                    call_system, call_prompt = system, prompt
                    if feedback and prompt_builder is not None:
                        try:
                            call_system, call_prompt = prompt_builder(feedback)
                        except Exception:
                            call_system, call_prompt = system, prompt
                    try:
                        raw, meta = client.converse(
                            model,
                            call_system,
                            call_prompt,
                            max_tokens=current_max_tokens,
                            temperature=temperature,
                        )
                        raw_excerpt = raw[:800]
                        if meta.get("stopReason") == "max_tokens":
                            raise ContractError("Resposta cortada pelo limite max_tokens.")
                        if validator is not None:
                            validator(raw)
                        elif text_has_refusal(raw):
                            raise ContractError("Resposta parece recusa, nao traducao no contrato.")
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
                            self.logger.event("WARN", "Modelo indisponivel para esta conta ou forma de chamada; tentando outro.", model=model, error=last_error[:700])
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
                                f"A resposta anterior foi recusada pela validacao automatica: {last_error} "
                                "Traduza ou adapte esses IDs para portugues brasileiro de verdade. "
                                "Se e somente se o texto for vocalizacao musical sem sentido lexical "
                                "(refrao de silabas, onomatopeia, scat), repita exatamente o mesmo texto: "
                                "isso e aceito e nao e considerado erro."
                            )
                        else:
                            hard_failures += 1
                            cycle_soft_only = False
                            feedback = f"A resposta anterior foi recusada: {last_error} Corrija e devolva o contrato JSON exato."
                        self.logger.event(
                            "WARN",
                            "Resposta fora do contrato; vou retentar." if not is_soft else "Heuristica de qualidade recusou a resposta; vou retentar com feedback.",
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
                                "e marcando para revisao em vez de travar o lote.",
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
                # Um ciclo inteiro de modelos so falhou na heuristica: o payload e valido,
                # entao seguir tentando so queima tokens. Aceita e marca para revisao.
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
                    "Ciclo completo de modelos falhou apenas na heuristica de qualidade; "
                    "aceitando a traducao e marcando os IDs para revisao.",
                    batch=batch,
                    cue_ids=list(accepted["cue_ids"])[:20],
                    error=accepted["reason"][:400],
                )
                return accepted.get("raw", ""), accepted.get("meta") or {}, accepted["model"], outcome
            if len(self.unavailable_models) >= len(self.config.models):
                raise RuntimeError(
                    "Todos os modelos configurados ficaram indisponiveis para esta conta/regiao. "
                    "Verifique acesso no console do Amazon Bedrock em Model access, ou troque a lista de modelos."
                )
            if max_cycles is not None and cycle >= max_cycles:
                raise RuntimeError(f"Falha apos {cycle} ciclo(s) de modelos. Ultimo erro: {last_error}")
            if self.config.retry_forever:
                sleep = min(self.config.max_backoff, self.config.base_backoff * (2 ** min(cycle, 6)))
                self.logger.event(
                    "WARN",
                    "Todos os modelos falharam neste ciclo; retomando a fila apos backoff.",
                    batch=batch,
                    sleep=round(sleep, 1),
                )
                self.sleep_or_stop(sleep)
                continue
            raise RuntimeError(f"Falha apos tentar todos os modelos. Ultimo erro: {last_error}")

    @staticmethod
    def soft_consensus_record(soft_records: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Aceita quando modelos independentes concordam no mesmo texto suspeito.

        Se dois provedores diferentes devolvem exatamente os mesmos IDs marcados pela
        heuristica, a evidencia aponta para a heuristica errada, nao para o modelo.
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

    def add_usage(self, state: dict[str, Any], meta: dict[str, Any]) -> None:
        usage = meta.get("usage") if isinstance(meta, dict) else None
        if not isinstance(usage, dict):
            return
        total = state.setdefault("usage", {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0})
        for key in ("inputTokens", "outputTokens", "totalTokens"):
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
        "Voce e um tradutor/adaptador senior de legendas audiovisuais para portugues brasileiro. "
        "A tarefa e traduzir trechos de um arquivo SRT fornecido pelo usuario. "
        "Traduza de forma contextual, idiomatica e natural para publico brasileiro, sem literalismo duro. "
        "Preserve sentido, subtexto, humor, ironia, intensidade de palavroes, nomes proprios, tags como <i> e quebras de linha quando ajudarem a leitura. "
        "Preserve erros deliberados, nomes escritos errado, mal-entendidos e autocorrecoes quando eles sustentarem uma piada ou informacao posterior. "
        "Quando houver musica legendada, trate como legenda audiovisual: adapte o sentido para pt-BR, mantenha simbolos musicais como ♪, e nao devolva o texto original sem traduzir. "
        "Nao inclua timestamps, comentarios, markdown ou explicacoes. "
        "A resposta deve comecar com {\"translations\": e terminar com }. "
        "Retorne somente JSON valido no contrato pedido."
    )
    payload = {
        "response_format_required": 'Retorne exatamente: {"translations":[{"id":1,"text":"texto em pt-BR"}]}. A chave de topo deve ser translations.',
        "movie_context": context,
        "batching_instructions": [
            "CONTEXTO_ANTERIOR pode trazer source e ptbr ja traduzido; use para manter continuidade.",
            "LOTE_ATUAL e o unico bloco que deve ser traduzido e retornado.",
            "CONTEXTO_SEGUINTE existe apenas para desambiguar o LOTE_ATUAL.",
            "Retorne exatamente um item por id do LOTE_ATUAL, sem IDs extras e sem omitir nenhum.",
            f"Use no maximo {job.config.max_lines} linhas por cue sempre que possivel.",
            f"Procure manter cada linha com ate {job.config.max_line_length} caracteres visiveis.",
            f"Procure manter velocidade de leitura confortavel, alvo ate {job.config.max_cps:.1f} caracteres por segundo.",
            "Se a fala tiver duas pessoas com hifen, preserve essa estrutura quando natural.",
            "Para SDH/som entre colchetes, traduza o conteudo do colchete para pt-BR quando for descritivo: [laughs] -> [ri].",
            "Se houver idioma em colchetes, use forma natural em pt-BR: [speaks French] -> [fala frances].",
            "Se houver tags HTML simples, preserve as tags ao redor do texto equivalente.",
            "Tokens listados em protected_tokens_by_id devem ser copiados exatamente; eles podem ser nomes, grafias intencionais ou piadas.",
            "Nao resuma, nao censure, nao explique.",
        ],
        "protected_tokens_by_id": protected,
        "previous_context_source_and_ptbr": prev_items,
        "current_batch_translate_this": [cue.as_prompt_item(include_time=True) for cue in batch.cues],
        "next_context_source_only": next_items,
    }
    if feedback:
        payload["retry_feedback_fix_this_first"] = feedback
    return system, prompt_json(payload)


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
        "Voce e revisor senior de legendas em portugues brasileiro. "
        "Revise apenas o lote atual para naturalidade, contexto, concisao de legenda e consistencia. "
        "Preserve IDs e nao inclua comentarios. A resposta deve comecar com {\"translations\": e terminar com }. Retorne somente JSON valido."
    )
    payload = {
        "response_format_required": 'Retorne exatamente: {"translations":[{"id":1,"text":"texto revisado em pt-BR"}]}. A chave de topo deve ser translations.',
        "movie_context": context,
        "rules": [
            "Melhore frases duras ou literais.",
            "Mantenha timing de legenda: frases curtas e legiveis.",
            f"Use no maximo {job.config.max_lines} linhas por cue sempre que possivel.",
            f"Procure manter cada linha com ate {job.config.max_line_length} caracteres visiveis e ate {job.config.max_cps:.1f} cps.",
            "Preserve nomes, tags, hifens de dialogo e simbolos musicais.",
            "Tokens listados em protected_tokens_by_id devem continuar exatamente iguais.",
            "Retorne exatamente os IDs do lote atual.",
        ],
        "protected_tokens_by_id": protected,
        "previous_context_source_and_ptbr": prev_items,
        "current_batch_review_this": current,
        "next_context_source_only": next_items,
    }
    if feedback:
        payload["retry_feedback_fix_this_first"] = feedback
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

    O diretorio de estado nasce ao lado do .srt de origem. Sem este indice, uma
    legenda escolhida pelo campo de caminho absoluto virava um trabalho que roda
    mas nao aparece na tela: sem status, sem log e sem botao de parar.
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
        "last_error": state.get("last_error") or ("Processo de traducao nao esta ativo; use Retomar para continuar." if stale_running else None),
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
    # Relatorio gravado por uma versao anterior dos criterios e recalculado, senao a UI
    # mostra numeros que nao batem com as regras atuais.
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
            state["last_error"] = "Processo de traducao nao esta ativo; use Retomar para continuar."
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
    # derivar daqui evita mostrar cue que ja foi corrigido.
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
    state["log_tail"] = JsonLogger(job_dir, echo=False).tail(240)
    state["preview"] = preview_current(job_dir, state, translations)
    return state


def preview_current(job_dir: Path, state: dict[str, Any], translations: dict[str, Any]) -> dict[str, Any]:
    src = state.get("source_path")
    current = state.get("current") or {}
    if not src or not Path(src).exists():
        return {}
    try:
        doc = SrtDocument.load(Path(src))
    except Exception:
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
            self.send_html(UI_HTML)
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
        self.send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/start":
                data = self.read_json()
                source = Path(data.get("path", "")).expanduser().resolve()
                if not source.exists():
                    self.send_json({"error": "Arquivo nao encontrado."}, 400)
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
                client = BedrockClient(profile, region, int(data.get("call_timeout") or 60), JsonLogger(echo=False))
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
                    "Runner terminou com excecao nao tratada.",
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

    /* ---- botao de ajuda e popover ---- */
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
    #help .pin-hint { margin: 8px 0 0; font-size: 11px; color: var(--muted); }

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

    /* ---- cartao de resultado ---- */
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
    @media (max-width: 980px) {
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

        <label class="field-label" for="path">Ou caminho absoluto <button class="info" data-help="path" aria-label="Ajuda">i</button></label>
        <input id="path" placeholder="/caminho/arquivo.srt">

        <div class="row">
          <div>
            <label class="field-label" for="profile">AWS profile <button class="info" data-help="profile" aria-label="Ajuda">i</button></label>
            <input id="profile" value="default">
          </div>
          <div>
            <label class="field-label" for="region">Regiao <button class="info" data-help="region" aria-label="Ajuda">i</button></label>
            <input id="region" value="us-east-1">
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
            <label class="field-label" for="maxLines">Maximo de linhas <button class="info" data-help="maxLines" aria-label="Ajuda">i</button></label>
            <input id="maxLines" type="number" min="1" max="4" value="2">
          </div>
          <div>
            <label class="field-label" for="lineLength">Caracteres por linha <button class="info" data-help="lineLength" aria-label="Ajuda">i</button></label>
            <input id="lineLength" type="number" min="24" max="60" value="42">
          </div>
        </div>
        <div class="row">
          <div>
            <label class="field-label" for="maxCps">CPS maximo <button class="info" data-help="maxCps" aria-label="Ajuda">i</button></label>
            <input id="maxCps" type="number" min="8" max="30" step="0.5" value="17">
          </div>
          <div>
            <label class="field-label" for="qcRounds">Rodadas de reparo QC <button class="info" data-help="qcRounds" aria-label="Ajuda">i</button></label>
            <input id="qcRounds" type="number" min="0" max="6" value="2">
          </div>
        </div>

        <label class="toggle-row"><input id="retryForever" type="checkbox" checked> <span>Retentar ate concluir ou parar manualmente</span> <button class="info" data-help="retryForever" aria-label="Ajuda">i</button></label>
        <label class="toggle-row"><input id="retryQc" type="checkbox" checked> <span>Refazer automaticamente cues com erro duro de QC</span> <button class="info" data-help="retryQc" aria-label="Ajuda">i</button></label>
        <label class="toggle-row"><input id="contextPass" type="checkbox" checked> <span>Criar guia de contexto antes de traduzir</span> <button class="info" data-help="contextPass" aria-label="Ajuda">i</button></label>
        <label class="toggle-row"><input id="polishPass" type="checkbox"> <span>Rodar passe final de revisao</span> <button class="info" data-help="polishPass" aria-label="Ajuda">i</button></label>
        <label class="toggle-row"><input id="forceNew" type="checkbox"> <span>Criar trabalho novo mesmo se ja existir estado</span> <button class="info" data-help="forceNew" aria-label="Ajuda">i</button></label>

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
    </section>
  </main>
  <script>
    const HELP = {
      painelEntrada: {
        t: "Painel de entrada",
        p: "Aqui voce escolhe a legenda e como ela vai ser traduzida. Os valores padrao ja funcionam bem: na pratica voce so precisa conferir a legenda selecionada e clicar em Iniciar ou retomar.",
        e: "Mexer nos numeros so faz sentido se algo deu errado, por exemplo diminuir Legendas por lote quando um modelo insiste em devolver JSON quebrado."
      },
      refresh: {
        t: "Atualizar",
        p: "Rele a pasta base procurando arquivos .srt e recarrega a lista de trabalhos. Nao inicia, nao para e nao altera nenhuma traducao: e so uma releitura do disco.",
        e: "Voce acabou de baixar a legenda de outro filme e copiou para a pasta. Ela nao aparece no seletor porque a pagina foi carregada antes. Clique em Atualizar e ela aparece."
      },
      file: {
        t: "Legenda encontrada",
        p: "Lista os arquivos .srt achados na pasta base do servidor. A ordem prioriza o arquivo que esta na raiz da pasta e evita deixar versoes SDH em primeiro lugar, porque SDH traz descricoes de som que nem sempre voce quer.",
        e: "Numa pasta com <code>Filme.srt</code> e <code>Subs/Filme.en.SDH.srt</code>, o primeiro da lista sera <code>Filme.srt</code>."
      },
      path: {
        t: "Ou caminho absoluto",
        p: "Use quando a legenda estiver fora da pasta base. Se este campo estiver preenchido, ele manda e o seletor acima e ignorado. Deixe vazio para usar o seletor.",
        e: "<code>/Users/voce/Downloads/Outro.Filme.2024.srt</code>. Tambem funciona apontar para uma legenda <code>.INCOMPLETO.srt</code> ja gerada: ele reconhece o trabalho antigo e continua de onde parou."
      },
      profile: {
        t: "AWS profile",
        p: "Nome do perfil configurado no AWS CLI, em <code>~/.aws/credentials</code>. E com essa credencial que o script chama o Bedrock.",
        e: "Se estiver errado, a primeira chamada falha com erro de credencial. Rode <code>aws configure list-profiles</code> no terminal para ver os nomes disponiveis."
      },
      region: {
        t: "Regiao",
        p: "Regiao AWS onde o Bedrock sera chamado. O catalogo de modelos muda por regiao: um modelo que existe em uma pode nao existir em outra.",
        e: "<code>us-east-1</code> e onde os modelos Claude e Nova responderam nesta conta. Trocar para uma regiao sem esses modelos faz todos falharem com ResourceNotFound."
      },
      models: {
        t: "Modelos em ordem de fallback",
        p: "Um ID por linha, na ordem em que serao tentados. Se o primeiro falhar todas as tentativas, ele cai para o segundo, e assim por diante. IDs que comecam com <code>us.</code> sao inference profiles, exigidos pelo Bedrock para varios modelos novos.",
        e: "A ordem padrao poe o Claude Sonnet primeiro por ser o melhor tradutor, e deixa modelos menores embaixo como rede de seguranca. Esse mesmo fallback e o que permite o mecanismo de consenso: quando dois modelos diferentes devolvem o mesmo texto suspeito, a validacao aceita em vez de travar."
      },
      batchSize: {
        t: "Legendas por lote",
        p: "Quantas legendas vao em cada chamada ao modelo. Lote maior da mais contexto e usa menos chamadas, mas gera resposta mais longa, com mais risco de JSON cortado ou quebrado.",
        e: "Com 28 legendas por lote, um filme de 2435 legendas vira 87 lotes, cerca de 1,5 minuto de filme por chamada. Se o log mostrar muito erro de JSON, baixe para 20."
      },
      batchChars: {
        t: "Caracteres por lote",
        p: "Teto de caracteres do lote. Ele fecha o lote antes de atingir Legendas por lote se as falas forem longas. Serve para o tamanho da resposta nao explodir em cenas de dialogo denso.",
        e: "Com 28 legendas e 4300 caracteres, uma cena de falas longas pode fechar o lote em 20 legendas ao bater o limite de caracteres."
      },
      attempts: {
        t: "Tentativas por modelo",
        p: "Quantas vezes insistir no mesmo modelo antes de passar para o proximo da lista. Cada nova tentativa leva no prompt o motivo da recusa anterior, entao ela nao e apenas uma repeticao.",
        e: "Com 3, o Claude Sonnet tenta 3 vezes; se as 3 falharem, a vez passa para o Claude Haiku."
      },
      timeout: {
        t: "Timeout por chamada",
        p: "Segundos de espera por uma resposta antes de considerar a chamada perdida. Lote grande e modelo lento precisam de mais tempo.",
        e: "240s cobre um lote de 28 legendas com folga. Abaixo de 120s, lotes grandes podem ser cortados no meio e retentados a toa."
      },
      maxLines: {
        t: "Maximo de linhas",
        p: "Quantas linhas uma legenda pode ter na tela. O padrao da industria e 2: com 3 ou mais, a legenda cobre a imagem e fica cansativa.",
        e: "Uma frase longa vira duas linhas equilibradas em vez de uma linha unica atravessando a tela inteira."
      },
      lineLength: {
        t: "Caracteres por linha",
        p: "Comprimento maximo de cada linha, contando so o texto visivel (tags como <code>&lt;i&gt;</code> nao contam). 42 e o valor usado nos guias de legendagem para portugues.",
        e: "<code>Voce sempre diz isso quando algo esta errado.</code> tem 44 caracteres, entao e quebrado em duas linhas."
      },
      maxCps: {
        t: "CPS maximo",
        p: "Caracteres por segundo, ou seja, velocidade de leitura. Ate 17 e confortavel. Este limite nao e cobrado de forma absoluta: o aviso so aparece quando a traducao ficou mais de 15 por cento mais lenta de ler do que o original ja era.",
        e: "Muita legenda comercial ja passa de 17 cps na fonte. Nesta legenda, 48 por cento dos cues em ingles ja eram assim. Se cobrassemos o limite absoluto, metade do filme viraria aviso e voce nao conseguiria achar o problema de verdade. Original a 25 cps e traducao a 26 cps nao avisa; de 15 para 30 cps avisa."
      },
      qcRounds: {
        t: "Rodadas de reparo QC",
        p: "Depois de traduzir tudo, o QC procura erros duros. Este numero diz quantas vezes ele pode refazer os lotes afetados antes de desistir e marcar o arquivo como incompleto.",
        e: "Com 2 rodadas: se um cue continuar com tag <code>&lt;i&gt;</code> quebrada depois de 2 tentativas, o arquivo final sai como <code>.INCOMPLETO.srt</code> em vez de <code>.OK.srt</code>."
      },
      retryForever: {
        t: "Retentar ate concluir ou parar manualmente",
        p: "Ligado, o trabalho nunca desiste sozinho de um lote: ele espera com backoff exponencial e volta a tentar ate voce clicar em Parar. Desligado, o lote que falhar e marcado com erro e a traducao segue adiante.",
        e: "O Bedrock comeca a limitar chamadas por excesso de uso. Ligado, ele espera e continua sozinho. Desligado, aquele trecho do filme fica sem traducao no arquivo final."
      },
      retryQc: {
        t: "Refazer cues com erro duro de QC",
        p: "Ao terminar, refaz automaticamente os lotes que contem cues reprovados no QC. Desligue apenas se voce quiser ver o resultado bruto do modelo, sem nenhuma correcao posterior.",
        e: "Um cue voltou com o marcador musical perdido. Com esta opcao ligada, o lote inteiro daquele cue e refeito antes de fechar o arquivo."
      },
      contextPass: {
        t: "Criar guia de contexto antes de traduzir",
        p: "Faz uma chamada extra no comeco que le amostras da legenda inteira e monta um guia do filme: tom, registro, nomes recorrentes e formas de tratamento. Esse guia acompanha todos os lotes e e o que mantem a traducao coerente do inicio ao fim.",
        e: "E o que evita que o mesmo personagem seja tratado por voce no comeco e por tu no fim, ou que um apelido mude de traducao no meio do filme. Custa uma chamada a mais e vale a pena."
      },
      polishPass: {
        t: "Rodar passe final de revisao",
        p: "Uma segunda passada por todos os lotes, agora revisando a traducao ja feita em vez de traduzir do zero. Praticamente dobra o tempo e o custo.",
        e: "E o passe que tende a pegar deslizes de sentido, como um <code>chat room</code> traduzido como grupo de e-mail. Use quando a legenda for para valer e o tempo nao importar."
      },
      forceNew: {
        t: "Criar trabalho novo mesmo se ja existir estado",
        p: "Ignora todo o progresso salvo e comeca do zero. Cuidado: o trabalho anterior daquela legenda deixa de ser continuado.",
        e: "Voce trocou a lista de modelos e quer o filme inteiro traduzido pelo modelo novo, em vez de aproveitar o que ja foi feito pelo antigo."
      },
      doctor: {
        t: "Testar Bedrock",
        p: "Faz uma chamada real e minima (pede so a palavra OK) para CADA modelo da lista, usando o profile e a regiao preenchidos aqui. Serve como checagem de pre-voo antes de gastar tempo num filme inteiro.",
        e: "Ele garante tres coisas: a credencial do profile e valida, a regiao responde, e a sua conta tem acesso liberado aquele modelo. Ele NAO avalia qualidade de traducao. Se voltar <code>AccessDeniedException</code>, o modelo precisa ser liberado no console da AWS em Amazon Bedrock, Model access."
      },
      start: {
        t: "Iniciar ou retomar",
        p: "O botao principal. Se ja existe trabalho salvo para a legenda escolhida, ele continua exatamente de onde parou. Se nao existe, cria um trabalho novo. E seguro clicar duas vezes: um trabalho ja rodando nao e duplicado.",
        e: "Voce traduziu 60 por cento ontem e fechou tudo. Hoje escolhe a mesma legenda e clica aqui: ele reconhece os lotes ja prontos e comeca do lote seguinte, sem retraduzir nada."
      },
      resumeBtn: {
        t: "Retomar selecionado",
        p: "Retoma o trabalho que estiver marcado na lista Trabalhos, e nao a legenda do seletor. Util quando o arquivo de origem nao esta mais na pasta base ou quando ha varios trabalhos e voce quer continuar um especifico.",
        e: "A lista mostra um trabalho com status <code>stalled</code>, que significa progresso salvo mas processo morto. Selecione o cartao dele e clique aqui para voltar de onde parou."
      },
      stop: {
        t: "Parar",
        p: "Pede uma parada limpa. A chamada que ja esta no ar termina, o progresso e gravado em disco e o trabalho para com status <code>stopped</code>. Nada do que ja foi traduzido se perde.",
        e: "A parada nao e instantanea: ele pode levar alguns segundos ate a chamada em andamento voltar. Depois e so clicar em Iniciar ou retomar para continuar."
      },
      jobs: {
        t: "Trabalhos",
        p: "Todo trabalho ja iniciado nesta pasta, com status e progresso. Clique num cartao para ver o status, o log e o lote atual dele no painel da direita.",
        e: "Status possiveis: <code>running</code> em execucao, <code>stopped</code> parado por voce, <code>stalled</code> progresso salvo mas o processo morreu, <code>complete</code> terminou sem erro, <code>incomplete</code> terminou com pendencias, <code>failed</code> falhou."
      },
      status: {
        t: "Status",
        p: "Situacao do trabalho selecionado, atualizada sozinha a cada 2,5 segundos. Voce nao precisa recarregar a pagina.",
        e: "Se aparecer <code>stalled</code>, o progresso esta salvo mas ninguem esta traduzindo: o processo do servidor caiu. Clique em Retomar selecionado. Se aparecer <code>lote insistindo</code>, um lote entrou no segundo ciclo de modelos e vale olhar o log."
      },
      mProgress: {
        t: "Progresso",
        p: "Porcentagem de legendas com traducao aceita, e nao porcentagem de lotes. Uma legenda so conta aqui depois de passar por toda a validacao.",
        e: "1736 de 2435 legendas prontas mostram 71 por cento."
      },
      mBatch: {
        t: "Lote",
        p: "Qual lote esta sendo traduzido agora e quantos lotes o filme tem no total. Mostra um traco quando nao ha lote em andamento.",
        e: "<code>63/87</code> significa que ele esta no lote 63 de 87."
      },
      mModel: {
        t: "Modelo atual",
        p: "Qual modelo esta atendendo o lote neste momento. Se ele mudar sozinho para um modelo mais abaixo na lista, e porque o de cima falhou as tentativas.",
        e: "Ver <code>nova-pro</code> aqui quando o topo da lista e o Sonnet indica que o Sonnet falhou e o fallback entrou."
      },
      mErrors: {
        t: "Erros QC",
        p: "Legendas reprovadas em checagem dura: texto vazio, recusa do modelo, tag quebrada, marcador musical perdido, token protegido ausente. Enquanto este numero for maior que zero, o arquivo final sai como <code>.INCOMPLETO.srt</code>.",
        e: "Este numero nao inclui avisos de legibilidade como linha longa ou leitura rapida. Aviso nao bloqueia o arquivo."
      },
      mReview: {
        t: "Revisar",
        p: "Legendas que a heuristica marcou como suspeitas de nao terem sido traduzidas, mas que dois modelos diferentes devolveram iguais. Nesse caso a evidencia aponta para a heuristica errada, e o texto e aceito com marca de revisao em vez de travar o lote.",
        e: "Refrao de musica como <code>Guli guli guli guli ram sam sam</code> deve mesmo ficar igual ao original. Ele entra aqui para voce dar uma conferida, mas nao impede o <code>.OK.srt</code>."
      },
      arquivos: {
        t: "Arquivos gerados",
        p: "Tudo fica ao lado da legenda original, na mesma pasta do filme. Sao tres coisas: a legenda traduzida, um arquivo sidecar ao lado dela, e a pasta de estado <code>.srt_translator_jobs/</code>.",
        e: "A legenda traduzida tem o sufixo que indica o resultado: <code>.pt-BR.EM_ANDAMENTO.srt</code> enquanto roda, <code>.pt-BR.OK.srt</code> quando termina sem erro duro, <code>.pt-BR.INCOMPLETO.srt</code> quando sobrou pendencia. Todos sao SRT normais em UTF-8, com o mesmo numero de legendas e os mesmos tempos do original: da para abrir direto no VLC. Ao terminar, os arquivos antigos daquele trabalho sao apagados para sobrar so a legenda boa. O <code>.translator-state.json</code> ao lado e o que permite voce arrastar a legenda de volta e ele reconhecer o trabalho. Dentro de <code>.srt_translator_jobs/</code> ficam o estado, as traducoes por id, o log em jsonl e o <code>quality_report.json</code> com o detalhe de cada problema."
      },
      log: {
        t: "Log",
        p: "Eventos do trabalho em ordem cronologica, gravados tambem em disco em <code>events.jsonl</code>. Amarelo e aviso, vermelho e erro, verde e lote concluido.",
        e: "Uma sequencia saudavel e: Iniciando traducao do lote N, Chamando Bedrock, Resposta validada, Lote N concluido. Aviso repetido no mesmo lote indica que a validacao esta recusando a resposta."
      },
      preview: {
        t: "Lote atual",
        p: "As legendas do lote em andamento. Para cada uma aparece o texto original em cinza e, abaixo, a traducao quando ela ja existe.",
        e: "E aqui que voce ve a qualidade saindo em tempo real, sem precisar abrir o arquivo."
      }
    };

    const defaultModels = [
      "us.anthropic.claude-sonnet-4-6",
      "us.anthropic.claude-haiku-4-5-20251001-v1:0",
      "us.amazon.nova-pro-v1:0",
      "amazon.nova-pro-v1:0",
      "mistral.mistral-large-3-675b-instruct",
      "amazon.nova-lite-v1:0",
      "us.amazon.nova-lite-v1:0",
      "mistral.mistral-small-2402-v1:0",
      "meta.llama3-70b-instruct-v1:0"
    ];
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
        (h.e ? `<div class="ex"><b>Na pratica</b>${h.e}</div>` : "") +
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
        toast("Nao consegui copiar", "O navegador bloqueou o acesso a area de transferencia. Selecione o texto manualmente.", "warn");
      }
    }
    function busy(sel, on) {
      const b = document.querySelector(sel);
      if (!b) return;
      b.classList.toggle("busy", !!on);
      b.disabled = !!on;
    }

    /* ---------- api ---------- */
    async function api(path, opts) {
      let res, data;
      try {
        res = await fetch(path, opts);
      } catch (e) {
        throw new Error("Nao consegui falar com o servidor local. Ele ainda esta rodando no terminal?");
      }
      try { data = await res.json(); } catch (e) { throw new Error("Resposta invalida do servidor (HTTP " + res.status + ")."); }
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
        polish_pass: document.querySelector("#polishPass").checked,
        force_new: document.querySelector("#forceNew").checked
      };
    }

    /* ---------- acoes ---------- */
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
        toast("Traducao concluida", `${name} — arquivo pronto em ${fileName(job.output)}`, "success", {copy: job.output, timeout: 0});
      } else if (job.status === "incomplete") {
        toast("Terminou com pendencias", `${name} — saiu como ${fileName(job.output)}. Repasse esse arquivo e clique em Iniciar ou retomar para ele completar o que faltou.`, "warn", {timeout: 0});
      } else if (job.status === "failed") {
        toast("O trabalho falhou", job.last_error || name, "error", {timeout: 0});
      } else if (job.status === "stalled") {
        toast("Processo parou sozinho", `${name} — o progresso esta salvo. Clique em Retomar selecionado para continuar.`, "warn", {timeout: 0});
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
      // refresh separado: se ele falhar, o trabalho JA comecou e dizer
      // "nao consegui iniciar" seria mentira.
      try { await refreshJobs(); await refreshJob(); }
      catch (e) { toast("Trabalho rodando, mas a tela nao atualizou", e.message, "warn"); }
    }
    async function resumeJob() {
      if (!selectedJob) { toast("Nenhum trabalho selecionado", "Clique num cartao na lista Trabalhos primeiro.", "warn"); return; }
      const cfg = formConfig();
      cfg.job_id = selectedJob;
      busy("#resume", true);
      try {
        const data = await api("/api/resume", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(cfg)});
        selectedJob = data.job_id;
        lastStatus[data.job_id] = "running";
        toast("Trabalho retomado", "Ele continua do ponto em que parou; nada ja traduzido sera refeito.", "success");
      } finally { busy("#resume", false); }
      try { await refreshJobs(); await refreshJob(); }
      catch (e) { toast("Trabalho rodando, mas a tela nao atualizou", e.message, "warn"); }
    }
    async function stopJob() {
      if (!selectedJob) return;
      busy("#stop", true);
      try {
        await api("/api/stop", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({job_id: selectedJob})});
        toast("Parada solicitada", "A chamada em andamento vai terminar antes de parar. O progresso ja esta salvo.", "info");
        await refreshJob();
      } finally { busy("#stop", false); }
    }
    async function runDoctor() {
      const cfg = formConfig();
      busy("#doctor", true);
      const pending = toast("Testando Bedrock...", "Fazendo uma chamada minima para cada modelo da lista.", "info", {timeout: 0});
      try {
        const data = await api("/api/doctor", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(cfg)});
        pending.remove();
        const bad = data.results.filter(r => !r.ok);
        if (data.ok_count === 0) {
          toast("Nenhum modelo respondeu", `Confira o profile (${cfg.profile}) e a regiao (${cfg.region}). Primeiro erro: ` + ((bad[0] && bad[0].error) || "").slice(0, 220), "error", {timeout: 0});
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
        box.innerHTML = `<h3>Traducao concluida</h3>
          <div class="sub">${job.total_cues || 0} legendas traduzidas, nenhum erro duro de QC. O arquivo esta pronto para usar.</div>
          <div class="filebox"><div class="fb-main"><div class="fb-name">${escapeHtml(fileName(out))}</div>
            <div class="fb-dir">${escapeHtml(dirName(out))}</div></div>
            <button data-path="${escapeHtml(out)}">Copiar caminho</button></div>
          <ul>
            <li>E um SRT normal em UTF-8: abra no VLC pelo menu Legenda, Adicionar arquivo de legenda.</li>
            <li>Mesma quantidade de legendas e mesmos tempos do original, entao sincroniza igual.</li>
            ${job.review_cues ? `<li><b>${job.review_cues}</b> legendas foram aceitas por consenso entre modelos e valem uma conferida.</li>` : ""}
            ${q.warning_cues ? `<li>${q.warning_cues} avisos de legibilidade no relatorio de qualidade. Avisos nao bloqueiam o arquivo.</li>` : ""}
          </ul>`;
      } else if (job.status === "incomplete") {
        box.className = "result show warn";
        box.innerHTML = `<h3>Terminou com pendencias</h3>
          <div class="sub">${job.last_error || "Sobraram legendas sem traducao aceita."}</div>
          <div class="filebox"><div class="fb-main"><div class="fb-name">${escapeHtml(fileName(out))}</div>
            <div class="fb-dir">${escapeHtml(dirName(out))}</div></div>
            <button data-path="${escapeHtml(out)}">Copiar caminho</button></div>
          <ul>
            <li>O sufixo <b>INCOMPLETO</b> no nome e o aviso de que faltou coisa.</li>
            <li>As legendas que faltaram ficam marcadas dentro do arquivo com <code>[TRADUCAO_PENDENTE]</code>.</li>
            <li>Para completar: deixe esta legenda selecionada e clique em <b>Iniciar ou retomar</b>. Ele refaz so o que faltou e, ao terminar, troca o arquivo por um <code>.OK.srt</code>.</li>
          </ul>`;
      } else if (job.status === "failed") {
        box.className = "result show bad";
        box.innerHTML = `<h3>O trabalho falhou</h3>
          <div class="sub">${escapeHtml(job.last_error || "Erro nao identificado.")}</div>
          <ul><li>Clique em <b>Testar Bedrock</b> para checar credencial, regiao e acesso aos modelos.</li>
          <li>Nada do que ja foi traduzido se perdeu: depois de resolver, use <b>Retomar selecionado</b>.</li></ul>`;
      } else if (job.status === "stalled") {
        box.className = "result show warn";
        box.innerHTML = `<h3>O processo parou sozinho</h3>
          <div class="sub">O progresso esta salvo em disco, mas ninguem esta traduzindo agora. Normalmente o servidor foi encerrado.</div>
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
        pathLine("Relatorio", job.quality_report_path) +
        `<div class="pathline"><span class="pk">Numeros</span><span class="pv">${done}/${total} traduzidas &middot; ${job.pending_cues || q.pending_cues || 0} pendentes &middot; ${job.error_cues || 0} erros &middot; ${job.warning_cues || q.warning_cues || 0} avisos${usage.totalTokens ? ` &middot; ${Number(usage.totalTokens).toLocaleString("pt-BR")} tokens` : ""}</span></div>`;

      const err = document.querySelector("#lastError");
      const showErr = job.last_error && job.status !== "complete";
      err.className = "alert-line" + (showErr ? " show" : "");
      err.textContent = showErr ? job.last_error : "";

      const log = document.querySelector("#log");
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
      log.innerHTML = (job.log_tail || []).map(e => {
        const lvl = e.level || "INFO";
        const good = /concluido|sucesso|validada|pronto/i.test(e.message || "") ? " good" : "";
        return `<div class="ln ${lvl}${good}"><span class="ts">${escapeHtml((e.ts || "").slice(11, 19))}</span> ${escapeHtml(e.message || "")}${escapeHtml(formatEvent(e))}</div>`;
      }).join("");
      if (atBottom) log.scrollTop = log.scrollHeight;

      const preview = document.querySelector("#preview");
      const items = (job.preview && job.preview.items) || [];
      preview.innerHTML = items.length ? items.map(renderCue).join("") : "<div class='empty'>Sem lote ativo no momento.</div>";
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
        toast("Lista atualizada", `${n} legenda(s) encontradas na pasta base. Nenhuma traducao foi alterada.`, "info", {timeout: 4000});
      } catch (e) { toast("Falha ao atualizar", e.message, "error"); }
      finally { busy("#refresh", false); }
    };
    document.querySelector("#start").onclick = () => startJob().catch(err => toast("Nao consegui iniciar", err.message, "error"));
    document.querySelector("#resume").onclick = () => resumeJob().catch(err => toast("Nao consegui retomar", err.message, "error"));
    document.querySelector("#stop").onclick = () => stopJob().catch(err => toast("Nao consegui parar", err.message, "error"));
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
    raise RuntimeError("Nao encontrei porta livre para a UI.")


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
    print(f"AWS CLI: {aws or 'nao encontrado'}")
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
    client = BedrockClient(args.profile, args.region, args.call_timeout, logger)
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


def qc_cli(args: argparse.Namespace) -> int:
    translated = Path(args.translated).expanduser().resolve()
    source = Path(args.source).expanduser().resolve() if args.source else discover_resume_source(translated)
    if not source.exists():
        print("Fonte nao encontrada. Passe --source /caminho/original.srt", file=sys.stderr)
        return 1
    src_doc = SrtDocument.load(source)
    tr_doc = SrtDocument.load(translated)
    translations: dict[str, dict[str, Any]] = {}
    for idx, cue in enumerate(src_doc.cues):
        if idx < len(tr_doc.cues):
            translations[str(cue.id)] = {"status": "ok", "text": tr_doc.cues[idx].text}
        else:
            translations[str(cue.id)] = {"status": "error", "text": "", "error": "cue ausente na traducao"}
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
        print(f"AVISO: fonte tem {len(src_doc.cues)} cues; traducao tem {len(tr_doc.cues)} cues.")
    for cue_report in report["cues"][:20]:
        issue_text = "; ".join(f"{i['severity']}:{i['code']}" for i in cue_report["issues"])
        print(f"#{cue_report['id']} {cue_report['time']} {issue_text}")
    if len(report["cues"]) > 20:
        print(f"... mais {len(report['cues']) - 20} cues com avisos/erros no relatorio.")
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

    # Falha de heuristica precisa ser soft e carregar o payload, para que o lote possa
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
        raise AssertionError("heuristica deveria ter recusado")
    except SoftContractError as exc:
        assert exc.soft and exc.cue_ids == [2] and exc.payload["1"] == "Olá, meu amigo."
    try:
        validate_translation_payload({"translations": [{"id": 1, "text": "Olá."}]}, vocable_batch)
        raise AssertionError("IDs faltando deveria ter recusado")
    except ContractError as exc:
        assert not exc.soft, "quebra estrutural nao pode virar soft"

    # Consenso exige modelos distintos concordando nos mesmos IDs.
    rec_a = {"model": "modelo-a", "cue_ids": (2,), "payload": {"2": "x"}, "reason": "r", "raw": "", "meta": {}}
    rec_b = {"model": "modelo-b", "cue_ids": (2,), "payload": {"2": "y"}, "reason": "r", "raw": "", "meta": {}}
    assert TranslatorJob.soft_consensus_record([rec_a, rec_b]) is not None
    assert TranslatorJob.soft_consensus_record([rec_a, dict(rec_a)]) is None
    assert TranslatorJob.soft_consensus_record([rec_a, {**rec_b, "cue_ids": (3,)}]) is None

    # Aceito por consenso vira aviso no QC, nao erro duro.
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

    # CPS e cobrado como regressao contra a fonte: legenda comercial ja costuma passar do
    # limite, e marcar metade do filme esconderia o que a traducao de fato piorou.
    slow_source = SrtCue(id=1, number="1", timing="00:00:00,000 --> 00:00:04,000", text="Hi.")
    verbose = {"status": "ok", "text": "Uma frase muito comprida que ninguem consegue ler nesse tempo todo aqui."}
    codes_regression = {
        item["code"]: item["severity"]
        for item in cue_quality_issues(slow_source, verbose, max_lines=2, max_line_length=200, max_cps=17.0)
    }
    assert codes_regression.get("high_cps") == "warning", codes_regression
    fast_source = SrtCue(id=2, number="2", timing="00:00:00,000 --> 00:00:01,000", text="This line is already far too fast to read.")
    matched = {"status": "ok", "text": "Essa linha ja era rapida demais na fonte."}
    codes_inherited = {
        item["code"]: item["severity"]
        for item in cue_quality_issues(fast_source, matched, max_lines=2, max_line_length=200, max_cps=17.0)
    }
    assert codes_inherited.get("high_cps_inherited") == "info", codes_inherited
    assert "high_cps" not in codes_inherited, codes_inherited
    inherited_report = build_quality_report([fast_source], {"2": matched}, max_lines=2, max_line_length=200, max_cps=17.0)
    assert inherited_report["summary"]["warning_cues"] == 0, inherited_report["summary"]
    assert inherited_report["report_version"] == QUALITY_REPORT_VERSION

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
    print(f"saida: {state.get('final_output_path') or state.get('last_written_output')}")
    if state.get("last_error"):
        print(f"ultimo erro: {state.get('last_error')}")
    return 0 if state.get("status") == "complete" else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=APP_NAME)
    sub = parser.add_subparsers(dest="cmd")

    p_translate = sub.add_parser("translate", help="traduz um arquivo .srt")
    p_translate.add_argument("input", help="arquivo .srt de entrada ou saida incompleta com sidecar")
    add_common_job_args(p_translate)
    p_translate.set_defaults(func=translate_cli)

    p_ui = sub.add_parser("ui", help="abre a UI local")
    p_ui.add_argument("--host", default="127.0.0.1")
    p_ui.add_argument("--port", type=int, default=8765)
    p_ui.add_argument("--base", default=str(Path.cwd()), help="pasta base para listar .srt e trabalhos")
    p_ui.set_defaults(func=serve_ui)

    p_models = sub.add_parser("list-models", help="lista inference profiles uteis do Bedrock")
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
    p_qc.add_argument("--output", default=None, help="salva relatorio JSON")
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
    parser.add_argument("--polish-pass", action="store_true", help="roda um segundo passe de revisao")
    parser.add_argument("--no-retry-qc-issues", action="store_true", help="nao refaz automaticamente cues que falham no QC duro")
    parser.add_argument("--qc-repair-rounds", type=int, default=2)
    parser.add_argument("--max-lines", type=int, default=2)
    parser.add_argument("--max-line-length", type=int, default=42)
    parser.add_argument("--max-cps", type=float, default=17.0)
    parser.add_argument("--max-cues", type=int, default=None, help="debug: traduz so os N primeiros cues")
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
