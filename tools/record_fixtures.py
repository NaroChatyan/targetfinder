#!/usr/bin/env python3
"""Record trimmed collector fixtures (one listing sample ≤3 programs + one scope/detail sample per platform) into tests/fixtures/.

Network tool for maintainers; the test-suite never touches the network. Re-run it when a platform changes its payloads.
HTML payloads are reduced to the data script only, re-encoded in the platform's own format (Nuxt devalue, Next.js RSC chunks)
so the same parsing code runs on the fixture and on the live page.
Usage: python3 tools/record_fixtures.py [platform ...] [--immunefi-from LISTING.html SCOPE.html]
"""
import os, sys, json, re, html as htmlmod
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from targetfinder import http
from targetfinder.collectors import hackerone, hackenproof, rsc, btd

OUT = os.path.join(ROOT, "tests", "fixtures")
N = 3

def save(platform, name, obj, raw=False):
    d = os.path.join(OUT, platform); os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(obj if raw else json.dumps(obj, indent=1, ensure_ascii=False, sort_keys=True))
    print(f"wrote {os.path.relpath(p, ROOT)} ({os.path.getsize(p)} bytes)")

# ---- encoders -----------------------------------------------------------------------------------
def devalue_encode(obj):
    """Inverse of hackenproof.devalue for plain JSON values: returns the flat index list Nuxt embeds in __NUXT_DATA__."""
    out = []
    def enc(v):
        i = len(out); out.append(None)
        if isinstance(v, dict):
            out[i] = {k: enc(x) for k, x in v.items()}
        elif isinstance(v, list):
            out[i] = [enc(x) for x in v]
        else:
            out[i] = v
        return i
    enc(obj)
    return out

def nuxt_html(root):
    return '<!doctype html><html><head></head><body><script type="application/json" id="__NUXT_DATA__" data-ssr="true">' + json.dumps(devalue_encode(root), ensure_ascii=False) + '</script></body></html>\n'

def rsc_html(obj, key):
    """A minimal Next.js page whose flight payload carries {"key": obj} across two push chunks (as real pages do)."""
    payload = json.dumps({key: obj}, ensure_ascii=False, separators=(",", ":"))   # real flight payloads carry no spaces
    text = f'0:["$","div",null,{{"children":{payload}}}]\n'
    mid = len(text) // 2
    def chunk(s): return json.dumps(s, ensure_ascii=False)[1:-1]
    return f'<!doctype html><html><body><script>self.__next_f=self.__next_f||[];self.__next_f.push([0])</script><script>self.__next_f.push([1,"{chunk(text[:mid])}"])</script><script>self.__next_f.push([1,"{chunk(text[mid:])}"])</script></body></html>\n'

# ---- platforms ------------------------------------------------------------------------------------
def rec_hackerone():
    q = {"operationName": "DirectoryQuery", "query": hackerone.DIRECTORY, "variables": {"first": N, "after": None, "where": hackerone._where(), "secureOrderBy": {"launched_at": {"_direction": "DESC"}}}}
    d = http.post_json(hackerone.GQL, q, headers={"Accept": "application/json"})
    teams = d["data"]["teams"]; teams["pageInfo"]["hasNextPage"] = False
    for e in teams["edges"]:
        e["node"]["structured_scopes"]["edges"] = e["node"]["structured_scopes"]["edges"][:6]
    save("hackerone", "directory.json", d)
    handle = teams["edges"][0]["node"]["handle"]
    s = http.post_json(hackerone.GQL, {"operationName": "Scopes", "query": hackerone.SCOPES_PAGE, "variables": {"handle": handle, "after": None}}, headers={"Accept": "application/json"})
    ss = s["data"]["team"]["structured_scopes"]; ss["edges"] = ss["edges"][:6]; ss["pageInfo"]["hasNextPage"] = False
    save("hackerone", "scopes.json", s)

def rec_bugcrowd():
    d = http.get_json("https://bugcrowd.com/engagements.json?page=1", headers={"Accept": "application/json"})
    d["engagements"] = [e for e in d["engagements"] if not e.get("isDemo")][:N]
    d["paginationMeta"] = {"totalCount": len(d["engagements"]), "limit": 25}
    save("bugcrowd", "engagements.json", d)
    slugs = [e["briefUrl"].strip("/").split("/")[-1] for e in d["engagements"]]
    data = [p for p in btd.load("bugcrowd") if p.get("url", "").rstrip("/").split("/")[-1] in slugs]
    for p in data:
        for k in ("in_scope", "out_of_scope"):
            p["targets"][k] = p["targets"].get(k, [])[:6]
    save("bugcrowd", "bounty_targets_data.json", data)
    slug = slugs[0]
    t = http.get_text(f"https://bugcrowd.com/engagements/{slug}")
    m = re.search(r"/engagements/[^\"&]+/changelog/([0-9a-f-]{36})", htmlmod.unescape(t))
    if m:
        c = http.get_json(f"https://bugcrowd.com/engagements/{slug}/changelog/{m.group(1)}.json", headers={"Accept": "application/json"})
        for grp in (c.get("data") or {}).get("scope") or []:
            grp["targets"] = (grp.get("targets") or [])[:6]
        save("bugcrowd", "changelog.json", c)
        save("bugcrowd", "brief.html", f'<html><body><a href="/engagements/{slug}/changelog/{m.group(1)}">changelog</a></body></html>\n', raw=True)

