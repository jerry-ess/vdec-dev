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
import uuid
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger("vdash.llm")

PROVIDERS: dict[str, dict] = {
    # prices: USD per 1M tokens (check the provider's price list; DeepSeek peak prices as of Oct 2026)
    "deepseek": {"style": "openai", "base_url": "https://api.deepseek.com", "key_env": "DEEPSEEK_API_KEY",
                 "model": "deepseek-v4-pro",
                 "prices": {"deepseek-v4-pro": [1.32, 3.96], "deepseek-v4-flash": [0.30, 1.20]}},
    "openai": {"style": "openai", "base_url": "https://api.openai.com/v1", "key_env": "OPENAI_API_KEY",
               "model": "gpt-5.1-mini", "prices": {}},
    # v88: Gemini prices (USD per 1M tokens [input, output]; text). A price may change on a date:
    # {"default": [in, out], "from": {"YYYY-MM-DD": [in, out]}}. Gemini 3.8 Flash doubles on 2027-01-01.
    "gemini": {"style": "gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta",
               "key_env": "GEMINI_API_KEY", "model": "gemini-2.5-flash",
               "prices": {"gemini-3.1-flash-lite": [0.25, 1.50],
                          "gemini-3.8-flash": {"default": [0.75, 3.75], "from": {"2027-01-01": [1.50, 7.50]}}}},
    "anthropic": {"style": "anthropic", "base_url": "https://api.anthropic.com/v1", "key_env": "ANTHROPIC_API_KEY",
                  "model": "claude-sonnet-5", "prices": {}},
}


MODEL_ALIASES = {"deepseek-flash": "deepseek-v4-flash", "deepseek-pro": "deepseek-v4-pro"}   # short names -> the API model ids

# What makes a provider "not available" (v88): the call itself failed, so the next model in line may still work.
# A model's own bad answer (bad_json, too_long, refused) is not an outage and does not switch models.
FAILOVER_KINDS = frozenset({"no_key", "auth", "rate_limit", "timeout", "http"})


def price_pair(value, today: str | None = None) -> list | None:
    """[in, out] USD per 1M tokens from a price entry: a plain pair, or {"default": pair, "from": {date: pair}}
    where the latest date that has already begun (UTC) wins."""
    if not value:
        return None
    if isinstance(value, dict):
        today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pair = value.get("default")
        for d in sorted((value.get("from") or {})):
            if d <= today:
                pair = value["from"][d]
        return list(pair) if pair else None
    return list(value)


class LLMError(RuntimeError):
    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind, self.status = kind, status
        self.provider = self.model = None          # v88: which model failed (set by the client that raised)
        self.failed_attempts: list = []            # v88: earlier models that failed before this one (fail-over)


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
    call_id: str | None = None          # v83: the ledger row of this call (ai_call_log)
    failed_attempts: list = field(default_factory=list)   # v88: models that failed first (fail-over to this one)

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
                 env: dict | None = None, thinking: str = "high", max_tokens_ceiling: int = 64000,
                 gemini_thinking_level: str = ""):
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
        self._price_table = {**p["prices"], **(prices or {})}
        self.gemini_thinking_level = (gemini_thinking_level or "").strip().lower()
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

    @property
    def prices(self) -> dict:
        """model -> [in, out] as of today (date-dependent prices resolved)."""
        out = {}
        for m, v in self._price_table.items():
            pr = price_pair(v)
            if pr:
                out[m] = pr
        return out

    @classmethod
    def from_settings(cls, raw: dict, **kw) -> "LLMClient":
        s = (raw or {}).get("llm") or {}
        from .config import as_bool
        prov = (s.get("provider") or "deepseek").strip().lower()
        model = (s.get("model") or "").strip()
        model = MODEL_ALIASES.get(model.lower(), model)
        return cls(provider=prov, model=model, gemini_thinking_level=str(s.get("gemini_thinking_level") or ""), enabled=as_bool(s.get("enabled"), True),
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
                "gemini_thinking_level": (self.gemini_thinking_level or None) if self.provider == "gemini" else None,
                "key_env": self.key_env, "last": self.last}

    def price(self, tokens_in: int, tokens_out: int) -> float:
        p = price_pair(self._price_table.get(self.model))
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
            if self.gemini_thinking_level:                 # v88: optional; unset = the model's own default
                body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": self.gemini_thinking_level}
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
            text = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts") or [] if not p.get("thought"))
            thoughts = int(u.get("thoughtsTokenCount") or 0)     # v88: thinking is billed as output but not in candidatesTokenCount
            self._thinking_tokens += thoughts
            return (text, int(u.get("promptTokenCount") or 0), int(u.get("candidatesTokenCount") or 0) + thoughts,
                    c.get("finishReason"))
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
        err.provider, err.model = self.provider, self.model
        err.tokens_in, err.tokens_out = tin, tout                      # paid even when unusable
        err.cost_usd = self.price(tin, tout)
        with self._lock:
            self.last = {"ok": False, "at": time.time(), "kind": err.kind, "error": str(err), "attempts": attempt}
        log.warning("llm %s/%s failed (%s): %s", self.provider, self.model, err.kind, err)
        raise err


