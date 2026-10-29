"""Target normalization and classification.

Target `type` values: domain, smart contract, source code, mobile, other, api, ip,
cidr, hardware, blockchain-dlt, iot, executable, network, desktop. `wildcard` is a flag plus the
'wildcard' scope tag. scope_tags additionally carry web/api/mobile/etc; language_tags carry languages.
"""
import re, ipaddress, urllib.parse

LANGS = {"solidity", "rust", "go", "javascript", "typescript", "python", "ruby", "java", "kotlin", "swift", "c", "c++", "c#",
         "php", "move", "cairo", "vyper", "clarity", "scala", "elixir", "dart", "objective-c", "haskell", "ocaml", "zig", "lua",
         "perl", "shell", "wasm", "assembly", "bitcoin script", "sway", "noir", "circom", "fe", "huff", "yul"}
LANG_ALIASES = {"golang": "go", "js": "javascript", "ts": "typescript", "node": "javascript", "nodejs": "javascript", "node.js": "javascript",
                "cpp": "c++", "csharp": "c#", "c sharp": "c#", "objc": "objective-c", "sol": "solidity", "py": "python", "rb": "ruby"}

MOBILE_HOSTS = ("play.google.com", "apps.apple.com", "itunes.apple.com", "testflight.apple.com", "appgallery.huawei.com", "apps.microsoft.com")
CHAIN_EXPLORERS = ("etherscan.io", "bscscan.com", "polygonscan.com", "arbiscan.io", "basescan.org", "optimistic.etherscan.io", "snowtrace.io",
                   "ftmscan.com", "explorer.zksync.io", "lineascan.build", "scrollscan.com", "blastscan.io", "solscan.io", "explorer.solana.com",
                   "nearblocks.io", "tronscan.org", "suiscan.xyz", "suivision.xyz", "explorer.aptoslabs.com", "starkscan.co", "voyager.online",
                   "gnosisscan.io", "celoscan.io", "cronoscan.com", "moonscan.io", "era.zksync.network", "mantlescan.xyz", "sonicscan.org",
                   "explorer.inkonchain.com", "explorer.kaia.io", "kaiascan.io", "explorer.hedera.com", "hashscan.io", "explorer.stacks.co",
                   "explorer.hiro.so", "tonviewer.com", "tonscan.org", "explorer.zora.energy", "monadexplorer.com", "berascan.com")
REPO_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "gitea.com", "sr.ht", "huggingface.co")

def norm_lang(s):
    s = (s or "").strip().lower()
    s = LANG_ALIASES.get(s, s)
    return s if s in LANGS else None

def is_ip(s):
    try:
        ipaddress.ip_address(s); return True
    except Exception:
        return False

def is_cidr(s):
    try:
        ipaddress.ip_network(s, strict=False); return "/" in s
    except Exception:
        return False

_host_re = re.compile(r"^(\*\.)?([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}\.?$", re.I)
_evm_addr = re.compile(r"\b0x[0-9a-fA-F]{40}\b")

