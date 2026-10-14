"""The embedding contract.

The rule this module exists to enforce: **the database column and the provider must
agree on one dimension, and nothing may paper over a disagreement.** The PRD's own
`vector(1536)` alongside `all-MiniLM-L6-v2` is a 384-dimensional model - taking either
half of that literally would mean either rejecting every vector or silently truncating
them. So the dimension is declared once, here, and checked twice: against configuration
at startup, and again on every vector before it is persisted.

There is no padding, truncation, or reshape anywhere in this package. A vector of the
wrong length is an error, because the alternatives are all lies: a zero-padded vector
compares as "similar to everything", and a truncated one silently discards meaning.

On the choice of the shipped provider: see `HashingEmbeddingProvider`. The short version
is that it is deterministic, offline, and dependency-free, which keeps the whole feature
verifiable in CI. It is a lexical embedder, not a neural one, and that limitation is
documented rather than papered over. Swapping in a hosted model later is an
implementation change behind `EmbeddingProvider` and nothing else.
"""

import logging
import math
from typing import Protocol, runtime_checkable

log = logging.getLogger("app.knowledge.embedding")


class EmbeddingError(Exception):
    """Base for every embedding failure."""


class EmbeddingProviderUnavailable(EmbeddingError):
    """No provider is configured, or the configured one cannot be used."""


class EmbeddingInvalidDimension(EmbeddingError):
    """A vector's length does not match the declared contract.

    Never pad, truncate or reshape to get past this.
    """


