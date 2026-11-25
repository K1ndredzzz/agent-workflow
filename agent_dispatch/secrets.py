"""Secret scanning and redaction for text, structured data, and filenames."""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any, Iterator

from agent_dispatch.config import (
    PEM_END,
    PEM_START,
    SECRET_FILENAME_ALLOWLIST,
    SECRET_FILENAME_PATTERNS,
    SECRET_VALUE_PATTERNS,
)

_PEM_BLOCK = re.compile(PEM_START.pattern + r".*?(?:" + PEM_END.pattern + r"|\Z)", re.S)
_AUTH_HEADER_SCHEMES = r"(?:bearer|basic|token|apikey|oauth)"
_AUTH_PARAM_VAL = (
    r"""(?:"[^"\r\n]*"|'[^'\r\n]*'|\$\{\{[^}\r\n]+\}\}|\{\{[^}\r\n]+\}\}"""
    r"""|\$\{[^}\s\r\n]+\}|<[^>\s\r\n]+>|[^=\s,"';}\]][^\s,"';}\]]*)"""
)
_AUTH_PARAM_PAIR = re.compile(
    rf"""([A-Za-z_][A-Za-z0-9_-]*)[ \t]*=[ \t]*(?:"([^"\r\n]*)"|'([^'\r\n]*)'|(\$\{{\{{[^}}\r\n]+\}}\}}|\{{\{{[^}}\r\n]+\}}\}}|\$\{{[^}}\s\r\n]+\}}|<[^>\s\r\n]+>|[^=\s,"';}}\]][^\s,"';}}\]]*))"""
)
_AUTH_PARAM_LIST_QUOTED = re.compile(
    rf"""^(?:[A-Za-z_][A-Za-z0-9_-]*[ \t]*=[ \t]*{_AUTH_PARAM_VAL})(?:(?:[ \t]*,[ \t]*|[ \t]+)[A-Za-z_][A-Za-z0-9_-]*[ \t]*=[ \t]*{_AUTH_PARAM_VAL})*$"""
)
_AUTH_PARAM_LIST_UNQUOTED = (
    rf"""[A-Za-z_][A-Za-z0-9_-]*[ \t]*=[ \t]*{_AUTH_PARAM_VAL}(?:[ \t]*,[ \t]*[A-Za-z_][A-Za-z0-9_-]*[ \t]*=[ \t]*{_AUTH_PARAM_VAL})*"""
)
_AUTH_HEADER_VALUE = re.compile(
    rf"""(\b(?:proxy-)?authorization\b["']?[ \t]*[:=][ \t]*)"""
    rf"""(?:"""
    rf""""({_AUTH_HEADER_SCHEMES}[ \t]+)((?:\\.|[^"\\\r\n])*)"?"""
    rf"""|'({_AUTH_HEADER_SCHEMES}[ \t]+)((?:\\.|[^'\\\r\n])*)'?"""
    rf"""|({_AUTH_HEADER_SCHEMES}[ \t]+)({_AUTH_PARAM_LIST_UNQUOTED}|\$\{{\{{[^}}\r\n]+\}}\}}|\{{\{{[^}}\r\n]+\}}\}}|\$\{{[^}}\s\r\n]+\}}|[^\s"',;}}\]]+)"""
    rf"""|(?P<alias>\*[^\s"',;{{}}\[\]*]+)(?=$|[\s"',;{{}}\[\]])"""
    rf""")""",
    re.I,
)
_AUTH_NONSECRET_PARAMS = frozenset({
    "realm", "version", "algorithm", "qop", "charset",
    "oauth_version", "oauth_signature_method",
})
_AUTH_EMPTY_PARAM_KEYS = frozenset({
    "token", "key", "apikey", "api_key", "secret", "password",
    "credential", "access_token", "oauth_token", "oauth_consumer_key",
    "oauth_signature",
})
_API_KEY_HEADERS = ("x-api-key", "x-goog-api-key")
# Bare HTTP fields have a literal value. Source-code expressions have their own rules below.
_RAW_CREDENTIAL_FIELD = re.compile(
    r"^([+ -]?\ufeff?[ \t]*(?:(?:proxy-)?authorization:[ \t]*(?:bearer|basic|token|apikey|oauth)[ \t]+|(?:%s):[ \t]*))([^\r\n]+)"
    % "|".join(_API_KEY_HEADERS), re.I | re.M,
)
_API_KEY_HEADER_VALUE = re.compile(
    r"""(\b(?:%s)\b["']?[ \t]*[:=][ \t]*)(?:"((?:\\.|[^"\\\r\n])*)"?|'((?:\\.|[^'\\\r\n])*)'?|(\$\{[^}\s]+\}|[^\s"',;})\]]+))"""
    % "|".join(_API_KEY_HEADERS), re.I,
)
_API_KEY_PLACEHOLDER = re.compile(
    r"^(?:<[^>\s]+>|\$\{\{[^}]+\}\}|\{\{[^}]+\}\}|\$\{[^}\s]+\}|\$[A-Z_][A-Z0-9_]*)$"
)
_API_KEY_BENIGN_WORDS = frozenset({
    "string", "str", "number", "boolean", "bool", "object", "any", "unknown",
    "null", "none", "nil", "true", "false", "undefined", "required", "optional",
})
_API_KEY_CODE_EXPR = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+|"
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*[\[(].*|"
    r"[a-z]+(?:_[a-z]+)+)$"
)
_BEARER_VALUE = re.compile(r"""(?i)\b(bearer)([ \t]+)([A-Za-z0-9._~+/-]{8,}=*)""")
_BASIC_VALUE = re.compile(r"""(?i)\b(basic)([ \t]+)([A-Za-z0-9+/]{8,}={0,2})(?![A-Za-z0-9._~+/-])""")
_PLAIN_ALPHA_WORD = re.compile(r"(?:[A-Za-z][a-z]+|[A-Z]+)")
_CREDENTIAL_ENV = re.compile(r"(?:_API_KEY|_TOKEN|_PASSWORD|_SECRET|_ACCESS_KEY)$")
_ALLOW_MARKER = re.compile(r"# secret-scan:allow(?=$|\s)")
_REDACTED = "<REDACTED_SECRET>"


def _is_plausible_basic_token(tok: str) -> bool:
    if _PLAIN_ALPHA_WORD.fullmatch(tok):
        return False
    padded = tok + ("=" * (-len(tok) % 4))
    try:
        decoded = base64.b64decode(padded, validate=True).decode("utf-8")
        if ":" in decoded and all(32 <= ord(c) < 127 or c == "\t" for c in decoded):
            return True
    except Exception:
        pass
    if len(tok) >= 16:
        classes = sum(bool(re.search(p, tok)) for p in (r"[A-Z]", r"[a-z]", r"[0-9]", r"[+/=]"))
        if classes >= 3:
            return True
    return False


def _redact_bearer(m: re.Match[str]) -> str:
    if _PLAIN_ALPHA_WORD.fullmatch(m.group(3)):
        return m.group(0)
    return m.group(1) + m.group(2) + _REDACTED


def _redact_basic(m: re.Match[str]) -> str:
    if not _is_plausible_basic_token(m.group(3)):
        return m.group(0)
    return m.group(1) + m.group(2) + _REDACTED


def _redact_api_key_header(m: re.Match[str]) -> str:
    quoted = m[2] if m[2] is not None else m[3]
    val = quoted if quoted is not None else (m[4] or "")
    stripped = val.strip()
    if quoted is None and stripped.startswith("*"):
        return m[1] + _REDACTED
    if len(stripped) < 8 or stripped.lower() in _API_KEY_BENIGN_WORDS or _API_KEY_PLACEHOLDER.fullmatch(stripped):
        return m.group(0)
    if quoted is None:
        if _API_KEY_CODE_EXPR.fullmatch(stripped):
            return m[0]
        line_start = m.string.rfind("\n", 0, m.start()) + 1
        prefix = m.string[line_start:m.start()]
        # Source dictionaries can refer to variables. YAML flow mappings can use
        # the same quoted key with a literal bare scalar.
        if stripped.isidentifier() and m[1].rstrip().endswith(('":', "':")):
            assignment, brace, _ = prefix.partition("{")
            if brace and assignment.rstrip().endswith("="):
                return m[0]
        # Markdown field descriptions have a prose value spanning more than one word.
        if (prefix.strip() == "-" and _PLAIN_ALPHA_WORD.fullmatch(stripped)
                and m.string.startswith(" ", m.end()) and m.string[m.end() + 1:m.end() + 2].isalpha()):
            return m[0]
    if m[2] is not None:
        return m[1] + '"' + _REDACTED + '"'
    if m[3] is not None:
        return m[1] + "'" + _REDACTED + "'"
    return m[1] + _REDACTED


def _is_benign_auth_token(tok: str) -> bool:
    s = tok.strip()
    if not s or s.lower() in _API_KEY_BENIGN_WORDS or _API_KEY_PLACEHOLDER.fullmatch(s):
        return True
    if len(s) < 6:
        return True
    if len(s) < 8 and _PLAIN_ALPHA_WORD.fullmatch(s):
        return True
    return False


def _is_benign_auth_param_list(value: str) -> bool:
    empty_param = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_-]*)[ \t]*=", value)
    if empty_param and empty_param.group(1).lower() in _AUTH_EMPTY_PARAM_KEYS:
        return True
    if not _AUTH_PARAM_LIST_QUOTED.fullmatch(value):
        return False
    for pm in _AUTH_PARAM_PAIR.finditer(value):
        pkey = pm.group(1).lower()
        pval = pm.group(2) if pm.group(2) is not None else (pm.group(3) if pm.group(3) is not None else (pm.group(4) or ""))
        if pkey in _AUTH_NONSECRET_PARAMS:
            continue
        if not _is_benign_auth_token(pval):
            return False
    return True


