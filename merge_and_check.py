#!/usr/bin/env python3
"""
merge_and_check.py
------------------
Usage:
    python3 merge_and_check.py file1.json file2.json [...] --out merged.json

Merges multiple quiz batch JSON files into one database, runs a full
health check, and writes a plain-text report.

Broken files are skipped with a clear error message.
Duplicate rows (exact text match) are dropped automatically.
Near-duplicate pairs are flagged for human review but kept.

Outputs:
    merged.json       — merged database
    merge_report.txt  — full health check report
"""

import json, re, sys, os, argparse
from collections import Counter
from difflib import SequenceMatcher


# ── schema constants ──────────────────────────────────────────────────────────

REQUIRED_KEYS = {
    "id","category","age_band","difficulty","difficulty_source",
    "question_ru","options","answer_index","explanation_ru",
    "image_key","image_role","source","status"
}
VALID_CATEGORY   = {"math","geometry","logic"}
VALID_AGE_BAND   = {"08-09","10-12"}
VALID_DIFFICULTY = {1,2,3}
VALID_DIFF_SRC   = {"from_source","estimated"}
VALID_STATUS     = {"draft","reviewed","approved"}
VALID_IMAGE_ROLE = {"decorative","load_bearing","none"}

FORBIDDEN_EXPLANATION = [
    r'\bно\s+в\s+(вариант|худшем)', r'\bошибк', r'\bперепровер',
    r'\bпересмотр', r'\bисправим\b', r'\bпровери[мт]\b', r'\bдавайте\b',
    r'на самом деле', r'не подходит', r'не совпадает',
    r'возможно,?\s*ошибка', r'\bя\s+(должен|исправлю|перепутал)\b',
]


# ── helpers ───────────────────────────────────────────────────────────────────

def load_file(path):
    """Returns (questions_list, error_string_or_None)."""
    try:
        raw = open(path, encoding='utf-8').read()
        if '//' in raw or '/*' in raw:
            return None, "Contains JS-style comments (//) - invalid JSON"
        data = json.loads(raw)
        qs = data.get('questions')
        if not isinstance(qs, list):
            return None, "No 'questions' list at top level"
        return qs, None
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"
    except Exception as e:
        return None, f"Read error: {e}"


