import logging
import re
import unicodedata

from langdetect import DetectorFactory, detect, detect_langs, LangDetectException

from llm import LLMPurpose, get_llm_router

DetectorFactory.seed = 0

logger = logging.getLogger(__name__)

_THINKING_PHRASES: dict[str, str] = {
    "zh": "让我来思考思考，emm...",
    "zh-cn": "让我来思考思考，emm...",
    "zh-tw": "讓我來思考思考，emm...",
    "en": "Let me think about it, hmm...",
    "ja": "考えさせてください、うーん...",
    "ko": "생각해 볼게요, 음...",
    "ru": "Дай-ка подумать, эм...",
    "de": "Lass mich nachdenken, hmm...",
    "fr": "Laissez-moi réfléchir, euh...",
    "es": "Déjame pensarlo, hmm...",
    "pt": "Deixe-me pensar, hmm...",
    "ar": "دعني أفكر، همم...",
    "it": "Lasciami pensare, hmm...",
    "nl": "Laat me nadenken, hmm...",
    "pl": "Pozwól, że pomyślę, hmm...",
    "tr": "Düşünmemi sağlayın, hmm...",
    "vi": "Để tôi suy nghĩ đã, hmm...",
    "th": "ให้ฉันคิดดูสักครู่, อืม...",
    "hi": "मुझे सोचने दीजिए, हम्म...",
    "id": "Biar saya berpikir, hmm...",
    "uk": "Дай-ка подумати, ем...",
    "cs": "Dovolte mi přemýšlet, hmm...",
    "sv": "Låt mig tänka, hmm...",
    "da": "Lad mig tænke, hmm...",
    "fi": "Anna minun miettiä, hmm...",
    "hu": "Hadd gondolkodjam, hmm...",
    "ro": "Lasă-mă să mă gândesc, hmm...",
    "bg": "Дай ми да помисля, хм...",
    "hr": "Pustite me da razmislim, hmm...",
    "sk": "Dovoľte mi premýšľať, hmm...",
    "sl": "Pustite me razmisliti, hmm...",
    "et": "Las ma mõtlen, hmm...",
    "lv": "Ļaujiet man padomāt, hmm...",
    "lt": "Leiskite man pagalvoti, hmm...",
    "el": "Αφήστε με να σκεφτώ, χμμ...",
    "he": "תן לי לחשוב, חממ...",
    "ms": "Biar saya berfikir, hmm...",
    "tl": "Hayaan ninyong isipin ko, hmm...",
    "sw": "Niruhusu nifikirie, hmm...",
    "bn": "আমাকে ভাবতে দিন, হম্ম...",
    "ta": "என்னை யோசிக்க விடுங்கள், ஹ்ம்ம்...",
    "ur": "مجھے سوچنے دیں، ہمم...",
    "fa": "بگذار فکر کنم، همم...",
    "ca": "Deixa'm pensar, hmm...",
    "sr": "Оставите ме да размислим, хмм...",
    "no": "La meg tenke, hmm...",
    "af": "Laat my dink, hmm...",
}

_TOOL_CALL_PHRASES: dict[str, str] = {
    "zh": "正在调用工具",
    "zh-cn": "正在调用工具",
    "zh-tw": "正在呼叫工具",
    "en": "Calling tools",
    "ja": "ツールを呼び出し中",
    "ko": "도구 호출 중",
    "ru": "Вызов инструментов",
    "de": "Rufe Tools auf",
    "fr": "Appel des outils",
    "es": "Llamando herramientas",
    "pt": "Chamando ferramentas",
    "ar": "استدعاء الأدوات",
    "it": "Chiamata strumenti",
    "nl": "Tools aanroepen",
    "pl": "Wywoływanie narzędzi",
    "tr": "Araçlar çağrılıyor",
    "vi": "Đang gọi công cụ",
    "th": "กำลังเรียกใช้เครื่องมือ",
    "hi": "उपकरण कॉल किया जा रहा है",
    "id": "Memanggil alat",
    "uk": "Виклик інструментів",
    "cs": "Volání nástrojů",
    "sv": "Anropar verktyg",
    "da": "Kalder værktøjer",
    "fi": "Kutsutaan työkaluja",
    "hu": "Eszközök hívása",
    "ro": "Apelare instrumente",
    "bg": "Извикване на инструменти",
    "hr": "Pozivanje alata",
    "sk": "Volanie nástrojov",
    "sl": "Klicanje orodij",
    "et": "Tööriistade kutsumine",
    "lv": "Rīku izsaukšana",
    "lt": "Įrankių iškvietimas",
    "el": "Κλήση εργαλείων",
    "he": "קריאת כלים",
    "ms": "Memanggil alat",
    "tl": "Tumatawag ng mga tool",
    "sw": "Kuita zana",
    "bn": "টুল কল করা হচ্ছে",
    "ta": "கருவிகளை அழைக்கிறது",
    "ur": "ٹولز کال کیے جا رہے ہیں",
    "fa": "فراخوانی ابزارها",
    "ca": "Cridant eines",
    "sr": "Позивање алата",
    "no": "Kaller verktøy",
    "af": "Roep gereedskap",
}

