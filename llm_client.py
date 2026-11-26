#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Cliente Unificado de IA con Respaldo Multi-Proveedor y Multi-Clave
==================================================================
Soporta Google Gemini (`google-genai`), OpenRouter (`chat/completions`) y
NVIDIA NIM (`integrate.api.nvidia.com`), permitiendo configurar una o más
API keys por proveedor y conmutar automáticamente cuando un modelo o proveedor
alcanza su cuota diaria (429), se satura (500/503) o falla por autenticación.
"""

from dataclasses import dataclass
import json
import os
import re
import ssl
import sys
import time
from typing import Any, Callable, Optional
import urllib.error
import urllib.request

import requests

try:
    from google import genai
except ImportError:
    genai = None


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_CHAT_URL = f"{OPENROUTER_BASE_URL}/chat/completions"
OPENROUTER_KEY_URL = f"{OPENROUTER_BASE_URL}/key"

NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_CHAT_URL = f"{NVIDIA_BASE_URL}/chat/completions"
NVIDIA_MODELS_URL = f"{NVIDIA_BASE_URL}/models"
NVIDIA_STREAM_TIMEOUT_SECONDS = 75.0

# Modelos Gemini en orden de preferencia (Google AI Studio)
PREFERRED_GEMINI_MODELS = [
    "gemini-3.8-flash",       # Modelo principal (rápido, ágil y de alta cuota)
    "gemini-4-hydrogen",      # Modelo de respaldo (alta capacidad de análisis y precisión)
    "gemini-3.6-flash",       # Respaldo confiable adicional
    "gemini-3.5-flash-lite",  # Respaldo liviano
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-4-argon",
    "gemini-4-neon",
]

# Modelos gratuitos en OpenRouter adecuados para análisis jurídico en español + JSON
PREFERRED_OPENROUTER_MODELS = [
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "baidu/ernie-5.5-flash-preview:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3.5-super-vl:free",
    "inclusionai/ling-3.2-flash:free",
    "openrouter/free",
]

# Modelos en NVIDIA NIM (build.nvidia.com) adecuados para análisis jurídico en español + JSON
PREFERRED_NVIDIA_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "z-ai/glm-5.3-flash",
    "minimaxai/minimax-m3.1-flash",
    "google/gemma-3-12b-it",
    "nvidia/nemotron-3.5-super-vl-120b-a12b",
    "moonshotai/kimi-k3.1-pro",
    "mistralai/mistral-large-4-1t-a52b",
]

PLACEHOLDER_KEYS = {
    "",
    "none",
    "null",
    "tu_clave_api_aqui",
    "tu_clave_openrouter_aqui",
    "tu_clave_nvidia_aqui",
}


class LimiteDiarioExcedidoError(Exception):
    """Excepción lanzada cuando se agota la cuota en todos los proveedores/claves activos."""


class ProviderHTTPError(Exception):
    """Error HTTP normalizado para respuestas de proveedores REST (OpenRouter)."""

    def __init__(self, status_code: int, message: str, provider: str = "openrouter", headers: Optional[dict] = None):
        self.status_code = status_code
        self.message = message
        self.provider = provider
        self.headers = headers or {}
        super().__init__(f"HTTP {status_code} ({provider}): {message}")


@dataclass
class ModelSlot:
    """Representa una combinación concreta de proveedor, clave y modelo."""

    provider: str
    model: str
    key_index: int
    key_label: str
    is_free_openrouter: bool = False
    verified: bool = False
    exhausted: bool = False
    consecutive_busy: int = 0
    demoted: bool = False

    @property
    def display_name(self) -> str:
        prefix = f"{self.provider}:" if self.provider in ("openrouter", "nvidia") else ""
        suffix = f" [{self.key_label}]" if self.key_index > 0 else ""
        return f"{prefix}{self.model}{suffix}"


def collect_api_keys(primary_var: str, extra_vars: tuple[str, ...] = (), inline_default: str = "") -> list[str]:
    """
    Lee una o más API keys desde variables de entorno (separadas por comas,
    punto y coma o saltos de línea) y elimina duplicados/marcadores vacíos.
    """
    raw_values = []
    for var_name in (primary_var, *extra_vars):
        val = os.environ.get(var_name, "").strip()
        if val:
            raw_values.append(val)
    if not raw_values and inline_default:
        raw_values.append(inline_default.strip())

    keys: list[str] = []
    seen: set[str] = set()
    for block in raw_values:
        for item in re.split(r"[,;\r\n]+", block):
            cleaned = item.strip()
            if not cleaned or cleaned.lower() in PLACEHOLDER_KEYS:
                continue
            if cleaned not in seen:
                seen.add(cleaned)
                keys.append(cleaned)
    return keys


def _short_error(exc: Exception, limit: int = 95) -> str:
    """Formatea un resumen de error en una sola línea legible para logs."""
    msg = re.sub(r"\s+", " ", str(exc)).strip()
    if len(msg) > limit:
        msg = msg[: limit - 3] + "..."
    name = type(exc).__name__
    return f"{name}: {msg}" if msg else name


def _is_transient_probe_error(exc: Exception) -> bool:
    """Indica si un fallo durante la verificación inicial merece un reintento breve."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(
        token in text
        for token in ("servererror", "500", "502", "503", "504", "unavailable", "timeout", "timed out")
    )


