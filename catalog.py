"""Data-only catalog and BWIKI synchronizer; no AstrBot dependency."""

import asyncio
import json
import os
import random
import tempfile
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.parse import urlparse

try:
    from .roles import (CompositionError, ROLE_LABELS, SCORE_ROLES, draw_composition,
                        parse_role_scores, primary_role, roles_for, valid_scores)
except ImportError:  # Standalone maintenance script.
    from roles import (CompositionError, ROLE_LABELS, SCORE_ROLES, draw_composition,
                       parse_role_scores, primary_role, roles_for, valid_scores)

CST = timezone(timedelta(hours=8))
WIKI_BASE = "https://wiki.biligame.com/starengine/"
CHARACTER_URL = WIKI_BASE + quote("星趴角色图鉴")
MAP_URL = WIKI_BASE + quote("星趴地图图鉴")
ROLE_URL = WIKI_BASE + quote("筹码")
BUNDLED_PATH = Path(__file__).with_name("data") / "catalog.json"
MODES = {"合作挑战模式": "pve", "标准对战模式": "pvp", "限时玩法": "limited"}
MODE_ALIASES = {"合作": "pve", "对战": "pvp", "限时": "limited", "全部": "all",
                "pve": "pve", "pvp": "pvp", "limited": "limited", "all": "all"}
MAX_BYTES = 4 * 1024 * 1024


def now_cst():
    return datetime.now(CST)


class CatalogError(ValueError):
    pass


def parse_time(value):
    if not isinstance(value, str) or len(value) != 12 or not value.isascii() or not value.isdigit():
        raise ValueError("expected YYYYMMDDHHMM")
    return datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=CST)


def map_is_available(game_map, now=None):
    if game_map["mode"] != "limited":
        return True
    # A catalog entry alone does not prove that a time-limited event is open.
    if not game_map.get("available_from") or not game_map.get("available_until"):
        return False
    when = (now or now_cst()).astimezone(CST)
    return parse_time(game_map["available_from"]) <= when < parse_time(game_map["available_until"])


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise CatalogError(f"无效的 {field}")
    if any(ord(c) < 32 for c in value):
        raise CatalogError(f"{field} 包含控制字符")
    return value.strip()