_FALLBACK_LANG = "zh"

_LANG_DISPLAY_NAMES: dict[str, str] = {
    "zh": "汉语",
    "zh-cn": "汉语",
    "zh-tw": "繁体中文",
    "en": "English",
    "ja": "日本語",
    "ko": "한국어",
    "ru": "Русский",
    "de": "Deutsch",
    "fr": "Français",
    "es": "Español",
    "pt": "Português",
    "ar": "العربية",
    "it": "Italiano",
    "nl": "Nederlands",
    "pl": "Polski",
    "tr": "Türkçe",
    "vi": "Tiếng Việt",
    "th": "ไทย",
    "hi": "हिन्दी",
    "id": "Bahasa Indonesia",
    "uk": "Українська",
    "cs": "Čeština",
    "et": "Eesti",
    "lv": "Latviešu",
    "lt": "Lietuvių",
    "sv": "Svenska",
    "da": "Dansk",
    "fi": "Suomi",
    "hu": "Magyar",
    "ro": "Română",
    "bg": "Български",
    "hr": "Hrvatski",
    "sk": "Slovenčina",
    "sl": "Slovenščina",
    "el": "Ελληνικά",
    "he": "עברית",
    "ms": "Bahasa Melayu",
    "tl": "Filipino",
    "sw": "Kiswahili",
    "bn": "বাংলা",
    "ta": "தமிழ்",
    "ur": "اردو",
    "fa": "فارسی",
    "ca": "Català",
    "sr": "Српски",
    "no": "Norsk",
    "af": "Afrikaans",
}

SUPPORTED_LANGUAGES: list[str] = [
    "zh", "en", "ja", "ko", "ru", "de", "fr", "es", "pt", "ar",
    "it", "nl", "pl", "tr", "vi", "th", "hi", "id", "uk", "cs",
]


def _script_check(text: str) -> dict:
    has_hiragana = False
    has_katakana = False
    has_hangul = False
    has_cjk = False
    has_cyrillic = False
    has_arabic = False
    has_devanagari = False
    has_thai = False
    has_bengali = False
    has_tamil = False

    for ch in text:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x309F:
            has_hiragana = True
        elif 0x30A0 <= cp <= 0x30FF:
            has_katakana = True
        elif 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:
            has_hangul = True
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF:
            has_cjk = True
        elif 0x0400 <= cp <= 0x052F:
            has_cyrillic = True
        elif any((
            0x0600 <= cp <= 0x06FF,
            0x0750 <= cp <= 0x077F,
            0xFB50 <= cp <= 0xFDFF,
            0xFE70 <= cp <= 0xFEFF,
        )):
            has_arabic = True
        elif 0x0900 <= cp <= 0x097F:
            has_devanagari = True
        elif 0x0E00 <= cp <= 0x0E7F:
            has_thai = True
        elif 0x0980 <= cp <= 0x09FF:
            has_bengali = True
        elif 0x0B80 <= cp <= 0x0BFF:
            has_tamil = True

        all_script_flags_found = all(
            (
                has_hiragana,
                has_katakana,
                has_hangul,
                has_cjk,
                has_cyrillic,
                has_arabic,
                has_devanagari,
                has_thai,
                has_bengali,
                has_tamil,
            )
        )
        if all_script_flags_found:
            break

    return {
        "hiragana": has_hiragana,
        "katakana": has_katakana,
        "hangul": has_hangul,
        "cjk": has_cjk,
        "cyrillic": has_cyrillic,
        "arabic": has_arabic,
        "devanagari": has_devanagari,
        "thai": has_thai,
        "bengali": has_bengali,
        "tamil": has_tamil,
    }


def _is_pure_english(text: str) -> bool:
    for ch in text:
        if ch.isalpha() and ord(ch) > 0x007A:
            return False
    return True


_SCRIPT_LANG_MAP = {
    "cyrillic": "ru",
    "arabic": "ar",
    "devanagari": "hi",
    "thai": "th",
    "bengali": "bn",
    "tamil": "ta",
}


def _get_script_hint(script: dict) -> str | None:
    for script_name, lang_code in _SCRIPT_LANG_MAP.items():
        if script.get(script_name):
            return lang_code
    return None


