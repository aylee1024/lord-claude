#!/usr/bin/env python3
"""schema_util.py — make any backend return JSON that matches a caller-supplied JSON Schema.

No engine has native structured output, so the mechanism is uniform for grok/composer/codex/gemini:
prepend a compact "reply as ONLY this JSON" directive, parse the first JSON value out of the reply,
validate it (jsonschema if importable, else a stdlib required-keys/type fallback), and on failure
re-send the same turn with the validator error appended, up to SCHEMA_RETRIES times.

Pure helpers (extract_first_json / validate_obj / build_directive) are import-and-test clean; the
loop (run_with_schema) takes a send-callable so it needs no live backend to test.
"""
import json, os

SCHEMA_RETRIES = int(os.environ.get("SESSION_SCHEMA_RETRIES", "2"))

try:
    import jsonschema as _jsonschema
    _HAVE_JSONSCHEMA = True
except Exception:
    _jsonschema = None
    _HAVE_JSONSCHEMA = False


class SchemaUnmet(Exception):
    """Raised when the model could not produce schema-valid JSON within the retry budget."""
    def __init__(self, attempts, last_error, last_raw):
        self.attempts = attempts
        self.last_error = last_error
        self.last_raw = last_raw
        super().__init__(f"schema not satisfied after {attempts} attempt(s): {last_error}")


def _strip_code_fence(text):
    """If the reply wraps its JSON in a ``` / ```json fence, return the fence body; else the text."""
    s = text.strip()
    if "```" not in s:
        return s
    parts = s.split("```")
    # parts: [before, fenced0, between, fenced1, ...]; odd indices are fenced blocks
    for i in range(1, len(parts), 2):
        block = parts[i]
        # a fenced block may start with a language tag line, e.g. "json\n{...}"
        nl = block.find("\n")
        if nl != -1 and block[:nl].strip().lower() in ("json", "json5", ""):
            cand = block[nl + 1:]
        else:
            cand = block
        if "{" in cand or "[" in cand:
            return cand.strip()
    return s


