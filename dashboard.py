#!/usr/bin/env python3
"""
=============================================================================
  DASHBOARD.PY — «Режиссёр-Аналитик» Enterprise Control Panel v2.0
=============================================================================
  Премиальное Streamlit-приложение: оркестратор Content Arbitrage Pipeline.

  Архитектура:
    - Enterprise UI (Streamlit) с кэшируемыми настройками
    - Key Rotation («Револьвер») — автоитерация по массивам API-ключей
    - Interactive Fallback — без sys.exit, алерты + кнопки повтора
    - Air Gap — только config.json + scenario.md, никакого FFmpeg

  Запуск:
    streamlit run dashboard.py

  Зависимости:
    pip install streamlit google-api-python-client youtube-transcript-api groq google-generativeai
=============================================================================
"""

import os
import re
import json
import time
import datetime
import hashlib
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import streamlit as st



# =============================================================================
# CONSTANTS & DEFAULTS
# =============================================================================

APP_TITLE = "Режиссёр-Аналитик v2.0"
APP_ICON = "🎬"
SYSTEM_PROMPT_FILE = "SYSTEM_PROMPT.md"
OUTPUT_DIR = Path("output")
MAX_OUTPUT_TOKENS = 8192

GROQ_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-70b-versatile",
    "mixtral-8x7b-32768",
]
GEMINI_MODELS = [
    "gemini-1.5-pro-latest",
    "gemini-2.5-flash-preview-05-20",
    "gemini-1.5-flash",
]

# Retryable HTTP status codes / error substrings
RETRYABLE_ERRORS = ["429", "rate_limit", "quota", "quotaExceeded", "resource_exhausted"]



# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class PipelineError:
    """Structured error for UI rendering instead of sys.exit."""
    stage: str
    message: str
    recoverable: bool = True
    suggest_fallback: bool = False


@dataclass
class VideoData:
    """Container for fetched YouTube data."""
    video_id: str = ""
    title: str = ""
    channel_name: str = ""
    description: str = ""
    tags: list = field(default_factory=list)
    transcript: str = ""
    word_count: int = 0
    line_count: int = 0



# =============================================================================
# KEY ROTATION ENGINE («Револьвер»)
# =============================================================================

class KeyRevolver:
    """
    Manages an array of API keys with automatic rotation on quota/rate errors.
    Thread-safe within a single Streamlit session.
    """

    def __init__(self, keys: list[str], provider_name: str):
        self._keys = [k.strip() for k in keys if k.strip()]
        self._provider = provider_name
        self._index = 0
        self._exhausted: set[int] = set()

    @property
    def available_count(self) -> int:
        return len(self._keys) - len(self._exhausted)

    @property
    def total_count(self) -> int:
        return len(self._keys)

    @property
    def current_key(self) -> Optional[str]:
        if not self._keys or self._index >= len(self._keys):
            return None
        return self._keys[self._index]

    def rotate(self) -> Optional[str]:
        """Mark current key as exhausted and rotate to next available."""
        self._exhausted.add(self._index)
        # Find next non-exhausted key
        for i in range(len(self._keys)):
            candidate = (self._index + 1 + i) % len(self._keys)
            if candidate not in self._exhausted:
                self._index = candidate
                return self._keys[self._index]
        return None  # All keys exhausted

    def reset(self) -> None:
        """Reset all exhaustion markers (e.g., after cooldown)."""
        self._exhausted.clear()
        self._index = 0

    def mask_key(self, key: str) -> str:
        """Returns masked version for UI display: first4...last4."""
        if len(key) <= 10:
            return "***"
        return f"{key[:4]}...{key[-4:]}"



# =============================================================================
# YOUTUBE DATA LAYER
# =============================================================================

