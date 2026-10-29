"""Every collector is a pure parser over recorded fixtures (tests/fixtures/<platform>/) plus a thin collect() that fetches. No network here."""
import json, os, unittest
from tests import helpers  # noqa: F401  (sys.path)
from targetfinder.collectors import hackerone, bugcrowd, intigriti, yeswehack, hackenproof, sherlock, immunefi, program_id

FX = os.path.join(helpers.ROOT, "tests", "fixtures")

def load(platform, name):
    p = os.path.join(FX, platform, name)
    with open(p, encoding="utf-8") as f:
        return json.load(f) if name.endswith(".json") else f.read()

def by_id(programs, pid):
    return next(p for p in programs if p["id"] == pid)

def by_ident(targets, ident):
    return next(t for t in targets if t["identifier"] == ident)


class HackerOne(unittest.TestCase):
    def test_parse_listing(self):
        ps = hackerone.parse_listing(load("hackerone", "directory.json"))
        self.assertEqual(len(ps), 3)
        p = by_id(ps, "hackerone:xp_inc_vdp")
        self.assertEqual((p["platform"], p["handle"], p["program_kind"], p["reward_status"], p["program_status"]), ("HackerOne", "xp_inc_vdp", "vdp", "no_reward", "active"))
        self.assertEqual(p["report_count"], 1)
        t = by_ident(p["targets"], "xpi.com.br")
        self.assertTrue(t["in_scope"]); self.assertFalse(t["eligible_for_bounty"]); self.assertEqual(t["hint_type"], "URL")
        w = by_ident(by_id(ps, "hackerone:verisk_vdp")["targets"], "*.iso.com")
        self.assertEqual(w["hint_type"], "WILDCARD")
        self.assertTrue(any(f["metric"] == "reports_resolved_total" for f in p["payout_facts"]))

    def test_parse_scope_classifies_wildcard(self):
        ts = hackerone.parse_scope(load("hackerone", "scopes.json"), "xp_inc_vdp")
        self.assertEqual(len(ts), 6)
        t = by_ident(ts, "xpi.com.br")
        self.assertEqual(t["source_url"], "https://hackerone.com/xp_inc_vdp/policy_scopes?type=team")
        self.assertIn("created_at", t["metadata"]["history_source"])


class Bugcrowd(unittest.TestCase):
    def test_parse_listing(self):
        ps = bugcrowd.parse_listing(load("bugcrowd", "engagements.json"))
        self.assertEqual(len(ps), 3)
        p = by_id(ps, "bugcrowd:engagements/bitso-mbb-og")
        self.assertEqual((p["platform"], p["handle"], p["program_kind"], p["bounty_min"], p["bounty_max"]), ("Bugcrowd", "engagements/bitso-mbb-og", "bounty", 100.0, 15000.0))
        self.assertIsNone(p["targets"])   # scope needs a second fetch
        w = by_id(ps, "bugcrowd:engagements/webdotcom")
        self.assertEqual((w["bounty_min"], w["bounty_max"], w["program_kind"]), (None, 3000.0, "bounty"))

    def test_parse_scope_from_changelog(self):
        ts = bugcrowd.parse_scope(load("bugcrowd", "changelog.json"), "bitso-mbb-og")
        self.assertTrue(by_ident(ts, "https://bitso.com")["in_scope"])
        self.assertFalse(by_ident(ts, "help.bitso.com")["in_scope"])
        self.assertFalse(by_ident(ts, "help.bitso.com")["eligible_for_bounty"])

    def test_parse_btd_scope(self):
        ts = bugcrowd.parse_btd(load("bugcrowd", "bounty_targets_data.json"), "bitso-mbb-og")
        self.assertEqual(len(ts), 12); self.assertEqual(sum(t["in_scope"] for t in ts), 6)
        self.assertEqual(by_ident(ts, "https://bitso.com")["hint_type"], "website")
        self.assertIsNone(bugcrowd.parse_btd(load("bugcrowd", "bounty_targets_data.json"), "nope"))

    def test_changelog_id_from_brief(self):
        self.assertRegex(bugcrowd.changelog_id(load("bugcrowd", "brief.html")), r"^[0-9a-f-]{36}$")


class Intigriti(unittest.TestCase):
    def test_parse_listing(self):
        ps = intigriti.parse_listing(load("intigriti", "programs.json"))
        self.assertEqual(len(ps), 3)
        p = by_id(ps, "intigriti:usz-vdp")
        self.assertEqual((p["platform"], p["program_kind"], p["reward_status"], p["program_status"]), ("Intigriti", "vdp", "no_reward", "active"))
        k = by_id(ps, "intigriti:kruidvat")
        self.assertEqual((k["program_kind"], k["bounty_max"], k["reward_currency"]), ("bounty", 8500.0, "USD"))
        self.assertIsNone(k["targets"])

    def test_parse_scope_from_btd(self):
        data = load("intigriti", "bounty_targets_data.json")
        ts = intigriti.parse_scope(data, ("aswatson", "kruidvat"))
        self.assertEqual(len(ts), 6)
        self.assertEqual(by_ident(ts, "www.kruidvat.nl")["hint_type"], "url")
        usz = intigriti.parse_scope(data, ("universitatsspitalzurich", "usz-vdp"))
        self.assertFalse(by_ident(usz, "*.unispital.ch ")["eligible_for_bounty"])   # impact "No Bounty"
        self.assertIsNone(intigriti.parse_scope(data, ("x", "y")))


