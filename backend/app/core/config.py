"""Typed, environment-driven application configuration."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# backend/app/core/config.py -> backend/app/core -> backend/app -> backend -> repo root
REPO_ROOT_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

AppEnv = Literal["development", "test", "production"]


class Settings(BaseSettings):
    """All runtime configuration. Secrets come from the environment, never from code."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Application ---------------------------------------------------
    app_env: AppEnv = "development"
    app_name: str = "quali-fly-api"
    app_version: str = "0.1.0"
    log_level: str = "INFO"

    # -- Database ------------------------------------------------------
    database_url: str
    # Owner/migration role. Must be a different, non-superuser application role so
    # RLS cannot be bypassed: superusers and table owners always ignore RLS.
    migration_database_url: str | None = None
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_echo: bool = False

    # -- HTTP ----------------------------------------------------------
    # Comma-separated or JSON list of exact origins. `*` is rejected on purpose.
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    api_prefix: str = "/api/v1"
    request_id_header: str = "X-Request-ID"

    # -- Auth ----------------------------------------------------------
    session_ttl_seconds: int = 60 * 60 * 24 * 7  # 7 days

    # -- Meta integration ----------------------------------------------
    # The unprivileged database role. Migration 0003 needs it to GRANT EXECUTE on
    # the trusted source-resolution function to that role only.
    app_db_role: str | None = None
    # Platform-level Meta app secret. Signatures are HMAC'd with this, so it is
    # the boundary between "genuinely delivered by our Meta app" and everything
    # else. Never logged, never returned. Also used server-to-server for the
    # authorization-code exchange, which is why it must never reach a client.
    meta_app_secret: str | None = None
    meta_signature_header: str = "X-Hub-Signature-256"
    # Meta app id. Required for OAuth only; webhook delivery does not use it.
    meta_app_id: str | None = None
    # Graph API version used for every outbound call. Verified against Meta's
    # changelog on 2026-09-28: v26.0 was released 2026-07-29 and is current;
    # each version is supported at least two years. Change this in one place.
    meta_graph_api_version: str = "v26.0"
    # Absolute URL Meta redirects back to. Must byte-for-byte match a Valid OAuth
    # Redirect URI in the Meta app dashboard, or Meta rejects the exchange.
    meta_oauth_redirect_uri: str | None = None
    # Scopes requested at authorization time. The Lead Ads ones are what Meta's Lead Ads
    # documentation requires for webhook subscription plus lead retrieval;
    # `instagram_basic` is what Phase 10-O.2 needs to read a Page's
    # `instagram_business_account` edge, which is how Instagram accounts are discovered.
    # Verifying them against your own app's approved permissions is a deployment step -
    # see README.
    #
    # Deliberately still absent: `instagram_manage_messages` and `pages_messaging`. Those
    # are what *sending* needs (and `app/services/channels.py` already declares them as the
    # channels' required permissions), but requesting them changes the consent screen for
    # every tenant to connect a Page for lead retrieval alone. Whether to ask for them is
    # a deployment decision taken before Phase 10-O.3 needs them, not a side effect of
    # being able to see the Instagram account.
    meta_oauth_scopes: str = (
        "leads_retrieval,pages_manage_metadata,pages_show_list,pages_read_engagement,"
        "ads_management,instagram_basic"
    )
    # Short-lived and single-use: this only has to survive the user clicking
    # "approve" in a Meta dialog.
    meta_oauth_state_ttl_seconds: int = 600
    # base64url of 32 bytes. Encrypts Meta access tokens at rest (AES-256-GCM).
    # Required in production; see the validator below.
    meta_token_encryption_key: str | None = None
    # Per-request budget for an outbound Graph call. The webhook ACK never waits
    # on this, but a worker must not hang forever either.
    meta_graph_timeout_seconds: float = 10.0

    # Phase 10-D: webhook event claim ownership. How long a claimed event may sit in
    # PROCESSING before another worker may reclaim it, and how many processing attempts one
    # event may have before it is left for a human.
    #
    # The lease must exceed `meta_graph_timeout_seconds` plus the time to persist the
    # result. If it did not, a worker doing a slow-but-successful lead retrieval would be
    # reclaimed mid-flight and its own result discarded as stale - the guard that stops
    # stale writes would be manufacturing them.
    webhook_processing_lease_seconds: int = 300
    # Bounded so an event failing for a persistent reason that merely looks transient (a
    # leadgen id Meta has not finished indexing, say) stops instead of spinning forever.
    webhook_max_attempts: int = 5

    # -- AI qualification (Phase 5) ------------------------------------
    # Master switch. When off, an inbound message is still stored by Phase 4 and no AI
    # call is made at all, so the product degrades to "capture without replying" rather
    # than failing.
    ai_enabled: bool = False
    # Provider names, resolved by the registry in `app/services/llm_registry.py`.
    # Never branched on in business logic.
    ai_primary_provider: str = "groq"
    ai_fallback_provider: str = "gemini"

    # Provider credentials. Never logged, never returned, never sent to a browser.
    groq_api_key: str | None = None
    gemini_api_key: str | None = None
    # A fallback provider without a key is skipped rather than failing the run, so
    # turning Gemini off is a configuration change and not a code change.
    gemini_enabled: bool = True

    # Model identifiers are configuration, not business logic. Defaults preserve the
    # PRD's tiered intent: a small/fast primary and a larger escalation model. Verified
    # against Groq's model list on 2026-09-28.
    groq_primary_model: str = "llama-3.1-8b-instant"
    groq_escalation_model: str = "llama-3.3-70b-versatile"
    gemini_model: str = "gemini-2.5-flash"

    # PRD values. Gemini gets its own temperature because Google's current guidance is
    # that 3.x-family models can loop or degrade below 1.0; see the provider module.
    ai_temperature: float = 0.3
    ai_gemini_temperature: float = 1.0
    ai_max_output_tokens: int = 150
    ai_request_timeout_seconds: float = 20.0
    # Bound on every provider call, and on how long a rate-limit backoff may sleep.
    ai_max_provider_attempts: int = 3
    ai_max_backoff_seconds: float = 2.0

    # Prompt construction. Both are hard bounds, not suggestions.
    ai_history_limit: int = 10
    ai_max_response_chars: int = 1000
    ai_max_prompt_chars: int = 12000

    # PRD rule: a total score above this is high intent and hands off to a human.
    ai_handoff_score_threshold: int = 75

    @field_validator("ai_history_limit", "ai_max_output_tokens", "ai_max_response_chars")
    @classmethod
    def _must_be_positive(cls, value: int, info: Any) -> int:
        if value < 1:
            raise ValueError(f"{info.field_name} must be at least 1")
        return value

    @field_validator("ai_handoff_score_threshold")
    @classmethod
    def _valid_threshold(cls, value: int) -> int:
        # Dimensions are 0-25 each, so the total is 0-100.
        if not 0 <= value <= 100:
            raise ValueError("AI_HANDOFF_SCORE_THRESHOLD must be between 0 and 100")
        return value

    # -- knowledge base / RAG (Phase 6) --------------------------------
    # Master switch. When off, retrieval returns nothing and Phase 5 falls back to its
    # no-knowledge path, so a deployment can disable RAG without affecting the AI pipeline.
    embedding_enabled: bool = False
    # Provider and model are recorded on every result for audit, but the *dimension* is
    # fixed by the provider implementation and cross-checked at startup. Configuring a
    # dimension is not a way to reshape the column; see `_validate_embedding_contract`.
    embedding_provider: str = "hashing"
    embedding_model: str = "hashed-ngram-384"
    embedding_dimension: int = 384
    embedding_timeout: float = 30.0

    # Chunking, in characters. 1200 with 200 overlap keeps a typical FAQ answer in one
    # chunk while still splitting long documents, and the overlap stops a boundary from
    # severing a sentence.
    knowledge_chunk_size: int = 1200
    knowledge_chunk_overlap: int = 200
    # Hard ceiling on chunks persisted per document. Bounds the worst case (a pathological
    # paste) so one request cannot start an unbounded number of provider calls.
    knowledge_max_chunks_per_document: int = 500

    # Top-5 is a Phase 6 contract, not a preference, so this is capped rather than
    # free-form: a larger K would push context selection into the AI layer's lap, which is
    # exactly the boundary this phase is meant to hold.
    rag_top_k: int = 5
    # Cosine similarity floor. Deliberately conservative: a weakly-matching chunk is worse
    # than none, because the model may treat it as fact and state it to a lead. Set to 0 to
    # disable the filter and take the top K regardless.
    rag_min_similarity: float = 0.15
    # Ceiling on rendered knowledge context, so a tenant with many large chunks cannot
    # crowd out the conversation itself.
    rag_max_context_chars: int = 4000

    @field_validator("rag_top_k")
    @classmethod
    def _bounded_top_k(cls, value: int) -> int:
        if not 1 <= value <= 5:
            raise ValueError("RAG_TOP_K must be between 1 and 5")
        return value

    @field_validator("rag_min_similarity")
    @classmethod
    def _valid_similarity(cls, value: float) -> float:
        # Cosine similarity is bounded; anything outside this cannot occur, so a value
        # here is a typo rather than a tuning choice.
        if not -1.0 <= value <= 1.0:
            raise ValueError("RAG_MIN_SIMILARITY must be between -1.0 and 1.0")
        return value

    @field_validator("knowledge_chunk_size", "knowledge_max_chunks_per_document")
    @classmethod
    def _knowledge_value_positive(cls, value: int, info: Any) -> int:
        # Distinctly named from the Phase 5 `_must_be_positive`. Two validators with the
        # same name in one class silently keep only the last, which would have removed the
        # AI bounds above without any error - a validator that looks present and is not.
        if value < 1:
            raise ValueError(f"{info.field_name} must be at least 1")
        return value

    @field_validator("knowledge_chunk_overlap")
    @classmethod
    def _valid_overlap(cls, value: int, info: Any) -> int:
        if value < 0:
            raise ValueError("KNOWLEDGE_CHUNK_OVERLAP must not be negative")
        return value

    @model_validator(mode="after")
    def _validate_embedding_contract(self) -> "Settings":
        """Fail closed if the configured dimension disagrees with the provider.

        This is the whole point of the setting existing. `EMBEDDING_DIMENSION` is checked
        *against* the provider rather than being used to configure it, so nobody can point
        a 384-wide column at a 1536-wide model and have vectors silently truncated on the
        way in.
        """
        from app.services.embeddings import EmbeddingProviderUnavailable, build_provider

        if self.embedding_overlap_is_invalid():
            raise ValueError("KNOWLEDGE_CHUNK_OVERLAP must be smaller than KNOWLEDGE_CHUNK_SIZE")
        if not self.embedding_enabled:
            return self
        try:
            provider = build_provider(self)
        except EmbeddingProviderUnavailable as exc:
            raise ValueError(str(exc)) from exc
        if provider.dimension != self.embedding_dimension:
            raise ValueError(
                f"EMBEDDING_DIMENSION is {self.embedding_dimension} but provider "
                f"'{provider.name}' model '{provider.model}' produces "
                f"{provider.dimension}. They must match; the database column is fixed at "
                f"the provider's width."
            )
        return self

    def embedding_overlap_is_invalid(self) -> bool:
        return self.knowledge_chunk_overlap >= self.knowledge_chunk_size

    # -- automated follow-up engine (Phase 7) ---------------------------
    # Master switch. When off, nothing is scheduled and the poller does not start, so a
    # deployment can run the product with capture-only behaviour.
    follow_up_enabled: bool = False
    # The poller is only a trigger. PostgreSQL `scheduled_tasks` is the durable source of
    # truth, which is what makes a container restart harmless.
    follow_up_poll_interval_seconds: int = 30
    follow_up_batch_size: int = 20
    # How long a claimed task may sit in PROCESSING before another worker may reclaim it.
    # Must comfortably exceed the slowest plausible execute (AI call + Meta send), or two
    # workers will race on the same task. Validated against that in `_validate_follow_ups`.
    follow_up_processing_lease_seconds: int = 300
    follow_up_max_attempts: int = 3
    follow_up_retry_base_seconds: int = 60
    # The PRD's 7-day inactivity boundary at which a silent lead becomes GHOSTED.
    follow_up_ghosted_days: int = 7
    # Allow AI enhancement of the tenant's template. Off means the deterministic template
    # is always used, which is the safe default when no provider is configured.
    follow_up_ai_enhance: bool = True

    @field_validator("follow_up_poll_interval_seconds", "follow_up_batch_size")
    @classmethod
    def _follow_up_positive(cls, value: int, info: Any) -> int:
        if value < 1:
            raise ValueError(f"{info.field_name} must be at least 1")
        return value

    @field_validator("webhook_max_attempts")
    @classmethod
    def _webhook_attempts(cls, value: int) -> int:
        if value < 1:
            raise ValueError("WEBHOOK_MAX_ATTEMPTS must be at least 1")
        if value > 20:
            # Bounded above for the same reason follow-ups are: an event retried twenty
            # times is a lead being fetched from Meta twenty times.
            raise ValueError("WEBHOOK_MAX_ATTEMPTS must not exceed 20")
        return value

    @field_validator("webhook_processing_lease_seconds")
    @classmethod
    def _webhook_lease(cls, value: int, info: Any) -> int:
        if value < 1:
            raise ValueError("WEBHOOK_PROCESSING_LEASE_SECONDS must be at least 1")
        return value

    @field_validator("follow_up_max_attempts", "follow_up_ghosted_days")
    @classmethod
    def _follow_up_range(cls, value: int, info: Any) -> int:
        if value < 1:
            raise ValueError(f"{info.field_name} must be at least 1")
        if info.field_name == "follow_up_max_attempts" and value > 10:
            # Bounded on the upper side too. Follow-ups are a courtesy; a task retried ten
            # times is a lead being messaged ten times.
            raise ValueError("FOLLOW_UP_MAX_ATTEMPTS must not exceed 10")
        return value

    @model_validator(mode="after")
    def _validate_follow_ups(self) -> "Settings":
        if self.follow_up_processing_lease_seconds < 1:
            raise ValueError("FOLLOW_UP_PROCESSING_LEASE_SECONDS must be at least 1")
        if self.webhook_processing_lease_seconds <= self.meta_graph_timeout_seconds:
            # The invariant the stale-write guard depends on. A lease shorter than the
            # slowest legitimate external call guarantees the call completes *after* the
            # lease expired, so its result is correctly refused as stale - which means the
            # work is thrown away every time and the event can never complete.
            raise ValueError(
                "WEBHOOK_PROCESSING_LEASE_SECONDS must exceed META_GRAPH_TIMEOUT_SECONDS, "
                f"got {self.webhook_processing_lease_seconds} <= {self.meta_graph_timeout_seconds}"
            )
        return self

    # -- human handoff (Phase 8) -----------------------------------------
    # Master switch. When off, an escalation is still *recorded* by Phase 5 exactly as
    # before, but no handoff row, no notification and no acknowledgement are created. The
    # AI keeps replying, so this is a rollback lever rather than a quiet degradation: it
    # hands the conversation back to automation, which is a decision an operator should
    # have to make explicitly.
    handoff_enabled: bool = True
    # The deterministic acknowledgement. Sent only when the AI's own reply did not reach
    # the lead, so a healthy AI pipeline produces exactly one message and not two.
    #
    # No placeholders on purpose. This is the one message a lead may see when the product
    # has already failed them once (the AI send failed), so it must not be able to render a
    # name we do not have, and it must not be a template that invites one. It also has to be
    # true: promising a callback we may not make is worse than the silence.
    handoff_acknowledgement_text: str = (
        "Thanks for your patience - I've passed this to our team and someone will be in "
        "touch shortly."
    )
    # How much of the lead's last message is kept on the handoff row for the queue preview.
    # Bounded because this is a triage line, not a transcript; the full conversation is
    # always one click away and always has been.
    handoff_preview_chars: int = 280

    @field_validator("handoff_preview_chars")
    @classmethod
    def _handoff_preview_range(cls, value: int) -> int:
        if not 40 <= value <= 2000:
            raise ValueError("HANDOFF_PREVIEW_CHARS must be between 40 and 2000")
        return value

    @field_validator("meta_graph_api_version")
    @classmethod
    def _validate_graph_version(cls, value: str) -> str:
        version = value.strip()
        if not version.startswith("v") or not version[1:].split(".")[0].isdigit():
            raise ValueError("META_GRAPH_API_VERSION must look like 'v26.0'.")
        return version

    @model_validator(mode="after")
    def _fail_closed_in_production(self) -> "Settings":
        """Refuse to boot production with the secrets Phase 3 depends on.

        Encrypting a token with a missing key must never silently fall back to
        plaintext, and OAuth must never appear configured when it is not. Both
        are startup failures, not runtime surprises.
        """
        if not self.is_production:
            return self
        if not self.meta_token_encryption_key:
            raise ValueError(
                "META_TOKEN_ENCRYPTION_KEY is required in production so Meta access "
                "tokens are never stored in plaintext."
            )
        if not (self.meta_app_id and self.meta_app_secret and self.meta_oauth_redirect_uri):
            raise ValueError(
                "META_APP_ID, META_APP_SECRET and META_OAUTH_REDIRECT_URI are all "
                "required in production for the Meta OAuth flow."
            )
        if self.ai_enabled and not self.groq_api_key:
            # AI_ENABLED without a credential would fail every inbound message at the
            # provider instead of at boot, which is much harder to diagnose.
            raise ValueError(
                "AI_ENABLED is set but GROQ_API_KEY is missing. Provide a key, or set "
                "AI_ENABLED=false to run without AI replies."
            )
        return self

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        value = value.strip()
        if value.startswith("["):
            return json.loads(value)
        return [origin.strip() for origin in value.split(",") if origin.strip()]

    @field_validator("cors_allowed_origins")
    @classmethod
    def _reject_wildcard(cls, value: list[str]) -> list[str]:
        if "*" in value:
            raise ValueError("CORS_ALLOWED_ORIGINS must list explicit origins; '*' is not allowed.")
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton. Call `get_settings.cache_clear()` after changing env."""
    return Settings()  # type: ignore[call-arg]