def repo_from_url(u):
    """Return (repo_url, path_kind, path_prefix, provider, host, full_name) or None."""
    try:
        p = urllib.parse.urlparse(u if "://" in u else "https://" + u)
    except Exception:
        return None
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in REPO_HOSTS:
        return None
    if any(ch in p.path for ch in "*,; ") or "://" in p.path:
        return None   # org wildcards (acme/*) and comma-joined scope strings are not pollable repositories
    parts = [x for x in p.path.split("/") if x]
    if host == "gitlab.com":
        # gitlab supports nested groups: take everything up to '/-/' or the first known subresource
        path_parts = []
        rest = []
        for i, x in enumerate(parts):
            if x == "-" or x in ("blob", "tree", "commit", "commits", "raw", "releases", "issues", "merge_requests"):
                rest = parts[i:]
                break
            path_parts.append(x)
        if len(path_parts) < 2:
            return None
        full = "/".join(path_parts)
        kind = "repository"; prefix = None
        if rest:
            # /-/blob/<ref>/path or /-/tree/<ref>/path
            r = [x for x in rest if x != "-"]
            if r and r[0] in ("blob", "tree", "raw") and len(r) >= 3:
                kind = "file" if r[0] == "blob" else "directory"; prefix = "/".join(r[2:]) or None
        return (f"https://gitlab.com/{full}", kind, prefix, "gitlab", "gitlab.com", full)
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if host == "github.com" and owner in ("orgs", "topics", "search", "settings", "marketplace", "features", "sponsors", "apps", "login"):
        return None
    full = f"{owner}/{repo}"
    kind = "repository"; prefix = None
    if len(parts) > 2:
        sub = parts[2]
        if sub in ("blob", "tree", "raw") and len(parts) >= 5:
            kind = "file" if sub == "blob" else "directory"; prefix = "/".join(parts[4:]) or None
        elif sub in ("blob", "tree"):
            kind = "directory"
        else:
            kind = "other"
    provider = {"github.com": "github", "gitlab.com": "gitlab", "bitbucket.org": "bitbucket", "codeberg.org": "codeberg"}.get(host, host.split(".")[0])
    return (f"https://{host}/{full}", kind, prefix, provider, host, full)