# ---------------------------------------------------------------- fail-over (v88)
class FallbackLLM:
    """A default model plus one or more backups, with the same interface as ``LLMClient``.

    A call goes to the first model that is not marked down. When it fails because the *service* is not available
    (``FAILOVER_KINDS``: no key, refused key, rate limit / empty balance, timeout, network or 5xx error) the next model
    answers instead, and the failed one is skipped for ``cooldown_seconds`` so the next questions do not wait for a
    timeout again; after that it is tried again, so the default comes back by itself. A bad answer from a working model
    (bad JSON, cut off, refused) does not switch models. Every failed attempt is returned with the result
    (``failed_attempts``) so the usage ledger records it; only the accepted answer is ever charged, at the price of the
    model that actually wrote it."""

    def __init__(self, clients: list, cooldown_seconds: float = 120, clock: Callable[[], float] = time.time):
        if not clients:
            raise ValueError("FallbackLLM needs at least one client")
        self.clients = list(clients)
        self.cooldown = max(0.0, float(cooldown_seconds))
        self._clock = clock
        self._down: dict[int, float] = {}
        self._lock = threading.Lock()
        self.failover: dict = {"count": 0}

    # the first model stands for the pair wherever a single client is read (names, settings, cache key)
    def __getattr__(self, name):
        if name in ("clients", "_down", "_lock", "_clock", "cooldown", "failover"):
            raise AttributeError(name)
        return getattr(self.clients[0], name)

    @property
    def key(self) -> str:
        return next((c.key for c in self.clients if c.key), "")

    @property
    def enabled(self) -> bool:
        return self.clients[0].enabled

    @property
    def prices(self) -> dict:
        out: dict = {}
        for c in reversed(self.clients):
            out.update(c.prices)
        return out

    def _order(self) -> list:
        now = self._clock()
        with self._lock:
            up = [c for i, c in enumerate(self.clients) if self._down.get(i, 0) <= now]
        return up or list(self.clients)           # everything marked down: try them all rather than refuse

    def _mark_down(self, c) -> None:
        with self._lock:
            self._down[self.clients.index(c)] = self._clock() + self.cooldown

    def complete_json(self, system: str, user: str, **kw) -> LLMResult:
        if not self.enabled:
            raise LLMError("disabled", "AI writing is switched off in the settings")
        failed: list = []
        order = self._order()
        for n, c in enumerate(order):
            try:
                res = c.complete_json(system, user, **kw)
            except LLMError as e:
                e.provider, e.model = e.provider or c.provider, e.model or c.model
                if e.kind in FAILOVER_KINDS:
                    self._mark_down(c)
                if e.kind in FAILOVER_KINDS and n < len(order) - 1:
                    failed.append(_attempt(e))
                    log.warning("llm %s/%s unavailable (%s) - trying %s/%s", c.provider, c.model, e.kind,
                                order[n + 1].provider, order[n + 1].model)
                    continue
                e.failed_attempts = failed
                raise
            if failed:
                res.failed_attempts = failed
                with self._lock:
                    self.failover = {"count": self.failover.get("count", 0) + 1, "at": self._clock(),
                                     "from": f"{failed[0]['provider']}/{failed[0]['model']}", "kind": failed[0]["kind"],
                                     "to": f"{res.provider}/{res.model}"}
            return res
        raise LLMError("http", "no AI model is available")          # unreachable: order is never empty

    def status(self) -> dict:
        now = self._clock()
        st = self.clients[0].status()
        st["fallbacks"] = [c.status() for c in self.clients[1:]]
        st["failover"] = dict(self.failover)
        st["down"] = [f"{c.provider}/{c.model}" for i, c in enumerate(self.clients) if self._down.get(i, 0) > now]
        return st