def parse_json_list(raw_text: str) -> Optional[list[Any]]:
    """
    Extrae una lista JSON desde la respuesta cruda de un modelo.
    Soporta bloques <think>...</think>, fences markdown, arreglos envueltos en dict
    y texto previo con corchetes como [sic].
    """
    if not raw_text:
        return None

    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL | re.IGNORECASE).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    def _unwrap(parsed: Any) -> Optional[list[Any]]:
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("maximas", "sentencias", "resultados", "items", "data"):
                val = parsed.get(key)
                if isinstance(val, list):
                    return val
            list_values = [v for v in parsed.values() if isinstance(v, list)]
            if len(list_values) == 1:
                return list_values[0]
        return None

    try:
        unwrapped = _unwrap(json.loads(cleaned))
        if unwrapped is not None:
            return unwrapped
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    first_empty: Optional[list[Any]] = None
    for idx, ch in enumerate(cleaned):
        if ch not in "[{":
            continue
        try:
            obj, _ = decoder.raw_decode(cleaned[idx:])
        except json.JSONDecodeError:
            continue
        unwrapped = _unwrap(obj)
        if unwrapped is not None:
            if unwrapped and all(isinstance(item, dict) for item in unwrapped):
                return unwrapped
            if not unwrapped and first_empty is None:
                first_empty = unwrapped

    return first_empty


def _classify_error(slot: ModelSlot, exc: Exception) -> str:
    """
    Clasifica errores de Gemini y OpenRouter en:
      - 'auth_or_payment': clave inválida, revocada o sin créditos (401/402/403)
      - 'daily_limit': cuota diaria agotada (RPD)
      - 'model_unavailable': modelo inexistente o sin endpoints (404)
      - 'busy': saturación temporal, RPM 429, 500/502/503/504 o timeout
      - 'other': otro error inesperado
    """
    status = getattr(exc, "status_code", None)
    text = str(exc)
    low = text.lower()

    if status in (401, 402, 403) or any(
        k in low
        for k in (
            "api_key_invalid",
            "api key not valid",
            "permission_denied",
            "unauthorized",
            "invalid api key",
            "insufficient credits",
            "more credits, or fewer max_tokens",
        )
    ):
        return "auth_or_payment"

    if slot.provider == "gemini":
        if any(
            k in low
            for k in (
                "limit: 20",
                "generate content requests per day limit exceeded",
                "free_tier_requests",
                "per day limit exceeded",
                "requests_per_day",
                "requests per day",
            )
        ):
            return "daily_limit"
    elif slot.provider == "openrouter":
        if any(
            k in low
            for k in (
                "free-models-per-day",
                "requests per day",
                "per-day",
                "daily limit",
                "free model daily",
            )
        ):
            return "daily_limit"
    else:
        if any(
            k in low
            for k in (
                "requests per day",
                "daily limit",
                "credits exhausted",
                "quota exceeded",
            )
        ):
            return "daily_limit"

    if status == 404 or any(
        k in low
        for k in (
            "not found",
            "no endpoints found",
            "is not supported for generatecontent",
            "not a valid model",
            "invalid model",
        )
    ):
        return "model_unavailable"

    if status in (408, 429, 500, 502, 503, 504) or any(
        k in low
        for k in (
            "503",
            "unavailable",
            "429",
            "resource_exhausted",
            "servererror",
            "overloaded",
            "rate limit",
            "timeout",
            "timed out",
            "connection",
        )
    ):
        return "busy"

    return "other"


