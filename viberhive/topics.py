"""Find recurring subjects in saved GIF titles and searches using local metadata."""

import re
import unicodedata
from collections import Counter, defaultdict
from functools import lru_cache
from html import unescape
from math import log2


_ALIASES = {
    "cats": "cat cats kitten kittens kitty kitties feline felines kittycat tabby tabbies",
    "dogs": "dog dogs doggo doggos puppy puppies pup pups pupper puppers canine canines",
    "rabbits": "rabbit rabbits bunny bunnies",
    "birds": "bird birds birdie birdies",
    "horses": "horse horses pony ponies",
    "cars": "car cars automobile automobiles",
    "robots": "robot robots",
    "space": "space outerspace",
    "laughing": "laugh laughs laughing laughter lol lmao",
    "dancing": "dance dances dancing",
    "waving": "wave waves waving",
    "smiling": "smile smiles smiling grin grins grinning",
    "crying": "cry cries crying sob sobbing",
}
_CANONICAL = {word: name for name, forms in _ALIASES.items() for word in forms.split()}
_STOP = set("""
    a an and are as at be been being but by can did do does for from has have he her
    here him his how i if in into is it its me my not of on or our out she so some
    that the their them there these they this those to up us was we were what when
    where which who will with you your via
    gif gifs giphy tenor klipy google wikimedia commons pinterest tumblr reddit
    com www http https file files webp png jpg jpeg mp4 html original source
    image images pic pics picture pictures photo photos animation animations animated
    reaction reactions meme memes sticker stickers clip clips video videos hd download
    share sharing discover discoverable explore search find best top new official
    unfiled favorites folders free media gifdb imgur wikimedia
""".split())
_DESCRIPTORS = set("""
    cute funny hilarious awesome beautiful cool adorable happy angry sad sleepy
    laughing dancing waving smiling crying dramatic surprised excited shocked
    playing nod nods nodding smirk smirks smirking wink winks winking
""".split())
_TOKENS = re.compile(r"[^\W_]+|[|/,:;.!?()]+", re.UNICODE)


@lru_cache(maxsize=2048)
def _candidates(text, base):
    text = unicodedata.normalize("NFKC", unescape(text or "")).casefold()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.split(r"\s[|·]\s|\bgifs?\s+(?:by|on|via)\b", text, maxsplit=1)[0]
    text = re.sub(r"['’]s\b", "", text)
    runs, current, descriptors = [], [], set()
    for word in _TOKENS.findall(text[:512]):
        word = _CANONICAL.get(word, word)
        if (word in _STOP or word in _DESCRIPTORS or not 2 <= len(word) <= 32
                or not word[0].isalpha() or not word.isalnum()
                or re.fullmatch(r"[a-f0-9]{8,}", word)):
            if current:
                runs.append(current)
                current = []
            if word in _DESCRIPTORS:
                descriptors.add(word)
        elif not current or current[-1] != word:
            current.append(word)
    if current:
        runs.append(current)
    if not runs:
        return {word: 1 for word in descriptors}

    result = {}
    for run in runs:
        for start in range(len(run)):
            for size in range(1, min(3, len(run) - start) + 1):
                term = " ".join(run[start:start + size])
                if len(term) > 64:
                    continue
                exact = len(runs) == 1 and size == len(run)
                score = base + int(exact) * 3 + int(size == 1 and term in _ALIASES)
                result[term] = max(result.get(term, 0), score)
    return result


def _merge_plurals(matches):
    # Only merge when the other spelling actually occurs. This keeps names
    # such as "Star Wars" intact and also works for subjects outside the aliases.
    for term in sorted(matches, key=len, reverse=True):
        word = term.rsplit(" ", 1)[-1]
        if len(word) < 4 or not word.endswith("s") or word.endswith(("ss", "us", "is")):
            continue
        for suffix, ending in (("ies", "y"), ("ves", "f"), ("ves", "fe"), ("es", ""), ("s", "")):
            singular = term[:-len(suffix)] + ending
            if word.endswith(suffix) and singular in matches:
                combined = {hit[0]: hit for hit in matches[singular]}
                for hit in matches.pop(term):
                    if hit[0] not in combined or combined[hit[0]][2] < hit[2]:
                        combined[hit[0]] = hit
                matches[singular] = list(combined.values())
                break


def recurring_topics(records):
    """Return one supported topic per record, plus the number assigned to each topic.

    Each (id, title, original query, count) votes once per subject. Saved GIFs
    always count as one. Search records can represent several separate submissions.
    """
    matches = defaultdict(list)
    for key, title, query, count in records:
        titles = _candidates(title, 4)
        queries = _candidates(query, 7)
        for term in titles.keys() | queries.keys():
            strength = max(titles.get(term, 0), queries.get(term, 0))
            if term in titles and term in queries:
                strength += 2
            matches[term].append((key, count, strength))

    _merge_plurals(matches)
    ranked = []
    for term, hits in matches.items():
        support = sum(count for _, count, _ in hits)
        if support >= 2:
            strength = sum(count * score for _, count, score in hits) / support
            # A repeated full search is stronger evidence than one word in a caption.
            # Shared multiword names win ties over fragments of the same name.
            rank = strength + 2 * min(5, log2(support))
            ranked.append((-rank, -support, -len(term.split()), term))

    assignments, counts = {}, Counter()
    for *_, term in sorted(ranked):
        free = [(key, count) for key, count, _ in matches[term] if key not in assignments]
        support = sum(count for _, count in free)
        if support >= 2:
            counts[term] = support
            for key, _ in free:
                assignments[key] = term
    return assignments, counts
