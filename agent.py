#!/usr/bin/env python3
"""
=============================================================================
  ИИ-Агент «Режиссёр-Аналитик» — Скрипт-Интегратор v1.0
=============================================================================
  Назначение:
    Принимает ссылку на YouTube-видео конкурента, извлекает метаданные
    и транскрипт, отправляет всё это в LLM (Groq или Gemini) вместе
    с системным промптом, а затем сохраняет результат в два файла:
      - config.json   (конфигурация для скрипта-монтажёра)
      - scenario.md   (сценарий с таблицей и переводом)

  Требования (установить перед запуском):
    pip install google-api-python-client youtube-transcript-api groq google-generativeai

  Запуск:
    python agent.py
=============================================================================
"""

import os
import re
import sys
import json
import datetime

# =============================================================================
# БЛОК 1: API-КЛЮЧИ И НАСТРОЙКИ
# =============================================================================
# Заполните ключи здесь — или оставьте пустыми строками.
# Если ключ пустой, скрипт запросит его через консоль при запуске.

YOUTUBE_API_KEY = ""   # Ключ от Google Cloud Console (YouTube Data API v3)
GROQ_API_KEY    = ""   # Ключ от console.groq.com
GEMINI_API_KEY  = ""   # Ключ от aistudio.google.com

# -----------------------------------------------------------------------------
# ПЕРЕКЛЮЧАТЕЛЬ LLM:
#   "groq"   — быстрее, лучше для коротких/средних транскриптов (до ~30 000 токенов)
#   "gemini" — медленнее, но поддерживает огромные контекстные окна (до 1M токенов)
# -----------------------------------------------------------------------------
LLM_PROVIDER = "groq"

# Модели (можно изменить при необходимости)
GROQ_MODEL   = "llama-3.3-70b-versatile"   # или "mixtral-8x7b-32768"
GEMINI_MODEL = "gemini-1.5-pro-latest"      # или "gemini-1.5-flash"

# Максимум токенов в ответе LLM
MAX_OUTPUT_TOKENS = 8192

# Путь к файлу с системным промптом (должен лежать рядом со скриптом)
SYSTEM_PROMPT_FILE = "SYSTEM_PROMPT.md"

# Выходные файлы
OUTPUT_JSON     = "config.json"
OUTPUT_SCENARIO = "scenario.md"


# =============================================================================
# БЛОК 2: ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ — ВВОД И ВАЛИДАЦИЯ
# =============================================================================

def prompt_for_key(env_name: str, description: str) -> str:
    """Запрашивает API-ключ у пользователя через консоль."""
    print(f"\n⚠️  Ключ {description} не найден.")
    key = input(f"   Введите {env_name}: ").strip()
    if not key:
        print(f"❌ Ключ не введён. Работа невозможна без {description}.")
        sys.exit(1)
    return key


def ensure_keys() -> None:
    """Проверяет наличие всех нужных ключей; запрашивает пустые."""
    global YOUTUBE_API_KEY, GROQ_API_KEY, GEMINI_API_KEY

    if not YOUTUBE_API_KEY:
        YOUTUBE_API_KEY = prompt_for_key("YOUTUBE_API_KEY", "YouTube Data API v3")

    if LLM_PROVIDER == "groq" and not GROQ_API_KEY:
        GROQ_API_KEY = prompt_for_key("GROQ_API_KEY", "Groq API")

    if LLM_PROVIDER == "gemini" and not GEMINI_API_KEY:
        GEMINI_API_KEY = prompt_for_key("GEMINI_API_KEY", "Google Gemini API")