def _redact_raw_credential_field(m: re.Match[str]) -> str:
    value = re.sub(r"[ \t]+#.*$", "", m[2].strip()).strip()
    if (
        not value
        or value[0] in ("'", '"')
        or _API_KEY_PLACEHOLDER.fullmatch(value)
        or _is_benign_auth_param_list(value)
        or value.lower() in _API_KEY_BENIGN_WORDS
        or any(ch.isspace() for ch in value)
    ):
        return m[0]
    return m[1] + _REDACTED


def _redact_auth_header(m: re.Match[str]) -> str:
    if m["alias"]:
        return m[1] + _REDACTED
    if m[2] is not None:
        quote, scheme, raw_val = '"', m[2], m[3] or ""
        unescaped = raw_val.replace('\\"', '"').strip()
    elif m[4] is not None:
        quote, scheme, raw_val = "'", m[4], m[5] or ""
        unescaped = raw_val.replace("\\'", "'").strip()
    else:
        quote, scheme, raw_val = "", m[6], m[7] or ""
        unescaped = raw_val.strip()

    if not unescaped:
        return m[0]
    if _is_benign_auth_param_list(unescaped):
        return m[0]
    if not _AUTH_PARAM_LIST_QUOTED.fullmatch(unescaped):
        if any(ch.isspace() for ch in unescaped):
            return m[0]
        if _is_benign_auth_token(unescaped) and (quote or not unescaped.startswith("*")):
            return m[0]
        if not quote and _PLAIN_ALPHA_WORD.fullmatch(unescaped) and re.match(r"[ \t]+[A-Za-z]", m.string[m.end():]):
            return m[0]
    if quote:
        return m[1] + quote + scheme + _REDACTED + quote
    return m[1] + scheme + _REDACTED


