"""Optional local embedding model for "Ask this report" (Step 1) - re-ranks the facts chosen for a question.

Off by default. Switched on in config/settings.json:
  "ask": {"embedder": {"enabled": true, "model": "google/embeddinggemma-2", "dim": 256, "weight": 0.5,
                       "min_cos": 0.35, "device": "cpu", "query_prompt": "SearchQuery", "doc_prompt": "Document"}}
Two ways to get the model:
  local   - ``pip install sentence-transformers`` (+ torch) and a one-time download from Hugging Face;
  remote  - "url": "https://<your-space>.hf.space" calls a Gradio app (no torch on the dashboard server; a private
            Space needs HF_TOKEN in the environment; optional "api_name", "timeout_seconds", "workers"). If the
package, the model or the download is missing - or anything fails at ask time - Ask silently uses word matching only.
Vectors are normalised, so a dot product is the cosine. Fact vectors are cached in memory by text, so a report's facts
are embedded once.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from typing import Callable, Sequence

log = logging.getLogger("vdash.ask")


def _norm(v: Sequence[float]) -> list[float]:
    s = sum(x * x for x in v) ** 0.5
    return [x / s for x in v] if s else list(v)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class Embedder:
    """``encode_fn(texts, kind)`` -> list of vectors; kind is "query" or "doc". Everything else (truncation to ``dim``,
    normalising, caching, failure handling) is done here, so tests can pass a fake ``encode_fn``."""

    def __init__(self, encode_fn: Callable[[list[str], str], list[list[float]]], name: str = "custom", dim: int | None = None,
                 weight: float = 0.5, min_cos: float = 0.35, cache_size: int = 4000):
        self._encode, self.name, self.dim = encode_fn, name, dim
        self.weight, self.min_cos = float(weight), float(min_cos)
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._size, self._lock = cache_size, threading.Lock()

    def _prep(self, vecs) -> list[list[float]]:
        out = []
        for v in vecs:
            v = [float(x) for x in v]
            out.append(_norm(v[: self.dim] if self.dim else v))     # Matryoshka: cut, then re-normalise
        return out

    def embed_query(self, text: str) -> list[float]:
        return self._prep(self._encode([text], "query"))[0]

    def embed_docs(self, texts: list[str]) -> list[list[float]]:
        keys = [hashlib.sha256((self.name + "|" + str(self.dim) + "|" + t).encode()).hexdigest() for t in texts]
        with self._lock:
            have = {k: self._cache[k] for k in keys if k in self._cache}
        miss = sorted({k: t for k, t in zip(keys, texts) if k not in have}.items())
        if miss:
            vecs = self._prep(self._encode([t for _, t in miss], "doc"))
            with self._lock:
                for (k, _), v in zip(miss, vecs):
                    self._cache[k] = v
                    have[k] = v
                while len(self._cache) > self._size:
                    self._cache.popitem(last=False)
        return [have[k] for k in keys]


def sentence_transformer_embedder(model: str = "google/embeddinggemma-2", dim: int | None = 256, weight: float = 0.5,
                                  min_cos: float = 0.35, device: str = "cpu", query_prompt: str = "SearchQuery",
                                  doc_prompt: str = "Document") -> Embedder:
    """Loads the model on first use (not at start-up). Raises ImportError / OSError if it cannot be loaded."""
    holder: dict = {}
    lock = threading.Lock()

    def load():
        with lock:
            if "m" not in holder:
                from sentence_transformers import SentenceTransformer     # imported late: it is optional
                holder["m"] = SentenceTransformer(model, device=device)
            return holder["m"]

    def encode(texts: list[str], kind: str):
        m = load()
        prompt = query_prompt if kind == "query" else doc_prompt
        try:
            vecs = m.encode(texts, prompt_name=prompt, convert_to_numpy=True, show_progress_bar=False)
        except (ValueError, KeyError):          # the model has no prompt of that name: encode as plain text
            vecs = m.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return vecs.tolist()

    return Embedder(encode, name=model, dim=dim, weight=weight, min_cos=min_cos)


# ---------------------------------------------------------------- a remote model: a Gradio app (e.g. a Hugging Face Space)
def _http(url: str, data: dict | None, headers: dict, timeout: float) -> str:
    import json as _json
    import urllib.request
    body = _json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method="POST" if data is not None else "GET",
                                 headers={**({"Content-Type": "application/json"} if data is not None else {}), **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 - the URL is the operator's own setting
        return r.read().decode("utf-8", "replace")


def _vector(x) -> list[float]:
    """The embedding out of whatever a Gradio JSON component returned (list, JSON text, or {"embedding": [...]})."""
    import json as _json
    if isinstance(x, str):
        x = _json.loads(x)
    if isinstance(x, dict):
        for k in ("embedding", "vector", "embeddings"):
            if k in x:
                return _vector(x[k])
        raise ValueError("no embedding in the answer")
    if x and isinstance(x[0], list):
        x = x[0]
    return [float(v) for v in x]


def gradio_embedder(url: str, api_name: str | None = None, token_env: str = "HF_TOKEN", dim: int | None = 256,
                    weight: float = 0.5, min_cos: float = 0.35, timeout: float = 60.0, workers: int = 4) -> Embedder:
    """Calls a Gradio app that turns one text into one vector (the "EmbeddingGemma-2 API" Space). The endpoint is found
    from the app's own /info when ``api_name`` is not given. A private Space needs the token in the environment
    variable ``token_env`` (default HF_TOKEN). One call per text; fact vectors are cached, so a report is embedded once."""
    import json as _json
    import os
    from concurrent.futures import ThreadPoolExecutor
    base = url.rstrip("/")
    state: dict = {}
    lock = threading.Lock()

    def headers() -> dict:
        tok = (os.environ.get(token_env) or "").strip()
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    def resolve():
        with lock:
            if "call" in state:
                return state["call"]
            last = None
            for prefix in ("/gradio_api", ""):
                try:
                    info = _json.loads(_http(f"{base}{prefix}/info", None, headers(), timeout))
                except Exception as exc:   # noqa: BLE001 - try the other prefix
                    last = exc
                    continue
                eps = info.get("named_endpoints") or {}
                name = (api_name or "").lstrip("/") or next((k.lstrip("/") for k, v in eps.items()
                                                              if len(v.get("parameters") or []) == 1), None)
                if not name:
                    raise ValueError(f"the app at {base} has no endpoint that takes one text (found: {list(eps)})")
                state["call"] = f"{base}{prefix}/call/{name}"
                return state["call"]
            hint = ""
            if "404" in str(last) and not (os.environ.get(token_env) or "").strip():
                hint = f" - a private Hugging Face Space answers 404 without a token: set {token_env} (a read token) or make the Space public"
            raise ConnectionError(f"cannot read {base}/info: {last}{hint}")

    def one(text: str) -> list[float]:
        call = resolve()
        ev = _json.loads(_http(call, {"data": [text]}, headers(), timeout))["event_id"]
        stream = _http(f"{call}/{ev}", None, headers(), timeout)
        event, out = None, None
        for line in stream.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:") and event == "complete":
                out = _json.loads(line[5:].strip())
                break
            elif line.startswith("data:") and event == "error":
                raise RuntimeError(f"the embedding app returned an error: {line[5:].strip()[:200]}")
        if out is None:
            raise RuntimeError("the embedding app sent no result")
        return _vector(out[0] if isinstance(out, list) and len(out) == 1 else out)

    def encode(texts: list[str], kind: str):      # the app has no query/document distinction: both are plain text
        if len(texts) == 1:
            return [one(texts[0])]
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            return list(pool.map(one, texts))

    return Embedder(encode, name=f"gradio:{base}", dim=dim, weight=weight, min_cos=min_cos)


def build(cfg: dict) -> Embedder:
    """From an ``ask.embedder`` settings block: ``url`` -> the remote Gradio app, otherwise the local model."""
    if cfg.get("url"):
        return gradio_embedder(cfg["url"], api_name=cfg.get("api_name"), token_env=cfg.get("token_env") or "HF_TOKEN",
                               dim=cfg.get("dim", 256), weight=cfg.get("weight", 0.5), min_cos=cfg.get("min_cos", 0.35),
                               timeout=float(cfg.get("timeout_seconds", 60)), workers=int(cfg.get("workers", 4)))
    return sentence_transformer_embedder(
        model=cfg.get("model") or "google/embeddinggemma-2", dim=cfg.get("dim", 256), weight=cfg.get("weight", 0.5),
        min_cos=cfg.get("min_cos", 0.35), device=cfg.get("device") or "cpu",
        query_prompt=cfg.get("query_prompt") or "SearchQuery", doc_prompt=cfg.get("doc_prompt") or "Document")


_LOADED: dict = {}


def from_settings(raw: dict) -> Embedder | None:
    """The embedder configured in settings.json -> ask.embedder, or None (off / not configured)."""
    cfg = ((raw or {}).get("ask") or {}).get("embedder") or {}
    if not cfg.get("enabled"):
        return None
    key = repr(sorted(cfg.items()))
    if key not in _LOADED:
        try:
            _LOADED[key] = build(cfg)
        except Exception as exc:   # noqa: BLE001 - optional feature: never stop Ask from working
            log.warning("ask embedder not available (%s): %s", type(exc).__name__, exc)
            _LOADED[key] = None
    return _LOADED[key]
