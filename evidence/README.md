# Reviewer evidence

Committed evidence is written only by the explicit README demo commands that pass
`--evidence-dir evidence/...`. Ordinary runs write to `.cua-out/<timestamp>/` (gitignored),
and tests write only to pytest `tmp_path`; nothing overwrites this directory automatically.

Layout (docs/spec.md section 16.2), produced by the README demo commands:

```text
evidence/mockbank/lookup_member_balance/
  discovery/            meta.json, events.jsonl, artifact.json
  replay-success/       meta.json, events.jsonl, result.json
  replay-notfound/      meta.json, events.jsonl, result.json
  replay-interstitial/  meta.json, events.jsonl, result.json
  replay-hitl/          meta.json, events.jsonl, intervention.json, human-events.jsonl, result.json, masked.png
```

All persisted text is sanitized: raw member IDs and balances never appear in artifacts or evidence.
