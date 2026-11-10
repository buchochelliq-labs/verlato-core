# Writing style and vocabulary

These controls shape model suggestions. They are recorded with every applied suggestion, so a
release note's wording can always be traced back to the settings that produced it.

## Tone

| Tone | Wording |
|---|---|
| `release-note` (default) | polished release-note wording for a broad product audience |
| `concise` | short, direct wording with no extra qualifiers |
| `technical` | precise engineering wording that names APIs, files, protocols or internal behaviour |
| `customer-facing` | plain wording that emphasises user-visible value and avoids internal jargon |

Set it per run with `--tone`, or with `suggestion_tone`, `VERLATO_SUGGESTION_TONE` or
`verlato.suggestionTone`. The extension's Intent inference panel can change the tone and
regenerate a single item.

## Project style

```json
{
  "suggestion_style": {
    "guidance": "Name the user-visible effect first. Prefer present tense.",
    "project_vocabulary": ["Release Control", "changeset"],
    "banned_phrases": ["various improvements", "minor fixes"],
    "specificity_floor": 0.3
  }
}
```

- **`guidance`**: free text added to every suggestion prompt (the first 300 characters).
- **`project_vocabulary`**: product terms the model should prefer.
- **`banned_phrases`**: suggestions containing any of these are rejected.
- **`specificity_floor`**: between 0 and 1. Suggestions scoring below it for specificity
  (naming concrete things rather than saying "improvements") are rejected. `0`, the default,
  turns the check off.

Environment variables override the file: `VERLATO_STYLE_GUIDANCE`,
`VERLATO_PROJECT_VOCABULARY` and `VERLATO_BANNED_PHRASES` (comma-separated lists), and
`VERLATO_SPECIFICITY_FLOOR`. The VS Code settings `verlato.styleGuidance`,
`verlato.projectVocabulary`, `verlato.bannedPhrases` and `verlato.specificityFloor` set them.

## Prompt profiles

`--prompt-profile` (`suggestion_prompt_profile`, `VERLATO_SUGGESTION_PROMPT_PROFILE`,
`verlato.suggestionPromptProfile`) sets how much evidence goes into single-item prompts:
`conservative` (default, the most context), `lean` or `minimal` (for small or slow models).

## Feedback

Verlato records lightweight feedback in `.verlato_state.json` as you work:

- applying, editing and accepting summaries are recorded automatically;
- the extension also records skipped and regenerated suggestions, and suggestions you mark
  useful or not useful;
- `verlato feedback record --event <name> --item-id <id> [--metadata-json '{...}']` records
  your own.

By default an event stores hashes and metrics (tone, prompt profile, specificity, source),
not the summary text. Set `feedback_include_text: true` (or `VERLATO_FEEDBACK_INCLUDE_TEXT=1`,
or `verlato.feedbackIncludeText`) to keep accepted and edited text too.

```sh
verlato feedback export --output feedback.json   # or --format jsonl, --event summary_edited
verlato feedback recommend                       # advice from the aggregates
```

`feedback recommend` looks only at the aggregates (never at prompts or responses) and
suggests changes such as a different tone or a specificity floor, for example:

```json
{
  "schema_version": "verlato.feedback_recommendations.v1",
  "event_count": 2,
  "recommendations": [
    {
      "kind": "keep-current-style",
      "title": "Keep the current prompt style",
      "confidence": "medium",
      "detail": "Current aggregate feedback does not show a strong reason to adjust prompt or style settings."
    }
  ]
}
```

It recommends; it never changes your settings.