def _attempt(e: LLMError) -> dict:
    return {"provider": e.provider, "model": e.model, "kind": e.kind, "error": str(e)[:200],
            "tokens_in": getattr(e, "tokens_in", 0), "tokens_out": getattr(e, "tokens_out", 0),
            "cost_usd": getattr(e, "cost_usd", 0.0)}


def llm_from_settings(raw: dict, **kw):
    """The client for ``raw['llm']``: one ``LLMClient``, or a ``FallbackLLM`` when the block has a ``fallback``
    ({"provider", "model", ...} or a list of them; unnamed fields come from the main block)."""
    s = (raw or {}).get("llm") or {}
    primary = LLMClient.from_settings(raw, **kw)
    fbs = s.get("fallback")
    fbs = [fbs] if isinstance(fbs, dict) else [f for f in (fbs or []) if isinstance(f, dict)]
    fbs = [f for f in fbs if any(v not in (None, "") for v in f.values())]
    if not fbs:
        return primary
    base = {k: v for k, v in s.items() if k != "fallback"}
    clients = [primary]
    for f in fbs:
        m = dict(base)
        if f.get("provider") and str(f["provider"]).strip().lower() != str(base.get("provider") or "deepseek").strip().lower():
            m.pop("model", None)                       # the other provider's default model unless one is named
            m.pop("thinking", None) if "thinking" not in f else None
        m.update({k: v for k, v in f.items() if v not in (None, "")})
        clients.append(LLMClient.from_settings({"llm": m}, **kw))
    return FallbackLLM(clients, cooldown_seconds=float(s.get("failover_cooldown_seconds") or 120))


# ---------------------------------------------------------------- cache + usage (report database)
def cache_key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def cached_complete_json(llm: LLMClient, store, tenant_id: str, month: str, key_parts: list, system: str, user: str,
                         **kw) -> LLMResult:
    """Answer from the cache when the same request was answered before; otherwise call, store, count usage.
    A store problem never blocks the call (it is only logged). v83: every call - cached, ok or failed - is also one
    row in the AI usage ledger (``ai_credits``); inside an ``ai_action`` the row belongs to that action."""
    key = cache_key(llm.provider, llm.model, *key_parts)
    try:
        hit = store.get_llm_cache(key) if store is not None else None
    except Exception as exc:   # noqa: BLE001 - the cache is an optimisation
        log.warning("llm cache read failed: %s", exc)
        hit = None
    if hit:
        res = LLMResult(data=hit["data"], text=hit.get("text") or "", provider=hit["provider"], model=hit["model"],
                        tokens_in=hit["tokens_in"], tokens_out=hit["tokens_out"], cost_usd=0.0, cached=True)
        res.call_id = _ledger(llm, store, tenant_id, kw, status="cached", tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                              provider=res.provider, model=res.model)
        return res
    t0 = time.perf_counter()
    try:
        res = llm.complete_json(system, user, **kw)
    except LLMError as e:
        _record_failed(llm, store, tenant_id, month, kw, getattr(e, "failed_attempts", None) or [])
        _usage(store, tenant_id, month, getattr(e, "tokens_in", 0), getattr(e, "tokens_out", 0),
               getattr(e, "cost_usd", 0.0), ok=False)
        _ledger(llm, store, tenant_id, kw, status="failed", tokens_in=getattr(e, "tokens_in", 0),
                tokens_out=getattr(e, "tokens_out", 0), cost_usd=getattr(e, "cost_usd", 0.0), error_kind=e.kind,
                latency_ms=int((time.perf_counter() - t0) * 1000), provider=getattr(e, "provider", None),
                model=getattr(e, "model", None))
        raise
    _record_failed(llm, store, tenant_id, month, kw, res.failed_attempts)
    _usage(store, tenant_id, month, res.tokens_in, res.tokens_out, res.cost_usd, ok=True)
    res.call_id = _ledger(llm, store, tenant_id, kw, status="ok", tokens_in=res.tokens_in, tokens_out=res.tokens_out,
                          tokens_reasoning=res.thinking_tokens, cost_usd=res.cost_usd, latency_ms=res.ms,
                          provider=res.provider, model=res.model)
    try:
        if store is not None:
            store.put_llm_cache(key, tenant_id, {"data": res.data, "text": res.text, "provider": res.provider,
                                                 "model": res.model, "tokens_in": res.tokens_in,
                                                 "tokens_out": res.tokens_out, "cost_usd": res.cost_usd})
    except Exception as exc:   # noqa: BLE001
        log.warning("llm cache write failed: %s", exc)
    return res


