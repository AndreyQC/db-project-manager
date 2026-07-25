# Checkpoints — conventions

Purpose of `-=CHECKPOINTS=-`: give a new agent session (or a returning human)
**fast project context** without reading the full `-=tasks=-` and `-=PHASES=-` history or chat
logs. A checkpoint is a snapshot of *where the project is right now*.

---

## 1. When to write a checkpoint

- At the **end of a PHASE** (Phase N complete) — always.
- At the **end of a working session** if substantial work was done (new
  feature, refactor, or a tricky bug fix worth recording) — optional but
  recommended.
- **Before** starting a new phase or a large refactor — captures the
  pre-change baseline.
- After merging a PR / branch into `main`.

Do NOT write a checkpoint for trivial changes (typo, small test, doc tweak).

## 2. File naming

```
<YYYYMMDD>_<NNN>_checkpoint.md
```

- `YYYYMMDD` — date the checkpoint is written.
- `NNN` — three-digit sequence within the day (001, 002, …), starting fresh each day.

Examples:
- `20260719_001_checkpoint.md` — first checkpoint on 2026-07-19.
- `20260722_002_checkpoint.md` — second checkpoint on 2026-07-22.

Order is alphabetical = chronological. No version suffixes (`v2`, `final`) —
the sequence number already disambiguates; if a checkpoint needs a correction,
write the next one rather than editing a published one.

## 3. Structure (sections)

Keep it short and dense. Use the same sections every time so the reader knows
where to look. Recommended (omit a section only if it's truly empty):

1. **Header** — date, branch, last commit hash+subject, current phase, what's next.
2. **What this project is** — 2–3 sentences, language, stack.
3. **Architecture map** — table: concern → path → one-line note. This is the
   single most useful section for a new session; be concrete with paths.
4. **Implemented features** — what works, by phase.
5. **Known gaps / NOT done** — explicit list of what's missing or deferred.
   Be honest; this prevents the next session from re-discovering gaps.
6. **Key decisions & constraints** — non-obvious choices worth knowing
   (e.g., "no QScintilla", "unset SSL_CERT_FILE for uv").
7. **Test status** — the exact commands and current pass/fail counts.
8. **Where to read more** — ordered reading list with 1-line "why" for each.
9. **Next planned work** — what the next session should tackle first.

Not every checkpoint needs all 9; adapt, but keep the table (§3) and the
gaps list (§5) — they carry the most value.

## 4. Writing rules

- **Dense, not decorative.** Less prose, more paths, hashes, counts, bullet
  lists. The reader is skimming for facts, not a narrative.
- **Concrete paths** — always relative to repo root (e.g.
  `app\src\store\dictionaries.ts`, not "the graph module").
- **Commit hashes** for the last state — lets the reader `git show` to verify.
- **No new design** here — checkpoints describe *what is*, not *what should be*.
  Design discussion belongs in `-=tasks=-/YYYY-MM-DD/YYYYMMDD_NN_<desc>_draft.md`.
- **Cross-link, don't duplicate.** If a decision is explained in
  `Phase_2_vision_final.md` Q4, link it; don't re-explain in the checkpoint.
- **Plain markdown, ASCII tables, no emojis.** Works in any viewer, diffs
  cleanly, no rendering surprises.
- **Russian or English** — match the surrounding docs (this repo uses Russian
  prose, English identifiers). Don't mix mid-sentence.

## 5. Length target

Aim for **80–150 lines**. If it's longer, the architecture map or gap list is
probably duplicating `-=tasks=-` content — link instead. If shorter, the
architecture map is likely incomplete.

## 6. Commit the checkpoint

One commit per checkpoint file:
```
docs(checkpoint): add 20260719_001 — Phase 2 complete status
```
Commit the checkpoint together with the convention file only on first setup;
after that, checkpoints are individual commits.

## 7. Relationship to `-=tasks=-`

| `-=tasks=-` | `-=CHECKPOINTS=-` |
|-------------|-------------------|
| Design history (drafts, plans, vision) — *how we got here* | Current snapshot — *where we are now* |
| Detailed, often long | Dense, skimmable |
| Append-only (drafts kept) | Newest = current truth; older ones are historical snapshots |
| Read when designing the next step | Read first, every new session |

A new session should read **the latest checkpoint first**, then dive into `-=PHASES=-` and
`-=tasks=-/YYYY-MM-DD/` only for the specific phase it's working on.
