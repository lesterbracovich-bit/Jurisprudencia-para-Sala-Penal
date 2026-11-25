#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Cliente Unificado de IA con Respaldo Multi-Proveedor y Multi-Clave
==================================================================
Soporta Google Gemini (`google-genai`) y OpenRouter (`chat/completions`),
permitiendo configurar una o más API keys por proveedor y conmutar
automáticamente cuando un modelo o proveedor alcanza su cuota diaria (429),
se satura (500/503) o presenta errores de autenticación/créditos.
"""

from dataclasses import dataclass
import json
import os
import re
import sys
import time
from typing import Any, Callable, Optional

import requests

try:
    from google import genai
except ImportError:
    genai = None


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_CHAT_URL = f"{OPENROUTER_BASE_URL}/chat/completions"
OPENROUTER_KEY_URL = f"{OPENROUTER_BASE_URL}/key"

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

PLACEHOLDER_KEYS = {
    "",
    "none",
    "null",
    "tu_clave_api_aqui",
    "tu_clave_openrouter_aqui",
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

    @property
    def display_name(self) -> str:
        prefix = "openrouter:" if self.provider == "openrouter" else ""
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
    else:
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
    """Orquestador de modelos Gemini + OpenRouter con rotación y conmutación automática."""

    def __init__(
        self,
        gemini_keys: list[str],
        openrouter_keys: list[str],
        gemini_models: Optional[list[str]] = None,
        openrouter_models: Optional[list[str]] = None,
        timeout_ms: Optional[int] = 180000,
    ):
        self.gemini_keys = gemini_keys
        self.openrouter_keys = openrouter_keys
        self.gemini_models = list(gemini_models or PREFERRED_GEMINI_MODELS)
        self.openrouter_models = list(openrouter_models or PREFERRED_OPENROUTER_MODELS)
        self.timeout_seconds = (timeout_ms / 1000.0) if timeout_ms else 180.0
        self.gemini_clients: list[Any] = []
        self.slots: list[ModelSlot] = []
        self.primary_label: str = ""
        self.fallback_label: str = ""
        self._http = requests.Session()

        for key in self.gemini_keys:
            if genai is None:
                raise ImportError("Falta la librería 'google-genai'. Instálala con: pip install google-genai")
            kwargs: dict[str, Any] = {"api_key": key}
            if timeout_ms:
                kwargs["http_options"] = {"timeout": timeout_ms}
            self.gemini_clients.append(genai.Client(**kwargs))

    def _invoke_gemini(self, key_index: int, model: str, prompt: str) -> str:
        response = self.gemini_clients[key_index].models.generate_content(
            model=model,
            contents=prompt,
        )
        return (response.text or "").strip()

    def _invoke_openrouter(self, key_index: int, model: str, prompt: str, is_probe: bool = False) -> str:
        api_key = self.openrouter_keys[key_index]
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/tsj-jurisprudencia",
            "X-Title": "Extractor Jurisprudencia TSJ",
        }
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 2048 if is_probe else 8192,
        }
        if is_probe:
            payload["reasoning"] = {"effort": "low"}

        resp = self._http.post(
            OPENROUTER_CHAT_URL,
            headers=headers,
            json=payload,
            timeout=30 if is_probe else self.timeout_seconds,
        )
        if resp.status_code >= 400:
            err_msg = resp.text[:300]
            try:
                body = resp.json()
                err_obj = body.get("error") or {}
                meta_raw = ((err_obj.get("metadata") or {}).get("raw")) or ""
                msg = err_obj.get("message") or err_msg
                err_msg = f"{msg} ({meta_raw})" if meta_raw and meta_raw not in msg else msg
            except ValueError:
                pass

            # Si el modelo no acepta el parámetro opcional 'reasoning' en un probe, reintentar sin él
            if is_probe and resp.status_code == 400 and "reasoning" in err_msg.lower():
                payload.pop("reasoning", None)
                resp = self._http.post(OPENROUTER_CHAT_URL, headers=headers, json=payload, timeout=30)
                if resp.status_code < 400:
                    return self._extract_openrouter_content(resp)
            raise ProviderHTTPError(resp.status_code, err_msg, provider="openrouter", headers=dict(resp.headers))

        return self._extract_openrouter_content(resp)

    @staticmethod
    def _extract_openrouter_content(resp: requests.Response) -> str:
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            return "".join(parts).strip()
        return (content or "").strip()

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
                lambda m=model_name: self._invoke_gemini(key_index, m, "Responde solo: OK"),
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
                    lambda s=short: self._invoke_gemini(key_index, s, "Responde solo: OK"),
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

    def select_models(self) -> tuple[str, str]:
        """
        Verifica los modelos disponibles en Gemini y prepara los de OpenRouter
        como respaldo (sin gastar peticiones gratuitas de OpenRouter si Gemini ya responde).
        """
        self.slots = []
        gemini_verified: list[ModelSlot] = []

        # 1) Probar claves de Gemini
        for idx in range(len(self.gemini_keys)):
            if gemini_verified:
                # Clave adicional de Gemini: encolar sin frenar el arranque
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

        # Si Gemini no logró ningún modelo operativo, probar OpenRouter hasta verificar al menos uno
        if not gemini_verified and openrouter_slots:
            print("\n  🌐 Verificando modelos disponibles en OpenRouter...", flush=True)
            verified_or: list[ModelSlot] = []
            remaining_or: list[ModelSlot] = []
            for slot in openrouter_slots:
                if slot.exhausted:
                    continue
                if verified_or:
                    remaining_or.append(slot)
                    continue
                print(f"  Probando OpenRouter: {slot.display_name} ...", end=" ", flush=True)
                ok, err = self._probe_call(
                    lambda s=slot: self._invoke_openrouter(s.key_index, s.model, "Responde solo: OK", is_probe=True),
                    slot.display_name,
                )
                if ok:
                    print("✔ disponible", flush=True)
                    slot.verified = True
                    verified_or.append(slot)
                else:
                    print(f"✘ ({err})", flush=True)
                    low_err = err.lower()
                    if any(k in low_err for k in ("401", "402", "403", "unauthorized", "credits")):
                        slot.exhausted = True
                        for other in openrouter_slots:
                            if other.key_index == slot.key_index:
                                other.exhausted = True
                    elif any(k in low_err for k in ("404", "not found", "invalid model", "no endpoints")):
                        slot.exhausted = True
            openrouter_slots = verified_or + [s for s in remaining_or if not s.exhausted]

        self.slots.extend(openrouter_slots)
        active = self.active_slots()

        if not active:
            print("\n❌ No se encontró ningún modelo disponible (ni en Gemini ni en OpenRouter).")
            sys.exit(1)

        if not gemini_verified and not any(s.verified for s in active):
            print("\n❌ No se pudo verificar ningún modelo operativo en OpenRouter.")
            sys.exit(1)

        self.primary_label = active[0].display_name
        self.fallback_label = active[1].display_name if len(active) > 1 else active[0].display_name
        return self.primary_label, self.fallback_label

    def active_slots(self) -> list[ModelSlot]:
        return [s for s in self.slots if not s.exhausted]

    def describe_openrouter_backup(self) -> str:
        or_slots = [s for s in self.active_slots() if s.provider == "openrouter"]
        if not or_slots:
            return ""
        first = or_slots[0].display_name
        extra = len(or_slots) - 1
        return f"{first} (+ {extra} modelo(s) en reserva)" if extra > 0 else first

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
        Conmuta automáticamente entre modelos, claves y proveedores (Gemini -> OpenRouter)
        si ocurre saturación o se agota la cuota diaria.
        """
        active = self.active_slots()
        if not active:
            raise LimiteDiarioExcedidoError("Todos los proveedores, claves y modelos agotaron su cuota.")

        slot_pos = 0
        if preferred_label:
            for idx, s in enumerate(active):
                if s.display_name == preferred_label:
                    slot_pos = idx
                    break

        transient_failures = 0
        parse_failures = 0
        last_error = ""
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
                    raw = self._invoke_openrouter(slot.key_index, slot.model, prompt, is_probe=False)

                if not raw:
                    raise ValueError("Respuesta vacía del modelo.")

                parsed = parse_json_list(raw)
                if parsed is not None:
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
                    next_slot = remaining[0]
                    print(
                        f"\n    ⚠️ {slot.display_name} {reason} → cambiando a {next_slot.display_name}...",
                        end=" ",
                        flush=True,
                    )
                    slot_pos = 0
                    time.sleep(1.5)
                    continue

                transient_failures += 1
                if len(active) > 1:
                    slot_pos = (slot_pos + 1) % len(active)
                    next_slot = active[slot_pos]
                    if kind == "busy":
                        print(f"\n    ⚠️ {slot.display_name} saturado → {next_slot.display_name}", end=" ", flush=True)
                    else:
                        print(f"\n    ⚠ Error ({slot.display_name}): {_short_error(exc)} → {next_slot.display_name}", end=" ", flush=True)
                else:
                    print(f"\n    ⚠ Error ({slot.display_name}): {_short_error(exc)}", flush=True)

                if transient_failures >= max_retries:
                    break

                switched_provider = len(active) > 1 and next_slot.provider != slot.provider
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
    inline_gemini_key: str = "",
    inline_openrouter_key: str = "",
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

    env_or_models = os.environ.get("OPENROUTER_MODELS", "").strip()
    custom_or_models = [m.strip() for m in env_or_models.split(",") if m.strip()] if env_or_models else None

    if not gemini_keys and not openrouter_keys:
        print("=" * 60)
        print("⚠  ATENCIÓN: No se detectaron claves API (GEMINI_API_KEY ni OPENROUTER_API_KEY).")
        print("   Configura al menos una en GitHub Actions:")
        print("   (Settings > Secrets and variables > Actions)")
        print("   • GEMINI_API_KEY       (Google AI Studio)")
        print("   • OPENROUTER_API_KEY   (OpenRouter como principal o respaldo)")
        print("   O defínelas como variables de entorno antes de ejecutar.")
        print("=" * 60)
        sys.exit(1)

    return MultiProviderClient(
        gemini_keys=gemini_keys,
        openrouter_keys=openrouter_keys,
        gemini_models=gemini_models,
        openrouter_models=custom_or_models or openrouter_models,
        timeout_ms=timeout_ms,
    )


def select_model(client: MultiProviderClient) -> tuple[str, str]:
    """Punto de entrada compatible para seleccionar modelo primario y de respaldo."""
    return client.select_models()
