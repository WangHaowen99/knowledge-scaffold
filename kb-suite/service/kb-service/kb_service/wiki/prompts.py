"""Wiki generation prompts.

Adapted from WeKnora internal/agent/prompts_wiki.go (MIT License, Copyright
(C) 2026 Tencent): candidate extraction with granularity guidance, chunk
citation, deduplication, summary page, page modify (add / retract) and index
introduction. Image rules are dropped because kb-service does not ingest
images. Placeholders are ``{{Name}}``; JSON examples keep literal braces.
"""

from __future__ import annotations


def render(template: str, **values: object) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", str(value))
    return out


GRANULARITY = {
    "focused": """**FOCUSED mode — aggressive pruning.**
Extract ONLY the document's primary subjects: the handful of entities/concepts that this document is fundamentally ABOUT. At most 3-7 items total across entities and concepts combined.
EXCLUDE technology stacks or tools mentioned in passing, generic concepts merely referenced, background places/organizations, and anything with only one sentence of content. If unsure, LEAVE IT OUT.""",
    "standard": """**STANDARD mode — balanced (default).**
Extract the document's main subjects PLUS entities/concepts that are substantively discussed — a dedicated paragraph, multiple bullet points, or at least 2-3 sentences of context. Include named methods or techniques when the document explains HOW they are used.
EXCLUDE items only listed in a comma-separated list, one-off mentions, parenthetical references, generic infrastructure nouns, and items whose whole contribution fits in one short sentence. When in doubt, EXCLUDE.""",
    "exhaustive": """**EXHAUSTIVE mode — maximum recall.**
Extract every named entity and every recognizable concept, including technologies, tools, standards and methodologies mentioned even once by name, provided they are concrete (not generic terms like "database" or "function").
EXCLUDE ONLY truly generic terms and items that appear only inside URLs or reference citations.""",
}


CANDIDATES = """You are a knowledge extraction system. Analyze the following document and list all significant entities AND key concepts as a lightweight candidate set. Another pass will later attach concrete supporting chunks to each item, so you do NOT need to write exhaustive per-item facts here.

<document>
<content>
{{Content}}
</content>
</document>

<previous_slugs>
{{PreviousSlugs}}
</previous_slugs>

<instructions>
Return a JSON object with two arrays: "entities" and "concepts".
**IMPORTANT: Write ALL names, descriptions, and details in {{Language}}**.

If the <content> block is empty or carries no substantive information, return {"entities": [], "concepts": []}. Do NOT invent entities or concepts.

### Extraction Scope (Granularity: {{Granularity}})
{{GranularityGuidance}}
{{ExtraInstructions}}
### Slug Continuity Rules
If previous slugs are provided above:
- If an item from the previous extraction still exists in the current document, reuse its exact slug.
- If it no longer appears in the document, do NOT include it.
- Only generate new slugs for genuinely new items.

### Entities (people, organizations, products, equipment, places, technologies, events, etc.)
Each entity has:
- "name": human-readable name in {{Language}}.
- "slug": "entity/<lowercase-hyphenated-name>" (use pinyin/romanized form for non-Latin names). Reuse the previous slug if extracted before.
- "aliases": names that refer to THE EXACT SAME entity only (abbreviations, full/short names, translations). [] if none.
- "description": one sentence (at most 40 words) stating what THIS DOCUMENT says the entity is and its role in the document; self-contained.
- "details": a short 1-3 sentence fallback summary of what the document says about it, under 300 characters.

### Concepts (topics, methods, procedures, theories, standards, etc.)
Same fields, with "slug": "concept/<lowercase-hyphenated-name>".

### Source Grounding (CRITICAL)
- "description" and "details" restate only what <content> says. Do NOT add background knowledge, industry context, definitions or characterizing/evaluative wording (e.g. "专注于……", "领先的", "知名", "头部", "致力于") that the document does not state.
- If the document does not say what an entity is (it is only listed or mentioned), describe only how the document mentions it; do not guess its business, field or history.
- Keep names, numbers, dates and units exactly as written in the document.

### Deduplication Rules
- Specific named things go ONLY in "entities"; abstract ideas, methods and procedures go ONLY in "concepts".
- Never duplicate items across the two arrays.

### JSON Formatting Rules
- Do NOT use literal newline characters inside JSON string values; use \\n.
</instructions>

Output ONLY valid JSON. Example:
{"entities": [{"name": "Acme Corp", "slug": "entity/acme-corp", "aliases": ["Acme"], "description": "...", "details": "..."}],
 "concepts": [{"name": "Retrieval-Augmented Generation", "slug": "concept/retrieval-augmented-generation", "aliases": ["RAG"], "description": "...", "details": "..."}]}"""