def extract_video_id(url: str) -> str:
    """
    Извлекает video_id из различных форматов YouTube-ссылок:
      https://www.youtube.com/watch?v=VIDEO_ID
      https://youtu.be/VIDEO_ID
      https://youtube.com/shorts/VIDEO_ID
    """
    patterns = [
        r"(?:v=)([a-zA-Z0-9_-]{11})",
        r"youtu\.be/([a-zA-Z0-9_-]{11})",
        r"shorts/([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    print("❌ Не удалось распознать video_id из ссылки.")
    print("   Убедитесь, что ссылка является стандартным YouTube URL.")
    sys.exit(1)


# =============================================================================
# БЛОК 3: СБОР ДАННЫХ С YOUTUBE
# =============================================================================

def fetch_metadata(video_id: str) -> dict:
    """
    Запрашивает метаданные видео через YouTube Data API v3.
    Возвращает словарь с полями: title, description, tags, channel_name.
    """
    try:
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ImportError:
        print("❌ Библиотека google-api-python-client не установлена.")
        print("   Установите: pip install google-api-python-client")
        sys.exit(1)

    print(f"\n📡 Запрос метаданных для видео: {video_id}")

    try:
        youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        request = youtube.videos().list(
            part="snippet",
            id=video_id
        )
        response = request.execute()
    except Exception as e:
        # Перехватываем ошибки авторизации и сети
        err_str = str(e)
        if "keyInvalid" in err_str or "API key not valid" in err_str:
            print("❌ YouTube API: неверный ключ. Проверьте YOUTUBE_API_KEY.")
        elif "quotaExceeded" in err_str:
            print("❌ YouTube API: исчерпана дневная квота запросов.")
        else:
            print(f"❌ Ошибка YouTube API: {e}")
        sys.exit(1)

    items = response.get("items", [])
    if not items:
        print(f"❌ Видео с ID '{video_id}' не найдено или недоступно.")
        sys.exit(1)

    snippet = items[0]["snippet"]
    metadata = {
        "video_id":     video_id,
        "title":        snippet.get("title", "N/A"),
        "channel_name": snippet.get("channelTitle", "N/A"),
        "description":  snippet.get("description", "")[:1500],  # обрезаем длинные описания
        "tags":         snippet.get("tags", []),
    }

    print(f"✅ Метаданные получены: «{metadata['title']}» | {metadata['channel_name']}")
    return metadata


def fetch_transcript(video_id: str) -> str:
    """
    Извлекает транскрипт видео с таймкодами.
    Приоритет: ручные субтитры (en) → автоматические (en) → любые доступные.
    Возвращает строку в формате: [HH:MM:SS] Текст...
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
    except ImportError:
        print("❌ Библиотека youtube-transcript-api не установлена.")
        print("   Установите: pip install youtube-transcript-api")
        sys.exit(1)

    print("\n📄 Извлечение транскрипта...")

    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

        # Пытаемся получить ручные английские субтитры
        transcript_obj = None
        try:
            transcript_obj = transcript_list.find_manually_created_transcript(["en"])
            print("   → Найдены ручные субтитры (en).")
        except NoTranscriptFound:
            pass

        # Если нет — автоматические английские
        if not transcript_obj:
            try:
                transcript_obj = transcript_list.find_generated_transcript(["en"])
                print("   → Найдены автоматические субтитры (en).")
            except NoTranscriptFound:
                pass

        # Если нет английских — берём первые доступные
        if not transcript_obj:
            available = list(transcript_list)
            if available:
                transcript_obj = available[0]
                lang = transcript_obj.language_code
                print(f"   ⚠️  Английские субтитры не найдены. Используем: [{lang}].")
                print("      LLM попытается работать с этим языком.")
            else:
                raise NoTranscriptFound(video_id, [], "")

        raw_entries = transcript_obj.fetch()

    except Exception as e:
        err_str = str(e)
        if "TranscriptsDisabled" in err_str or "disabled" in err_str.lower():
            print("❌ Субтитры для этого видео отключены автором.")
        elif "NoTranscriptFound" in err_str or "Could not retrieve" in err_str:
            print("❌ Субтитры не найдены ни на одном языке для этого видео.")
        else:
            print(f"❌ Ошибка получения транскрипта: {e}")
        sys.exit(1)

    # Форматируем в [HH:MM:SS] Текст
    lines = []
    for entry in raw_entries:
        start_sec = int(entry["start"])
        hours   = start_sec // 3600
        minutes = (start_sec % 3600) // 60
        seconds = start_sec % 60
        timecode = f"[{hours:02d}:{minutes:02d}:{seconds:02d}]"
        text = entry["text"].replace("\n", " ").strip()
        lines.append(f"{timecode} {text}")

    transcript_str = "\n".join(lines)
    word_count = len(transcript_str.split())
    print(f"✅ Транскрипт получен: {len(lines)} реплик, ~{word_count} слов.")
    return transcript_str


# =============================================================================
# БЛОК 4: ЗАГРУЗКА СИСТЕМНОГО ПРОМПТА
# =============================================================================

def load_system_prompt() -> str:
    """Загружает системный промпт из файла SYSTEM_PROMPT.md."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    prompt_path = os.path.join(script_dir, SYSTEM_PROMPT_FILE)

    if not os.path.exists(prompt_path):
        print(f"❌ Файл системного промпта не найден: {prompt_path}")
        print(f"   Убедитесь, что {SYSTEM_PROMPT_FILE} лежит рядом со скриптом.")
        sys.exit(1)

    with open(prompt_path, "r", encoding="utf-8") as f:
        content = f.read()

    print(f"✅ Системный промпт загружен ({len(content)} символов).")
    return content


# =============================================================================
# БЛОК 5: ФОРМИРОВАНИЕ PAYLOAD (пользовательское сообщение для LLM)
# =============================================================================

def build_user_payload(metadata: dict, transcript: str) -> str:
    """
    Собирает единое сообщение пользователя, которое будет отправлено в LLM.
    Содержит метаданные видео и полный транскрипт.
    """
    tags_str = ", ".join(metadata.get("tags", [])) or "N/A"

    payload = f"""
=== VIDEO METADATA ===
Title:        {metadata['title']}
Channel:      {metadata['channel_name']}
Video ID:     {metadata['video_id']}
Tags:         {tags_str}
Description:  {metadata['description']}

=== FULL TRANSCRIPT WITH TIMECODES ===
{transcript}

=== YOUR TASK ===
Apply your full analysis and production pipeline to this material.
Generate the Script Table and config.json exactly as specified in your system instructions.
"""
    return payload.strip()


# =============================================================================
# БЛОК 6: ЗАПРОС К LLM
# =============================================================================

def call_groq(system_prompt: str, user_message: str) -> str:
    """Отправляет запрос в Groq API и возвращает текст ответа."""
    try:
        from groq import Groq
    except ImportError:
        print("❌ Библиотека groq не установлена.")
        print("   Установите: pip install groq")
        sys.exit(1)

    print(f"\n🤖 Отправка запроса в Groq ({GROQ_MODEL})...")
    print("   Это может занять от 30 секунд до нескольких минут...")

    try:
        client = Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_message},
            ],
            max_tokens=MAX_OUTPUT_TOKENS,
            temperature=0.4,  # Умеренная креативность; 0 = детерминизм
        )
        result = response.choices[0].message.content
        print(f"✅ Ответ получен ({len(result)} символов).")
        return result

    except Exception as e:
        err_str = str(e)
        if "401" in err_str or "invalid_api_key" in err_str.lower():
            print("❌ Groq API: неверный ключ. Проверьте GROQ_API_KEY.")
        elif "rate_limit" in err_str.lower():
            print("❌ Groq API: превышен лимит запросов. Подождите и повторите.")
        elif "context_length" in err_str.lower() or "too long" in err_str.lower():
            print("❌ Groq API: транскрипт слишком длинный для этой модели.")
            print("   Попробуйте переключить LLM_PROVIDER на 'gemini'.")
        else:
            print(f"❌ Ошибка Groq API: {e}")
        sys.exit(1)