def check_row(q):
    """
    Returns list of (severity, message).
    ERROR  = structural defect, row excluded from output.
    WARN   = process flag, row kept but noted in report.
    """
    issues = []
    E = lambda m: issues.append(('ERROR', m))
    W = lambda m: issues.append(('WARN',  m))

    # required keys
    missing = REQUIRED_KEYS - set(q.keys())
    if missing:
        E(f"Missing required keys: {sorted(missing)}")

    # enum fields
    if q.get('category')          not in VALID_CATEGORY:   E(f"Bad category: {q.get('category')!r}")
    if q.get('age_band')          not in VALID_AGE_BAND:   E(f"Bad age_band: {q.get('age_band')!r}")
    if q.get('difficulty')        not in VALID_DIFFICULTY: E(f"Bad difficulty: {q.get('difficulty')!r}")
    if q.get('difficulty_source') not in VALID_DIFF_SRC:   E(f"Bad difficulty_source: {q.get('difficulty_source')!r}")
    if q.get('status')            not in VALID_STATUS:     E(f"Bad status: {q.get('status')!r}")
    if q.get('image_role')        not in VALID_IMAGE_ROLE: E(f"Bad image_role: {q.get('image_role')!r}")

    # options
    opts = q.get('options', [])
    if not isinstance(opts, list) or len(opts) != 4:
        E(f"options must be exactly 4 items (got {len(opts) if isinstance(opts,list) else type(opts).__name__})")
    elif len(set(opts)) != 4:
        E(f"Duplicate option text: {opts}")

    # answer_index
    ai = q.get('answer_index')
    if not isinstance(ai, int) or not (0 <= ai <= 3):
        E(f"answer_index must be int 0-3, got {ai!r}")

    # image role/key pairing
    role, key = q.get('image_role'), q.get('image_key')
    if role == 'none' and key is not None:
        E(f"image_role='none' but image_key={key!r}")
    elif role in ('decorative','load_bearing') and key is None:
        E(f"image_role={role!r} but image_key is None")

    # explanation scratchpad leak (Gate 3)
    expl = q.get('explanation_ru', '')
    if not isinstance(expl, str) or not expl.strip():
        E("explanation_ru is empty")
    else:
        hits = [p for p in FORBIDDEN_EXPLANATION if re.search(p, expl.lower())]
        if hits:
            E(f"explanation_ru has scratchpad phrases: {hits}")
        if '[' in expl or ']' in expl:
            E("explanation_ru contains '[' or ']' (possible pasted options array)")

    # uniqueness_check object (new schema)
    if 'uniqueness_check' in q:
        uc = q['uniqueness_check']
        if not isinstance(uc, dict):
            E("uniqueness_check must be an object")
        else:
            needed = {"method","candidates_examined","valid_count","valid_options"}
            if not needed.issubset(uc.keys()):
                E(f"uniqueness_check missing: {needed - set(uc.keys())}")
            else:
                if uc.get('method') not in ('enumeration','algebraic','not_applicable'):
                    E(f"uniqueness_check.method invalid: {uc.get('method')!r}")
                vc = uc.get('valid_count')
                vo = uc.get('valid_options', [])
                if not isinstance(vo, list) or vc != len(vo):
                    E(f"valid_count={vc} != len(valid_options)={len(vo) if isinstance(vo,list) else '?'}")
                elif isinstance(ai, int) and 0 <= ai <= 3:
                    if ai not in vo:
                        E(f"answer_index={ai} not in valid_options={vo}")
                    if vc != 1:
                        E(f"valid_count={vc}, must be 1")
                if uc.get('method') == 'enumeration' and uc.get('candidates_examined',2) <= 1:
                    W("enumeration claimed but candidates_examined<=1 (no real search)")

    # uniqueness_checked boolean (old schema)
    elif 'uniqueness_checked' in q:
        if q['uniqueness_checked'] is False:
            W("uniqueness_checked=false (old schema, answer not independently verified)")

    else:
        W("No uniqueness_check or uniqueness_checked field")

    # process inconsistency: verified_by populated while still draft
    vby = q.get('verified_by')
    if vby is not None and q.get('status') == 'draft':
        W(f"verified_by={vby!r} set but status='draft'")

    return issues


