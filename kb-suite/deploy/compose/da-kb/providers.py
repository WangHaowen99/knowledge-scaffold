"""Print the SQL that points the DA knowledge pipeline at the kb-suite model endpoint.

DA reads its models from settings(key='providers') and only at start-up, so
run this once the schema exists and restart the container afterwards:

    python3 providers.py | docker compose exec -T da-kb-postgres psql -U deepanalyze -d deepanalyze

Reads KBS_LLM_BASE_URL, KBS_LLM_API_KEY and optionally DA_KB_CHAT_MODEL,
DA_KB_EMBED_MODEL from the environment. The SQL goes to stdout only (the key
never appears on a command line). DA's vector column is fixed at 1024 dimensions.
"""

from __future__ import annotations

import json
import os
import sys


def main() -> None:
    base = os.environ.get("KBS_LLM_BASE_URL", "").rstrip("/")
    key = os.environ.get("KBS_LLM_API_KEY", "")
    if not base or not key:
        sys.exit("KBS_LLM_BASE_URL and KBS_LLM_API_KEY must be set")
    chat = os.environ.get("DA_KB_CHAT_MODEL", "qwen-plus")
    embed = os.environ.get("DA_KB_EMBED_MODEL", "text-embedding-v4")
    value = {
        "providers": [
            {"id": "main-llm", "name": "main-llm", "type": "openai-compatible", "endpoint": base, "apiKey": key,
             "model": chat, "maxTokens": 0, "supportsToolUse": True, "enabled": True},
            {"id": "remote-embedding", "name": "remote-embedding", "type": "openai-compatible", "endpoint": base,
             "apiKey": key, "model": embed, "supportsToolUse": False, "enabled": True, "dimension": 1024},
        ],
        "defaults": {"main": "main-llm", "summarizer": "main-llm", "embedding": "remote-embedding", "vlm": "",
                     "tts": "", "image_gen": "", "video_gen": "", "music_gen": "", "audio_transcribe": "",
                     "video_understand": ""},
    }
    literal = json.dumps(value, ensure_ascii=False).replace("'", "''")
    sys.stdout.write(
        "INSERT INTO settings (key, value) VALUES ('providers', '" + literal + "'::jsonb)\n"
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;\n"
    )


if __name__ == "__main__":
    main()
