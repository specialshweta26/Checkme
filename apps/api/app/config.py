from __future__ import annotations

import os
from functools import lru_cache
from urllib.parse import quote_plus

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env(*names: str) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip().strip('"').strip("'")
        if value:
            return value
    return ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
        env_ignore_empty=True,
    )

    public_web_url: str = "http://localhost:3000"

    openai_api_key: str = ""
    openai_model: str = "gpt-6-luna"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_reasoning_effort: str = "none"
    embedding_dims: int = 1536

    google_client_id: str = Field(default="", validation_alias=AliasChoices("GOOGLE_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_ID"))
    google_client_secret: str = Field(
        default="", validation_alias=AliasChoices("GOOGLE_CLIENT_SECRET", "GOOGLE_OAUTH_CLIENT_SECRET")
    )
    microsoft_client_id: str = Field(
        default="", validation_alias=AliasChoices("MICROSOFT_CLIENT_ID", "OUTLOOK_CLIENT_ID", "AZURE_CLIENT_ID")
    )
    microsoft_client_secret: str = Field(
        default="",
        validation_alias=AliasChoices("MICROSOFT_CLIENT_SECRET", "OUTLOOK_CLIENT_SECRET", "AZURE_CLIENT_SECRET"),
    )
    microsoft_tenant: str = "common"

    session_secret: str = "dev-only-change-me"
    token_encryption_key: str = ""
    session_ttl_days: int = 14
    file_link_ttl_days: int = 90

    database_url: str = "postgresql+asyncpg://assistant:assistant@localhost:5432/assistant"
    database_hostport: str = ""
    postgres_user: str = "assistant"
    postgres_password: str = ""
    postgres_db: str = "assistant"
    redis_url: str = "redis://localhost:6379/0"

    letta_base_url: str = ""
    letta_api_key: str = ""
    letta_agent_model: str = ""
    letta_embedding: str = "openai/text-embedding-3-small"

    file_storage: str = "s3"
    storage_disk_path: str = "/var/data/attachments"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_public_endpoint_url: str = "http://localhost:9000"
    s3_bucket: str = "attachments"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    attachment_max_bytes: int = 25 * 1024 * 1024

    @property
    def use_disk_storage(self) -> bool:
        return (self.file_storage or "").strip().lower() in {"disk", "local", "fs", "filesystem"}

    allow_private_mcp_urls: bool = False

    @property
    def computer_backend(self) -> str:
        """Which sandbox backs the agent's computer: "e2b", "daytona", or "" when neither is configured."""
        wanted = (self.computer_provider or "").strip().lower()
        if wanted == "daytona" and self.daytona_api_key:
            return "daytona"
        if wanted == "e2b" and self.e2b_api_key:
            return "e2b"
        if self.e2b_api_key:
            return "e2b"
        if self.daytona_api_key:
            return "daytona"
        return ""

    daytona_api_key: str = ""
    daytona_api_url: str = "https://app.daytona.io/api"
    daytona_target: str = "us"
    # The agent's computer: "e2b" (E2B Desktop sandbox, default whenever E2B_API_KEY is set) or "daytona" (fallback).
    computer_provider: str = ""
    e2b_api_key: str = ""
    e2b_template: str = ""  # empty = E2B's public "desktop" template
    # Cloud shell toolchain (docs/COMPUTER.md). Every agent's shell runs on the cloud computer; missing runtimes and
    # CLIs (node/npm, pnpm, yarn, python/pip, git, gh, vercel, render, uv, ...) are installed on demand in user space.
    computer_toolchain_env: bool = True  # PATH/npm prefix prelude on every cloud shell command (~/.npm-global, ~/.local)
    computer_auto_install: bool = True  # exit 127 for a known tool: install it and retry the command once
    computer_install_timeout_s: int = 600
    computer_node_major: int = 22  # Node.js LTS line installed from nodejs.org when node is missing
    # user_computer_shell (the user's own machine) is only offered when a local computer is connected. When it is
    # called anyway without one, the command is re-run on the cloud computer (it never touches the user's machine).
    local_shell_fallback_to_cloud: bool = True
    # Stop / interrupt (docs/LLD.md §7.9): a running chat turn or background job can be cancelled from the UI,
    # the API or WhatsApp "stop"; a new message while a turn runs interrupts it and redirects by default.
    chat_interrupt_on_new_message: bool = True
    # Teach-by-demonstration: one session lasts at most this long (seconds); the UI counts down and auto-saves.
    teach_max_seconds: int = 600
    # Spoken narration recorded while teaching is transcribed with this OpenAI model (segment timestamps
    # need whisper-1's verbose_json; other models fall back to one untimed segment).
    openai_transcribe_model: str = "whisper-1"
    teach_audio_max_bytes: int = 24 * 1024 * 1024
    # Seconds of inactivity before the sandbox pauses (Hobby plan max 3600, Pro max 86400). Each tool call extends it.
    e2b_sandbox_timeout: int = 3600
    # Web search order (comma list of stages or provider names). Default: paid APIs (Tavily first, then Brave API,
    # Serper, Exa - only the keyed ones run), then the computer browser, then keyless server engines (Brave,
    # DuckDuckGo, Wikipedia). Each stage has a time budget.
    search_order: str = "paid,browser,free"
    search_provider_timeout_s: float = 8.0  # each free engine
    search_free_timeout_s: float = 15.0  # all free engines together
    search_browser_timeout_s: float = 45.0  # browser stage when the computer is already running
    search_browser_cold_timeout_s: float = 120.0  # browser stage when it has to boot the computer first
    # auto: boot a stopped computer for search only when no paid provider is left; always; never
    search_browser_boot: str = "auto"
    search_paid_timeout_s: float = 20.0  # all paid providers together
    # Optional paid web search API keys. Tavily is the first-choice provider when configured.
    tavily_api_key: str = ""
    # Ask Tavily search for each result's page text too (include_raw_content), cached briefly so opening a result
    # is instant; and use Tavily Extract as a read-only step of the browsing ladder. Both need TAVILY_API_KEY.
    tavily_raw_content: bool = True
    # Fast answers (docs/LLD.md §7.7): quick web turns never wait on the cloud computer.
    fast_path_enabled: bool = True
    stream_tokens: bool = True  # stream model tokens into the chat's working bubble (progress draft)
    tavily_include_answer: bool = True
    tavily_search_timeout_s: float = 10.0
    search_inline_pages: int = 3  # top web_search results that carry their page text
    search_inline_chars: int = 2500
    search_cache_ttl_s: float = 120.0  # identical searches within this window reuse the result
    web_fetch_on_screen: bool = False  # true: paint every web_fetch on the live computer screen (old, slow)
    web_fetch_browser_fallback: str = "warm"  # warm | always | never: blocked pages in the computer browser
    web_fetch_quick_total_s: float = 12.0
    web_fetch_quick_step_s: float = 6.0
    web_read_max_pages: int = 5
    web_read_page_timeout_s: float = 10.0
    tavily_extract: bool = True
    brave_search_api_key: str = ""
    serper_api_key: str = ""
    exa_api_key: str = ""
    composio_api_key: str = ""

    # Resilient browsing (docs/BROWSING.md). Everything is optional; the defaults need no account.
    browser_locale: str = "en-IN"  # browser language (navigator.language, Accept-Language)
    browser_timezone: str = ""  # empty = the user's profile timezone (default Asia/Kolkata)
    browser_http2: bool = True  # real Chrome speaks HTTP/2; turning it off is a bot signal
    browser_channel: str = ""  # "" = bundled Chromium; "chrome" = install and use Google Chrome
    browser_challenge_wait_s: float = 15.0  # how long a passive JS bot check may take to clear by itself
    # Egress proxy for the computer browser and the server fetch, e.g. a residential/ISP proxy:
    # http://user:pass@host:port (also https:// or socks5://). Empty = direct.
    browser_proxy_url: str = ""
    browser_proxy_for_fetch: bool = True
    # Keep each user's browser cookies (encrypted, per user) so a recycled computer is a returning visitor.
    browser_profile_persist: bool = True
    # Optional hosted browser used when the site blocks ours: "browserbase" or "steel" (+ its API key).
    remote_browser_provider: str = ""
    remote_browser_use_proxy: bool = False  # the provider's own proxies (paid plans)
    browserbase_api_key: str = ""
    browserbase_project_id: str = ""
    steel_api_key: str = ""
    # Read-only alternates when a site blocks: mobile / AMP versions, a reader service, a web archive copy.
    browse_alternates: bool = True
    browse_reader_url: str = "https://r.jina.ai/"  # empty disables the reader step
    jina_api_key: str = ""  # optional, raises the reader's rate limit
    browse_archive: bool = True
    browse_step_timeout_s: float = 20.0  # each read-only ladder step
    browse_total_timeout_s: float = 60.0  # the whole read-only ladder for one URL
    browse_remote_timeout_s: float = 60.0  # one hosted-browser read

    # Fast browser (docs/BROWSING.md "Fast browser"). Each switch falls back to the older path on any error.
    browser_fast_rpc: bool = True  # cache "daemon ready" per sandbox, send commands inline, live frame off the hot path
    browser_ready_ttl_s: float = 600.0  # how long a healthy daemon is trusted without a health probe
    browser_smart_waits: bool = True  # DOM-quiet / load-state waits instead of fixed sleeps in the daemon
    browser_compact_snapshot: bool = True  # model sees the numbered tree, not the 150-element JSON list
    browser_fast_loop: bool = True  # browser_task: a small model plans batches of actions, escalates on failure
    browser_fast_model: str = ""  # model for routine browser steps; empty = OPENAI_MODEL
    browser_escalate_model: str = ""  # model after repeated failures; empty = OPENAI_MODEL
    browser_fast_max_steps: int = 12  # model rounds per browser_task
    browser_replay: bool = True  # save successful browser_task runs and replay them without model calls
    browser_direct_downloads: bool = True  # fetch file links with the session cookies instead of clicking through
    browser_teach_dom: bool = True  # teach mode also records selectors (not just screen coordinates)
    browser_prewarm: str = "computer"  # computer = boot/warm the browser when a turn needs it; warm = only if running; off
    browser_timing_logs: bool = True  # one log line per browser command and per fast-loop step (no typed values)

    # Optional: pin the Composio GitHub auth config used for Connect GitHub (docs/GITHUB.md). Leave empty to use the
    # first enabled GitHub auth config (Composio creates a managed one if none exists).
    composio_github_auth_config_id: str = ""

    # WhatsApp channel (Meta WhatsApp Business Cloud API, docs/WHATSAPP.md). The channel is off unless the phone
    # number id, access token, app secret and verify token are all set.
    whatsapp_phone_number_id: str = ""
    whatsapp_access_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_business_number: str = ""  # E.164 digits of the business number, used for the wa.me link
    whatsapp_template_notify: str = ""  # approved template name for messages outside the 24h window
    whatsapp_template_lang: str = "en"
    whatsapp_graph_version: str = "v21.0"
    whatsapp_rate_limit_per_minute: int = 20
    # Which WhatsApp provider runs the channel: "twilio" or "meta". Empty picks twilio when TWILIO_ACCOUNT_SID is
    # set, else meta. One shared business number serves every user; numbers are linked to accounts by code.
    whatsapp_provider: str = ""
    # Public base URL Twilio posts to (the web app, which proxies /api). Twilio signs this exact URL, so it must
    # match what is configured on the sender. Empty falls back to PUBLIC_WEB_URL.
    whatsapp_public_base_url: str = ""

    # WhatsApp via Twilio (docs/WHATSAPP.md). On when the account SID, auth token and WhatsApp number are set.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""  # validates X-Twilio-Signature; also the REST fallback credential
    twilio_api_key_sid: str = ""  # preferred REST credential (SK...) with twilio_api_key_secret
    twilio_api_key_secret: str = ""
    twilio_whatsapp_number: str = ""  # "whatsapp:+14155238886" or "+14155238886"
    twilio_content_sid_approval: str = ""  # optional quick-reply Content template (HX...) for approvals
    twilio_content_sid_notify: str = ""  # optional approved Content template (HX...) for outside the 24h window
    twilio_status_callback: bool = False  # ask Twilio to post delivery status to /api/channels/whatsapp/twilio/status

    # Routing knobs, tuned by the routing eval.
    route_margin: float = 0.08
    max_tool_schemas: int = 12
    chat_rate_limit_per_minute: int = 30
    history_messages: int = 8
    max_agent_steps: int = 20

    @model_validator(mode="after")
    def fill_oauth_from_environ(self):
        if not self.google_client_id:
            self.google_client_id = _env("GOOGLE_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_ID")
        if not self.google_client_secret:
            self.google_client_secret = _env("GOOGLE_CLIENT_SECRET", "GOOGLE_OAUTH_CLIENT_SECRET")
        if not self.microsoft_client_id:
            self.microsoft_client_id = _env("MICROSOFT_CLIENT_ID", "OUTLOOK_CLIENT_ID", "AZURE_CLIENT_ID")
        if not self.microsoft_client_secret:
            self.microsoft_client_secret = _env(
                "MICROSOFT_CLIENT_SECRET", "OUTLOOK_CLIENT_SECRET", "AZURE_CLIENT_SECRET"
            )
        if self.public_web_url:
            self.public_web_url = self.public_web_url.rstrip("/")
        return self

    @property
    def secure_cookies(self) -> bool:
        return self.public_web_url.startswith("https://")

    @property
    def async_database_url(self) -> str:
        from urllib.parse import urlparse

        hostport = (self.database_hostport or "").strip()
        for prefix in ("http://", "https://", "postgresql://", "postgres://", "postgresql+asyncpg://"):
            if hostport.lower().startswith(prefix):
                hostport = hostport[len(prefix) :].lstrip("/")
                break
        if "@" in hostport:
            hostport = hostport.rsplit("@", 1)[-1]
        hostport = hostport.split("/")[0].strip()
        if hostport:
            user = quote_plus(self.postgres_user or "assistant")
            password = quote_plus(self.postgres_password or "")
            db = quote_plus(self.postgres_db or "assistant")
            return f"postgresql+asyncpg://{user}:{password}@{hostport}/{db}"
        url = (self.database_url or "").strip()
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        if url.startswith("postgresql://") and "+asyncpg" not in url:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        host = (urlparse(url.replace("postgresql+asyncpg://", "postgresql://", 1)).hostname or "").lower()
        if os.environ.get("RENDER") and host in {"", "localhost", "127.0.0.1", "postgres", "postgresql"}:
            raise RuntimeError(
                "Render DATABASE_HOSTPORT is missing or still points at Docker. "
                "On checkme-api, set DATABASE_HOSTPORT from checkme-postgres (hostport) and remove any DATABASE_URL that uses host 'postgres'."
            )
        return url

    @property
    def database_host(self) -> str:
        from urllib.parse import urlparse

        raw = self.async_database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
        parsed = urlparse(raw)
        host = parsed.hostname or ""
        port = parsed.port
        return f"{host}:{port}" if port else host


@lru_cache
def get_settings() -> Settings:
    return Settings()