class MultiProviderClient:
    """Orquestador de modelos Gemini + OpenRouter + NVIDIA NIM con rotación automática."""

    def __init__(
        self,
        gemini_keys: list[str],
        openrouter_keys: list[str],
        nvidia_keys: Optional[list[str]] = None,
        gemini_models: Optional[list[str]] = None,
        openrouter_models: Optional[list[str]] = None,
        nvidia_models: Optional[list[str]] = None,
        timeout_ms: Optional[int] = 180000,
    ):
        self.gemini_keys = gemini_keys
        self.openrouter_keys = openrouter_keys
        self.nvidia_keys = list(nvidia_keys or [])
        self.gemini_models = list(gemini_models or PREFERRED_GEMINI_MODELS)
        self.openrouter_models = list(openrouter_models or PREFERRED_OPENROUTER_MODELS)
        self.nvidia_models = list(nvidia_models or PREFERRED_NVIDIA_MODELS)
        self.timeout_seconds = (timeout_ms / 1000.0) if timeout_ms else 180.0
        self.gemini_clients: list[Any] = []
        self.slots: list[ModelSlot] = []
        self.primary_label: str = ""
        self.fallback_label: str = ""
        self._http = requests.Session()
        self._ssl_ctx = ssl.create_default_context()
        self._usage_stats: dict[tuple[str, int, str], dict[str, int]] = {}

        for key in self.gemini_keys:
            if genai is None:
                raise ImportError("Falta la librería 'google-genai'. Instálala con: pip install google-genai")
            kwargs: dict[str, Any] = {"api_key": key}
            if timeout_ms:
                kwargs["http_options"] = {"timeout": timeout_ms}
            self.gemini_clients.append(genai.Client(**kwargs))

    def _record_usage(self, provider: str, key_index: int, model: str, is_probe: bool, ok: bool) -> None:
        key = (provider, key_index, model)
        entry = self._usage_stats.setdefault(
            key,
            {"sent": 0, "probes": 0, "probe_ok": 0, "extract_ok": 0, "errors": 0},
        )
        entry["sent"] += 1
        if is_probe:
            entry["probes"] += 1
            if ok:
                entry["probe_ok"] += 1
            else:
                entry["errors"] += 1
        else:
            if ok:
                entry["extract_ok"] += 1
            else:
                entry["errors"] += 1

    def _invoke_gemini(self, key_index: int, model: str, prompt: str, is_probe: bool = False) -> str:
        try:
            response = self.gemini_clients[key_index].models.generate_content(
                model=model,
                contents=prompt,
            )
            text = (response.text or "").strip()
            self._record_usage("gemini", key_index, model, is_probe=is_probe, ok=bool(text))
            return text
        except Exception:
            self._record_usage("gemini", key_index, model, is_probe=is_probe, ok=False)
            raise

    def _system_ssl_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: Optional[dict[str, Any]] = None,
        timeout: float = 30.0,
    ) -> tuple[int, str, dict[str, str]]:
        """Ejecuta una petición HTTP usando el almacén de certificados del sistema (Windows/Linux)."""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        is_stream = bool(payload and payload.get("stream"))
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self._ssl_ctx) as resp:
                if is_stream:
                    # Leer línea a línea evita timeouts del gateway proxy y reinicia el timeout por chunk
                    chunks = [raw_line.decode("utf-8", errors="replace") for raw_line in resp]
                    body = "".join(chunks)
                else:
                    body = resp.read().decode("utf-8", errors="replace")
                return resp.status, body, dict(resp.headers)
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", errors="replace")
            return err.code, body, dict(err.headers or {})

    @staticmethod
    def _extract_openai_error(status_code: int, raw_text: str) -> str:
        err_msg = raw_text[:300]
        try:
            body = json.loads(raw_text)
            err_obj = body.get("error") or {}
            if isinstance(err_obj, dict):
                meta_raw = ((err_obj.get("metadata") or {}).get("raw")) or ""
                msg = err_obj.get("message") or err_msg
                return f"{msg} ({meta_raw})" if meta_raw and meta_raw not in msg else msg
            if isinstance(err_obj, str):
                return err_obj
            if body.get("detail"):
                return str(body["detail"])
        except ValueError:
            pass
        return err_msg

    @staticmethod
    def _extract_content_text(content: Any) -> str:
        if isinstance(content, list):
            return "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return content or ""

    @classmethod
    def _extract_openai_content_from_json(cls, data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        return cls._extract_content_text(msg.get("content")).strip()

    def _extract_openai_content(self, provider: str, raw_body: str) -> str:
        """Extrae texto desde una respuesta JSON estándar o un stream SSE (data: ...)."""
        try:
            data = json.loads(raw_body)
            if isinstance(data, dict) and data.get("error"):
                raise ProviderHTTPError(502, self._extract_openai_error(502, raw_body), provider=provider)
            return self._extract_openai_content_from_json(data)
        except ValueError:
            pass

        parts: list[str] = []
        for line in raw_body.splitlines():
            stripped = line.strip()
            if not stripped.startswith("data:"):
                continue
            payload_str = stripped[5:].strip()
            if payload_str == "[DONE]":
                break
            try:
                chunk = json.loads(payload_str)
            except ValueError:
                continue
            if isinstance(chunk, dict) and chunk.get("error"):
                raise ProviderHTTPError(502, self._extract_openai_error(502, payload_str), provider=provider)
            choices = chunk.get("choices") or []
            if choices:
                msg_or_delta = choices[0].get("delta") or choices[0].get("message") or {}
                piece = self._extract_content_text(msg_or_delta.get("content"))
                if piece:
                    parts.append(piece)
        return "".join(parts).strip()

    def _invoke_openai_compatible(
        self,
        provider: str,
        key_index: int,
        model: str,
        prompt: str,
        is_probe: bool = False,
    ) -> str:
        is_nvidia_stream = provider == "nvidia" and not is_probe
        if provider == "openrouter":
            api_key = self.openrouter_keys[key_index]
            url = OPENROUTER_CHAT_URL
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/tsj-jurisprudencia",
                "X-Title": "Extractor Jurisprudencia TSJ",
            }
            use_system_ssl = False
        elif provider == "nvidia":
            api_key = self.nvidia_keys[key_index]
            url = NVIDIA_CHAT_URL
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream, application/json" if is_nvidia_stream else "application/json",
            }
            use_system_ssl = True
        else:
            raise ValueError(f"Proveedor OpenAI-compatible desconocido: {provider}")

        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 2048 if is_probe else 8192,
        }
        if is_probe and provider == "openrouter":
            payload["reasoning"] = {"effort": "low"}
        if is_nvidia_stream:
            payload["stream"] = True

        if is_probe:
            req_timeout = 30
        elif is_nvidia_stream:
            req_timeout = min(self.timeout_seconds, NVIDIA_STREAM_TIMEOUT_SECONDS)
        else:
            req_timeout = self.timeout_seconds

        def _send(body_payload: dict[str, Any]) -> tuple[int, str, dict[str, str]]:
            if use_system_ssl:
                return self._system_ssl_request("POST", url, headers, body_payload, timeout=req_timeout)
            resp = self._http.post(url, headers=headers, json=body_payload, timeout=req_timeout)
            raw_body = resp.text if getattr(resp, "text", "") else json.dumps(resp.json())
            return resp.status_code, raw_body, dict(resp.headers)

        try:
            status_code, raw_body, resp_headers = _send(payload)
            if status_code >= 400:
                err_msg = self._extract_openai_error(status_code, raw_body)
                if is_probe and status_code == 400 and "reasoning" in err_msg.lower() and "reasoning" in payload:
                    payload.pop("reasoning", None)
                    status_code, raw_body, resp_headers = _send(payload)
                elif status_code == 400 and "stream" in err_msg.lower() and payload.pop("stream", None):
                    status_code, raw_body, resp_headers = _send(payload)
                if status_code >= 400:
                    raise ProviderHTTPError(status_code, err_msg, provider=provider, headers=resp_headers)

            text = self._extract_openai_content(provider, raw_body)
            self._record_usage(provider, key_index, model, is_probe=is_probe, ok=bool(text))
            return text
        except Exception:
            self._record_usage(provider, key_index, model, is_probe=is_probe, ok=False)
            raise

    def _invoke_openrouter(self, key_index: int, model: str, prompt: str, is_probe: bool = False) -> str:
        return self._invoke_openai_compatible("openrouter", key_index, model, prompt, is_probe=is_probe)

    def _fetch_nvidia_catalog(self) -> set[str]:
        """Consulta el catálogo público de NVIDIA NIM (/v1/models) usando SSL del sistema."""
        try:
            status_code, body, _ = self._system_ssl_request(
                "GET",
                NVIDIA_MODELS_URL,
                headers={"Accept": "application/json"},
                timeout=15,
            )
            if status_code == 200:
                data = json.loads(body).get("data") or []
                return {m.get("id", "") for m in data if isinstance(m, dict) and m.get("id")}
        except Exception:
            pass
        return set()

    def _validate_openrouter_key(self, key_index: int) -> tuple[bool, str]:
        """Valida una API key de OpenRouter usando GET /api/v1/key sin gastar cuota de inferencia."""
        api_key = self.openrouter_keys[key_index]
        try:
            resp = self._http.get(
                OPENROUTER_KEY_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
            )
        except Exception as exc:
            return True, f"no se pudo consultar /key ({_short_error(exc, 50)}); se deja en reserva"

        if resp.status_code in (401, 403):
            return False, f"clave inválida o rechazada (HTTP {resp.status_code})"
        if resp.status_code >= 400:
            return True, f"aviso HTTP {resp.status_code} al consultar /key; se deja en reserva"

        try:
            data = (resp.json() or {}).get("data") or {}
        except ValueError:
            return True, "clave válida"

        details = []
        if data.get("is_free_tier") is True:
            details.append("tier gratuito")
        elif data.get("is_free_tier") is False:
            details.append("con créditos")

        rem = data.get("limit_remaining")
        if rem is not None:
            details.append(f"límite restante: ${rem}")

        daily = data.get("free_model_daily_requests")
        if isinstance(daily, dict) and "remaining" in daily and "limit" in daily:
            details.append(f"{daily['remaining']}/{daily['limit']} req/día gratis")

        summary = ", ".join(details) if details else "clave válida"
        return True, summary

    def _probe_call(self, fn: Callable[[], str], label: str) -> tuple[bool, str]:
        """Ejecuta una prueba de disponibilidad con 1 reintento breve ante errores 500/503."""
        for attempt in range(2):
            try:
                text = fn()
                if text:
                    return True, ""
                last_err = "respuesta vacía"
            except Exception as exc:
                last_err = _short_error(exc)
                if attempt == 0 and _is_transient_probe_error(exc):
                    time.sleep(2)
                    continue
                return False, last_err
        return False, last_err

    def _probe_gemini_key(self, key_index: int) -> list[ModelSlot]:
        label = f"Gemini #{key_index + 1}"
        found: list[ModelSlot] = []
        for model_name in self.gemini_models:
            tag = f"{model_name}" if key_index == 0 else f"{model_name} [{label}]"
            print(f"  Probando modelo: {tag} ...", end=" ", flush=True)
            ok, err = self._probe_call(
                lambda m=model_name: self._invoke_gemini(key_index, m, "Responde solo: OK", is_probe=True),
                tag,
            )
            if ok:
                print("✔ disponible", flush=True)
                found.append(ModelSlot("gemini", model_name, key_index, label, verified=True))
                if len(found) >= 2:
                    break
            else:
                print(f"✘ ({err})", flush=True)

        if found:
            return found

        # Búsqueda dinámica en la cuenta si ningún modelo preferido de Gemini respondió
        print(f"\n  Buscando modelos Gemini dinámicamente en {label}...", flush=True)
        skip_tokens = ("embedding", "tts", "audio", "image", "robotics", "veo", "lyria", "aqa")
        try:
            checked = 0
            for m in self.gemini_clients[key_index].models.list():
                name = getattr(m, "name", "") or ""
                low = name.lower()
                if "gemini" not in low or any(tok in low for tok in skip_tokens):
                    continue
                short = name.replace("models/", "")
                if short in self.gemini_models:
                    continue
                checked += 1
                if checked > 8:
                    break
                print(f"  Probando dinámico: {short} ...", end=" ", flush=True)
                ok, err = self._probe_call(
                    lambda s=short: self._invoke_gemini(key_index, s, "Responde solo: OK", is_probe=True),
                    short,
                )
                if ok:
                    print("✔ disponible", flush=True)
                    found.append(ModelSlot("gemini", short, key_index, label, verified=True))
                    if len(found) >= 2:
                        break
                else:
                    print(f"✘ ({err})", flush=True)
        except Exception as exc:
            print(f"  ⚠ Error al listar modelos en {label}: {_short_error(exc)}", flush=True)

        return found

    def _probe_openai_provider_slots(self, provider_title: str, provider: str, slots: list[ModelSlot]) -> list[ModelSlot]:
        """Verifica en orden los slots de un proveedor OpenAI-compatible hasta confirmar uno operativo."""
        print(f"\n  🌐 Verificando modelos disponibles en {provider_title}...", flush=True)
        verified: list[ModelSlot] = []
        remaining: list[ModelSlot] = []
        for slot in slots:
            if slot.exhausted:
                continue
            if verified:
                remaining.append(slot)
                continue
            print(f"  Probando {provider_title}: {slot.display_name} ...", end=" ", flush=True)
            ok, err = self._probe_call(
                lambda s=slot: self._invoke_openai_compatible(
                    provider, s.key_index, s.model, "Responde solo: OK", is_probe=True
                ),
                slot.display_name,
            )
            if ok:
                print("✔ disponible", flush=True)
                slot.verified = True
                verified.append(slot)
            else:
                print(f"✘ ({err})", flush=True)
                low_err = err.lower()
                if any(k in low_err for k in ("401", "402", "403", "unauthorized", "credits", "api key")):
                    slot.exhausted = True
                    for other in slots:
                        if other.key_index == slot.key_index:
                            other.exhausted = True
                elif any(k in low_err for k in ("404", "not found", "invalid model", "no endpoints")):
                    slot.exhausted = True
        return verified + [s for s in remaining if not s.exhausted]

    def select_models(self) -> tuple[str, str]:
        """
        Verifica los modelos disponibles en Gemini y prepara OpenRouter + NVIDIA NIM
        como respaldo escalonado (sin gastar peticiones de reserva si ya hay capa previa).
        """
        self.slots = []
        gemini_verified: list[ModelSlot] = []

        # 1) Probar claves de Gemini
        for idx in range(len(self.gemini_keys)):
            if gemini_verified:
                label = f"Gemini #{idx + 1}"
                for m in self.gemini_models:
                    self.slots.append(ModelSlot("gemini", m, idx, label, verified=False))
                continue

            found = self._probe_gemini_key(idx)
            gemini_verified.extend(found)
            self.slots.extend(found)

        # 2) Validar claves de OpenRouter (/key) y preparar su cola de modelos
        openrouter_slots: list[ModelSlot] = []
        for idx in range(len(self.openrouter_keys)):
            label = f"OpenRouter #{idx + 1}"
            valid, summary = self._validate_openrouter_key(idx)
            icon = "🔑" if valid else "⚠️"
            print(f"  {icon} {label}: {summary}", flush=True)
            if not valid:
                continue
            for m in self.openrouter_models:
                openrouter_slots.append(
                    ModelSlot(
                        provider="openrouter",
                        model=m,
                        key_index=idx,
                        key_label=label,
                        is_free_openrouter=(m.endswith(":free") or m == "openrouter/free"),
                        verified=False,
                    )
                )

        if not gemini_verified and openrouter_slots:
            openrouter_slots = self._probe_openai_provider_slots("OpenRouter", "openrouter", openrouter_slots)

        self.slots.extend(openrouter_slots)
        has_active_prior = bool(gemini_verified or any(s.verified for s in openrouter_slots))

        # 3) Preparar claves de NVIDIA NIM como 3ra capa
        nvidia_slots: list[ModelSlot] = []
        if self.nvidia_keys:
            catalog = self._fetch_nvidia_catalog()
            modelos_nv = [m for m in self.nvidia_models if not catalog or m in catalog]
            if not modelos_nv:
                modelos_nv = list(self.nvidia_models)
            for idx in range(len(self.nvidia_keys)):
                label = f"NVIDIA #{idx + 1}"
                print(
                    f"  🔑 NVIDIA NIM #{idx + 1}: clave en reserva ({len(modelos_nv)} modelo(s) listos)",
                    flush=True,
                )
                for m in modelos_nv:
                    nvidia_slots.append(
                        ModelSlot(
                            provider="nvidia",
                            model=m,
                            key_index=idx,
                            key_label=label,
                            verified=False,
                        )
                    )

            if not has_active_prior and nvidia_slots:
                nvidia_slots = self._probe_openai_provider_slots("NVIDIA NIM", "nvidia", nvidia_slots)

        self.slots.extend(nvidia_slots)
        active = self.active_slots()

        if not active:
            print("\n❌ No se encontró ningún modelo disponible (Gemini, OpenRouter ni NVIDIA NIM).")
            sys.exit(1)

        if not gemini_verified and not any(s.verified for s in active):
            print("\n❌ No se pudo verificar ningún modelo operativo en los proveedores de respaldo.")
            sys.exit(1)

        self.primary_label = active[0].display_name
        self.fallback_label = active[1].display_name if len(active) > 1 else active[0].display_name
        return self.primary_label, self.fallback_label

    def active_slots(self) -> list[ModelSlot]:
        return [s for s in self.slots if not s.exhausted]

    def _describe_provider_backup(self, provider: str) -> str:
        slots = [s for s in self.active_slots() if s.provider == provider]
        if not slots:
            return ""
        first = slots[0].display_name
        extra = len(slots) - 1
        return f"{first} (+ {extra} modelo(s) en reserva)" if extra > 0 else first

    def describe_openrouter_backup(self) -> str:
        return self._describe_provider_backup("openrouter")

    def describe_nvidia_backup(self) -> str:
        return self._describe_provider_backup("nvidia")

    def get_usage_summary(self) -> dict[str, Any]:
        """Devuelve métricas acumuladas de peticiones enviadas por proveedor y modelo."""
        total_sent = 0
        total_probes = 0
        total_extract_ok = 0
        total_errors = 0
        by_provider: dict[tuple[str, int], dict[str, Any]] = {}
        provider_names = {"gemini": "Gemini", "openrouter": "OpenRouter", "nvidia": "NVIDIA NIM"}

        for (provider, key_index, model), counts in self._usage_stats.items():
            sent = counts["sent"]
            probes = counts["probes"]
            extract_ok = counts["extract_ok"]
            errors = counts["errors"]
            total_sent += sent
            total_probes += probes
            total_extract_ok += extract_ok
            total_errors += errors

            group = by_provider.setdefault(
                (provider, key_index),
                {
                    "provider": provider,
                    "key_index": key_index,
                    "label": f"{provider_names.get(provider, provider)} #{key_index + 1}",
                    "sent": 0,
                    "probes": 0,
                    "extract_ok": 0,
                    "errors": 0,
                    "models": {},
                },
            )
            group["sent"] += sent
            group["probes"] += probes
            group["extract_ok"] += extract_ok
            group["errors"] += errors
            group["models"][model] = dict(counts)

        return {
            "total_sent": total_sent,
            "total_probes": total_probes,
            "total_extractions": total_sent - total_probes,
            "total_extract_ok": total_extract_ok,
            "total_errors": total_errors,
            "by_provider": list(by_provider.values()),
        }

    def print_usage_summary(self, check_openrouter_quota: bool = True) -> None:
        """Imprime el resumen de peticiones enviadas a cada proveedor/modelo (sin gastar cuota)."""
        summary = self.get_usage_summary()
        print(f"\n  ── Peticiones a APIs de IA ──────────────────────────────")
        print(
            f"  📡 Total peticiones enviadas:  {summary['total_sent']} "
            f"(inicio: {summary['total_probes']} | extracción: {summary['total_extractions']})"
        )
        print(
            f"  ✔️  Extracciones exitosas:     {summary['total_extract_ok']} "
            f"| ❌ Fallos/reintentos API: {summary['total_errors']}"
        )

        printed_or_keys: set[int] = set()
        printed_nv_keys: set[int] = set()
        for grp in summary["by_provider"]:
            extra_quota = ""
            if grp["provider"] == "openrouter" and check_openrouter_quota:
                printed_or_keys.add(grp["key_index"])
                ok_key, info_key = self._validate_openrouter_key(grp["key_index"])
                if ok_key and info_key:
                    extra_quota = f" | estado: {info_key}"
            elif grp["provider"] == "nvidia":
                printed_nv_keys.add(grp["key_index"])
            print(
                f"  • {grp['label']}: {grp['sent']} env. "
                f"(inicio: {grp['probes']}, extracción ok: {grp['extract_ok']}, errores: {grp['errors']}){extra_quota}"
            )
            for model, cnt in grp["models"].items():
                ext_sent = cnt["sent"] - cnt["probes"]
                print(
                    f"      - {model}: {cnt['sent']} env. "
                    f"(inicio: {cnt['probes']}, ext: {ext_sent}, ok: {cnt['extract_ok']}, errores: {cnt['errors']})"
                )

        if check_openrouter_quota:
            for idx in range(len(self.openrouter_keys)):
                if idx not in printed_or_keys:
                    ok_key, info_key = self._validate_openrouter_key(idx)
                    status_txt = info_key if ok_key else f"inválida ({info_key})"
                    print(f"  • OpenRouter #{idx + 1}: 0 env. (sin uso en esta corrida | {status_txt})")

        for idx in range(len(self.nvidia_keys)):
            if idx not in printed_nv_keys:
                print(f"  • NVIDIA NIM #{idx + 1}: 0 env. (sin uso en esta corrida | en reserva)")

    def _demote_slot(self, slot: ModelSlot) -> bool:
        """
        Mueve un slot crónicamente saturado al final de su bloque (proveedor, clave)
        para priorizar automáticamente los modelos que sí están respondiendo.
        """
        same_group_indices = [
            i for i, s in enumerate(self.slots)
            if s.provider == slot.provider and s.key_index == slot.key_index and not s.exhausted
        ]
        if len(same_group_indices) <= 1:
            return False
        cur_idx = self.slots.index(slot)
        last_idx = same_group_indices[-1]
        if cur_idx == last_idx:
            return False
        self.slots.pop(cur_idx)
        # Al quitar cur_idx (< last_idx), el antiguo last_idx queda en last_idx - 1;
        # insertar justo en last_idx lo coloca inmediatamente detrás de ese grupo.
        self.slots.insert(last_idx, slot)
        slot.demoted = True
        slot.consecutive_busy = 0
        return True

    def _mark_exhausted(self, slot: ModelSlot, kind: str) -> None:
        if kind == "auth_or_payment":
            for s in self.slots:
                if s.provider == slot.provider and s.key_index == slot.key_index:
                    s.exhausted = True
        elif kind == "daily_limit" and slot.provider == "openrouter" and slot.is_free_openrouter:
            for s in self.slots:
                if s.provider == "openrouter" and s.key_index == slot.key_index and s.is_free_openrouter:
                    s.exhausted = True
        else:
            slot.exhausted = True

    def extract_json_list(
        self,
        prompt: str,
        preferred_label: str = "",
        max_retries: int = 4,
    ) -> tuple[list[Any], bool, str]:
        """
        Envía el prompt al slot activo y parsea una lista JSON válida.
        Conmuta automáticamente entre modelos, claves y proveedores
        (Gemini -> OpenRouter -> NVIDIA NIM) si ocurre saturación o se agota la cuota diaria.
        """
        active = self.active_slots()
        if not active:
            raise LimiteDiarioExcedidoError("Todos los proveedores, claves y modelos agotaron su cuota.")

        slot_pos = 0
        if preferred_label:
            for idx, s in enumerate(active):
                if s.display_name == preferred_label and not s.demoted:
                    slot_pos = idx
                    break

        transient_failures = 0
        parse_failures = 0
        last_error = ""
        failed_this_call: set[int] = set()
        switch_budget = len(self.slots) * 2 + 4

        while switch_budget > 0:
            switch_budget -= 1
            active = self.active_slots()
            if not active:
                raise LimiteDiarioExcedidoError(
                    f"Cuota agotada en todos los proveedores configurados: {last_error}"
                )
            slot_pos = slot_pos % len(active)
            slot = active[slot_pos]

            try:
                if slot.provider == "gemini":
                    raw = self._invoke_gemini(slot.key_index, slot.model, prompt)
                else:
                    raw = self._invoke_openai_compatible(
                        slot.provider, slot.key_index, slot.model, prompt, is_probe=False
                    )

                if not raw:
                    raise ValueError("Respuesta vacía del modelo.")

                parsed = parse_json_list(raw)
                if parsed is not None:
                    slot.consecutive_busy = 0
                    return parsed, True, ""

                parse_failures += 1
                last_error = f"Formato JSON no interpretable ({slot.display_name})."
                if parse_failures >= max_retries:
                    return [], False, last_error
                if len(active) > 1:
                    slot_pos = (slot_pos + 1) % len(active)
                    print(f"\n    ⚠️ JSON inválido ({slot.display_name}) → probando {active[slot_pos].display_name}...", end=" ", flush=True)
                time.sleep(1.5)
                continue

            except Exception as exc:
                error_msg = str(exc)
                last_error = error_msg
                failed_this_call.add(id(slot))
                kind = _classify_error(slot, exc)

                if kind in ("auth_or_payment", "daily_limit", "model_unavailable"):
                    self._mark_exhausted(slot, kind)
                    remaining = self.active_slots()
                    if not remaining:
                        raise LimiteDiarioExcedidoError(
                            f"Cuota/disponibilidad agotada en todos los proveedores ({slot.display_name}): {error_msg}"
                        )
                    reason = {
                        "auth_or_payment": "clave/crédito no disponible",
                        "daily_limit": "límite diario alcanzado",
                        "model_unavailable": "modelo no disponible",
                    }[kind]
                    slot_pos = 0
                    for cand_idx, cand in enumerate(remaining):
                        if id(cand) not in failed_this_call:
                            slot_pos = cand_idx
                            break
                    next_slot = remaining[slot_pos]
                    print(
                        f"\n    ⚠️ {slot.display_name} {reason} → cambiando a {next_slot.display_name}...",
                        end=" ",
                        flush=True,
                    )
                    time.sleep(1.5)
                    continue

                transient_failures += 1
                next_slot = None
                if len(active) > 1:
                    next_target = active[(slot_pos + 1) % len(active)]
                    demoted_now = False
                    if kind == "busy":
                        slot.consecutive_busy += 1
                        if slot.consecutive_busy >= 2:
                            demoted_now = self._demote_slot(slot)
                    else:
                        slot.consecutive_busy = 0

                    updated_active = self.active_slots()
                    if next_target in updated_active:
                        slot_pos = updated_active.index(next_target)
                        next_slot = next_target
                    else:
                        slot_pos = 0
                        next_slot = updated_active[0]

                    if kind == "busy":
                        suffix_demote = " (relegado por saturación)" if demoted_now else ""
                        print(f"\n    ⚠️ {slot.display_name} saturado{suffix_demote} → {next_slot.display_name}", end=" ", flush=True)
                    else:
                        print(f"\n    ⚠ Error ({slot.display_name}): {_short_error(exc)} → {next_slot.display_name}", end=" ", flush=True)
                else:
                    print(f"\n    ⚠ Error ({slot.display_name}): {_short_error(exc)}", flush=True)

                if transient_failures >= max_retries:
                    break

                switched_provider = next_slot is not None and next_slot.provider != slot.provider
                if switched_provider:
                    wait = 1.5
                elif kind == "busy":
                    wait = min(4 * transient_failures, 16)
                    print(f"\n    ⏳ Reintentando en {wait}s...", end=" ", flush=True)
                else:
                    wait = 2
                time.sleep(wait)

        return [], False, last_error