def call_gemini(system_prompt: str, user_message: str) -> str:
    """Отправляет запрос в Google Gemini API и возвращает текст ответа."""
    try:
        import google.generativeai as genai
    except ImportError:
        print("❌ Библиотека google-generativeai не установлена.")
        print("   Установите: pip install google-generativeai")
        sys.exit(1)

    print(f"\n🤖 Отправка запроса в Gemini ({GEMINI_MODEL})...")
    print("   Это может занять от 1 до нескольких минут...")

    try:
        genai.configure(api_key=GEMINI_API_KEY)

        generation_config = genai.GenerationConfig(
            max_output_tokens=MAX_OUTPUT_TOKENS,
            temperature=0.4,
        )

        model = genai.GenerativeModel(
            model_name=GEMINI_MODEL,
            system_instruction=system_prompt,
            generation_config=generation_config,
        )

        response = model.generate_content(user_message)
        result = response.text
        print(f"✅ Ответ получен ({len(result)} символов).")
        return result

    except Exception as e:
        err_str = str(e)
        if "API_KEY_INVALID" in err_str or "invalid" in err_str.lower():
            print("❌ Gemini API: неверный ключ. Проверьте GEMINI_API_KEY.")
        elif "quota" in err_str.lower():
            print("❌ Gemini API: исчерпана квота. Подождите или проверьте биллинг.")
        elif "RECITATION" in err_str:
            print("❌ Gemini API: запрос заблокирован по политике безопасности (RECITATION).")
        else:
            print(f"❌ Ошибка Gemini API: {e}")
        sys.exit(1)


