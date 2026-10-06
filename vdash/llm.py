"""LLM adapter (v37) - one small interface over DeepSeek, Gemini, OpenAI and Anthropic, for the report story writer.

    llm = LLMClient.from_settings(settings.raw)          # Services.llm
    r = llm.complete_json(system, user, max_tokens=4000) # -> LLMResult (data = parsed JSON object)

Design rules
* **The report never depends on it.** Every failure is an ``LLMError`` with a ``kind`` (no_key, timeout, http,
  auth, rate_limit, bad_json, refused, disabled) and a plain message without secrets; the caller falls back to
  the rule-written text.
* **Keys only from environment variables** (``config/local.env`` locally, the Render environment online) - never in
  synced files, never logged, never in error messages.
* **Plain HTTPS (stdlib), no SDKs**: nothing extra to install on Render; the transport can be replaced in tests.
* **DeepSeek thinking** (``llm.thinking``: off | low | high | max): the model reasons before answering; the
  reasoning counts as output tokens, so the output limit must leave room - an answer cut off at the limit is
  retried once with twice the limit (``too_long``), up to ``llm.max_tokens_ceiling``.
* **JSON out**: each provider's JSON mode, then a strict parse (code fences tolerated); malformed JSON is retried once.
* **Cost**: tokens from the provider's usage report x prices in settings (USD per 1M tokens, editable).
* **Cache** (``cached_complete_json``): the answer is stored in the report database under a key made of
  tenant | data version | report spec | language | prompt version | provider | model, so the same report from
  the same data is never paid for twice (also across restarts). Usage per business and month is recorded there.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Callable

log = logging.getLogger("vdash.llm")

PROVIDERS: dict[str, dict] = {
    # prices: USD per 1M tokens (check the provider's price list; DeepSeek peak prices as of Oct 2026)
    "deepseek": {"style": "openai", "base_url": "https://api.deepseek.com", "key_env": "DEEPSEEK_API_KEY",
                 "model": "deepseek-v4-pro",
                 "prices": {"deepseek-v4-pro": [1.32, 3.96], "deepseek-v4-flash": [0.30, 1.20]}},
    "openai": {"style": "openai", "base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY",
               "model": "gpt-5.1-mini", "prices": {}},
    "gemini": {"style": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta",
               "key_env": "GEMINI_API_KEY", "model": "gemini-2.5-flash", "prices": {}},
    "anthropic": {"style": "anthropic", "base_url": "https://api.anthropic.com/v1", "key_env": "ANTHROPIC_API_KEY",
                  "model": "claude-sonnet-5", "prices": {}},
}


class LLMError(RuntimeError):
    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind, self.status = kind, status


@dataclass
class LLMResult:
    data: Any
    text: str
    provider: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    ms: int = 0
    attempts: int = 1
    cached: bool = False
    thinking_tokens: int = 0

    def meta(self) -> dict:
        d = asdict(self)
        d.pop("data"), d.pop("text")
        return d


# ---------------------------------------------------------------- transport (replaceable in tests)
Transport = Callable[[str, dict, bytes, float], tuple[int, bytes]]


def http_transport(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except (socket.timeout, TimeoutError) as e:
        raise LLMError("timeout", f"no answer within {int(timeout)} s") from e
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise LLMError("timeout", f"no answer within {int(timeout)} s") from e
        raise LLMError("http", f"cannot reach the provider ({type(e.reason).__name__})") from e


def parse_json(text: str) -> Any:
    """Strict JSON, tolerating a ```json fence or text around one top-level object."""
    t = (text or "").strip()
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", t, re.S)
    if m:
        t = m.group(1)
    try:
        return json.loads(t)
    except ValueError:
        i, j = t.find("{"), t.rfind("}")
        if 0 <= i < j:
            return json.loads(t[i:j + 1])
        raise


def _redact(s: str, secret: str | None) -> str:
    s = s or ""
    if secret:
        s = s.replace(secret, "***")
    return re.sub(r"(sk-|AIza)[A-Za-z0-9_\-]{8,}", r"\1***", s)[:300]


class LLMClient:
    def __init__(self, provider: str = "deepseek", model: str = "", enabled: bool = True, timeout: float = 90,
                 max_output_tokens: int = 4000, temperature: float = 0.3, retries: int = 1,
                 prices: dict | None = None, base_url: str = "", transport: Transport | None = None,
                 env: dict | None = None, thinking: str = "high", max_tokens_ceiling: int = 64000):
        if provider not in PROVIDERS:
            raise ValueError(f"unknown LLM provider '{provider}' (use {', '.join(PROVIDERS)})")
        p = PROVIDERS[provider]
        self.provider, self.style = provider, p["style"]
        self.model = model or p["model"]
        self.base_url = (base_url or p["base_url"]).rstrip("/")
        self.key_env = p["key_env"]
        self.enabled = enabled
        self.timeout, self.max_output_tokens = float(timeout), int(max_output_tokens)
        self.temperature, self.retries = float(temperature), max(0, int(retries))
        self.prices = {**p["prices"], **(prices or {})}
        # v39.1: DeepSeek V4 "thinks" before answering (on by default, effort high); its thinking counts as output
        # tokens. off | low | high | max. An answer cut off at the output limit is retried with twice the limit,
        # up to max_tokens_ceiling.
        self.thinking = (thinking or "high").strip().lower()
        if self.thinking not in ("off", "low", "high", "max"):
            raise ValueError(f"llm.thinking must be off, low, high or max (not '{thinking}')")
        self.max_tokens_ceiling = max(int(max_tokens_ceiling), self.max_output_tokens)
        self.transport = transport or http_transport
        self._env = env if env is not None else os.environ
        self._lock = threading.Lock()
        self.last: dict = {}

    @classmethod
    def from_settings(cls, raw: dict, **kw) -> "LLMClient":
        s = (raw or {}).get("llm") or {}
        from .config import as_bool
        prov = (s.get("provider") or "deepseek").strip().lower()
        return cls(provider=prov, model=(s.get("model") or "").strip(), enabled=as_bool(s.get("enabled"), True),
                   timeout=float(s.get("timeout_seconds") or 90), max_output_tokens=int(s.get("max_output_tokens") or 4000),
                   temperature=float(s.get("temperature") if s.get("temperature") not in (None, "") else 0.3),
                   retries=int(s.get("retries") if s.get("retries") not in (None, "") else 1),
                   prices=(s.get("prices") or {}).get(prov), thinking=str(s.get("thinking") or "high"),
                   max_tokens_ceiling=int(s.get("max_tokens_ceiling") or 64000), **kw)

    # ------------------------------------------------------------ status
    @property
    def key(self) -> str:
        return (self._env.get(self.key_env) or "").strip()

    def status(self) -> dict:
        return {"provider": self.provider, "model": self.model, "enabled": self.enabled, "key_set": bool(self.key),
                "thinking": self.thinking if self.provider == "deepseek" else None,
                "key_env": self.key_env, "last": self.last}

    def price(self, tokens_in: int, tokens_out: int) -> float:
        p = self.prices.get(self.model)
        if not p:
            return 0.0
        return round(tokens_in / 1e6 * float(p[0]) + tokens_out / 1e6 * float(p[1]), 6)

    # ------------------------------------------------------------ provider requests
    def _request(self, system: str, user: str, max_tokens: int, temperature: float,
                 thinking: str | None = None) -> tuple[str, dict, bytes]:
        key = self.key
        if self.style == "openai":
            body = {"model": self.model, "temperature": temperature, "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if self.provider == "deepseek":
                think = thinking or self.thinking      # v39.3: a call may ask for less (e.g. a retry)
                if think == "off":
                    body["thinking"] = {"type": "disabled"}
                else:                                  # thinking mode takes no temperature
                    body["thinking"] = {"type": "enabled"}
                    body["reasoning_effort"] = think
                    body.pop("temperature", None)
            return (f"{self.base_url}/chat/completions",
                    {"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                    json.dumps(body, ensure_ascii=False).encode("utf-8"))
        if self.style == "gemini":
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens,
                                         "responseMimeType": "application/json"}}
            return (f"{self.base_url}/models/{self.model}:generateContent",
                    {"Content-Type": "application/json", "x-goog-api-key": key},
                    json.dumps(body, ensure_ascii=False).encode("utf-8"))
        body = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature, "system": system,
                "messages": [{"role": "user", "content": user + "\n\nAnswer with one JSON object only."}]}
        return (f"{self.base_url}/messages",
                {"Content-Type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"},
                json.dumps(body, ensure_ascii=False).encode("utf-8"))

    def _read(self, payload: dict) -> tuple[str, int, int, str | None]:
        """-> (text, tokens_in, tokens_out, stop reason)"""
        if self.style == "openai":
            ch = (payload.get("choices") or [{}])[0]
            u = payload.get("usage") or {}
            det = u.get("completion_tokens_details") or {}
            self._thinking_tokens += int(det.get("reasoning_tokens") or 0)
            return ((ch.get("message") or {}).get("content") or "", int(u.get("prompt_tokens") or 0),
                    int(u.get("completion_tokens") or 0), ch.get("finish_reason"))
        if self.style == "gemini":
            c = (payload.get("candidates") or [{}])[0]
            u = payload.get("usageMetadata") or {}
            text = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts") or [])
            return text, int(u.get("promptTokenCount") or 0), int(u.get("candidatesTokenCount") or 0), c.get("finishReason")
        u = payload.get("usage") or {}
        text = "".join(b.get("text", "") for b in payload.get("content") or [] if b.get("type") == "text")
        return text, int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0), payload.get("stop_reason")

    # ------------------------------------------------------------ the call
    def complete_json(self, system: str, user: str, max_tokens: int | None = None,
                      temperature: float | None = None, thinking: str | None = None) -> LLMResult:
        if not self.enabled:
            raise LLMError("disabled", "AI writing is switched off in the settings")
        if not self.key:
            raise LLMError("no_key", f"no API key: set {self.key_env}")
        max_tokens = int(max_tokens or self.max_output_tokens)
        temperature = self.temperature if temperature is None else float(temperature)
        t0, tin, tout, attempt, err = time.perf_counter(), 0, 0, 0, None
        self._thinking_tokens = 0
        while attempt <= self.retries:
            attempt += 1
            if thinking is not None and thinking not in ("off", "low", "high", "max"):
                raise ValueError(f"thinking must be off, low, high or max (not '{thinking}')")
            url, headers, body = self._request(system, user, max_tokens, temperature, thinking)
            try:
                status, raw = self.transport(url, headers, body, self.timeout)
            except LLMError as e:
                err = e
                if e.kind == "timeout":
                    continue
                break
            if status in (401, 403):
                err = LLMError("auth", f"the provider refused the API key (HTTP {status})", status)
                break
            if status == 429:
                err = LLMError("rate_limit", "the provider's rate limit or balance is exhausted (HTTP 429)", status)
                break
            if status >= 500 or status == 408:
                err = LLMError("http", f"provider error HTTP {status}", status)
                continue
            if status >= 400:
                detail = _redact(raw.decode("utf-8", "replace"), self.key)
                err = LLMError("http", f"request rejected (HTTP {status}): {detail}", status)
                break
            try:
                payload = json.loads(raw.decode("utf-8"))
            except ValueError:
                err = LLMError("bad_json", "the provider's answer was not readable")
                continue
            text, i, o, stop = self._read(payload)
            tin, tout = tin + i, tout + o
            if stop in ("content_filter", "SAFETY", "refusal"):
                err = LLMError("refused", "the provider declined to answer")
                break
            if stop in ("length", "MAX_TOKENS", "max_tokens"):
                err = LLMError("too_long", f"the answer was cut off at the output limit ({max_tokens} tokens"
                               + (f", of which {self._thinking_tokens} thinking" if self._thinking_tokens else "") + ")")
                if max_tokens >= self.max_tokens_ceiling:
                    break
                max_tokens = min(max_tokens * 2, self.max_tokens_ceiling)      # try again with room to finish
                continue
            try:
                data = parse_json(text)
            except ValueError:
                err = LLMError("bad_json", "the answer was not valid JSON")
                continue
            if not isinstance(data, dict):
                err = LLMError("bad_json", "the answer was not a JSON object")
                continue
            res = LLMResult(data=data, text=text, provider=self.provider, model=self.model, tokens_in=tin,
                            tokens_out=tout, cost_usd=self.price(tin, tout),
                            ms=int((time.perf_counter() - t0) * 1000), attempts=attempt,
                            thinking_tokens=self._thinking_tokens)
            with self._lock:
                self.last = {"ok": True, "at": time.time(), "ms": res.ms, "attempts": attempt}
            return res
        err = err or LLMError("http", "no answer")
        err.tokens_in, err.tokens_out = tin, tout                      # paid even when unusable
        err.cost_usd = self.price(tin, tout)
        with self._lock:
            self.last = {"ok": False, "at": time.time(), "kind": err.kind, "error": str(err), "attempts": attempt}
        log.warning("llm %s/%s failed (%s): %s", self.provider, self.model, err.kind, err)
        raise err


# ---------------------------------------------------------------- cache + usage (report database)
def cache_key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def cached_complete_json(llm: LLMClient, store, tenant_id: str, month: str, key_parts: list, system: str, user: str,
                         **kw) -> LLMResult:
    """Answer from the cache when the same request was answered before; otherwise call, store, count usage.
    A store problem never blocks the call (it is only logged)."""
    key = cache_key(llm.provider, llm.model, *key_parts)
    try:
        hit = store.get_llm_cache(key) if store is not None else None
    except Exception as exc:   # noqa: BLE001 - the cache is an optimisation
        log.warning("llm cache read failed: %s", exc)
        hit = None
    if hit:
        return LLMResult(data=hit["data"], text=hit.get("text") or "", provider=hit["provider"], model=hit["model"],
                         tokens_in=hit["tokens_in"], tokens_out=hit["tokens_out"], cost_usd=0.0, cached=True)
    try:
        res = llm.complete_json(system, user, **kw)
    except LLMError as e:
        _usage(store, tenant_id, month, getattr(e, "tokens_in", 0), getattr(e, "tokens_out", 0),
               getattr(e, "cost_usd", 0.0), ok=False)
        raise
    _usage(store, tenant_id, month, res.tokens_in, res.tokens_out, res.cost_usd, ok=True)
    try:
        if store is not None:
            store.put_llm_cache(key, tenant_id, {"data": res.data, "text": res.text, "provider": res.provider,
                                                 "model": res.model, "tokens_in": res.tokens_in,
                                                 "tokens_out": res.tokens_out, "cost_usd": res.cost_usd})
    except Exception as exc:   # noqa: BLE001
        log.warning("llm cache write failed: %s", exc)
    return res


def _usage(store, tenant_id, month, tin, tout, cost, ok):
    if store is None:
        return
    try:
        store.add_llm_usage(tenant_id, month, tokens_in=tin, tokens_out=tout, cost_usd=cost, ok=ok)
    except Exception as exc:   # noqa: BLE001
        log.warning("llm usage not recorded: %s", exc)