def qsimilarity(a, b):
    """Returns similarity ratio on full normalised question text."""
    return SequenceMatcher(None, a, b).quick_ratio()


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('files', nargs='+')
    parser.add_argument('--out',    default='merged.json')
    parser.add_argument('--report', default='merge_report.txt')
    parser.add_argument('--high-sim',  type=float, default=0.95,
                        help='Ratio >= this: flagged as probable duplicate (default 0.95)')
    parser.add_argument('--low-sim',   type=float, default=0.85,
                        help='Ratio >= this: flagged as same-template (default 0.85)')
    args = parser.parse_args()

    R = []  # report lines
    log = lambda s='': R.append(str(s))

    log("QUIZ DATABASE — MERGE + HEALTH CHECK REPORT")
    log("=" * 60)
    log()

    # ── 1. load ────────────────────────────────────────────────────────────
    log("STEP 1 — LOADING FILES")
    log("-" * 40)

    all_rows = []       # list of (source_filename, row_dict)
    skipped  = []

    for path in args.files:
        fname = os.path.basename(path)
        qs, err = load_file(path)
        if err:
            log(f"  SKIP  {fname}: {err}")
            skipped.append((fname, err))
        else:
            log(f"  OK    {fname}: {len(qs)} rows")
            for q in qs:
                all_rows.append((fname, q))

    log()
    log(f"Files loaded:  {len(args.files)-len(skipped)} / {len(args.files)}")
    log(f"Total rows in: {len(all_rows)}")
    log()

    # ── 2. per-row health check ────────────────────────────────────────────
    log("STEP 2 — ROW HEALTH CHECK")
    log("-" * 40)

    clean = []      # (fname, q, issues) - zero errors
    errored = []    # (fname, q, issues) - has errors
    err_counter  = Counter()
    warn_counter = Counter()

    for fname, q in all_rows:
        issues = check_row(q)
        errors = [m for s,m in issues if s=='ERROR']
        warns  = [m for s,m in issues if s=='WARN']
        for m in errors: err_counter[m[:70]] += 1
        for m in warns:  warn_counter[m[:70]] += 1
        if errors:
            errored.append((fname, q, issues))
        else:
            clean.append((fname, q, issues))

    log(f"Rows with errors  (excluded): {len(errored)}")
    log(f"Rows clean/warned (included): {len(clean)}")
    log()

    if err_counter:
        log("Error types:")
        for msg, n in err_counter.most_common(20):
            log(f"  {n:3d}x  {msg}")
        log()

    if warn_counter:
        log("Warning types:")
        for msg, n in warn_counter.most_common(15):
            log(f"  {n:3d}x  {msg}")
        log()

    if errored:
        log("Error detail (first 50):")
        for fname, q, issues in errored[:50]:
            qid = q.get('id','???')
            log(f"  [{fname}] {qid}")
            for sev, msg in issues:
                if sev == 'ERROR':
                    log(f"    ERROR: {msg}")
        if len(errored) > 50:
            log(f"  ...and {len(errored)-50} more")
        log()

    # ── 3. duplicate detection ─────────────────────────────────────────────
    log("STEP 3 — DUPLICATE DETECTION")
    log("-" * 40)

    # exact ID duplicates
    id_counter = Counter(q.get('id','???') for _,q,_ in clean)
    exact_id = {i:n for i,n in id_counter.items() if n>1}
    log(f"Exact duplicate IDs:       {len(exact_id)}")
    for qid, n in sorted(exact_id.items()):
        log(f"  {n}x  {qid}")

    # exact question text duplicates
    txt_counter = Counter(q.get('question_ru','').lower().strip() for _,q,_ in clean)
    exact_txt = {t:n for t,n in txt_counter.items() if n>1}
    log(f"Exact duplicate questions: {len(exact_txt)}")
    for t, n in sorted(exact_txt.items()):
        log(f"  {n}x  '{t[:70]}'")

    # near-duplicate detection on full question text
    log(f"Near-duplicate scan  (high>={args.high_sim} | template>={args.low_sim})...")
    texts = [(i, q.get('question_ru','').lower().strip(), q.get('id','???'))
             for i, (_,q,_) in enumerate(clean)]

    high_sim_pairs = []
    low_sim_pairs  = []
    for i in range(len(texts)):
        for j in range(i+1, len(texts)):
            # skip if exact match (already caught above)
            if texts[i][1] == texts[j][1]:
                continue
            r = qsimilarity(texts[i][1], texts[j][1])
            if r >= args.high_sim:
                high_sim_pairs.append((texts[i][2], texts[j][2],
                                       texts[i][1][:60], texts[j][1][:60], r))
            elif r >= args.low_sim:
                low_sim_pairs.append((texts[i][2], texts[j][2],
                                      texts[i][1][:60], texts[j][1][:60], r))

    log(f"Likely duplicates (>={args.high_sim}): {len(high_sim_pairs)}")
    for id1,id2,t1,t2,r in high_sim_pairs[:30]:
        log(f"  {r:.2f}  {id1} <-> {id2}")
        log(f"        '{t1}'")
        log(f"        '{t2}'")

    log(f"Same-template pairs ({args.low_sim}–{args.high_sim}): {len(low_sim_pairs)}")
    for id1,id2,t1,t2,r in low_sim_pairs[:20]:
        log(f"  {r:.2f}  {id1} <-> {id2}")
        log(f"        '{t1}'")
        log(f"        '{t2}'")
    log()

    # ── 4. deduplicate and assign fresh IDs ───────────────────────────────
    log("STEP 4 — DEDUPLICATION + ID ASSIGNMENT")
    log("-" * 40)

    seen_texts = {}
    final = []
    for fname, q, issues in clean:
        norm = q.get('question_ru','').lower().strip()
        if norm in seen_texts:
            log(f"  DROP  {q.get('id')} — exact duplicate of {final[seen_texts[norm]][1].get('id')}")
            continue
        seen_texts[norm] = len(final)
        final.append((fname, q, issues))

    for idx, (_, q, _) in enumerate(final, start=1):
        q['_original_id'] = q.get('id')
        q['id'] = f"q{idx:04d}"

    log(f"Rows after deduplication: {len(final)}")
    log()

    # ── 5. distribution stats ──────────────────────────────────────────────
    log("STEP 5 — DISTRIBUTION")
    log("-" * 40)

    fqs = [q for _,q,_ in final]
    for label, field in [("Category", "category"), ("Age band", "age_band"),
                          ("Difficulty", "difficulty"), ("Status", "status")]:
        counts = Counter(q.get(field) for q in fqs)
        log(f"{label}:")
        for k in sorted(counts, key=str):
            log(f"  {str(k):<14} {counts[k]:4d}")

    log()
    log("Coverage (category x age_band):")
    header = f"  {'':15}" + "  ".join(f"{a:>6}" for a in sorted(VALID_AGE_BAND))
    log(header)
    any_thin = False
    for cat in sorted(VALID_CATEGORY):
        row = f"  {cat:<15}"
        for age in sorted(VALID_AGE_BAND):
            n = sum(1 for q in fqs if q.get('category')==cat and q.get('age_band')==age)
            row += f"  {n:>6}"
            if n < 10:
                any_thin = True
        log(row)
    if any_thin:
        log("  WARNING: some buckets have < 10 rows (marked above)")
    log()

    # ── 6. write output ────────────────────────────────────────────────────
    log("STEP 6 — OUTPUT")
    log("-" * 40)

    out_data = {
        "_meta": {
            "total": len(fqs),
            "sources": [os.path.basename(p) for p in args.files],
            "skipped_files": [f for f,_ in skipped],
            "excluded_errors": len(errored),
            "excluded_duplicates": len(clean) - len(final),
            "distribution": {
                "category":   dict(Counter(q.get('category')   for q in fqs)),
                "age_band":   dict(Counter(q.get('age_band')    for q in fqs)),
                "difficulty": {str(k):v for k,v in Counter(q.get('difficulty') for q in fqs).items()},
            }
        },
        "questions": fqs
    }

    out_dir  = '.'
    out_path = os.path.join(out_dir, os.path.basename(args.out))
    rpt_path = os.path.join(out_dir, os.path.basename(args.report))

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out_data, f, ensure_ascii=False, indent=2)

    with open(rpt_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(R))

    log(f"Database: {out_path}  ({len(fqs)} rows)")
    log(f"Report:   {rpt_path}")

    # final summary to stdout
    print()
    print("=" * 50)
    print("DONE")
    print(f"  Input rows:            {len(all_rows)}")
    print(f"  Excluded (errors):     {len(errored)}")
    print(f"  Excluded (duplicates): {len(clean)-len(final)}")
    print(f"  Output rows:           {len(fqs)}")
    print(f"  Skipped files:         {len(skipped)}")
    print(f"  Database: {out_path}")
    print(f"  Report:   {rpt_path}")
    print("=" * 50)


if __name__ == '__main__':
    main()