CITATIONS = """You are a precise citation system. Scan a batch of document chunks and decide, for each candidate entity/concept below, which chunks substantively discuss it.

<instructions>
**IMPORTANT: Write ALL names, descriptions, and details in {{Language}}**.

### Primary task
For each candidate slug in <candidate_slugs>, select the chunk IDs from <chunks> that **substantively discuss** it — the chunk states at least one concrete fact, attribute, step, date, number or relationship about the candidate, not a passing mention.
- Only cite chunks that appear in <chunks>; use the "id" attribute verbatim (e.g. "c003").
- Omit candidates that are not meaningfully discussed in this batch (no empty arrays).
- A chunk may be cited by several candidates.

### Secondary task: new slugs
If this batch reveals a significant entity/concept NOT in <candidate_slugs>, add it under "new_slugs" with "type" ("entity" or "concept"), "name", "slug", "aliases", "description", "details" and "source_chunks". Do not rediscover listed candidates.

### JSON Formatting Rules
- Do NOT use literal newline characters inside JSON string values; use \\n. Output ONLY valid JSON.
</instructions>

Output format:
{"citations": {"entity/xxx": ["c001", "c003"], "concept/yyy": ["c002"]}, "new_slugs": []}

If nothing in this batch is cite-worthy, return: {"citations": {}, "new_slugs": []}

<candidate_slugs>
{{CandidateSlugs}}
</candidate_slugs>

<chunks>
{{Chunks}}
</chunks>

Now output ONLY the JSON."""


DEDUP = """You are a strict deduplication system. Each newly extracted <item> below carries its OWN short list of existing wiki pages that are surface-similar to it (its <candidates>). For each item, decide whether it refers to the **exact same** real-world entity or concept as ONE of its own candidates.

<items>
{{Items}}
</items>

<instructions>
- Merge an item only into a candidate listed inside that same item, never into a page under another item, never into an invented slug.
- Entities merge only with entities, concepts only with concepts.
- Merge only when both refer to the same real-world thing and their names or aliases are variations (abbreviation, translation, spelling, explicit alternative name). A shared acronym alone is not enough.
- **related ≠ same**: different products, companies, versions, models of equipment, certificates or documents in the same category are DIFFERENT. When in doubt, do NOT merge.

Return {"merges": {"<new item slug>": "<existing page slug>"}} with only high-confidence merges, or {"merges": {}}.
Do NOT use literal newline characters inside JSON strings. Output ONLY valid JSON.
</instructions>"""


SUMMARY = """You are a wiki editor. Given the following document content, create a structured wiki summary page in Markdown.

<document>
<content>
{{Content}}
</content>
</document>

<available_wiki_pages>
{{Slugs}}
</available_wiki_pages>

<instructions>
1. The FIRST line of your output MUST be: SUMMARY: {one plain-text sentence, at most 40 words, describing what this document is about; no [[...]] links and no slugs}
2. After the SUMMARY line, write a summary of the document in Markdown: key facts, procedures, figures and conclusions.
3. Use ## for sections and ### for subsections.
4. **Wiki-link rule**: available_wiki_pages maps slugs to display names ("[[slug]] = name (Aliases: ...)"). Whenever you mention a listed name or alias, write it as [[slug|display name]] — always with the "|display name" part. Use the EXACT slugs; never invent slugs. A slug is internal: never write one as plain text, never write [[slug]] without the display name, and never add the name again in brackets after a link.
5. End with a "## 要点" section of bullet points.
6. Write in {{Language}}. Keep it concise; the length follows how much the document itself says (a short document gets a short page). Never pad.
7. If the content is empty or carries no substantive information, output exactly "SUMMARY: No textual content was extractable from this document." and a one-line note. Do NOT guess.
8. **Source grounding (CRITICAL)**: write only facts, figures, names and conclusions stated in <content>, the SUMMARY line included. Do NOT add background knowledge, definitions, industry context or characterizing/evaluative wording (e.g. "一家专注于……的企业", "领先的", "知名", "头部", "旨在", "致力于") that the document does not state. When the document only lists or mentions an organization or product, say only that; do not describe what it does. Keep numbers, dates and units exactly as written. These rules override any business instructions below.
{{ExtraInstructions}}</instructions>

Output the SUMMARY line first, then the Markdown content, with no other preamble."""