def extract_video_id(url: str) -> Optional[str]:
    """Extract YouTube video_id from various URL formats."""
    patterns = [
        r"(?:v=)([a-zA-Z0-9_-]{11})",
        r"youtu\.be/([a-zA-Z0-9_-]{11})",
        r"shorts/([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def fetch_metadata(video_id: str, revolver: KeyRevolver) -> tuple[Optional[dict], Optional[PipelineError]]:
    """
    Fetch video metadata via YouTube Data API v3 with key rotation.
    Returns (metadata_dict, None) on success or (None, PipelineError) on failure.
    """
    from googleapiclient.discovery import build

    last_error = ""
    while revolver.available_count > 0:
        key = revolver.current_key
        try:
            youtube = build("youtube", "v3", developerKey=key)
            response = youtube.videos().list(part="snippet", id=video_id).execute()

            items = response.get("items", [])
            if not items:
                return None, PipelineError(
                    stage="YouTube Metadata",
                    message=f"Видео '{video_id}' не найдено или недоступно.",
                    recoverable=False,
                )

            snippet = items[0]["snippet"]
            return {
                "video_id": video_id,
                "title": snippet.get("title", "N/A"),
                "channel_name": snippet.get("channelTitle", "N/A"),
                "description": snippet.get("description", "")[:1500],
                "tags": snippet.get("tags", []),
            }, None

        except Exception as e:
            last_error = str(e)
            if any(err in last_error.lower() for err in RETRYABLE_ERRORS):
                next_key = revolver.rotate()
                if next_key:
                    continue
            break

    return None, PipelineError(
        stage="YouTube Metadata",
        message=f"Все YouTube API ключи исчерпаны или ошибка: {last_error[:200]}",
        recoverable=True,
    )



def fetch_transcript(video_id: str) -> tuple[Optional[str], Optional[PipelineError]]:
    """
    Fetch transcript with timecodes using robust multi-strategy fallback.
    Priority: manual en → auto en → any language translated to en → any raw.
    Returns (formatted_transcript, None) or (None, PipelineError).
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None, PipelineError(
            stage="Transcript",
            message="Библиотека youtube-transcript-api не установлена.",
            recoverable=False,
        )

    try:
        from youtube_transcript_api import TranscriptsDisabled, NoTranscriptFound
    except ImportError:
        TranscriptsDisabled = Exception
        NoTranscriptFound = Exception

    raw_entries = None

    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

        # ─── Strategy 1: Find English transcript (manual or auto) ───
        try:
            transcript_obj = transcript_list.find_transcript(["en", "en-US", "en-GB"])
            raw_entries = transcript_obj.fetch()
        except Exception:
            pass

        # ─── Strategy 2: Manual transcript in any language → translate to English ───
        if not raw_entries:
            try:
                for tr in transcript_list:
                    if not tr.is_generated:
                        try:
                            raw_entries = tr.translate("en").fetch()
                            break
                        except Exception:
                            raw_entries = tr.fetch()
                            break
            except Exception:
                pass

        # ─── Strategy 3: Auto-generated in any language → translate to English ───
        if not raw_entries:
            try:
                for tr in transcript_list:
                    if tr.is_generated:
                        try:
                            raw_entries = tr.translate("en").fetch()
                            break
                        except Exception:
                            raw_entries = tr.fetch()
                            break
            except Exception:
                pass

        # ─── Strategy 4: Take literally anything available ───
        if not raw_entries:
            try:
                for tr in transcript_list:
                    raw_entries = tr.fetch()
                    break
            except Exception:
                pass

    except TranscriptsDisabled:
        return None, PipelineError(
            stage="Transcript",
            message="Субтитры отключены автором видео.",
            recoverable=False,
        )
    except Exception as e:
        err_str = str(e)
        if "disabled" in err_str.lower():
            return None, PipelineError(
                stage="Transcript",
                message="Субтитры отключены автором видео.",
                recoverable=False,
            )
        # Last resort: try simple get_transcript
        try:
            raw_entries = YouTubeTranscriptApi.get_transcript(video_id)
        except Exception:
            try:
                raw_entries = YouTubeTranscriptApi.get_transcript(video_id, languages=["en"])
            except Exception:
                pass

    if not raw_entries:
        return None, PipelineError(
            stage="Transcript",
            message="Субтитры не найдены ни одним методом для этого видео.",
            recoverable=False,
        )

    # Format: [HH:MM:SS] Text
    lines = []
    for entry in raw_entries:
        start_sec = int(entry.get("start", 0))
        h, m, s = start_sec // 3600, (start_sec % 3600) // 60, start_sec % 60
        text = entry.get("text", "").replace("\n", " ").strip()
        if text:
            lines.append(f"[{h:02d}:{m:02d}:{s:02d}] {text}")

    if not lines:
        return None, PipelineError(
            stage="Transcript",
            message="Транскрипт пуст — субтитры не содержат текста.",
            recoverable=False,
        )

    transcript_str = "\n".join(lines)
    return transcript_str, None



# =============================================================================
# LLM LAYER — WITH KEY ROTATION & FALLBACK SIGNALING
# =============================================================================

def _is_retryable(error_str: str) -> bool:
    """Check if the error is retryable (quota/rate limit)."""
    lower = error_str.lower()
    return any(err in lower for err in RETRYABLE_ERRORS)


def _is_context_overflow(error_str: str) -> bool:
    """Check if error is context length overflow."""
    lower = error_str.lower()
    return any(kw in lower for kw in ["context_length", "too long", "token limit", "max.*token"])


def call_groq(
    system_prompt: str,
    user_message: str,
    revolver: KeyRevolver,
    model: str,
    temperature: float = 0.4,
) -> tuple[Optional[str], Optional[PipelineError]]:
    """Call Groq API with key rotation. Returns (response_text, None) or (None, error)."""
    from groq import Groq

    last_error = ""
    while revolver.available_count > 0:
        key = revolver.current_key
        try:
            client = Groq(api_key=key)
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=temperature,
            )
            return response.choices[0].message.content, None

        except Exception as e:
            last_error = str(e)
            if _is_retryable(last_error):
                next_key = revolver.rotate()
                if next_key:
                    time.sleep(1)  # Brief cooldown between rotations
                    continue
            break

    # Determine error type for UI
    if _is_context_overflow(last_error):
        return None, PipelineError(
            stage="Groq LLM",
            message=f"Context overflow: транскрипт слишком длинный для {model}.",
            recoverable=True,
            suggest_fallback=True,
        )

    return None, PipelineError(
        stage="Groq LLM",
        message=f"Все Groq ключи исчерпаны или ошибка: {last_error[:300]}",
        recoverable=True,
        suggest_fallback=True,
    )



def call_gemini(
    system_prompt: str,
    user_message: str,
    revolver: KeyRevolver,
    model: str,
    temperature: float = 0.4,
) -> tuple[Optional[str], Optional[PipelineError]]:
    """Call Google Gemini API with key rotation. Returns (response_text, None) or (None, error)."""
    import google.generativeai as genai

    last_error = ""
    while revolver.available_count > 0:
        key = revolver.current_key
        try:
            genai.configure(api_key=key)
            generation_config = genai.GenerationConfig(
                max_output_tokens=MAX_OUTPUT_TOKENS,
                temperature=temperature,
            )
            gen_model = genai.GenerativeModel(
                model_name=model,
                system_instruction=system_prompt,
                generation_config=generation_config,
            )
            response = gen_model.generate_content(user_message)
            return response.text, None

        except Exception as e:
            last_error = str(e)
            if _is_retryable(last_error):
                next_key = revolver.rotate()
                if next_key:
                    time.sleep(1)
                    continue
            break

    return None, PipelineError(
        stage="Gemini LLM",
        message=f"Все Gemini ключи исчерпаны или ошибка: {last_error[:300]}",
        recoverable=True,
        suggest_fallback=False,
    )



# =============================================================================
# PAYLOAD BUILDER & RESPONSE PARSER
# =============================================================================

def build_user_payload(metadata: dict, transcript: str) -> str:
    """Assemble the user message for LLM containing metadata + transcript."""
    tags_str = ", ".join(metadata.get("tags", [])) or "N/A"
    return f"""=== VIDEO METADATA ===
Title:        {metadata['title']}
Channel:      {metadata['channel_name']}
Video ID:     {metadata['video_id']}
Tags:         {tags_str}
Description:  {metadata['description']}

=== FULL TRANSCRIPT WITH TIMECODES ===
{transcript}

=== YOUR TASK ===
Apply your full analysis and production pipeline to this material.
Generate the Script Table and config.json exactly as specified in your system instructions."""


def parse_llm_response(llm_response: str, metadata: dict) -> tuple[Optional[dict], str]:
    """
    Parse LLM response into (config_dict, scenario_markdown).
    Returns (None, scenario) if JSON extraction fails — scenario always returned.
    """
    # Extract JSON block
    json_pattern = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", re.IGNORECASE)
    json_match = json_pattern.search(llm_response)

    config_dict = None
    config_json_str = None

    if json_match:
        config_json_str = json_match.group(1).strip()
    else:
        # Fallback: find the largest {...} block
        brace_matches = re.findall(r"\{[\s\S]*\}", llm_response)
        if brace_matches:
            config_json_str = max(brace_matches, key=len).strip()

    if config_json_str:
        try:
            config_dict = json.loads(config_json_str)
            # Inject metadata if missing
            config_dict.setdefault("source_video_id", metadata["video_id"])
            config_dict.setdefault("source_title", metadata["title"])
            config_dict.setdefault("generated_at", datetime.datetime.utcnow().isoformat() + "Z")
        except json.JSONDecodeError:
            config_dict = None  # Will be shown as raw text in UI

    # Extract scenario (everything except JSON block)
    scenario_text = llm_response
    if json_match:
        scenario_text = llm_response[:json_match.start()] + llm_response[json_match.end():]
    elif config_json_str:
        scenario_text = llm_response.replace(config_json_str, "")

    scenario_text = scenario_text.strip()

    # Prepend header
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"""# Сценарий: {metadata['title']}
**Канал:** {metadata['channel_name']}  
**Video ID:** {metadata['video_id']}  
**Сгенерировано:** {now}  

---

"""
    return config_dict, header + scenario_text



# =============================================================================
# FILE I/O (AIR GAP: only JSON + MD, nothing else)
# =============================================================================

def save_outputs(config_dict: Optional[dict], scenario_md: str, video_id: str) -> tuple[Path, Path]:
    """Save config.json and scenario.md to output directory. Returns (config_path, scenario_path)."""
    OUTPUT_DIR.mkdir(exist_ok=True)

    # Use video_id as subfolder for organization
    run_dir = OUTPUT_DIR / video_id
    run_dir.mkdir(exist_ok=True)

    config_path = run_dir / "config.json"
    scenario_path = run_dir / "scenario.md"

    if config_dict:
        config_path.write_text(json.dumps(config_dict, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        config_path.write_text("{}", encoding="utf-8")

    scenario_path.write_text(scenario_md, encoding="utf-8")

    return config_path, scenario_path


def load_system_prompt() -> Optional[str]:
    """Load SYSTEM_PROMPT.md from script directory."""
    script_dir = Path(__file__).parent
    prompt_path = script_dir / SYSTEM_PROMPT_FILE
    if not prompt_path.exists():
        return None
    return prompt_path.read_text(encoding="utf-8")



# =============================================================================
# STREAMLIT UI — PAGE CONFIG & STYLES
# =============================================================================

def configure_page():
    """Set Streamlit page config and inject custom CSS."""
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon=APP_ICON,
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown("""
    <style>
        /* Dark premium theme overrides */
        .stApp { background-color: #0e1117; }
        .block-container { padding-top: 2rem; max-width: 1400px; }

        /* Status cards */
        .status-card {
            background: linear-gradient(135deg, #1a1f2e 0%, #141824 100%);
            border: 1px solid #2d3748;
            border-radius: 12px;
            padding: 1.2rem;
            margin-bottom: 0.8rem;
        }
        .status-card h4 { color: #e2e8f0; margin: 0 0 0.5rem 0; font-size: 0.9rem; }
        .status-card .metric { color: #63b3ed; font-size: 1.8rem; font-weight: 700; }

        /* Pipeline stages */
        .stage-active { border-left: 4px solid #48bb78; }
        .stage-error { border-left: 4px solid #fc8181; }
        .stage-waiting { border-left: 4px solid #4a5568; }

        /* Revolver indicator */
        .revolver-status {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.75rem;
            font-weight: 600;
        }
        .revolver-ok { background: #22543d; color: #9ae6b4; }
        .revolver-warn { background: #744210; color: #fbd38d; }
        .revolver-dead { background: #742a2a; color: #feb2b2; }

        /* Hide Streamlit branding */
        #MainMenu { visibility: hidden; }
        footer { visibility: hidden; }
        header { visibility: hidden; }
    </style>
    """, unsafe_allow_html=True)



# =============================================================================
# STREAMLIT UI — SIDEBAR (Settings & Keys)
# =============================================================================

def render_sidebar() -> dict:
    """Render sidebar with all settings. Returns config dict."""
    with st.sidebar:
        st.caption("Режиссёр-Аналитик v2.0")

        # --- LLM Provider ---
        st.markdown("### ⚡ LLM Provider")
        provider = st.radio(
            "Провайдер",
            ["groq", "gemini"],
            horizontal=True,
            help="Groq — быстрый (до ~30K токенов). Gemini — огромный контекст (до 1M).",
        )

        if provider == "groq":
            model = st.selectbox("Модель Groq", GROQ_MODELS)
        else:
            model = st.selectbox("Модель Gemini", GEMINI_MODELS)

        temperature = st.slider(
            "Temperature",
            min_value=0.0,
            max_value=1.0,
            value=0.4,
            step=0.05,
            help="0 = детерминизм, 1 = максимальная креативность",
        )

        st.markdown("---")

        # --- API Keys ---
        yt_keys_raw = st.text_area(
            "YouTube Data API v3",
            height=80,
            placeholder="AIza...\nAIza...",
            key="yt_keys",
        )

        groq_keys_raw = st.text_area(
            "Groq API Keys",
            height=80,
            placeholder="gsk_...\ngsk_...",
            key="groq_keys",
        )

        gemini_keys_raw = st.text_area(
            "Gemini API Keys",
            height=80,
            placeholder="AIza...\nAIza...",
            key="gemini_keys",
        )

        # Parse key arrays
        yt_keys = [k for k in yt_keys_raw.strip().split("\n") if k.strip()]
        groq_keys = [k for k in groq_keys_raw.strip().split("\n") if k.strip()]
        gemini_keys = [k for k in gemini_keys_raw.strip().split("\n") if k.strip()]

        # Display revolver status
        st.markdown("---")
        st.markdown("### 📊 Revolver Status")

        def _revolver_badge(count: int, label: str):
            if count == 0:
                cls = "revolver-dead"
                icon = "⛔"
            else:
                cls = "revolver-ok"
                icon = "✅"
            st.markdown(
                f'{icon} **{label}**: <span class="revolver-status {cls}">{count} keys</span>',
                unsafe_allow_html=True,
            )

        _revolver_badge(len(yt_keys), "YouTube")
        _revolver_badge(len(groq_keys), "Groq")
        _revolver_badge(len(gemini_keys), "Gemini")

        st.markdown("---")
        st.markdown("### ⚙️ Advanced")
        max_tokens = st.number_input(
            "Max output tokens",
            min_value=2048,
            max_value=32768,
            value=MAX_OUTPUT_TOKENS,
            step=1024,
        )

        return {
            "provider": provider,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "yt_keys": yt_keys,
            "groq_keys": groq_keys,
            "gemini_keys": gemini_keys,
        }



# =============================================================================
# STREAMLIT UI — MAIN PANEL
# =============================================================================

def render_error_alert(error: PipelineError):
    """Render an interactive error alert with recovery options."""
    icon = "🔴" if not error.recoverable else "🟡"
    st.error(f"{icon} **[{error.stage}]** {error.message}")

    if error.suggest_fallback:
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🔄 Повторить этап", key=f"retry_{error.stage}"):
                st.session_state["retry_stage"] = error.stage
                st.rerun()
        with col2:
            if st.button("🧠 Fallback → Gemini", key=f"fallback_{error.stage}"):
                st.session_state["force_fallback"] = True
                st.rerun()
    elif error.recoverable:
        if st.button("🔄 Повторить", key=f"retry_{error.stage}"):
            st.session_state["retry_stage"] = error.stage
            st.rerun()


def render_main_panel(config: dict):
    """Render the main content area."""

    # Header
    st.markdown("# 🎬 Content Arbitrage Pipeline")
    st.markdown("*Деконструкция → Ремонтаж нарратива → Генерация config.json*")
    st.markdown("---")

    # --- Input Section ---
    col_input, col_status = st.columns([3, 1])

    with col_input:
        video_url = st.text_input(
            "🔗 YouTube URL",
            placeholder="https://www.youtube.com/watch?v=...",
            help="Поддерживаются: youtube.com/watch, youtu.be, youtube.com/shorts",
        )

    with col_status:
        st.markdown('<div class="status-card">', unsafe_allow_html=True)
        st.markdown("<h4>Pipeline Status</h4>", unsafe_allow_html=True)
        status = st.session_state.get("pipeline_status", "⏸️ Ожидание")
        st.markdown(f'<div class="metric">{status}</div>', unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)

    # --- Launch Button ---
    launch_disabled = not video_url or not config["yt_keys"]
    if not config["yt_keys"]:
        st.warning("⚠️ Добавьте хотя бы один YouTube API ключ в боковой панели.")

    provider = config["provider"]
    if provider == "groq" and not config["groq_keys"]:
        st.warning("⚠️ Добавьте хотя бы один Groq API ключ.")
        launch_disabled = True
    elif provider == "gemini" and not config["gemini_keys"]:
        st.warning("⚠️ Добавьте хотя бы один Gemini API ключ.")
        launch_disabled = True

    # Check for forced fallback
    force_fallback = st.session_state.get("force_fallback", False)
    if force_fallback:
        provider = "gemini"
        st.info("🧠 Активирован Fallback → Gemini")

    run_pipeline = st.button(
        "🚀 Запустить Pipeline",
        disabled=launch_disabled,
        use_container_width=True,
        type="primary",
    )

    # --- Pipeline Execution ---
    if run_pipeline and video_url:
        _execute_pipeline(video_url, config, provider)

    # --- Results Display ---
    _render_results()



def _execute_pipeline(video_url: str, config: dict, provider: str):
    """Execute the full pipeline with progress indicators."""
    global MAX_OUTPUT_TOKENS
    MAX_OUTPUT_TOKENS = config["max_tokens"]

    # Reset fallback flag
    st.session_state["force_fallback"] = False

    progress = st.progress(0, text="Инициализация...")
    status_container = st.container()

    # ─── Stage 1: Extract Video ID ───
    progress.progress(5, text="📎 Извлечение Video ID...")
    video_id = extract_video_id(video_url)
    if not video_id:
        with status_container:
            render_error_alert(PipelineError(
                stage="URL Parsing",
                message="Не удалось извлечь Video ID. Проверьте формат ссылки.",
                recoverable=False,
            ))
        return

    st.session_state["pipeline_status"] = "▶️ Работаю"

    # ─── Stage 2: Fetch Metadata ───
    progress.progress(15, text="📡 Получение метаданных...")
    yt_revolver = KeyRevolver(config["yt_keys"], "YouTube")
    metadata, error = fetch_metadata(video_id, yt_revolver)
    if error:
        with status_container:
            render_error_alert(error)
        st.session_state["pipeline_status"] = "❌ Ошибка"
        return

    with status_container:
        st.success(f"✅ **{metadata['title']}** | {metadata['channel_name']}")

    # ─── Stage 3: Fetch Transcript ───
    progress.progress(30, text="📄 Извлечение транскрипта...")
    transcript, error = fetch_transcript(video_id)
    if error:
        with status_container:
            render_error_alert(error)
        st.session_state["pipeline_status"] = "❌ Ошибка"
        return

    word_count = len(transcript.split())
    line_count = transcript.count("\n") + 1
    with status_container:
        st.info(f"📄 Транскрипт: **{line_count}** реплик, **~{word_count:,}** слов")

    # ─── Stage 4: Load System Prompt ───
    progress.progress(40, text="📋 Загрузка промпта...")
    system_prompt = load_system_prompt()
    if not system_prompt:
        with status_container:
            render_error_alert(PipelineError(
                stage="System Prompt",
                message=f"Файл {SYSTEM_PROMPT_FILE} не найден рядом с dashboard.py.",
                recoverable=False,
            ))
        st.session_state["pipeline_status"] = "❌ Ошибка"
        return

    # ─── Stage 5: Build Payload & Estimate ───
    progress.progress(50, text="🧮 Формирование запроса...")
    user_payload = build_user_payload(metadata, transcript)
    estimated_tokens = (len(system_prompt) + len(user_payload)) // 4
    with status_container:
        st.info(f"📊 Размер запроса: **~{estimated_tokens:,}** токенов → **{provider.upper()}**")

    # ─── Stage 6: Call LLM ───
    progress.progress(60, text=f"🤖 Запрос к {provider.upper()}...")

    if provider == "groq":
        revolver = KeyRevolver(config["groq_keys"], "Groq")
        llm_response, error = call_groq(
            system_prompt, user_payload, revolver,
            model=config["model"],
            temperature=config["temperature"],
        )
    else:
        revolver = KeyRevolver(config["gemini_keys"], "Gemini")
        llm_response, error = call_gemini(
            system_prompt, user_payload, revolver,
            model=config["model"],
            temperature=config["temperature"],
        )

    if error:
        with status_container:
            render_error_alert(error)
        st.session_state["pipeline_status"] = "❌ Ошибка"
        return

    with status_container:
        st.success(f"✅ LLM ответил: **{len(llm_response):,}** символов")

    # ─── Stage 7: Parse & Save ───
    progress.progress(85, text="💾 Парсинг и сохранение...")
    config_dict, scenario_md = parse_llm_response(llm_response, metadata)
    config_path, scenario_path = save_outputs(config_dict, scenario_md, video_id)

    # Store results in session state
    st.session_state["result_config"] = config_dict
    st.session_state["result_config_raw"] = json.dumps(config_dict, ensure_ascii=False, indent=2) if config_dict else "{}"
    st.session_state["result_scenario"] = scenario_md
    st.session_state["result_config_path"] = str(config_path)
    st.session_state["result_scenario_path"] = str(scenario_path)
    st.session_state["result_metadata"] = metadata

    # ─── Done ───
    progress.progress(100, text="✅ Pipeline завершён!")
    st.session_state["pipeline_status"] = "✅ Готово"

    segments_count = len(config_dict.get("segments", [])) if config_dict else 0
    with status_container:
        st.success(f"🎉 **Готово!** Сегментов: {segments_count} | Файлы: `{config_path}`, `{scenario_path}`")



def _render_results():
    """Render results section with editable fields."""
    if "result_config" not in st.session_state:
        return

    st.markdown("---")
    st.markdown("## 📦 Результаты")

    metadata = st.session_state.get("result_metadata", {})
    if metadata:
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("🎬 Видео", metadata.get("title", "N/A")[:40])
        with col2:
            config_dict = st.session_state.get("result_config")
            seg_count = len(config_dict.get("segments", [])) if config_dict else 0
            st.metric("🧩 Сегменты", seg_count)
        with col3:
            st.metric("📂 Канал", metadata.get("channel_name", "N/A"))

    # Tabs for results
    tab_scenario, tab_config, tab_raw = st.tabs(["📋 Scenario", "⚙️ Config JSON", "🔍 Raw LLM"])

    with tab_scenario:
        scenario = st.session_state.get("result_scenario", "")
        edited_scenario = st.text_area(
            "Сценарий (редактируемый)",
            value=scenario,
            height=500,
            key="editor_scenario",
        )
        col_s1, col_s2 = st.columns(2)
        with col_s1:
            if st.button("💾 Сохранить сценарий", key="save_scenario"):
                path = Path(st.session_state.get("result_scenario_path", "output/scenario.md"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(edited_scenario, encoding="utf-8")
                st.success(f"Сохранено → `{path}`")
        with col_s2:
            st.download_button(
                "⬇️ Скачать scenario.md",
                data=edited_scenario,
                file_name="scenario.md",
                mime="text/markdown",
            )

    with tab_config:
        config_raw = st.session_state.get("result_config_raw", "{}")
        edited_config = st.text_area(
            "config.json (редактируемый)",
            value=config_raw,
            height=500,
            key="editor_config",
        )
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            if st.button("💾 Сохранить config", key="save_config"):
                path = Path(st.session_state.get("result_config_path", "output/config.json"))
                path.parent.mkdir(parents=True, exist_ok=True)
                # Validate before saving
                try:
                    validated = json.loads(edited_config)
                    path.write_text(json.dumps(validated, ensure_ascii=False, indent=2), encoding="utf-8")
                    st.success(f"✅ Валидный JSON сохранён → `{path}`")
                except json.JSONDecodeError as e:
                    st.error(f"❌ Невалидный JSON: {e}")
        with col_c2:
            st.download_button(
                "⬇️ Скачать config.json",
                data=edited_config,
                file_name="config.json",
                mime="application/json",
            )

    with tab_raw:
        st.caption("Полный ответ LLM (debug)")
        raw = st.session_state.get("result_scenario", "") + "\n\n---\n\n" + st.session_state.get("result_config_raw", "")
        st.code(raw[:10000], language="markdown")



# =============================================================================
# ENTRYPOINT
# =============================================================================

def main():
    """Application entrypoint."""
    configure_page()

    # Initialize session state defaults
    if "pipeline_status" not in st.session_state:
        st.session_state["pipeline_status"] = "⏸️ Ожидание"
    if "force_fallback" not in st.session_state:
        st.session_state["force_fallback"] = False

    # Render UI
    config = render_sidebar()
    render_main_panel(config)


if __name__ == "__main__":
    main()