def extract_first_json(text):
    """Return the first complete JSON value (object or array) found in `text`, or None.

    Scans for the first '{' or '[' and walks forward honoring string literals + escapes so that
    braces inside strings don't confuse the depth count. Falls back to parsing the whole (de-fenced)
    text. Tolerates leading prose ("Here is the JSON: {...}") and trailing junk."""
    if text is None:
        return None
    body = _strip_code_fence(text)
    # fast path: the whole thing is already JSON
    try:
        return json.loads(body)
    except Exception:
        pass
    n = len(body)
    tried = 0
    for start in range(n):
        c = body[start]
        if c not in "{[":
            continue
        tried += 1
        if tried > 4096:        # bound worst-case O(N^2) on pathological "{{{{..." input (real JSON is at opener 1-2)
            break
        opener = c
        closer = "}" if c == "{" else "]"
        depth = 0
        in_str = False
        esc = False
        for i in range(start, n):
            ch = body[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    chunk = body[start:i + 1]
                    try:
                        return json.loads(chunk)
                    except Exception:
                        break   # this opener didn't yield valid JSON; try the next opener
    return None


def _stdlib_validate(obj, schema):
    """Minimal fallback when jsonschema is unavailable: enforce top-level type, required keys, and
    each declared property's primitive JSON type. Not a full validator — just a portable safety net."""
    errs = []
    _TYPES = {"object": dict, "array": list, "string": str, "number": (int, float),
              "integer": int, "boolean": bool, "null": type(None)}

    def check(o, sch, path):
        t = sch.get("type")
        if isinstance(t, str) and t in _TYPES:
            py = _TYPES[t]
            # JSON has no separate bool/int: a bool is not an acceptable integer/number
            if t in ("integer", "number") and isinstance(o, bool):
                errs.append(f"{path or 'value'}: expected {t}, got boolean")
            elif not isinstance(o, py):
                errs.append(f"{path or 'value'}: expected {t}, got {type(o).__name__}")
                return
        if sch.get("type") == "object" and isinstance(o, dict):
            for rk in sch.get("required", []) or []:
                if rk not in o:
                    errs.append(f"{path or 'value'}: missing required key '{rk}'")
            props = sch.get("properties", {}) or {}
            for k, subsch in props.items():
                if k in o and isinstance(subsch, dict):
                    check(o[k], subsch, f"{path}.{k}" if path else k)
        if sch.get("type") == "array" and isinstance(o, list):
            items = sch.get("items")
            if isinstance(items, dict):
                for idx, el in enumerate(o):
                    check(el, items, f"{path}[{idx}]")

    check(obj, schema, "")
    return errs


def validate_obj(obj, schema):
    """Return (ok: bool, error: str). Uses jsonschema when present (full Draft validation), else the
    stdlib fallback above."""
    if _HAVE_JSONSCHEMA:
        validator_cls = _jsonschema.validators.validator_for(schema)
        try:
            validator_cls.check_schema(schema)
        except Exception as e:
            return False, f"invalid schema: {e}"
        validator = validator_cls(schema)
        errors = sorted(validator.iter_errors(obj), key=lambda e: list(e.path))
        if not errors:
            return True, ""
        msgs = []
        for e in errors[:5]:
            loc = "/".join(str(p) for p in e.path) or "(root)"
            msgs.append(f"at {loc}: {e.message}")
        return False, "; ".join(msgs)
    errs = _stdlib_validate(obj, schema)
    return (len(errs) == 0), ("; ".join(errs))


def build_directive(schema):
    """Compact instruction appended to the user's prompt asking for schema-valid JSON only."""
    compact = json.dumps(schema, separators=(",", ":"))
    return ("\n\n---\nReply with ONLY a single JSON value that validates against this JSON Schema. "
            "Output nothing else: no explanation, no prose, no markdown code fence.\n"
            f"JSON Schema:\n{compact}")


def _retry_directive(error, raw):
    snippet = (raw or "").strip()
    if len(snippet) > 600:
        snippet = snippet[:600] + " …(truncated)"
    return ("\n\n---\nYour previous reply did NOT satisfy the schema.\n"
            f"Validator error: {error}\n"
            f"Your previous reply was:\n{snippet}\n\n"
            "Reply again with ONLY the corrected JSON value — nothing else, no code fence.")


def run_with_schema(send_fn, text, schema, retries=None, log=lambda *_: None, should_abort=None):
    """Drive `send_fn(prompt) -> reply_text` until the reply parses to schema-valid JSON.

    Returns (obj, raw_reply): the validated Python object and the raw model reply it came from.
    Raises SchemaUnmet after `retries`+1 total attempts. `send_fn` is the warm backend's send, so each
    attempt is a real turn on the same session — the retry sees its own bad answer plus the error.
    If `should_abort()` becomes true (e.g. the turn was cancelled), stop retrying and return the last
    reply best-effort `(obj_or_None, raw)` — the caller decides what to do with a non-validated reply."""
    if retries is None:
        retries = SCHEMA_RETRIES
    prompt = text + build_directive(schema)
    last_err = "no reply"
    last_raw = ""
    last_obj = None
    for attempt in range(retries + 1):
        raw = send_fn(prompt)
        last_raw = raw or ""
        obj = extract_first_json(raw or "")
        last_obj = obj
        if obj is None:
            last_err = "no JSON value found in reply"
        else:
            ok, err = validate_obj(obj, schema)
            if ok:
                if attempt:
                    log(f"schema satisfied on retry {attempt}")
                return obj, raw
            last_err = err
        if should_abort is not None and should_abort():
            log("schema loop aborted (turn cancelled)")
            return last_obj, last_raw
        log(f"schema attempt {attempt + 1}/{retries + 1} failed: {last_err}")
        # re-include the schema on retry (a stateless send_fn would otherwise lose it; a warm session
        # already remembers it, so this is cheap insurance) plus the validator error + the bad reply.
        prompt = text + build_directive(schema) + _retry_directive(last_err, last_raw)
    raise SchemaUnmet(retries + 1, last_err, last_raw)


if __name__ == "__main__":
    # tiny self-check
    sch = {"type": "object", "required": ["n", "ok"],
           "properties": {"n": {"type": "integer"}, "ok": {"type": "boolean"}}}
    assert extract_first_json('blah {"n": 3, "ok": true} trailing') == {"n": 3, "ok": True}
    assert extract_first_json('```json\n{"n":3,"ok":true}\n```') == {"n": 3, "ok": True}
    assert validate_obj({"n": 3, "ok": True}, sch)[0] is True
    assert validate_obj({"n": "x", "ok": True}, sch)[0] is False
    seq = iter(['not json', '{"n": 1, "ok": true}'])
    obj, raw = run_with_schema(lambda p: next(seq), "count", sch, retries=2)
    assert obj == {"n": 1, "ok": True}, obj
    print("schema_util self-check OK (jsonschema=%s)" % _HAVE_JSONSCHEMA)