class EmbeddingGenerationFailed(EmbeddingError):
    """The provider was reachable and produced nothing usable."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    """What the knowledge services need.

    `dimension` is part of the interface rather than configuration the caller passes in,
    which is what makes the startup cross-check possible.
    """

    name: str
    model: str
    dimension: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def validate_vector(vector: list[float], expected: int) -> list[float]:
    """Assert a vector's length and finiteness before it can reach the database.

    Called on the hot path and in tests, because the alternative - a `vector(384)` column
    quietly accepting a 1536-element value as a database error, or worse, being coerced -
    turns a provider bug into an outage.
    """
    if len(vector) != expected:
        raise EmbeddingInvalidDimension(
            f"expected {expected} dimensions, provider returned {len(vector)}"
        )
    for value in vector:
        if not math.isfinite(value):
            raise EmbeddingGenerationFailed(
                "provider returned a non-finite value; embeddings must be finite"
            )
    return vector


def l2_normalize(vector: list[float]) -> list[float]:
    """Scale to unit length so pgvector's cosine operator behaves predictably.

    A zero vector (empty or fully-punctuation input) is left as zeros rather than raising:
    it is a legitimate output meaning "nothing to say", and cosine distance against a zero
    vector is simply 1.0 - the least similar thing possible, which is the right outcome.
    """
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector
    return [value / norm for value in vector]


class HashingEmbeddingProvider:
    """The shipped provider: a deterministic, local, hashed-feature embedder.

    **What it is.** Text is projected into a fixed-width vector using the hashing trick
    over word tokens and character n-grams: each feature is hashed to a dimension index,
    weighted, and the result is L2-normalised. Identical input always produces an identical
    vector, with no network, no model file, and no runtime dependency.

    **Why it is the default.** The two alternatives the PRD names both make this feature
    unverifiable here and heavier than it needs to be:
      * `text-embedding-3-small` is 1536-dimensional but requires network plus an API key.
      * `all-MiniLM-L6-v2` is 384-dimensional but requires `sentence-transformers`, which
        drags in torch (~2 GB) and a model download at first use.

    Shipping either as the default would mean a knowledge base that cannot be exercised in
    CI and, for the second, a production image two orders of magnitude larger for no
    product benefit. So Phase 6 ships the small honest thing, and this class is the single
    place a different provider gets introduced.

    **What it is not.** This is a *lexical* embedder. It captures shared vocabulary,
    sub-word overlap and character shape, so "pricing" matches "price" and "₹50,000"
    matches "50000". It does **not** capture synonymy that shares no characters -
    "cheap" and "affordable" are distant to it. That is a real quality ceiling and it is
    the reason `RAG_MIN_SIMILARITY` exists and why the context prompt tells the model the
    retrieved text is a *reference*, not an answer key. Upgrading to a neural model is a
    deliberate later decision with a re-indexing cost, not a config tweak.
    """

    name = "hashing"
    model = "hashed-ngram-384"

    #: Fixed by this implementation. Configuration cannot widen or narrow it; that is the
    #: point of the contract.
    dimension = 384

    def __init__(self, dimension: int | None = None) -> None:
        if dimension is not None and dimension != self.dimension:
            # Fail loudly at construction rather than writing 384-float vectors into a
            # differently-shaped column.
            raise EmbeddingInvalidDimension(
                f"{self.model} produces {self.dimension} dimensions, not {dimension}"
            )
        self._cache: dict[str, list[float]] = {}

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self.embed_sync(texts)

    def _embed_one(self, text: str) -> list[float]:
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        vector = [0.0] * self.dimension
        tokens = _tokenize(text)
        if not tokens:
            # Nothing to embed. Zeros are a valid, least-similar output rather than an
            # error: an empty attachment with no text is normal traffic.
            self._cache[text] = vector
            return vector

        # Word features carry the signal; character trigrams keep morphological variants
        # ("pricing"/"price", "delivery"/"deliver") near each other. Word features are
        # weighted higher because exact vocabulary is the stronger relevance signal.
        for token in tokens:
            _add_feature(vector, f"w:{token}", 1.0)
        for token in tokens:
            padded = f"^{token}$"
            for i in range(len(padded) - 2):
                _add_feature(vector, f"c:{padded[i : i + 3]}", 0.35)

        # Sublinear term weighting, so a repeated word does not dominate the vector.
        vector = [math.copysign(abs(v) ** 0.5, v) if v else 0.0 for v in vector]
        vector = l2_normalize(vector)
        self._cache[text] = vector
        return vector


#: Configured name -> implementation. Adding a hosted provider later is a new class and one
#: line here; nothing in the knowledge services names a concrete provider.
_PROVIDERS: dict[str, type] = {
    HashingEmbeddingProvider.name: HashingEmbeddingProvider,
}


def build_provider(settings) -> EmbeddingProvider:
    """Construct the configured provider.

    Raises `EmbeddingProviderUnavailable` for an unknown name rather than falling back to
    a default, because silently embedding with the wrong model would write vectors that
    look fine and retrieve badly for the life of the index.
    """
    implementation = _PROVIDERS.get(settings.embedding_provider)
    if implementation is None:
        known = ", ".join(sorted(_PROVIDERS))
        raise EmbeddingProviderUnavailable(
            f"unknown EMBEDDING_PROVIDER '{settings.embedding_provider}'; known: {known}"
        )
    return implementation()


def _add_feature(vector: list[float], feature: str, weight: float) -> None:
    """Hash a feature to an index, and sign it, to limit collision bias.

    FNV-1a rather than Python's `hash()`, because `hash()` is salted per process and would
    make a vector non-reproducible across restarts - which would silently invalidate every
    previously indexed chunk.
    """
    h = 0x811C9DC5
    for byte in feature.encode("utf-8"):
        h = ((h ^ byte) * 0x01000193) & 0xFFFFFFFF
    index = h % len(vector)
    sign = 1.0 if (h >> 31) & 1 else -1.0
    vector[index] += sign * weight


def _tokenize(text: str) -> list[str]:
    """Lowercase word tokens, dropping single characters.

    Single characters are dropped because they are mostly punctuation once lowercased, and
    a one-character feature is almost pure noise in a 384-wide space.
    """
    out: list[str] = []
    current: list[str] = []
    for char in text.lower():
        if char.isalnum():
            current.append(char)
        elif current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return [token for token in out if len(token) > 1]


__all__ = [
    "EmbeddingError",
    "EmbeddingGenerationFailed",
    "EmbeddingInvalidDimension",
    "EmbeddingProvider",
    "EmbeddingProviderUnavailable",
    "HashingEmbeddingProvider",
    "build_provider",
    "l2_normalize",
    "validate_vector",
]
