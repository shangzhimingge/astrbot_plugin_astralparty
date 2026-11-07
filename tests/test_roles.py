import asyncio
import copy
import importlib
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
roles = importlib.import_module(ROOT.name + ".roles")
catalog = importlib.import_module(ROOT.name + ".catalog")


@pytest.mark.parametrize("text, expected", [
    ("2辅2c", ("support", "support", "damage", "damage")),
    ("２辅助２Ｃ", ("support", "support", "damage", "damage")),
    ("1辅 1坦 + 2输出", ("support", "tank", "damage", "damage")),
    ("4c", ("damage",) * 4),
    ("2卡2攻", ("card", "card", "attack", "attack")),
    ("1辅3任意", ("support", "any", "any", "any")),
    ("", ()),
])
def test_composition_syntax(text, expected):
    assert roles.parse_composition(text) == expected


@pytest.mark.parametrize("text", ["2辅1c", "5c", "2辅x2c", "2foo2c", "2辅-2c",
                                  "c2辅2", "0辅", "2辅2c垃圾", "4" * 81])
def test_invalid_syntax_is_not_silently_ignored(text):
    with pytest.raises(roles.CompositionError):
        roles.parse_composition(text)


def test_classification_is_exclusive_and_manual_overrides_are_authoritative():
    hybrid = {"role_scores": [15, 60, 60, 15]}
    assert roles.roles_for(hybrid) == {"any", "support"}
    assert roles.roles_for(dict(hybrid, roles=["support"])) == {"any", "support"}
    assert roles.roles_for(dict(hybrid, roles=["card"])) == {"any", "card", "damage"}
    assert roles.roles_for(dict(hybrid, roles=["damage", "support"])) == {"any", "damage"}
    assert roles.roles_for(dict(hybrid, roles=[])) == {"any"}
    assert roles.roles_for({}) == {"any"}
    assert roles.roles_for({}, hybrid) == {"any", "support"}
    assert roles.roles_for({}, {"roles": ["support"]}) == {"any", "support"}


def test_legacy_multi_role_list_does_not_switch_to_fill_missing_output():
    pool = [{"id": "a", "roles": ["damage", "support"]},
            {"id": "b", "roles": ["support"]},
            {"id": "c", "roles": ["damage"]},
            {"id": "d", "roles": ["damage"]}]
    for seed in range(15):
        team = roles.draw_composition(pool, "1辅3c", rng=random.Random(seed))
        assert team[0]["id"] == "b"
        assert {c["id"] for c in team} == {"a", "b", "c", "d"}
    assert all("assigned_role" not in c for c in pool)


def test_impossible_shared_pool_returns_error_instead_of_duplicates():
    pool = [{"id": "a", "roles": ["support", "damage"]},
            {"id": "b", "roles": ["support", "damage"]},
            {"id": "c", "roles": ["tank"]},
            {"id": "d", "roles": ["tank"]}]
    with pytest.raises(roles.CompositionError, match="无法组成"):
        roles.draw_composition(pool, "2辅2c")


@pytest.mark.parametrize("composition", ["2辅2c", "1辅3c", "4c", "4辅", "4任", "2攻2卡"])
def test_current_roster_satisfies_requested_slots_without_mutation(tmp_path, composition):
    store = catalog.CatalogStore(tmp_path / "catalog.json")
    before = copy.deepcopy(store.data)
    team = store.team(composition=composition)
    assert len(team) == len({c["id"] for c in team}) == 4
    for c, expected in zip(team, roles.parse_composition(composition)):
        assert c["assigned_role"] == expected
        assert expected in roles.roles_for(c)
    assert store.data == before


def test_old_cache_without_classification_uses_new_bundled_defaults(tmp_path):
    cached = catalog.load_json(catalog.BUNDLED_PATH)
    cached["synced_at"] = "2099-01-01T00:00:00+08:00"
    for c in cached["characters"]:
        c.pop("role_scores")
        c.pop("roles")
    path = tmp_path / "catalog.json"
    catalog.atomic_save(path, cached)
    store = catalog.CatalogStore(path)
    assert len(store.team(composition="2辅2c")) == 4


@pytest.mark.parametrize("old_cache", [False, True])
def test_confirmed_roles_and_entire_roster_are_exclusive(tmp_path, old_cache):
    path = tmp_path / "catalog.json"
    if old_cache:
        cached = catalog.load_json(catalog.BUNDLED_PATH)
        cached["synced_at"] = "2099-01-01T00:00:00+08:00"
        for c in cached["characters"]:
            c.pop("roles")
            c["role_scores"] = [15, 100, 100, 15]
        catalog.atomic_save(path, cached)
    store = catalog.CatalogStore(path)
    by_name = {c["name"]: c for c in store.characters()}
    for name in ("远野汉娜", "恋", "璐璐", "姬梦朝", "照"):
        assert roles.roles_for(by_name[name]) == {"any", "support"}
    assert roles.roles_for(by_name["梅加斯"]) == {"any", "card", "damage"}
    for c in store.characters():
        assert len(c["roles"]) == 1
        assert not {"support", "damage"} <= roles.roles_for(c)
    for composition in ("2辅2c", "1辅3c", "4c", "4辅"):
        for _ in range(30):
            team = store.team(composition=composition)
            for c in team:
                if c["name"] in ("远野汉娜", "恋", "璐璐", "姬梦朝", "照"):
                    assert c["assigned_role"] == "support"
                if c["name"] == "梅加斯":
                    assert c["assigned_role"] == "damage"


