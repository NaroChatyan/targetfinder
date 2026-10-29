"""Helpers for Next.js React Server Components payloads (self.__next_f.push chunks)."""
import re, json

def blob(html):
    chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.S)
    out = []
    for c in chunks:
        try:
            out.append(json.loads('"' + c + '"'))
        except Exception:
            continue
    return "".join(out)

def extract_array(text, key):
    """Find '"key":[' and return the parsed JSON array (bracket-balanced), or None."""
    i = text.find(f'"{key}":[')
    if i < 0:
        return None
    j = i + len(key) + 3
    depth = 0; in_str = False; esc = False
    for k in range(j, len(text)):
        c = text[k]
        if in_str:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': in_str = False
            continue
        if c == '"': in_str = True
        elif c == "[": depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[j:k + 1])
                except Exception:
                    return None
    return None

def extract_object(text, key):
    i = text.find(f'"{key}":{{')
    if i < 0:
        return None
    j = i + len(key) + 3
    depth = 0; in_str = False; esc = False
    for k in range(j, len(text)):
        c = text[k]
        if in_str:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': in_str = False
            continue
        if c == '"': in_str = True
        elif c == "{": depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[j:k + 1])
                except Exception:
                    return None
    return None