def _record_failed(llm, store, tenant_id, month, kw, attempts) -> None:
    """v88: one failed ledger row (and usage count) for each model that failed before a backup answered."""
    for a in attempts or []:
        _usage(store, tenant_id, month, a.get("tokens_in", 0), a.get("tokens_out", 0), a.get("cost_usd", 0.0), ok=False)
        _ledger(llm, store, tenant_id, kw, status="failed", tokens_in=a.get("tokens_in", 0), tokens_out=a.get("tokens_out", 0),
                cost_usd=a.get("cost_usd", 0.0), error_kind=a.get("kind"), provider=a.get("provider"), model=a.get("model"))


def _ledger(llm, store, tenant_id, kw, **row) -> str | None:
    """One row in the AI usage ledger for one provider call; never raises. Inside an ``ai_action`` the row belongs to
    the action (feature, user, request); outside one it is written as feature 'unknown' (still counted, never charged)."""
    if store is None:
        return None
    try:
        from .ai_credits import current_action
        act = current_action()
        extra = dict(llm=llm, temperature=kw.get("temperature"), max_output_tokens=kw.get("max_tokens"),
                     thinking=kw.get("thinking"), **row)
        if act is not None and act.svc is not None:
            return act.record(**extra)
        r2 = {k: v for k, v in row.items() if k in ("status", "tokens_in", "tokens_out", "tokens_reasoning", "latency_ms",
                                                      "error_kind", "provider", "model")}
        p = (getattr(llm, "prices", None) or {}).get(r2.get("model") or llm.model) or [None, None]
        return store.credits.log_call(
            request_id=str(uuid.uuid4()), tenant_id=tenant_id, trigger="system", feature="unknown",
            provider=r2.pop("provider", None) or llm.provider, model=r2.pop("model", None) or llm.model,
            temperature=kw.get("temperature"), max_output_tokens=kw.get("max_tokens"),
            thinking_level=kw.get("thinking") or getattr(llm, "thinking", None), price_in_per_m=p[0], price_out_per_m=p[1],
            cost_usd_micro=int(round(float(row.get("cost_usd") or 0) * 1e6)), credits_charged_mc=0, charge_reason="unlogged_action",
            exempt=True, mode="off", **{k: (v or 0) if k.startswith(("tokens", "latency")) else v for k, v in r2.items()})
    except Exception as exc:   # noqa: BLE001 - the ledger never breaks an answer
        log.warning("ai ledger row not written: %s", exc)
        return None


def _usage(store, tenant_id, month, tin, tout, cost, ok):
    if store is None:
        return
    try:
        store.add_llm_usage(tenant_id, month, tokens_in=tin, tokens_out=tout, cost_usd=cost, ok=ok)
    except Exception as exc:   # noqa: BLE001
        log.warning("llm usage not recorded: %s", exc)
