"""Blocked words: slurs and other terms that must not go out unreviewed.

Two sources are combined, and both are re-read for every clip:

- Downloaded lists, from the LDNOOBW project ("List of Dirty, Naughty, Obscene and
  Otherwise Bad Words", CC BY 4.0), pinned to one commit. ``peakreel safety download``
  fetches the languages in ``safety.languages`` into ``data/safety/``.
- Your own ``safety.yaml``::

      blocked_words:         # added to the lists
        - someword
      ignore_words:          # removed from the lists (false alarms on your streams)
        - ass

Matching ignores case. Words in scripts written with spaces match whole words only
(``ass`` does not match ``class``); Japanese, Chinese, Korean and Thai terms match
anywhere in the text, because those scripts do not separate words.
"""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from peakreel.config import ConfigError

__all__ = [
    "LDNOOBW_COMMIT",
    "WordList",
    "download_word_lists",
    "find_words",
    "load_word_list",
]

LDNOOBW_COMMIT = "5faf2ba42d7b1c0977169ec3611df25a3c08eb13"
"""Pinned commit of the LDNOOBW lists (2020-07-13), so a download is reproducible."""

_URL = (
    "https://raw.githubusercontent.com/LDNOOBW/"
    "List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words/{commit}/{language}"
)
_LANGUAGE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9-]+)?$")
# Scripts written without spaces between words: match terms as substrings.
# Kana, CJK ideographs, Hangul, Thai, half-width kana.
_UNSPACED = re.compile("[぀-ヿ㐀-鿿가-힯฀-๿ｦ-ﾟ]")


@dataclass(frozen=True, slots=True)
class WordList:
    """The effective blocked words.

    Attributes
    ----------
    spaced : re.Pattern or None
        Whole-word matcher for terms in space-separated scripts.
    unspaced : frozenset of str
        Terms matched as substrings (CJK and Thai).
    size : int
        Number of terms.
    missing : tuple of str
        Configured languages whose list has not been downloaded.
    """

    spaced: re.Pattern[str] | None
    unspaced: frozenset[str]
    size: int
    missing: tuple[str, ...] = ()


def download_word_lists(directory: Path, languages: tuple[str, ...]) -> dict[str, int]:
    """Download the pinned LDNOOBW lists into ``directory``.

    Parameters
    ----------
    directory : pathlib.Path
        Created if needed. Each language is written to ``<language>.txt``, plus a
        ``SOURCE.md`` recording origin, commit and licence.
    languages : tuple of str
        LDNOOBW language codes, for example ``("en", "pt", "ja")``.

    Returns
    -------
    dict of str to int
        Terms per language.

    Raises
    ------
    ValueError
        For a malformed language code.
    OSError
        If a download fails (``urllib.error.HTTPError`` for an unknown language).
    """
    directory.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for language in languages:
        if not _LANGUAGE.match(language):
            raise ValueError(f"invalid language code {language!r}")
        url = _URL.format(commit=LDNOOBW_COMMIT, language=language)
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - fixed https URL
            text = cast("bytes", response.read()).decode("utf-8")
        terms = [line.strip() for line in text.splitlines() if line.strip()]
        temp = directory / f"{language}.txt.part"
        temp.write_text("\n".join(terms) + "\n", encoding="utf-8")
        temp.replace(directory / f"{language}.txt")
        counts[language] = len(terms)
    (directory / "SOURCE.md").write_text(
        "Word lists from LDNOOBW, "
        "https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words\n"
        f"Commit {LDNOOBW_COMMIT}. Licence: CC BY 4.0 (Shutterstock and contributors).\n",
        encoding="utf-8",
    )
    return counts


def _read_user_words(path: Path) -> tuple[set[str], set[str]]:
    if not path.exists():
        return set(), set()
    try:
        raw: object = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read safety words {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must be a mapping with `blocked_words` and `ignore_words`")
    data = cast("dict[str, object]", raw)
    result: list[set[str]] = []
    for key in ("blocked_words", "ignore_words"):
        value: object = data.get(key) or list[object]()
        if not isinstance(value, list):
            raise ConfigError(f"{path}: `{key}` must be a list")
        result.append({str(word).strip().casefold() for word in cast("list[object]", value)})
    return result[0] - {""}, result[1] - {""}


def load_word_list(lists_dir: Path, languages: tuple[str, ...], user_file: Path) -> WordList:
    """Build the effective word list from the downloaded lists and ``safety.yaml``.

    Parameters
    ----------
    lists_dir : pathlib.Path
        Directory written by :func:`download_word_lists`.
    languages : tuple of str
        Languages to load; those not downloaded are reported in ``missing``.
    user_file : pathlib.Path
        ``safety.yaml``; optional.

    Returns
    -------
    WordList

    Raises
    ------
    ConfigError
        If ``safety.yaml`` exists but is malformed.
    """
    terms: set[str] = set()
    missing: list[str] = []
    for language in languages:
        path = lists_dir / f"{language}.txt"
        if not path.exists():
            missing.append(language)
            continue
        terms |= {
            line.strip().casefold()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    blocked, ignored = _read_user_words(user_file)
    terms = (terms | blocked) - ignored
    unspaced = frozenset(t for t in terms if _UNSPACED.search(t))
    spaced = sorted(terms - unspaced, key=len, reverse=True)
    pattern = (
        re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, spaced)) + r")(?!\w)", re.IGNORECASE)
        if spaced
        else None
    )
    return WordList(pattern, unspaced, len(terms), tuple(missing))


def find_words(text: str, words: WordList) -> list[str]:
    r"""Return the blocked terms found in ``text``, each once, in order of appearance.

    Parameters
    ----------
    text : str
    words : WordList

    Returns
    -------
    list of str
        Matched terms, lower-cased.

    Examples
    --------
    >>> import re
    >>> words = WordList(re.compile(r"(?<!\w)(?:darn)(?!\w)", re.I), frozenset({"くそ"}), 2)
    >>> find_words("Darn it, くそっ! darnation", words)
    ['darn', 'くそ']
    """
    found: dict[str, int] = {}
    folded = text.casefold()
    if words.spaced is not None:
        for match in words.spaced.finditer(folded):
            found.setdefault(match.group(0), match.start())
    for term in words.unspaced:
        position = folded.find(term)
        if position >= 0:
            found.setdefault(term, position)
    return sorted(found, key=found.__getitem__)