def call_llm(system_prompt: str, user_message: str) -> str:
    """Диспетчер: выбирает нужный провайдер LLM на основе LLM_PROVIDER."""
    if LLM_PROVIDER == "groq":
        return call_groq(system_prompt, user_message)
    elif LLM_PROVIDER == "gemini":
        return call_gemini(system_prompt, user_message)
    else:
        print(f"❌ Неизвестный LLM_PROVIDER: '{LLM_PROVIDER}'. Используйте 'groq' или 'gemini'.")
        sys.exit(1)


# =============================================================================
# БЛОК 7: ПАРСИНГ И СОХРАНЕНИЕ РЕЗУЛЬТАТА
# =============================================================================

def parse_and_save(llm_response: str, metadata: dict) -> None:
    """
    Разбирает ответ LLM на два компонента:
      1. JSON-блок  → сохраняет в config.json
      2. Остальной текст (таблица) → сохраняет в scenario.md
    """

    # --- Извлекаем JSON-блок ---
    # Ищем содержимое между ```json ... ``` или просто { ... } на верхнем уровне
    json_pattern = re.compile(
        r"```(?:json)?\s*(\{[\s\S]*?\})\s*```",
        re.IGNORECASE
    )
    json_match = json_pattern.search(llm_response)

    config_json_str = None
    if json_match:
        config_json_str = json_match.group(1).strip()
    else:
        # Запасной вариант: ищем самый большой {...} блок в ответе
        brace_pattern = re.compile(r"\{[\s\S]*\}")
        all_matches = brace_pattern.findall(llm_response)
        if all_matches:
            # Берём самый длинный (скорее всего это и есть config.json)
            config_json_str = max(all_matches, key=len).strip()

    # --- Сохраняем config.json ---
    if config_json_str:
        # Валидируем JSON перед сохранением
        try:
            parsed_json = json.loads(config_json_str)

            # Добавляем метаданные источника, если LLM их не включила
            if "source_video_id" not in parsed_json:
                parsed_json["source_video_id"] = metadata["video_id"]
            if "source_title" not in parsed_json:
                parsed_json["source_title"] = metadata["title"]
            if "generated_at" not in parsed_json:
                parsed_json["generated_at"] = datetime.datetime.utcnow().isoformat() + "Z"

            with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
                json.dump(parsed_json, f, ensure_ascii=False, indent=2)

            segments_count = len(parsed_json.get("segments", []))
            print(f"\n✅ config.json сохранён → {OUTPUT_JSON}")
            print(f"   Сегментов в конфиге: {segments_count}")

        except json.JSONDecodeError as e:
            print(f"\n⚠️  JSON найден, но не прошёл валидацию: {e}")
            print("   Сохраняю сырой JSON-блок для ручной проверки...")
            with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
                f.write(config_json_str)
            print(f"   Сырой JSON → {OUTPUT_JSON}")
    else:
        print("\n⚠️  JSON-блок не найден в ответе LLM.")
        print("   Проверьте scenario.md — возможно, LLM нарушила формат вывода.")

    # --- Извлекаем таблицу-сценарий (всё, кроме JSON-блока) ---
    # Удаляем JSON-блок из текста ответа
    scenario_text = llm_response
    if json_match:
        scenario_text = llm_response[:json_match.start()] + llm_response[json_match.end():]
    elif config_json_str:
        scenario_text = llm_response.replace(config_json_str, "")

    scenario_text = scenario_text.strip()

    # Добавляем заголовок с метаданными
    header = f"""# Сценарий: {metadata['title']}
**Канал:** {metadata['channel_name']}  
**Video ID:** {metadata['video_id']}  
**Сгенерировано:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  
**Провайдер LLM:** {LLM_PROVIDER.upper()}  

---

"""
    with open(OUTPUT_SCENARIO, "w", encoding="utf-8") as f:
        f.write(header + scenario_text)

    print(f"✅ Сценарий сохранён → {OUTPUT_SCENARIO}")