def _redact_heuristic_credentials(text: str) -> str:
    out = _RAW_CREDENTIAL_FIELD.sub(_redact_raw_credential_field, text)
    out = _AUTH_HEADER_VALUE.sub(_redact_auth_header, out)
    out = _API_KEY_HEADER_VALUE.sub(_redact_api_key_header, out)
    out = _BEARER_VALUE.sub(_redact_bearer, out)
    return _BASIC_VALUE.sub(_redact_basic, out)


def _live_credential_values() -> Iterator[str]:
    for name, value in os.environ.items():
        if _CREDENTIAL_ENV.search(name) and len(value) >= 8:
            yield value


def redact_secrets(text: str) -> str:
    out = _redact_heuristic_credentials(_PEM_BLOCK.sub(_REDACTED, str(text)))
    for pat in SECRET_VALUE_PATTERNS:
        out = pat.sub(_REDACTED, out)
    for value in _live_credential_values():
        out = out.replace(value, _REDACTED)
    return out


def redact_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            redact_secrets(k): _REDACTED if str(k).lower() in ("authorization", "proxy-authorization", *_API_KEY_HEADERS) else redact_data(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_data(v) for v in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


def safe_json(value: Any, **kwargs: Any) -> str:
    return json.dumps(redact_data(value), **kwargs)


def is_secret_filename(path: str) -> bool:
    if any(allow.search(path) for allow in SECRET_FILENAME_ALLOWLIST):
        return False
    return any(pat.search(path) for pat in SECRET_FILENAME_PATTERNS)


def scan_text_for_secrets(text: str, field_name: str, *, allow_marker: bool = True) -> list[str]:
    """Report a finding for sensitive text, honoring source markers by default."""
    text = text or ""
    # These rules inspect the original text, including marked lines and comments.
    if (
        PEM_START.search(text)
        or any(pattern.search(text) for pattern in SECRET_VALUE_PATTERNS)
        or any(value in text for value in _live_credential_values())
    ):
        return [f"Potential secret token in {redact_secrets(field_name)}"]
    if allow_marker and _ALLOW_MARKER.search(text):
        text = "\n".join("" if _ALLOW_MARKER.search(line) else line for line in text.split("\n"))
    if _redact_heuristic_credentials(text) != text:
        return [f"Potential secret token in {redact_secrets(field_name)}"]
    return []