def _detect_ascii_latin_language(text: str) -> str:
    words = re.findall(r"[A-Za-z]+", text)
    alpha_chars = sum(len(word) for word in words)
    if len(words) < 3 or alpha_chars < 12:
        return "en"

    try:
        candidates = detect_langs(text)
    except LangDetectException:
        return "en"

    if not candidates:
        return "en"
    top = candidates[0]
    detected = validate_lang(top.lang)
    if detected and top.prob >= 0.85:
        return detected
    return "en"


def _detect_language_via_llm(text: str, script_hint: str | None = None) -> str | None:
    try:
        hint_part = ""
        if script_hint:
            hint_part = (
                "The text contains mixed languages (e.g., English technical terms and a non-English language). "
                "Ignore English words and identify the NON-ENGLISH language. "
            )
        response = get_llm_router().call(
            purpose=LLMPurpose.TASK,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"{hint_part}"
                        "Identify the language of the following text. "
                        "Reply with ONLY the ISO 639-1 language code (e.g., en, zh, ja, ko, fr, de, es, ru, ar). "
                        "Do not output anything else.\n\n"
                        f"{text[:500]}"
                    ),
                }
            ],
            tools=[],
            stream=False,
            temperature=0,
            max_tokens=10,
            enable_reasoning=False,
            run_id=None,
            request_label="language_detect",
        )
        result = str(response.content or "").strip().lower()
        if result and len(result) <= 5 and result.replace("-", "").isalpha():
            return result
    except Exception as e:
        logger.debug(f"[lang] LLM detection failed: {e}")
    return None


def detect_language(text: str, lang: str | None = None, default_lang: str | None = None) -> str:
    validated = validate_lang(lang)
    if validated:
        return validated
    default_validated = validate_lang(default_lang) or _FALLBACK_LANG

    if not text or not text.strip():
        return default_validated

    script = _script_check(text)

    if script["hiragana"] or script["katakana"]:
        return "ja"
    if script["hangul"]:
        return "ko"
    if script["cjk"]:
        return "zh-cn"

    script_hint = _get_script_hint(script)
    if script_hint:
        return script_hint

    if _is_pure_english(text):
        return _detect_ascii_latin_language(text)

    llm_result = _detect_language_via_llm(text, script_hint="non_english_expected")
    validated_llm_result = validate_lang(llm_result)
    if validated_llm_result:
        return validated_llm_result

    try:
        lang = detect(text)
    except LangDetectException:
        logger.debug(f"[langdetect] Failed to detect language for text: {text[:100]}")
        return default_validated

    if lang == "ko" and not script["hangul"]:
        return default_validated
    if lang in ("zh-cn", "zh-tw") and script["hangul"]:
        return "ko"

    return validate_lang(lang) or default_validated


def _resolve_phrase(lang: str, table: dict[str, str]) -> str:
    if lang in table:
        return table[lang]

    base = lang.split("-")[0]
    if base in table:
        return table[base]

    return table[_FALLBACK_LANG]


def validate_lang(lang: str | None) -> str | None:
    if not lang or not lang.strip():
        return None
    lang = lang.strip().lower()
    if lang in _LANG_DISPLAY_NAMES:
        return lang
    base = lang.split("-")[0]
    if base in _LANG_DISPLAY_NAMES:
        return base
    return None


def get_thinking_phrase(lang: str) -> str:
    return _resolve_phrase(lang, _THINKING_PHRASES)


def get_tool_call_phrase(lang: str) -> str:
    return _resolve_phrase(lang, _TOOL_CALL_PHRASES)


def get_lang_display_name(lang: str) -> str:
    if lang in _LANG_DISPLAY_NAMES:
        return _LANG_DISPLAY_NAMES[lang]
    base = lang.split("-")[0]
    if base in _LANG_DISPLAY_NAMES:
        return _LANG_DISPLAY_NAMES[base]
    return lang


def translate_text(text: str, target_lang: str, source_lang: str = "auto") -> str:
    if not text or not text.strip():
        return text

    if target_lang in ("zh", "zh-cn") and source_lang in ("zh", "zh-cn", "auto"):
        return text

    if source_lang != "auto" and source_lang == target_lang:
        return text

    try:
        lang_name = get_lang_display_name(target_lang)
        prompt = (
            f"Translate the following text to {lang_name} ({target_lang}). "
            f"The text may contain English technical terms, codes, or identifiers mixed with non-English text. "
            f"Only translate the non-English parts; keep English terms, codes, and identifiers unchanged. "
            f"Only output the translated text, nothing else.\n\n{text}"
        )
        response = get_llm_router().call(
            purpose=LLMPurpose.TASK,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            stream=False,
            temperature=0,
            enable_reasoning=False,
            run_id=None,
            request_label="language_translate",
        )
        result = str(response.content or "").strip()
        if result:
            return result
    except Exception as e:
        logger.warning(f"[lang] Translation failed for {target_lang}: {e}")

    return text
