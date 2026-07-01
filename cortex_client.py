"""
cortex_client.py — OAuth2 client for Lilly Cortex (IBU Leadership Dashboard DEV).

Verified path 2026-06-01: returns 'pong' from `ibu-leadership-dashboard-dev`
(Claude Sonnet 4.6) via gateway.apim-dev.lilly.com/cortex.

Credentials read from (in order):
  1. Streamlit secrets: cortex_client_id / cortex_client_secret
  2. A local .env file (CORTEX_CLIENT_ID / CORTEX_CLIENT_SECRET)
  3. Environment vars:  CORTEX_CLIENT_ID / CORTEX_CLIENT_SECRET

Tenant ID is hardcoded (Lilly).
"""
from __future__ import annotations
import json
import os
import time
import requests
from pathlib import Path
from typing import Iterator, Optional

TENANT_ID = "18a59a81-eea8-4c30-948a-d8824cdc2580"
SCOPE     = "api://Cortex.lilly.com/.default"
APIM_BASE = "https://gateway.apim-dev.lilly.com/cortex"
MODEL     = "ibu-leadership-dashboard-dev"


def _load_dotenv(path: str = ".env") -> None:
    """Populate os.environ from a local .env (KEY=value). Does NOT overwrite
    vars already set, so it's a no-op in the Streamlit/CI deployment."""
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _load_creds():
    cid = csec = None
    try:
        import streamlit as st
        cid  = st.secrets.get("cortex_client_id", None)
        csec = st.secrets.get("cortex_client_secret", None)
    except Exception:
        pass
    _load_dotenv()  # local fallback; harmless when no .env present
    cid  = cid  or os.environ.get("CORTEX_CLIENT_ID")  or os.environ.get("cortex_client_id")
    csec = csec or os.environ.get("CORTEX_CLIENT_SECRET") or os.environ.get("cortex_client_secret")
    return cid, csec


_token_cache: dict = {"token": None, "fetched_at": 0.0, "ttl": 3000.0}


def _fetch_token() -> str:
    """Get a Bearer token from Entra ID. Cached ~50min in-process."""
    now = time.time()
    if _token_cache["token"] and (now - _token_cache["fetched_at"]) < _token_cache["ttl"]:
        return _token_cache["token"]
    cid, csec = _load_creds()
    if not (cid and csec):
        raise RuntimeError("Missing CORTEX_CLIENT_ID / CORTEX_CLIENT_SECRET")
    r = requests.post(
        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
        data={
            "client_id": cid,
            "client_secret": csec,
            "grant_type": "client_credentials",
            "scope": SCOPE,
        },
        timeout=15,
    )
    r.raise_for_status()
    j = r.json()
    _token_cache["token"] = j["access_token"]
    _token_cache["fetched_at"] = now
    _token_cache["ttl"] = max(int(j.get("expires_in", 3599)) - 120, 60)
    return _token_cache["token"]


def chat(prompt: str, system: Optional[str] = None,
         max_tokens: int = 600, temperature: float = 0.2,
         session_id: str = "ibu-leadership-dashboard") -> str:
    """Single-turn chat (non-streaming). Returns assistant content or '⚠️ ...'."""
    try:
        token = _fetch_token()
    except Exception as e:
        return f"⚠️ Auth error: {str(e)[:160]}"

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        r = requests.post(
            f"{APIM_BASE}/cortex-openai/chat/completions",
            params={"session_id": session_id},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            },
            timeout=60,
        )
        if r.status_code == 401:
            _token_cache["token"] = None
            return chat(prompt, system, max_tokens, temperature, session_id)
        if r.status_code != 200:
            try:
                msg = r.json().get("message", "")[:160]
            except Exception:
                msg = r.text[:160]
            return f"⚠️ Cortex {r.status_code}: {msg or 'request failed'}"
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"⚠️ {type(e).__name__}: {str(e)[:160]}"


def chat_stream(prompt: str, system: Optional[str] = None,
                max_tokens: int = 600, temperature: float = 0.2,
                session_id: str = "ibu-leadership-dashboard") -> Iterator[str]:
    """Streaming chat. Yields content deltas as they arrive (SSE format).

    On error, yields a single '⚠️ ...' chunk and stops.
    """
    try:
        token = _fetch_token()
    except Exception as e:
        yield f"⚠️ Auth error: {str(e)[:160]}"
        return

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    try:
        r = requests.post(
            f"{APIM_BASE}/cortex-openai/chat/completions",
            params={"session_id": session_id},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
            json={
                "model": MODEL,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            },
            timeout=120,
            stream=True,
        )
        if r.status_code == 401:
            _token_cache["token"] = None
            yield from chat_stream(prompt, system, max_tokens, temperature, session_id)
            return
        if r.status_code != 200:
            try:
                msg = r.json().get("message", "")[:160]
            except Exception:
                msg = r.text[:160]
            yield f"⚠️ Cortex {r.status_code}: {msg or 'request failed'}"
            return

        for raw in r.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta", {})
            chunk = delta.get("content")
            if chunk:
                yield chunk
    except Exception as e:
        yield f"⚠️ {type(e).__name__}: {str(e)[:160]}"


def ping() -> tuple[bool, str]:
    """Quick connectivity probe. Returns (ok, message)."""
    try:
        out = chat("Reply with only the single word: pong", max_tokens=10)
        ok = out.strip().lower().startswith("pong")
        return ok, out
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


if __name__ == "__main__":
    ok, msg = ping()
    print(f"OK={ok}  Response: {msg!r}")