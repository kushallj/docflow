# DocFlow — OCR + rule engine for business documents

DocFlow reads invoices with **PaddleOCR**, pulls out the fields that matter, and runs them
through a **configurable nested rule engine** that decides what happens next: auto-approve,
send to a review queue, or reject. Every decision comes with a condition-by-condition
explanation, and every human correction is audited.

The rule format is the same `Fact / Operator / Value` + `ALL / ANY` group model as the
[Flutter rule builder](https://github.com/kushallj/Rule-Engine), so non-developers at each
client can change their own document workflow without a code change or a deploy.

```
upload (PDF / image)
   │
   ▼
PaddleOCR (PP-OCRv5)  ── text lines + confidence + bounding boxes per page
   │
   ▼
Field extraction      ── layout-aware: label → value on the same line, to the right, or below
   │                     invoice no., dates, GSTINs, subtotal, CGST/SGST/IGST, grand total …
   ▼
Derived facts         ── GSTIN checksum, vendor master lookup, duplicate check,
   │                     subtotal + tax = total, days until due, lowest field confidence …
   ▼
Rule engine           ── every active rule set evaluated; strictest action wins
   │                     (reject > review > approve); low confidence blocks auto-approval
   ▼
Approved  /  Needs review (queue: finance-manager, ap-clerk …)  /  Rejected
   │
   ▼
Reviewer dashboard    ── page image with field boxes, inline corrections that re-run the
                         rules, approve / reject with a note, full audit log
```

## Results on the sample set

`samples/make_invoices.py` generates five realistic GST invoices (one PDF, one simulated phone
scan with blur and rotation). Run through real PaddleOCR on a laptop CPU:

| Invoice | What is wrong with it | Decision | Why |
|---|---|---|---|
| 01 office supplies (PDF) | nothing | **Approved** | approved vendor, valid GSTIN, totals add up |
| 02 laptops | ₹6.86 lakh | **Needs review** → finance-manager | above ₹2,00,000 |
| 03 facility services | fake GSTIN (checksum fails) | **Rejected** | GSTIN mod-36 check digit wrong |
| 04 packaging (blurred scan) | vendor not in master | **Needs review** → finance-manager | unknown vendor |
| 05 stationery | total ≠ subtotal + tax | **Needs review** → ap-clerk | totals do not add up |

All 11 fields were extracted correctly on all five invoices (mean OCR confidence 0.997).
OCR takes ~10–12 s per A4 page on CPU with the mobile detection model (the model loads once
per worker, ~20 s). The recorded OCR output is kept in `tests/fixtures/ocr/` and replayed in
the test suite, so extraction is regression-tested against real PaddleOCR output.

## The rule engine

Pure Python (`apps/rules/engine.py`, no Django imports), so it is easy to test and reuse.

```json
{
  "type": "any",
  "rules": [
    {"fact": "total_amount", "operator": "greaterThan", "value": 200000},
    {"type": "all", "rules": [
      {"fact": "vendor_approved", "operator": "equalTo", "value": false},
      {"fact": "tax_amount", "operator": "greaterThan", "value": {"fact": "subtotal"}}
    ]}
  ]
}
```

- **Groups**: `all` / `any`, optional `"not": true`, nested up to 8 levels.
- **Fact types**: `string`, `number`, `bool`, `date` (Flutter's `int` / `double` map to `number`).
- **Operators**: `equalTo`, `notEqualTo`, `greaterThan(OrEqual)`, `lessThan(OrEqual)`, `between`,
  `in`, `notIn`, `contains`, `notContains`, `startsWith`, `matches` (regex), `exists`,
  `notExists`, and date operators `isInPast`, `olderThanDays`, `withinNextDays`.
- **Fact-to-fact comparisons**: `"value": {"fact": "subtotal"}`.
- **Fails closed**: a condition on a missing or unreadable value is false, so an amount the OCR
  could not read never satisfies an approval rule. A rule set that becomes invalid (for example
  it references a removed field) sends documents to review, never to approval.
- **Validated on save** against the document type's fact catalog: unknown facts, operators that
  do not fit the type (`greaterThan` on a string), bad regexes and inverted ranges are refused
  with a message that points at the exact node (`root.rules[1].value: …`).
- **Explainable**: evaluation returns a trace tree with the actual value of every condition,
  shown in the dashboard and returned by the API.
- **Versioned and audited**: changing a rule bumps its version and is written to the audit log;
  each decision records which rule versions it used.

### How a decision is made

1. Every active rule set for the document type is evaluated.
2. Of the rule sets that match, the strictest action wins: **reject > review > approve**.
   The review queue comes from the matching rule (`route_to`).
3. No match → the document type's default (review, unless configured otherwise).
4. Safety net: auto-approval is downgraded to review when a required field is missing, below
   the confidence threshold, or auto-approval is switched off for that document type.
5. A person's approve / reject is final: later rule changes do not override it unless the
   document is reopened.

## Facts available to rules (invoice)

| Extracted fields | Derived facts |
|---|---|
| `invoice_number`, `invoice_date`, `due_date`, `po_number`, `vendor_name`, `vendor_gstin`, `buyer_gstin`, `subtotal`, `tax_amount` (CGST+SGST or IGST), `total_amount`, `currency` | `vendor_gstin_valid`, `buyer_gstin_valid` (official mod-36 checksum), `billed_to_us`, `vendor_known`, `vendor_approved`, `is_duplicate` (same vendor + invoice no., or same file hash), `totals_consistent`, `tax_rate_percent`, `days_until_due`, `invoice_age_days`, `min_field_confidence`, `low_confidence_count`, `missing_required_count`, `ocr_confidence`, `page_count` |

`GET /api/document-types/invoice/facts/` returns this catalog with the operators allowed for
each fact, which is exactly what the Flutter builder needs for its dropdowns.

## API

Token auth (`Authorization: Token <key>`) or session. Everything is scoped to the caller's
organisation; roles are **admin** (rules, vendors), **reviewer** (corrections, decisions) and
**viewer** (read only).

| Method | Path | |
|---|---|---|
| `POST` | `/api/auth/token/` | username + password → token |
| `POST` | `/api/documents/` | multipart `file`, `document_type` → processed document |
| `GET` | `/api/documents/?status=needs_review&queue=finance-manager` | review queue |
| `GET` | `/api/documents/{id}/` | fields with confidence + boxes, facts, rule traces, audit log |
| `PATCH` | `/api/documents/{id}/fields/{key}/` | `{"value": "10640.00"}` → stores correction, re-runs rules |
| `POST` | `/api/documents/{id}/review/` | `{"decision": "approve" \| "reject", "note": "…"}` |
| `POST` | `/api/documents/{id}/reopen/` · `/reevaluate/` · `/reprocess/` | hand back to rules · re-run rules · re-run OCR |
| `GET` | `/api/document-types/{slug}/facts/` | fact catalog + operators (Flutter dropdowns) |
| `GET/POST/PATCH/DELETE` | `/api/rulesets/` | rule sets (admin), validated on save |
| `POST` | `/api/rulesets/test/` | dry-run a tree against `document_id` or ad-hoc `facts`, returns the trace |
| `GET` | `/api/rulesets/{id}/history/` | versions and who changed what |
| `GET/POST/PATCH` | `/api/vendors/` | vendor master (GSTIN validated) |

Uploaded files and page images are served only through `/files/…`, which checks that the file
belongs to the caller's organisation.

## Run it

**Docker** (Postgres, Redis, Celery worker, models baked into the image):

```bash
docker compose up --build
# http://localhost:8000  ·  admin / docflow-demo  ·  reviewer / docflow-demo
```

**Local** (SQLite, OCR runs inline without Redis):

```bash
python -m venv .venv && . .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install paddlepaddle==3.2.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
pip install -r requirements-dev.txt
python manage.py migrate && python manage.py seed_demo
python samples/make_invoices.py                       # writes samples/invoices/*.png|pdf
python manage.py runserver
```

Then upload the files in `samples/invoices/` from the dashboard. Set `REDIS_URL` and run
`celery -A config worker --concurrency=1` to process documents in the background instead.

**Tests** (89 tests, ~45 s, no OCR model needed):

```bash
pytest
```

## Project layout

```
apps/
  ocr/          engine interface, PaddleOCR engine, PDF → page images
  extraction/   layout-aware locator, invoice extractor, amount / date / GSTIN parsers
  rules/        engine.py (pure Python), fact catalog + derived facts + decision, API
  documents/    models, pipeline, Celery task, review actions, API
  core/         organisations, memberships and roles, vendor master, seed_demo
  dashboard/    server-rendered reviewer UI
samples/        synthetic GST invoice generator (fictional companies and GSTINs)
tests/          engine, extraction (real OCR fixtures), pipeline + API + tenancy
```

## Design decisions

- **OCR engine behind an interface.** PaddleOCR is the default (Apache-2.0, strong on printed
  documents, runs on CPU). Swapping in another engine means implementing one `run()` method.
- **Deterministic extraction first.** Field extraction uses page geometry rather than an LLM:
  it is fast, free, explainable (every value has a box on the page) and testable. An LLM
  fallback for unusual layouts is a natural next step, not a dependency.
- **Rules as data, not code.** Each client gets its own rule sets, editable through the API
  and the Flutter builder, validated against a typed catalog so a bad rule cannot be saved.
- **Humans stay in control.** Uncertain reads go to people, people's decisions are final, and
  every correction and decision is in an append-only audit log.

## Roadmap

- Connect the Flutter rule builder to `/api/document-types/{slug}/facts/` and `/api/rulesets/`.
- More document types (purchase orders, KYC, insurance claims) through new extractors.
- Line-item table extraction (PaddleOCR PP-StructureV3) and three-way PO matching.
- Optional LLM fallback for low-confidence fields; webhooks to push decisions to ERPs.