def init_client(
    gemini_models: Optional[list[str]] = None,
    openrouter_models: Optional[list[str]] = None,
    nvidia_models: Optional[list[str]] = None,
    inline_gemini_key: str = "",
    inline_openrouter_key: str = "",
    inline_nvidia_key: str = "",
    timeout_ms: Optional[int] = 180000,
) -> MultiProviderClient:
    """Inicializa el cliente multi-proveedor leyendo variables de entorno y respaldos."""
    gemini_keys = collect_api_keys(
        "GEMINI_API_KEY",
        extra_vars=("GEMINI_API_KEYS", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_4"),
        inline_default=inline_gemini_key,
    )
    openrouter_keys = collect_api_keys(
        "OPENROUTER_API_KEY",
        extra_vars=(
            "OPENROUTER_API_KEYS",
            "OPENROUTER_API_KEY_2",
            "OPENROUTER_API_KEY_3",
            "OPENROUTER_API_KEY_4",
        ),
        inline_default=inline_openrouter_key,
    )
    nvidia_keys = collect_api_keys(
        "NVIDIA_API_KEY",
        extra_vars=(
            "NVIDIA_API_KEYS",
            "NVIDIA_API_KEY_2",
            "NVIDIA_API_KEY_3",
            "NVIDIA_API_KEY_4",
        ),
        inline_default=inline_nvidia_key,
    )

    env_or_models = os.environ.get("OPENROUTER_MODELS", "").strip()
    custom_or_models = [m.strip() for m in env_or_models.split(",") if m.strip()] if env_or_models else None
    env_nv_models = os.environ.get("NVIDIA_MODELS", "").strip()
    custom_nv_models = [m.strip() for m in env_nv_models.split(",") if m.strip()] if env_nv_models else None

    if not gemini_keys and not openrouter_keys and not nvidia_keys:
        print("=" * 60)
        print("⚠  ATENCIÓN: No se detectaron claves API (GEMINI_API_KEY, OPENROUTER_API_KEY ni NVIDIA_API_KEY).")
        print("   Configura al menos una en GitHub Actions:")
        print("   (Settings > Secrets and variables > Actions)")
        print("   • GEMINI_API_KEY       (Google AI Studio)")
        print("   • OPENROUTER_API_KEY   (OpenRouter - 2da capa)")
        print("   • NVIDIA_API_KEY       (NVIDIA NIM - 3ra capa)")
        print("   O defínelas como variables de entorno antes de ejecutar.")
        print("=" * 60)
        sys.exit(1)

    return MultiProviderClient(
        gemini_keys=gemini_keys,
        openrouter_keys=openrouter_keys,
        nvidia_keys=nvidia_keys,
        gemini_models=gemini_models,
        openrouter_models=custom_or_models or openrouter_models,
        nvidia_models=custom_nv_models or nvidia_models,
        timeout_ms=timeout_ms,
    )


def select_model(client: MultiProviderClient) -> tuple[str, str]:
    """Punto de entrada compatible para seleccionar modelo primario y de respaldo."""
    return client.select_models()