class YesWeHack(unittest.TestCase):
    def test_parse_listing_fee_is_credits(self):
        ps = yeswehack.parse_listing(load("yeswehack", "programs.json"))
        self.assertEqual(len(ps), 3)
        p = by_id(ps, "yeswehack:otto-de-bug-bounty")
        self.assertEqual(p["fee"], {"amount": 2, "unit": "credits", "kind": "fee", "source": "yeswehack_programs_api"})
        self.assertEqual((p["report_count"], p["bounty_max"], p["program_kind"]), (915, 2500, "bounty"))
        self.assertIsNone(p["targets"])

    def test_parse_scope(self):
        ts = yeswehack.parse_scope(load("yeswehack", "program_detail.json"), "otto-de-bug-bounty")
        t = by_ident(ts, "https://www.otto.de")
        self.assertEqual((t["hint_type"], t["in_scope"], t["eligible_for_bounty"], t["severity_label"]), ("web-application", True, True, "MEDIUM"))
        self.assertTrue(any(not t["in_scope"] for t in ts))


class HackenProof(unittest.TestCase):
    def test_parse_listing(self):
        ps = hackenproof.parse_listing(load("hackenproof", "programs.html"))
        self.assertEqual(len(ps), 3)
        p = by_id(ps, "hackenproof:rhea-finance-smart-contracts")
        self.assertEqual((p["platform"], p["bounty_max"], p["report_count"], p["program_status"]), ("HackenProof", 250000.0, 90, "active"))
        self.assertIsNone(p["targets"]); self.assertIsNone(p["fee"])

    def test_parse_scope_honours_out_of_scope(self):
        ts = hackenproof.parse_scope(load("hackenproof", "program_detail.html"), "zmo-exchange")
        self.assertTrue(by_ident(ts, "https://www.zmo.com")["in_scope"])
        self.assertEqual(by_ident(ts, "https://www.zmo.com")["hint_type"], "Web")
        self.assertFalse(by_ident(ts, "Any Third Party Service")["in_scope"])
        self.assertFalse(by_ident(ts, "Any Third Party Service")["eligible_for_bounty"])

    def test_parse_detail_defaults_fee_to_zero(self):
        d = hackenproof.parse_detail(load("hackenproof", "program_detail.html"), "zmo-exchange")
        self.assertEqual(d["fee"], {"amount": 0.0, "unit": "USD", "kind": "fee", "source": "hackenproof_catalog"})
        self.assertIn("rewards", d)

    def test_nuxt_data_decodes_devalue(self):
        root = hackenproof.nuxt_data(load("hackenproof", "programs.html"))
        self.assertEqual(root["data"]["programs-api-bounty"]["programs"][0]["slug"], "rhea-finance-smart-contracts")


class Sherlock(unittest.TestCase):
    def test_parse_listing_deposit_is_fee(self):
        ps = sherlock.parse_listing(load("sherlock", "bug_bounties.html"))
        self.assertEqual(len(ps), 3)
        p = by_id(ps, "sherlock:56")
        self.assertEqual((p["name"], p["bounty_max"], p["fee"]["amount"], p["fee"]["kind"], p["fee"]["unit"]), ("Usual Labs", 16000000.0, 250.0, "deposit", "USDC"))
        self.assertIsNone(p["targets"])

    def test_parse_scope(self):
        ts = sherlock.parse_scope(load("sherlock", "bounty_detail.html"), "300")
        self.assertEqual(len(ts), 5)
        t = by_ident(ts, "https://github.com/aave-dao/aave-address-book/")
        self.assertEqual((t["hint_type"], t["in_scope"], t["source_url"]), ("smart contract", True, "https://audits.sherlock.xyz/bug-bounties/300"))


class Immunefi(unittest.TestCase):
    def test_parse_listing_pay_to_submit_fee(self):
        ps = immunefi.parse_listing(load("immunefi", "bug_bounty.html"))
        self.assertEqual(len(ps), 3)
        p = by_id(ps, "immunefi:sky")
        self.assertEqual((p["fee"]["amount"], p["fee"]["unit"], p["bounty_max"], p["launch_at"]), (50.0, "USDC", 10000000.0, "2022-02-10T07:00:00.000Z"))
        self.assertEqual(by_id(ps, "immunefi:usdt0")["fee"]["amount"], 0.0)
        self.assertIsNone(p["targets"])

    def test_parse_scope(self):
        ts = immunefi.parse_scope(load("immunefi", "scope.html"), "sky")
        t = by_ident(ts, "https://github.com/sky-ecosystem/dss/blob/master/src/dai.sol")
        self.assertEqual((t["hint_type"], t["source_added_at"]), ("smart_contract", "2022-05-10T16:20:12.479Z"))


class Ids(unittest.TestCase):
    def test_program_id(self):
        self.assertEqual(program_id("YesWeHack", "Otto-DE"), "yeswehack:otto-de")


if __name__ == "__main__":
    unittest.main()