MODIFY_SYSTEM = """You are a wiki editor updating one wiki page. You process NEW information to add and/or deleted documents whose exclusive contributions must be removed.

### SOURCE GROUNDING & MERGE RULES (CRITICAL)
1. Chunk handles such as [c003] are internal metadata. NEVER output them; remove any legacy inline chunk handles.
2. Every newly added factual claim, name or number MUST be directly supported by the provided new source chunks.
3. Do not invent or infer information that is not explicitly present in the sources. If new chunks clearly supersede existing content, update it and add a brief "Contradictions / Updates" section; if the conflict is ambiguous, keep the existing text and only describe the conflict in that section.
4. The shared source-context block only calibrates scope and attribution; never copy it as evidence.
5. These grounding and factuality rules override any business instructions.

### EDITING AND OUTPUT RULES
1. You are a COMPILER, not a creative writer. Stay close to the source wording; lightly reorder and deduplicate, do not embellish.
2. Do not over-structure. Prefer one top-level heading, short paragraphs and flat factual lists.
3. No rhetorical filler ("旨在", "致力于", "具有重要意义") unless it appears verbatim in a source. Do not characterize the subject ("一家专注于……的企业", "领先的", "知名", "头部") unless a source says so; when the sources only list or mention it, say only that.
4. Keep self-reported claims scoped and attributed.
5. Preserve existing information that remains valid and on-topic, and the page's existing structure.
6. Keep a [[slug|name]] link only when its slug is in the valid-link list, always with the "|name" part; never invent a slug, never link the page to itself, and never write a slug as plain text.
7. The first output line must be "SUMMARY: {one plain-text sentence, at most 40 words, no links}", stating only what the page content supports, followed immediately by the Markdown page content.

Output the SUMMARY line first, followed by the updated Markdown, with no other preamble."""


MODIFY_USER = """{{SharedContext}}<page_metadata>
  <slug>{{Slug}}</slug>
  <title>{{Title}}</title>
  <type>{{Type}}</type>
  <aliases>{{Aliases}}</aliases>
</page_metadata>

This wiki page is specifically about **{{Title}}** (a {{Type}}). Every statement on the page MUST be directly about this exact {{Type}} — not about related or similarly-named things.

<existing_page_content>
{{Existing}}
</existing_page_content>
{{Additions}}{{Retractions}}
<valid_wiki_links>
{{ValidLinks}}
</valid_wiki_links>

<instructions>
1. The FIRST line MUST be: SUMMARY: {one plain-text sentence, at most 40 words, describing this page after the update; no links}
{{RetractRule}}{{AddRule}}4. Preserve existing information that is still valid and still about {{Title}}.
5. Keep [[slug|name]] links ONLY for slugs in <valid_wiki_links>, always with the "|name" part; never link {{Slug}} to itself.
6. Use "# {{Title}}" as the top-level heading if the page has none; do not add heading levels the sources do not justify.
7. Write in {{Language}}.
{{ExtraInstructions}}</instructions>

Output the SUMMARY line first, then the updated Markdown content, with no other preamble."""

ADDITIONS = """
<new_information>
{{NewContent}}
</new_information>

The <new_information> block is assembled from VERBATIM source chunks cited as directly supporting this page.
"""

ADDITIONS_EXTRACTED = """
<new_information>
{{NewContent}}
</new_information>

The <new_information> block is a short description extracted from the source document, not verbatim source text. Restate only what it says; do not expand on it or add anything it does not state.
"""

RETRACTIONS = """
<deleted_documents>
{{Deleted}}
</deleted_documents>

<remaining_source_documents>
{{Remaining}}
</remaining_source_documents>
"""

ADD_RULE = """3. ADD and MERGE the facts from <new_information>. First verify each piece is really about **{{Title}}**; reject parts that belong to a different but related thing. If genuinely about {{Title}} and contradicting old content, prefer the newer information.
"""

RETRACT_RULE = """2. REMOVE facts that were ONLY sourced from <deleted_documents> and are not present in <remaining_source_documents> or <new_information>. If the page becomes nearly empty with nothing to add, output "SUMMARY: (empty page)" followed by "# {{Title}}".
"""


INDEX_INTRO = """You are a wiki editor. Write a brief introduction for a wiki knowledge base index page.

<document_summaries>
{{Summaries}}
</document_summaries>

<instructions>
1. Write a title line starting with "# " that reflects the knowledge domain.
2. Follow with 2-3 sentences describing what this wiki covers, based only on the summaries above.
3. Mention only topics, organizations and products that appear in the summaries, and do not characterize them ("头部", "领先", "知名") unless a summary does.
4. Keep it concise; a directory listing is added separately below.
5. Write in {{Language}}.
</instructions>

Output ONLY the title and introduction paragraph. Do NOT list pages or links."""


PROFILE = """你在为一个知识库写说明，agent 会读这段说明来判断要不要检索这个知识库。

<documents>
{{Documents}}
</documents>

要求：
1. 用{{Language}}写 2-4 句话：知识库涵盖的主题和资料类型，适合回答哪些问题。
2. 只根据上面的文档标题和摘要，不要编造。
3. 直接输出说明正文，不要标题或前缀。
{{ExtraInstructions}}"""
