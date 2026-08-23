---
name: log-incident
description: Use when something broke during the build. Writes a real incident entry to DECISIONS.md.
---
Append to DECISIONS.md in this exact shape:

## YYYY-MM-DD HH:MM — one-line symptom
**Symptom:** what I actually observed, verbatim if there was an error.
**Diagnosis:** what was actually wrong, and how I found it.
**First fix:** what I tried first.
**Whether it worked:** yes or no. If no, say what it broke instead.
**Final fix:** what actually resolved it.
**Guard added:** the test or hook that stops it recurring, or "none — accepted risk because…"

Terse. No adjectives. No "successfully". If nothing broke, do not invent
something.