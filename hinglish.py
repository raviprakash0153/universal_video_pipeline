"""Dependency-free conversion of Devanagari Hindi captions to readable Roman Hindi."""

import re
import unicodedata


_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "ny",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh",
    "ष": "sh", "स": "s", "ह": "h", "ळ": "l",
    "ड़": "r", "ढ़": "rh", "फ़": "f", "ज़": "z", "ख़": "kh",
    "ग़": "gh", "क़": "q",
}
_VOWELS = {"अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u",
           "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au"}
_MATRA = {"ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri",
          "े": "e", "ै": "ai", "ो": "o", "ौ": "au"}
# Conventional spellings used in everyday Hindi captions. This also handles
# lexical schwa deletion, which cannot be inferred reliably from spelling alone.
_COMMON = {
    "भाई": "Bhai", "क्या": "Kya", "नहीं": "Nahi", "नही": "Nahi", "है": "Hai",
    "बहुत": "Bahut", "बढ़िया": "badhiya", "कर": "kar", "दिया": "diya",
    "और": "aur", "मैं": "main", "तुम": "tum", "ये": "ye", "यह": "yeh",
    "था": "tha", "थी": "thi", "थे": "the", "जीत": "jeet", "गया": "gaya",
    "गयी": "gayi", "गए": "gaye", "चलो": "chalo", "अरे": "are", "वाह": "wah",
    "करो": "karo", "रहा": "raha", "रही": "rahi", "रहे": "rahe", "हैं": "hain",
    "था": "tha", "कैसे": "kaise", "क्यों": "kyun", "अच्छा": "achha",
    "सच": "sach", "मुझे": "mujhe", "आप": "aap", "हम": "hum", "यहाँ": "yahan",
    "वहाँ": "wahan", "देखो": "dekho", "देख": "dekh", "मार": "maar",
    "मारा": "maara", "गया": "gaya", "करदिया": "kar diya", "शक्ति": "shakti",
    "शुक्रिया": "shukriya", "ज़िंदगी": "zindagi", "ज़िंदगी": "zindagi",
    "लड़का": "ladka", "लड़की": "ladki", "कमल": "kamal", "नमक": "namak",
}
_DEVANAGARI = re.compile(r"[\u0900-\u097f]+")
_ANUSVARA = {"ं": "n", "ँ": "n", "ः": "h"}


def _transliterate_word(word: str) -> str:
    word = unicodedata.normalize("NFC", word)
    # Some Unicode producers emit the base letter and nukta separately even
    # after normalization; collapse those sequences to the dedicated letters.
    for base, nukta, composed in (("ड", "़", "ड़"), ("ढ", "़", "ढ़"),
                                  ("फ", "़", "फ़"), ("ज", "़", "ज़"),
                                  ("ख", "़", "ख़"), ("ग", "़", "ग़"),
                                  ("क", "़", "क़")):
        word = word.replace(base + nukta, composed)
    if word in _COMMON:
        return _COMMON[word]
    out = []
    chars = list(word)
    i = 0
    while i < len(chars):
        ch = chars[i]
        # Normalize decomposed nukta spellings (e.g. ड + ़ => ड़).
        if ch in _CONSONANTS:
            consonant = ch
            if i + 1 < len(chars) and chars[i + 1] == "़" and ch + "़" in _CONSONANTS:
                consonant = ch + "़"
                i += 1
            out.append(_CONSONANTS[consonant])
            if i + 1 < len(chars) and chars[i + 1] == "्":
                i += 1  # conjunct: suppress inherent vowel
            elif i + 1 < len(chars) and chars[i + 1] in _MATRA:
                i += 1
                out.append(_MATRA[chars[i]])
            else:
                out.append("a")
        elif ch in _VOWELS:
            out.append(_VOWELS[ch])
        elif ch in _MATRA:
            out.append(_MATRA[ch])
        elif ch in _ANUSVARA:
            out.append(_ANUSVARA[ch])
        # ZWJ/ZWNJ and other combining marks are orthographic, not spoken.
        i += 1

    result = "".join(out)
    # Hindi's word-final inherent vowel is normally silent (कमल -> kamal).
    if result.endswith("a"):
        result = result[:-1]
    # A useful, conservative internal schwa rule: drop the first of two
    # adjacent inherent-a syllables before a later vowel (कमला -> kamla).
    result = re.sub(r"([kgcjtdnpbmyrlvshf])a(?=[kgcjtdnpbmyrlvshf]a[^aeiou]*[aeiou])", r"\1", result)
    return result


def to_hinglish(text: str) -> str:
    """Romanize Hindi while preserving English text, digits, and punctuation."""
    return _DEVANAGARI.sub(lambda match: _transliterate_word(match.group()), str(text))


def caption_words(words, language: str = "original"):
    """Return display copies; input transcript tuples/records remain unchanged."""
    if language not in {"original", "hinglish"}:
        raise ValueError("caption language must be 'original' or 'hinglish'")
    if language == "original":
        return list(words)
    return [(start, end, to_hinglish(text)) for start, end, text in words]