def test_revised_support_roles_replace_old_fixed_cache_and_are_reachable(tmp_path):
    cached = catalog.load_json(catalog.BUNDLED_PATH)
    cached["synced_at"] = "2099-01-01T00:00:00+08:00"
    targets = {"璐璐", "姬梦朝", "照"}
    for c in cached["characters"]:
        if c["name"] in targets:
            c["roles"] = ["attack" if c["name"] == "照" else "tank"]
            c.pop("role_revision")
    path = tmp_path / "catalog.json"
    catalog.atomic_save(path, cached)
    store = catalog.CatalogStore(path)
    supports = [c for c in store.characters() if c["name"] in targets]
    assert len(supports) == 3
    assert all(roles.roles_for(c) == {"any", "support"} for c in supports)
    assert all(c["role_revision"] == 1 for c in supports)
    # A constrained pool proves all three can fill support slots without relying
    # on a lucky random draw, and none can fill a damage slot.
    damage = next(c for c in store.characters() if c["name"] == "梅加斯")
    pool = supports + [damage]
    assert {c["name"] for c in roles.draw_composition(pool, "3辅1c")[:3]} == targets
    with pytest.raises(roles.CompositionError, match="无法组成"):
        roles.draw_composition(pool, "2辅2c")
    catalog.atomic_save(path, store.data)
    assert all(c["roles"] == ["support"] for c in catalog.CatalogStore(path).characters()
               if c["name"] in targets)


def test_tank_composition_reports_empty_pool_without_reclassifying_supports(tmp_path):
    store = catalog.CatalogStore(tmp_path / "catalog.json")
    with pytest.raises(catalog.CatalogError, match="无法组成"):
        store.team(composition="1辅1坦2c")


@pytest.mark.parametrize("revision", [-1, True, "1"])
def test_invalid_role_revisions_are_rejected(revision):
    data = catalog.load_json(catalog.BUNDLED_PATH)
    data["characters"][0]["role_revision"] = revision
    with pytest.raises(catalog.CatalogError, match="修订号"):
        catalog.validate(data)


def test_score_sync_never_changes_fixed_roles_and_new_estimate_is_persisted(tmp_path):
    from test_catalog import pages
    path = tmp_path / "catalog.json"
    store = catalog.CatalogStore(path)
    original = {c["id"]: c["roles"][:] for c in store.characters()}
    candidate = copy.deepcopy(store.data)
    candidate["characters"].append(dict(candidate["characters"][0], id="999", name="新角色"))
    characters, maps = pages(candidate)
    def fetch(url):
        if url == catalog.ROLE_URL:
            return score_html({**{cid: [100, 100, 100, 100] for cid in original},
                               "999": [100, 15, 15, 15]})
        return characters if url == catalog.CHARACTER_URL else maps
    assert "更新成功" in asyncio.run(store.refresh(fetch))
    assert {c["id"]: c["roles"] for c in store.characters() if c["id"] in original} == original
    new = next(c for c in store.characters() if c["id"] == "999")
    assert new["roles"] == ["attack"]
    reloaded = catalog.CatalogStore(path)
    def changed_scores(url):
        return score_html({"999": [15, 15, 100, 15]}) if url == catalog.ROLE_URL else fetch(url)
    assert "更新成功" in asyncio.run(reloaded.refresh(changed_scores))
    new = next(c for c in reloaded.characters() if c["id"] == "999")
    assert new["role_scores"] == [15, 15, 100, 15]
    assert roles.roles_for(new) == {"any", "attack", "damage"}


def score_html(rows):
    head = "<tr>" + "".join(f"<th>{h}</th>" for h in roles.RoleScoreParser.HEADERS) + "</tr>"
    body = "".join(
        f'<tr><td><a><img alt="UT Item Hero {cid}.png"></a></td>'
        + "".join(f"<td>{n}</td>" for n in scores) + "</tr>"
        for cid, scores in rows.items()
    )
    return "<table>" + head + body + "</table>"


def test_role_score_parser_uses_exact_table_and_hero_ids():
    html = "<table><tr><th>其他</th></tr><tr><td>999</td></tr></table>"
    html += score_html({"102": [15, 15, 100, 15], "306": [100, 15, 15, 15]})
    assert roles.parse_role_scores(html) == {"102": [15, 15, 100, 15], "306": [100, 15, 15, 15]}
    with pytest.raises(roles.CompositionError):
        roles.parse_role_scores("<html>captcha</html>")


def test_role_sync_failure_keeps_classification_and_core_update_succeeds(tmp_path):
    from test_catalog import pages
    store = catalog.CatalogStore(tmp_path / "catalog.json")
    characters, maps = pages(store.data)
    def fetch(url):
        if url == catalog.ROLE_URL:
            raise TimeoutError("role source offline")
        return characters if url == catalog.CHARACTER_URL else maps
    assert "更新成功" in asyncio.run(store.refresh(fetch))
    assert store.last_role_error
    assert len(store.team(composition="2辅2c")) == 4


def test_role_sync_updates_scores_without_removing_manual_roles(tmp_path):
    from test_catalog import pages
    store = catalog.CatalogStore(tmp_path / "catalog.json")
    store.data["characters"][0]["roles"] = ["support"]
    characters, maps = pages(store.data)
    def fetch(url):
        if url == catalog.ROLE_URL:
            return score_html({"101": [100, 15, 15, 15]})
        return characters if url == catalog.CHARACTER_URL else maps
    assert "更新成功" in asyncio.run(store.refresh(fetch))
    assert not store.last_role_error
    c = store.data["characters"][0]
    assert c["role_scores"] == [100, 15, 15, 15]
    assert roles.roles_for(c) == {"any", "support"}