# =============================================================================
# БЛОК 8: ГЛАВНАЯ ФУНКЦИЯ
# =============================================================================

def main():
    print("=" * 65)
    print("  ИИ-Агент «Режиссёр-Аналитик» v1.0")
    print("  Content Arbitrage Pipeline для рынка США")
    print("=" * 65)

    # 1. Проверка и запрос API-ключей
    ensure_keys()

    # 2. Ввод URL видео
    print(f"\n🔧 Активный провайдер LLM: {LLM_PROVIDER.upper()}")
    print()
    video_url = input("🎬 Введите ссылку на YouTube-видео: ").strip()
    if not video_url:
        print("❌ Ссылка не введена. Выход.")
        sys.exit(1)

    # 3. Извлечение video_id
    video_id = extract_video_id(video_url)
    print(f"   → Video ID: {video_id}")

    # 4. Получение метаданных
    metadata = fetch_metadata(video_id)

    # 5. Получение транскрипта
    transcript = fetch_transcript(video_id)

    # 6. Загрузка системного промпта
    system_prompt = load_system_prompt()

    # 7. Формирование пользовательского payload
    user_payload = build_user_payload(metadata, transcript)

    total_chars = len(system_prompt) + len(user_payload)
    estimated_tokens = total_chars // 4  # грубая оценка
    print(f"\n📊 Оценка размера запроса: ~{estimated_tokens:,} токенов")
    if estimated_tokens > 25000 and LLM_PROVIDER == "groq":
        print("   ⚠️  Запрос большой для Groq. Если получите ошибку контекста,")
        print("      переключите LLM_PROVIDER = 'gemini' в начале скрипта.")

    # 8. Запрос к LLM
    llm_response = call_llm(system_prompt, user_payload)

    # 9. Парсинг и сохранение результатов
    print("\n💾 Сохранение результатов...")
    parse_and_save(llm_response, metadata)

    # 10. Итог
    print("\n" + "=" * 65)
    print("  ✅ Готово! Результаты:")
    print(f"     📋 Сценарий: {OUTPUT_SCENARIO}")
    print(f"     ⚙️  Конфиг:   {OUTPUT_JSON}")
    print("=" * 65)


if __name__ == "__main__":
    main()