def classify(identifier, hint_type=None, display_name=None, description=None):
    """Return (type, wildcard, scope_tags, language_tags, extra). hint_type is the platform's own type string."""
    ident = (identifier or "").strip()
    low = ident.lower()
    hint = (hint_type or "").strip().lower()
    tags = set(); langs = set(); wildcard = False; ttype = None
    text = " ".join(x for x in (hint, display_name or "", description or "") if x).lower()

    # explicit platform hints first
    if hint in ("wildcard",):
        wildcard = True
    if any(k in hint for k in ("smart contract", "smart_contract", "smart contracts", "contract")) or any(h in low for h in CHAIN_EXPLORERS) or _evm_addr.search(ident):
        ttype = "smart contract"
    elif any(k in hint for k in ("source code", "source_code", "sourcecode", "repository", "code", "github")) or repo_from_url(ident):
        ttype = "source code"
    elif any(k in hint for k in ("android", "ios", "mobile", "apk", "ipa", "google_play", "apple_store", "app store", "application-android", "application-ios")) or any(h in low for h in MOBILE_HOSTS):
        ttype = "mobile"
    elif any(k in hint for k in ("hardware", "device", "firmware")):
        ttype = "hardware"
    elif any(k in hint for k in ("iot",)):
        ttype = "iot"
    elif any(k in hint for k in ("blockchain", "dlt", "protocol", "node", "chain")) and "smart" not in hint and "web" not in hint:
        ttype = "blockchain-dlt"
    elif any(k in hint for k in ("executable", "downloadable", "binary", "desktop", "application-desktop", "windows", "macos")):
        ttype = "executable"
    elif any(k in hint for k in ("cidr", "ip_address", "ip range", "ip-address")) or is_cidr(low):
        ttype = "cidr" if is_cidr(low) else ("ip" if is_ip(low) else "cidr")
    elif is_ip(low):
        ttype = "ip"
    elif any(k in hint for k in ("api",)) and "application" not in hint:
        ttype = "api"
    elif any(k in hint for k in ("network", "infrastructure")):
        ttype = "network"
    elif hint in ("ai_model", "ai model", "llm", "ai"):
        ttype = "other"; tags.add("ai")

    # URL / domain based classification
    hostpart = None
    if ttype is None or ttype in ("api",):
        cand = low
        if "://" in cand:
            try:
                pu = urllib.parse.urlparse(cand); hostpart = pu.hostname or ""
                path = pu.path or ""
            except Exception:
                hostpart = None; path = ""
        else:
            hostpart = cand.split("/")[0].split("?")[0].split(":")[0]; path = cand[len(hostpart):]
        if hostpart and hostpart.startswith("*."):
            wildcard = True
        if hostpart and _host_re.match(hostpart):
            if ttype is None:
                ttype = "domain"
            if hostpart.startswith("api.") or "/api" in path or "api-" in hostpart or ".api." in hostpart or "graphql" in path or "graphql" in hostpart:
                tags.add("api")
            if any(k in hostpart for k in ("staging", "stage.", "dev.", "-dev", "sandbox", "test.", "-test", "qa.", "uat.", "preprod", "pre-prod", "demo.")):
                tags.add("staging")
        elif ttype is None and ("*" in cand and "." in cand and " " not in cand):
            wildcard = True; ttype = "domain"
    if ttype is None:
        ttype = "other"

    # scope tags
    tags.add(ttype)
    if ttype == "domain":
        tags.add("domain")
        if "api" in text:
            tags.add("api")
        if any(k in text for k in ("web", "website", "url", "application")) or "://" in low:
            tags.add("web")
    if ttype == "mobile":
        tags.add("mobile")
        if "android" in text or "play.google" in low: tags.add("android")
        if "ios" in text or "apple.com" in low: tags.add("ios")
    if wildcard:
        tags.add("wildcard")
    if "graphql" in text:
        tags.add("graphql")
    if "ai" in hint.split("_") or "llm" in text or "ai model" in text:
        tags.add("ai")
    # languages from hint text
    for w in re.split(r"[,/;|\s]+", text):
        l = norm_lang(w)
        if l:
            langs.add(l)
    if ttype == "smart contract" and not langs:
        if any(h in low for h in ("solscan", "solana")): langs.add("rust")
        elif any(h in low for h in ("suiscan", "suivision", "aptos")): langs.add("move")
        elif any(h in low for h in ("starkscan", "voyager")): langs.add("cairo")
        elif any(h in low for h in ("stacks.co", "hiro.so")): langs.add("clarity")
        elif _evm_addr.search(ident) or any(h in low for h in CHAIN_EXPLORERS): langs.add("solidity")
    if ttype == "source code":
        if low.endswith(".sol"): langs.add("solidity")
        elif low.endswith(".rs"): langs.add("rust")
        elif low.endswith(".go"): langs.add("go")
        elif low.endswith(".py"): langs.add("python")
        elif low.endswith((".ts", ".tsx")): langs.add("typescript")
        elif low.endswith((".js", ".jsx")): langs.add("javascript")
    return ttype, wildcard, sorted(tags), sorted(langs)

def normalize_identifier(identifier, ttype=None, wildcard=False):
    """Display normalization: wildcard host -> base host; URLs lowercased host; strip trailing slash."""
    s = (identifier or "").strip()
    if not s:
        return s
    if ttype in ("source code", "smart contract", "mobile") or "://" in s and ttype not in ("domain", "api"):
        if "://" in s:
            try:
                p = urllib.parse.urlparse(s)
                return urllib.parse.urlunparse((p.scheme.lower(), (p.netloc or "").lower(), p.path.rstrip("/") if p.path != "/" else "", p.params, p.query, p.fragment)).rstrip("/") or s
            except Exception:
                return s
        return s
    low = s.lower()
    if "://" in low:
        try:
            p = urllib.parse.urlparse(low)
            host = p.hostname or ""
            path = p.path.rstrip("/")
            if host.startswith("*."):
                host = host[2:]
            return host + path + (("?" + p.query) if p.query else "")
        except Exception:
            return low
    if low.startswith("*."):
        return low[2:].rstrip("/")
    return low.rstrip("/")

def norm_key(identifier):
    """Identity key: aggressive, stable across platforms' formatting quirks."""
    s = (identifier or "").strip().lower()
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.rstrip("/").rstrip(".")
    s = re.sub(r"\s+", " ", s)
    return s
