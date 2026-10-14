"""Local SMART trigger detection. No API calls and no Discord dependency."""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


@dataclass(frozen=True)
class Decision:
    trigger: bool
    reason: str
    kind: str = ""


_CYRILLIC = re.compile(r"[а-я]")
_TOKEN = re.compile(r"[а-яa-z0-9]+")
_SECOND_PERSON = re.compile(r"\b(?:ты|тебя|тебе|тобой|вы|вас|вам|твой|твоя|твои)\b")
_HOSTILE = re.compile(r"\b(?:заткнись|заткнитесь|отвали|отстань|пошел|пошла|пошли|иди|сам\s+такой)\b")
_META = re.compile(r"\b(?:слово|цитат[аыуе]?|написал|написала|означает|называть|обзывать|ругательство|оскорбление|пример)\b")
_STRONG = (
    re.compile(r"п[ие]д(?:о|а)?р(?:ас|аст)?[а-я]*"),
    re.compile(r"педик[а-я]*"),
    re.compile(r"д[оа]лб[оа]е[бп][а-я]*"),
    re.compile(r"мудак[а-я]*|мудил[а-я]*"),
    re.compile(r"ублюд[а-я]*|чмо[а-я]*"),
    re.compile(r"pidor[a-z]*|dolboeb[a-z]*|dolbaeb[a-z]*"),
)
_MILD = (
    re.compile(r"дур[аыуеойк][а-я]*|дурак[а-я]*"),
    re.compile(r"идиот[а-я]*|дебил[а-я]*|кретин[а-я]*|придурок[а-я]*"),
    re.compile(r"тупиц[а-я]*|урод[а-я]*"),
    re.compile(r"лох[а-я]*|говнюк[а-я]*|скотин[а-я]*|твар[ьиаяю][а-я]*"),
    re.compile(r"dura|durak[a-z]*|idiot[a-z]*"),
)
_ADJECTIVE = re.compile(r"туп(?:ой|ая|ые|ого|ому|ым|ых|ую)")
_NON_INSULT = re.compile(r"(?:дурацк|дуракск|идиотск|дебильн)[а-я]*")
_LOOKALIKE = str.maketrans({"a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "y": "у", "k": "к", "m": "м", "t": "т", "b": "в", "h": "н", "0": "о", "3": "з"})


def normalize(text: str) -> str:
    """Fold common spelling tricks while keeping word boundaries."""
    text = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    text = re.sub(r"[\u200b-\u200f\ufeff]", "", text)
    text = re.sub(r"```.*?```|`[^`]*`", " ", text, flags=re.S)

    def fold(match: re.Match[str]) -> str:
        word = match.group()
        if _CYRILLIC.search(word):
            word = word.translate(_LOOKALIKE)
        return re.sub(r"(.)\1{2,}", r"\1", word)

    return _TOKEN.sub(fold, text)


def insult_level(text: str) -> str:
    for token in _TOKEN.findall(normalize(text)):
        if _NON_INSULT.fullmatch(token):
            continue
        if any(pattern.fullmatch(token) for pattern in _STRONG):
            return "strong"
        if any(pattern.fullmatch(token) for pattern in _MILD):
            return "mild"
        if _ADJECTIVE.fullmatch(token):
            return "adjective"
    return ""


def detect(
    text: str,
    *,
    bot_mentioned: bool = False,
    replied_to_bot: bool = False,
    other_mentioned: bool = False,
    replied_to_other: bool = False,
    bot_names: tuple[str, ...] = (),
    recent: tuple[dict, ...] | list[dict] = (),
    author_id: int = 0,
    mentions: bool = True,
    arguments: bool = True,
    name: bool = False,
    activity: str = "medium",
) -> Decision:
    folded = normalize(text)
    if mentions and (bot_mentioned or replied_to_bot):
        return Decision(True, "bot_mention", "mention")
    if name and any(re.search(r"(?<!\w)" + re.escape(normalize(n)) + r"(?!\w)", folded)
                    for n in bot_names if n.strip()):
        return Decision(True, "bot_name", "name")
    if not arguments:
        return Decision(False, "arguments_disabled")
    level = insult_level(text)
    recent_other = [item for item in recent[-5:] if item.get("author_id") != author_id]
    prior_hostility = any(item.get("hostile") or insult_level(item.get("text", "")) for item in recent_other)
    direct = bool(other_mentioned or replied_to_other or _SECOND_PERSON.search(folded))
    if level:
        if _META.search(folded) and not direct:
            return Decision(False, "quoted_or_discussion")
        if direct:
            return Decision(True, "direct_insult", "argument")
        if prior_hostility:
            return Decision(True, "insult_in_conflict_context", "argument")
        if level == "strong" and activity in {"medium", "high"}:
            return Decision(True, "strong_insult", "argument")
        if level == "mild" and activity == "high":
            return Decision(True, "mild_insult_high_activity", "argument")
        return Decision(False, "ambiguous_insult_no_target")
    if _HOSTILE.search(folded) and (direct or (prior_hostility and activity == "high")):
        return Decision(True, "hostile_reply", "argument")
    return Decision(False, "no_signal")