def rec_intigriti():
    items = http.get_json("https://app.intigriti.com/api/core/public/programs", headers={"Accept": "application/json"})
    known = {(p.get("company_handle"), p.get("handle")) for p in btd.load("intigriti")}   # only programs whose scope is public
    items = [e for e in items if e.get("status") == 3 and (e.get("companyHandle"), e.get("handle")) in known][:N]
    save("intigriti", "programs.json", items)
    want = {(e["companyHandle"], e["handle"]) for e in items}
    data = [p for p in btd.load("intigriti") if (p.get("company_handle"), p.get("handle")) in want]
    for p in data:
        for k in ("in_scope", "out_of_scope"):
            p["targets"][k] = p["targets"].get(k, [])[:6]
    save("intigriti", "bounty_targets_data.json", data)

def rec_yeswehack():
    d = http.get_json("https://api.yeswehack.com/programs?page=1")
    d["items"] = [e for e in d["items"] if not e.get("demo")][:N]; d["pagination"] = {"nb_pages": 1, "page": 1}
    save("yeswehack", "programs.json", d)
    slug = d["items"][0]["slug"]
    det = http.get_json(f"https://api.yeswehack.com/programs/{slug}")
    det["scopes"] = (det.get("scopes") or [])[:6]; det["out_of_scope"] = (det.get("out_of_scope") or [])[:3]
    for k in ("rules_html", "rules", "description_html", "description", "reports_sla", "business_unit"):
        det.pop(k, None)
    save("yeswehack", "program_detail.json", det)

def rec_hackenproof():
    root = hackenproof.nuxt_data(http.get_text("https://hackenproof.com/programs?page=1"))
    d = root["data"]["programs-api-bounty"]
    progs = d["programs"][:N]
    save("hackenproof", "programs.html", nuxt_html({"data": {"programs-api-bounty": {"programs": progs, "total_items": len(progs), "next_page": None}}}), raw=True)
    det = None
    for cand in d["programs"][:12]:   # prefer a program that also lists an out-of-scope entry, so both branches are covered
        det = hackenproof._detail(cand["slug"], print); slug = cand["slug"]
        if det and any(isinstance(s, dict) and s.get("out_of_scope") for s in det.get("scopes") or []):
            break
    print(f"hackenproof detail sample: {slug}")
    for k in ("description", "rules", "policy", "content", "rewardsDescription", "html"):
        det.pop(k, None)
    for key in ("scopes", "outOfScope", "out_of_scope", "inScope", "in_scope"):
        if isinstance(det.get(key), list): det[key] = det[key][:6]
    save("hackenproof", "program_detail.html", nuxt_html({"data": {f"programs-{slug}": det}}), raw=True)

def rec_sherlock():
    from targetfinder.collectors import sherlock
    b = rsc.blob(http.get_text(sherlock.LIST))
    items = sherlock._find_bounties(b)[:N]
    save("sherlock", "bug_bounties.html", rsc_html(items, "bounties"), raw=True)
    det = {"scope": []}
    for e in sherlock._find_bounties(b)[:8]:   # prefer a bounty whose detail page lists scope items
        bid = e.get("id") or e.get("slug")
        det = sherlock._detail(str(bid), print) or {"scope": []}
        if det["scope"]:
            break
    print(f"sherlock detail sample: {bid} ({len(det['scope'])} scope items)")
    save("sherlock", "bounty_detail.html", rsc_html(det["scope"][:6], "scope"), raw=True)

def rec_immunefi(listing_path=None, scope_path=None):
    """Immunefi is disabled by default (terms); record from previously saved pages when paths are given."""
    from targetfinder.collectors import immunefi
    lst = open(listing_path, encoding="utf-8", errors="replace").read() if listing_path else http.get_text(immunefi.LIST)
    items = (rsc.extract_array(rsc.blob(lst), "bounties") or [])[:N]
    for e in items:
        for k in ("description", "rules", "content", "overview", "faq", "programOverview"):
            e.pop(k, None)
    save("immunefi", "bug_bounty.html", rsc_html(items, "bounties"), raw=True)
    slug = items[0]["slug"]
    sc = open(scope_path, encoding="utf-8", errors="replace").read() if scope_path else http.get_text(f"https://immunefi.com/bug-bounty/{slug}/scope/")
    assets = (rsc.extract_array(rsc.blob(sc), "assets") or [])[:6]
    save("immunefi", "scope.html", rsc_html(assets, "assets"), raw=True)

if __name__ == "__main__":
    args = sys.argv[1:]
    imm_from = None
    if "--immunefi-from" in args:
        i = args.index("--immunefi-from"); imm_from = (args[i + 1], args[i + 2]); del args[i:i + 3]
    plats = args or ["hackerone", "bugcrowd", "intigriti", "yeswehack", "hackenproof", "sherlock"] + (["immunefi"] if imm_from else [])
    for p in plats:
        print(f"== {p}")
        if p == "immunefi": rec_immunefi(*(imm_from or (None, None)))
        else: globals()["rec_" + p]()