def validate(data):
    """Reject incomplete/changed layouts before replacing a usable catalog."""
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise CatalogError("不支持的数据版本")
    for kind in ("characters", "maps"):
        entries = data.get(kind)
        if not isinstance(entries, list) or not entries or len(entries) > 500:
            raise CatalogError(f"{kind} 数据为空或过大")
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise CatalogError("条目必须为对象")
            key = _text(entry.get("id"), "id")
            _text(entry.get("name"), "name")
            if key in seen:
                raise CatalogError(f"重复 id：{key}")
            seen.add(key)
            if kind == "characters":
                _text(entry.get("title"), "title")
                try:
                    parse_time(entry["release_at"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise CatalogError("角色上线时间无效") from exc
                if "role_scores" in entry and not valid_scores(entry["role_scores"]):
                    raise CatalogError("角色职业分值无效")
                if "roles" in entry and (
                    not isinstance(entry["roles"], list)
                    or any(not isinstance(role, str) or role not in ROLE_LABELS for role in entry["roles"])
                ):
                    raise CatalogError("角色定位标签无效")
            elif entry.get("mode") not in ("pve", "pvp", "limited"):
                raise CatalogError("地图模式无效")
            else:
                if "play_mode" in entry and entry["play_mode"] not in ("pve", "pvp"):
                    raise CatalogError("限时地图玩法无效")
                if "available_from" in entry or "available_until" in entry:
                    try:
                        if parse_time(entry["available_from"]) >= parse_time(entry["available_until"]):
                            raise ValueError("empty availability window")
                    except (KeyError, TypeError, ValueError) as exc:
                        raise CatalogError("地图开放时间无效，需同时提供起止时间") from exc
    if len(data["characters"]) < 4:
        raise CatalogError("角色不足四人")
    for mode in ("pve", "pvp", "limited"):
        if not any(m["mode"] == mode for m in data["maps"]):
            raise CatalogError(f"缺少 {mode} 地图")
    return data


class WikiParser(HTMLParser):
    """Parse observed card attributes and the map navigation table only."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.characters = []
        self.maps = []
        self.in_row = False
        self.in_header = False
        self.header = ""
        self.links = []
        self._card = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "div" and "divsort" in a.get("class", "").split():
            if "data-param1" in a and "data-param12" in a:
                self.characters.append({"id": a.get("data-param1"),
                                        "name": a.get("data-param2"),
                                        "title": a.get("data-param3"),
                                        "release_at": a.get("data-param12")})
                self._card = self.characters[-1]
        if tag == "img" and self._card and "avatar_url" not in self._card:
            src = a.get("src", "")
            if urlparse(src).hostname == "patchwiki.biligame.com" and src.startswith("https://"):
                # Newly added characters can use their portrait until a Q-version
                # icon is added to the bundled resources.
                self._card["avatar_url"] = src
        if tag == "tr":
            self.in_row, self.header, self.links = True, "", []
        elif tag == "th" and self.in_row:
            self.in_header = True
        elif tag == "a" and self.in_row:
            if a.get("href", "").startswith("/starengine/") and a.get("title"):
                self.links.append(a["title"])

    def handle_data(self, data):
        if self.in_header:
            self.header += data

    def handle_endtag(self, tag):
        if tag == "th":
            self.in_header = False
        elif tag == "tr" and self.in_row:
            mode = MODES.get(self.header.strip())
            if mode:
                for name in dict.fromkeys(self.links):
                    self.maps.append({"id": name, "name": name, "mode": mode})
            self.in_row = False
            self.in_header = False


def parse_wiki(character_html, map_html):
    c, m = WikiParser(), WikiParser()
    c.feed(character_html)
    m.feed(map_html)
    # Duplicate rows/cards are a layout change, not extra random weight.
    return validate({"schema_version": 1, "synced_at": now_cst().isoformat(),
                     "sources": [CHARACTER_URL, MAP_URL],
                     "characters": c.characters, "maps": m.maps})


def fetch_page(url):
    req = Request(url, headers={"User-Agent": "AstralPartyCatalog/1.1 (+AstrBot plugin)"})
    with urlopen(req, timeout=15) as response:
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise CatalogError("图鉴响应过大")
    return body.decode("utf-8")


def atomic_save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="catalog-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_json(path):
    if path.stat().st_size > MAX_BYTES:
        raise CatalogError("数据文件过大")
    return validate(json.loads(path.read_text(encoding="utf-8")))


def normalize_mode(mode):
    try:
        return MODE_ALIASES[mode.strip().lower()]
    except KeyError as exc:
        raise CatalogError("模式请选择：合作 / 对战 / 限时 / 全部") from exc


class CatalogStore:
    def __init__(self, cache_path, bundled_path=BUNDLED_PATH):
        self.cache_path = Path(cache_path)
        self.bundled_path = Path(bundled_path)
        self.data = load_json(self.bundled_path)
        self.role_defaults = {c["id"]: c for c in self.data["characters"]}
        self.source = "内置数据"
        self.last_error = ""
        self.last_role_error = ""
        self._lock = asyncio.Lock()
        if self.cache_path.exists():
            try:
                cached = load_json(self.cache_path)
                # An older persistent cache must not hide a newer bundled snapshot.
                if cached.get("synced_at", "") >= self.data.get("synced_at", ""):
                    self.data, self.source = cached, "本地缓存"
            except (OSError, ValueError) as exc:
                self.last_error = str(exc)
        for character in self.data["characters"]:
            self._fix_role(character, self.role_defaults.get(character["id"]))

    @staticmethod
    def _fix_role(character, fallback=None):
        role = primary_role(character, fallback)
        if role or "roles" in character or "roles" in (fallback or {}):
            character["roles"] = [role] if role else []

    def characters(self, now=None):
        when = (now or now_cst()).astimezone(CST).strftime("%Y%m%d%H%M")
        return [c for c in self.data["characters"] if c["release_at"] <= when]

    def maps(self, mode="pve"):
        mode = normalize_mode(mode)
        return [m for m in self.data["maps"] if mode == "all" or m["mode"] == mode]

    def draw_maps(self, mode="pve", now=None):
        """Playable pool, separate from the historical inventory used by sync."""
        mode = normalize_mode(mode)
        result = []
        for game_map in self.data["maps"]:
            if not map_is_available(game_map, now):
                continue
            play_mode = game_map.get("play_mode") if game_map["mode"] == "limited" else game_map["mode"]
            if mode == "all" or (mode == "limited" and game_map["mode"] == "limited") or play_mode == mode:
                result.append(game_map)
        return result

    def team(self, num=4, composition=""):
        pool = self.characters()
        if composition:
            if num != 4:
                raise CatalogError("定位组合目前只支持四人队伍")
            try:
                return draw_composition(pool, composition, self.role_defaults)
            except CompositionError as exc:
                raise CatalogError(str(exc)) from exc
        if not 1 <= num <= len(pool):
            raise CatalogError("可用角色数量不足")
        return random.sample(pool, num)

    def random_character(self):
        return self.team(1)[0]

    def random_map(self, mode="pve"):
        pool = self.draw_maps(mode)
        if not pool:
            raise CatalogError("该模式暂无已确认开放的地图")
        return random.choice(pool)

    async def refresh(self, fetcher=fetch_page):
        # A second simultaneous admin request reuses the in-flight attempt.
        if self._lock.locked():
            return "已有更新正在进行，请稍后查看 /星趴数据"
        async with self._lock:
            try:
                # Both documents must succeed before either collection is replaced.
                def download():
                    candidate = parse_wiki(fetcher(CHARACTER_URL), fetcher(MAP_URL))
                    try:
                        scores = parse_role_scores(fetcher(ROLE_URL))
                        return candidate, scores, ""
                    except Exception as exc:
                        return candidate, {}, str(exc)
                candidate, scores, role_error = await asyncio.to_thread(download)
                candidate["role_score_order"] = list(SCORE_ROLES)
                candidate["role_source"] = ROLE_URL
                previous = {c["id"]: c for c in self.data["characters"]}
                bundled = {c["id"]: c for c in load_json(self.bundled_path)["characters"]}
                for character in candidate["characters"]:
                    old = bundled.get(character["id"], previous.get(character["id"], {}))
                    if old.get("avatar_url"):
                        character["avatar_url"] = old["avatar_url"]
                    role_data = bundled.get(character["id"], {}) | previous.get(character["id"], {})
                    for key in ("role_scores", "roles"):
                        if key in role_data:
                            character[key] = role_data[key]
                    if character["id"] in scores:
                        character["role_scores"] = scores[character["id"]]
                    self._fix_role(character)
                old_maps = {m["id"]: m for m in load_json(self.bundled_path)["maps"]}
                for game_map in self.data["maps"]:
                    old_maps[game_map["id"]] = old_maps.get(game_map["id"], {}) | game_map
                for game_map in candidate["maps"]:
                    old = old_maps.get(game_map["id"], {})
                    # The wiki navigation table does not supply availability.
                    # Retain verified, finite windows rather than reopening archives.
                    for key in ("play_mode", "available_from", "available_until"):
                        if key in old:
                            game_map[key] = old[key]
                old_ids = {c["id"] for c in self.data["characters"]}
                if not old_ids.issubset({c["id"] for c in candidate["characters"]}):
                    raise CatalogError("图鉴角色缺失，保留原数据；请检查图鉴或手动维护")
                for mode in ("pve", "pvp", "limited"):
                    if len([m for m in candidate["maps"] if m["mode"] == mode]) < len(self.maps(mode)):
                        raise CatalogError(f"{mode} 地图数量异常减少，保留原数据")
                validate(candidate)
                await asyncio.to_thread(atomic_save, self.cache_path, candidate)
                self.data, self.source, self.last_error = candidate, "BWIKI 同步", ""
                self.last_role_error = role_error
                return f"更新成功：{len(self.characters())} 名已上线角色，{len(self.maps('all'))} 张地图资料（当前可抽限时 {len(self.draw_maps('limited'))} 张）"
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                return "更新失败，已保留现有数据。请检查网络或查看日志。"

    def status(self):
        counts = " / ".join(f"{label} {len(self.maps(mode))}" for label, mode in
                            (("合作", "pve"), ("对战", "pvp"), ("限时存档", "limited")))
        return (f"数据来源：{self.source}\n数据时间：{self.data.get('synced_at', '未知')}\n"
                f"已上线角色：{len(self.characters())}\n地图：{counts}\n"
                f"当前可抽限时：{len(self.draw_maps('limited'))}\n"
                f"已分类角色：{sum(roles_for(c, self.role_defaults.get(c['id'])) != {'any'} for c in self.characters())}\n"
                f"定位同步：{'沿用已有数据' if self.last_role_error else '正常'}\n"
                f"最近同步：{'失败（保留现有数据）' if self.last_error else '正常'}")


def character_label(character):
    label = f"{character['title']}·{character['name']}"
    if character.get("assigned_role"):
        label += f"（{ROLE_LABELS[character['assigned_role']]}）"
    return label


def map_label(game_map):
    suffix = "（限时地图）" if game_map["mode"] == "limited" else ""
    return game_map["name"] + suffix
