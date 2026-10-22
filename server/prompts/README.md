# Prompts

One file per system prompt, loaded at call time by `server/extract_llm.py`.

They live as files rather than string literals for two reasons. They are the security
boundary for hostile uploads — the paragraphs that say a document region is data and not
instruction — and that is easier to review as prose than as an escaped Python string. And
they are the thing most likely to need editing without a code change.

The **task** half of each call (what fields, what shape) is generated in code from
`tools/schema.py`, never written here: the schema is the single source of truth for the
field inventory, and a prompt that listed the fields separately would drift from it.
