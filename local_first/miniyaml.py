"""A tiny YAML-subset reader so task files need no third-party package.

Supported: `key: value` maps (nested by indentation), `- item` lists of scalars, inline lists `[a, "b c"]`,
`|` and `>` block text, quoted strings, true/false/null, numbers, and `# comments`.
Not supported: anchors, multi-document files, lists of maps, inline maps. Use JSON for anything fancier.
"""
import json
import re


class YamlError(ValueError):
    pass


def _strip_comment(s):
    q = None
    for i, ch in enumerate(s):
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i].rstrip()
    return s.rstrip()


def _split_inline(s):
    out, cur, q = [], "", None
    for ch in s:
        if q:
            cur += ch
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
            cur += ch
        elif ch == ",":
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return [x.strip() for x in out]


def _scalar(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        try:
            return json.loads(s)
        except ValueError:
            return s[1:-1]
    if len(s) >= 2 and s[0] == s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if s.startswith("["):
        if not s.endswith("]"):
            raise YamlError(f"unterminated list: {s}")
        return [_scalar(x) for x in _split_inline(s[1:-1])]
    if s.startswith("{"):
        raise YamlError("inline maps are not supported; use JSON for this file")
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d+", s):
        return float(s)
    return s


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _skip(lines, i):
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        i += 1
    return i


def _block_text(lines, i, parent, style):
    body, ind = [], None
    while i < len(lines):
        if lines[i].strip() and _indent(lines[i]) <= parent:
            break
        if lines[i].strip() and ind is None:
            ind = _indent(lines[i])
        body.append(lines[i][ind:] if ind is not None and lines[i].strip() else "")
        i += 1
    while body and not body[-1]:
        body.pop()
    text = "\n".join(body) if style.startswith("|") else " ".join(x for x in body if x)
    return text + ("\n" if not style.endswith("-") and text else ""), i


def _list(lines, i, indent):
    out = []
    while True:
        i = _skip(lines, i)
        if i >= len(lines) or _indent(lines[i]) != indent or not lines[i].lstrip().startswith("-"):
            break
        item = _strip_comment(lines[i].lstrip()[1:].strip())
        if re.match(r"^[A-Za-z0-9_.-]+:(\s|$)", item):
            raise YamlError(f"line {i + 1}: lists of maps are not supported; use JSON for this file")
        out.append(_scalar(item))
        i += 1
    return out, i


def _map(lines, i, indent):
    out = {}
    while True:
        i = _skip(lines, i)
        if i >= len(lines):
            break
        ind = _indent(lines[i])
        if ind < indent:
            break
        if ind > indent:
            raise YamlError(f"line {i + 1}: unexpected indentation")
        content = _strip_comment(lines[i].strip())
        m = re.match(r"^([A-Za-z0-9_.-]+|\"[^\"]*\"|'[^']*')\s*:(?:\s+(.*))?$", content)
        if not m:
            if content.startswith("-"):
                break
            raise YamlError(f"line {i + 1}: expected 'key: value', got: {content}")
        key, rest = m.group(1).strip("\"'"), (m.group(2) or "").strip()
        i += 1
        if re.fullmatch(r"[|>]-?", rest):
            out[key], i = _block_text(lines, i, indent, rest)
        elif rest == "":
            j = _skip(lines, i)
            if j < len(lines) and (_indent(lines[j]) > indent or (_indent(lines[j]) == indent and lines[j].lstrip().startswith("-"))):
                out[key], i = _value_block(lines, j)
            else:
                out[key] = None
        else:
            out[key] = _scalar(rest)
    return out, i


def _value_block(lines, i):
    i = _skip(lines, i)
    if i >= len(lines):
        return None, i
    ind = _indent(lines[i])
    if lines[i].lstrip().startswith("-"):
        return _list(lines, i, ind)
    return _map(lines, i, ind)


def loads(text):
    text = text.expandtabs(2)
    if text.lstrip().startswith("{"):
        return json.loads(text)
    lines = text.splitlines()
    val, i = _value_block(lines, 0)
    if _skip(lines, i) < len(lines):
        raise YamlError(f"line {_skip(lines, i) + 1}: could not read this line")
    return val if val is not None else {}
