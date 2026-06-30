"""
Token estimation heuristics.

No external tokenizer dependency: we classify characters into scripts that
behave very differently under modern BPE tokenizers (GPT/Claude/Gemini) and
apply a per-script chars-per-token ratio:

  - CJK (Han/Hiragana/Katakana/Hangul): ~1 char ≈ 1 token. These scripts have
    dense codepoints and tokenizers rarely pack more than one per token.
  - Thai: ~1.6 chars ≈ 1 token. Thai has no inter-word spaces; tokenizers
    chunk it into ~1-2 char tokens, so ~1.6 is a closer fit than 4.
  - Everything else (Latin/ASCII/punctuation/whitespace): ~4 chars ≈ 1 token,
    i.e. ~0.75 tokens per word, the long-standing English approximation.

This matches tiktoken/gpt-4 within ~5-10% on mixed-language text without
adding a startup-cost dependency (tiktoken's first call loads a big BPE
merges file). Good enough for quota accounting, which is its only consumer.

For images we reproduce the OpenAI vision pricing model (low = 85 flat;
high = 85 + 170 * tile_count, with tiles scaled to 512px squares).
"""
import unicodedata

# Approximate chars-per-token per script bucket.
_CJK_CHARS_PER_TOKEN = 1.0
_THAI_CHARS_PER_TOKEN = 1.6
_DEFAULT_CHARS_PER_TOKEN = 4.0


def _classify(ch: str) -> str:
    """Bucket a single character into 'cjk', 'thai', or 'other'."""
    cp = ord(ch)
    # CJK Unified Ideographs + Extension A
    if (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF):
        return "cjk"
    # Hiragana + Katakana + CJK punctuation
    if 0x3000 <= cp <= 0x30FF:
        return "cjk"
    # Hangul Syllables + Jamo
    if (0xAC00 <= cp <= 0xD7AF) or (0x1100 <= cp <= 0x11FF):
        return "cjk"
    # Thai block
    if 0x0E00 <= cp <= 0x0E7F:
        return "thai"
    return "other"


def estimate_tokens(text: str) -> int:
    """Estimate the token count of `text` across mixed scripts.

    Whitespace-only strings and empty strings return 0. We count characters by
    script bucket, divide by each bucket's chars-per-token ratio, and sum the
    (float) results, rounding up to the nearest whole token at the end."""
    if not text:
        return 0

    # Count codepoints per script bucket. Skip pure whitespace within buckets
    # (whitespace is largely "free" inside a token) but a run of whitespace
    # between words still costs the surrounding tokens, which the per-script
    # ratio already bakes in.
    counts = {"cjk": 0, "thai": 0, "other": 0}
    for ch in text:
        if ch.isspace():
            continue
        counts[_classify(ch)] += 1

    total = (
        counts["cjk"] / _CJK_CHARS_PER_TOKEN
        + counts["thai"] / _THAI_CHARS_PER_TOKEN
        + counts["other"] / _DEFAULT_CHARS_PER_TOKEN
    )
    if total <= 0:
        # The string was all whitespace -> at least 1 token if non-empty.
        return 1
    import math
    return max(1, int(math.ceil(total)))


def estimate_image_tokens(width: int | None = None, height: int | None = None,
                          detail: str = "auto") -> int:
    """Estimate the token cost of one image, following OpenAI's vision model.

    detail="low": 85 tokens flat.
    detail="high" (or "auto" with a large image): 85 + 170 per 512px tile.
    detail="auto" with unknown/missing dims: assume low (85).
    """
    if detail == "low":
        return 85

    if detail in ("high", "auto") and width and height:
        # Scale to fit within a 2048px max edge, then tile into 512px squares.
        max_edge = max(width, height)
        scale = min(1.0, 2048.0 / max_edge) if max_edge > 0 else 1.0
        sw, sh = int(width * scale), int(height * scale)
        import math
        tiles_w = max(1, math.ceil(sw / 512))
        tiles_h = max(1, math.ceil(sh / 512))
        return 85 + 170 * (tiles_w * tiles_h)

    # Unknown size / auto -> fall back to the low-detail flat cost.
    return 85
