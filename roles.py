"""Role classification, composition syntax and unbiased distinct assignments."""
import random
import re
import unicodedata
from functools import lru_cache
from html.parser import HTMLParser

SCORE_ROLES = ("attack", "card", "support", "tank")
ROLE_LABELS = {"support": "辅助", "damage": "输出", "attack": "攻击输出",
               "card": "卡牌输出", "tank": "生存", "any": "自由"}
ROLE_CODES = {"support": "辅", "damage": "C", "attack": "攻",
              "card": "卡", "tank": "坦", "any": "任"}
ALIASES = {"辅助": "support", "辅": "support", "奶": "support",
           "输出": "damage", "c": "damage", "攻击": "attack", "攻": "attack",
           "卡牌": "card", "卡": "card", "坦克": "tank", "生存": "tank",
           "坦": "tank", "t": "tank", "自由": "any", "任意": "any", "随机": "any", "任": "any"}
TOKEN = re.compile(r"(\d+)(" + "|".join(sorted(ALIASES, key=len, reverse=True)) + ")")
HELP = "组合示例：2辅2c、1辅3c、4c；人数合计必须为4"


class CompositionError(ValueError):
    pass


def valid_scores(scores):
    return (isinstance(scores, list) and len(scores) == 4
            and all(type(n) is int and 0 <= n <= 10000 for n in scores))


def primary_role(character, fallback=None):
    """One fixed primary role; old multi-role overrides use the first entry."""
    for source in (character, fallback or {}):
        if "roles" in source:
            return next((role for role in source["roles"]
                         if role in SCORE_ROLES or role == "damage"), None)
    scores = character.get("role_scores", (fallback or {}).get("role_scores"))
    if not valid_scores(scores) or max(scores) == 0:
        return None
    # Initial estimate only. CatalogStore persists it so later score sync cannot
    # move the character between support/output pools. Curated roles take priority.
    maximum = max(scores)
    return next(role for role in ("support", "attack", "card", "tank")
                if scores[SCORE_ROLES.index(role)] == maximum)


def roles_for(character, fallback=None):
    role = primary_role(character, fallback)
    tags = {"any"}
    if role:
        tags.add(role)
    if role in ("attack", "card"):
        tags.add("damage")
    return tags


def parse_composition(text):
    text = unicodedata.normalize("NFKC", text).lower()
    if len(text) > 80:
        raise CompositionError("组合参数过长。" + HELP)
    text = re.sub(r"[\s+,，/]", "", text)
    if not text:
        return ()
    roles, end = [], 0
    for match in TOKEN.finditer(text):
        if match.start() != end:
            raise CompositionError("无法识别组合参数。" + HELP)
        count = int(match[1])
        if count > 4:
            raise CompositionError("每种定位的人数不能超过4。" + HELP)
        roles.extend([ALIASES[match[2]]] * count)
        end = match.end()
    if end != len(text):
        raise CompositionError("无法识别组合参数。" + HELP)
    if len(roles) != 4:
        raise CompositionError(f"组合人数合计必须为4，当前为{len(roles)}。" + HELP)
    return tuple(roles)


def composition_label(roles):
    groups = []
    for role in roles:
        if groups and groups[-1][0] == role:
            groups[-1][1] += 1
        else:
            groups.append([role, 1])
    return "".join(f"{count}{ROLE_CODES[role]}" for role, count in groups)


def draw_composition(pool, text, defaults=None, rng=None):
    slots = parse_composition(text)
    if not slots:
        raise CompositionError("请提供组合参数。" + HELP)
    rng = rng or random
    defaults = defaults or {}
    groups = {}
    for character in pool:
        tags = roles_for(character, defaults.get(character["id"]))
        mask = sum(1 << i for i, role in enumerate(slots) if role in tags)
        if mask:
            groups.setdefault(mask, []).append(character)
    masks = tuple(groups)
    candidates = [groups[mask][:] for mask in masks]
    initial = tuple(len(group) for group in candidates)

    @lru_cache(maxsize=None)
    def completions(slot, remaining):
        if slot == len(slots):
            return 1
        total = 0
        for i, mask in enumerate(masks):
            if remaining[i] and mask & (1 << slot):
                next_counts = list(remaining)
                next_counts[i] -= 1
                total += remaining[i] * completions(slot + 1, tuple(next_counts))
        return total

    if not completions(0, initial):
        raise CompositionError("当前已上线且已分类的角色无法组成该阵容，请调整组合或角色定位。")
    result, remaining = [], initial
    for slot, role in enumerate(slots):
        ticket = rng.randrange(completions(slot, remaining))
        for i, mask in enumerate(masks):
            if not remaining[i] or not mask & (1 << slot):
                continue
            next_counts = list(remaining)
            next_counts[i] -= 1
            next_counts = tuple(next_counts)
            weight = remaining[i] * completions(slot + 1, next_counts)
            if ticket >= weight:
                ticket -= weight
                continue
            character = rng.choice(candidates[i])
            candidates[i].remove(character)
            result.append(dict(character, assigned_role=role))
            remaining = next_counts
            break
    return result


class RoleScoreParser(HTMLParser):
    HEADERS = ("角色", "攻击分值", "卡牌分值", "辅助分值", "生存分值")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.active = False
        self.cells = None
        self.cell = None
        self.hero_id = None
        self.scores = {}

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.cells, self.cell, self.hero_id = [], None, None
        elif tag in ("td", "th") and self.cells is not None:
            self.cell = ""
        elif tag == "img" and self.cells is not None:
            match = re.fullmatch(r"UT[ _]Item[ _]Hero[ _](\d+)\.png", dict(attrs).get("alt", ""))
            if match:
                self.hero_id = match[1]

    def handle_data(self, data):
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.cells.append(self.cell.strip())
            self.cell = None
        elif tag == "tr" and self.cells is not None:
            if tuple(self.cells) == self.HEADERS:
                self.active = True
            elif self.active and self.hero_id:
                try:
                    scores = [int(value) for value in self.cells[1:]]
                except ValueError as exc:
                    raise CompositionError("角色职业分值表包含非整数") from exc
                if not valid_scores(scores) or self.hero_id in self.scores:
                    raise CompositionError("角色职业分值表格式异常")
                self.scores[self.hero_id] = scores
            self.cells = None
        elif tag == "table":
            self.active = False


def parse_role_scores(html):
    parser = RoleScoreParser()
    parser.feed(html)
    if not parser.scores:
        raise CompositionError("图鉴未提供可识别的角色职业分值表")
    return parser.scores
