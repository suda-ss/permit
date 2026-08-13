---
name: permit-research
description: Workflow for answering construction permit-requirement questions (fees, timelines, deadlines, required documents/forms, submission rules) for a specific AHJ. Use whenever the user asks what's required to get a permit, what a permit costs, how long it takes, or what to submit for any jurisdiction.
---

# Permit Research Workflow

Use this workflow for any request that boils down to "what do I need to get
`<permit type>` in `<jurisdiction>`." The goal is always the same predictable
report (see Output Contract), built from a persistent knowledge base rather
than re-researched from scratch every time.

## Step 0 — Scope the request

Establish, asking the user one clarifying question if genuinely ambiguous
rather than guessing:

- **Permit type** — e.g. residential electrical, solar PV, addition/remodel,
  demolition, plumbing, mechanical, re-roof. If the user's phrasing is vague
  ("permit for my garage"), infer the most likely permit type but state your
  assumption in the final answer rather than silently picking one.
- **Jurisdiction** — either an AHJ name ("City of Austin", "Travis County")
  or a street address to resolve.

### Resolving an address to an AHJ

1. Call `find_ahj` with the city/county name from the address first — it's
   cheap and the AHJ may already be known.
2. If nothing matches, use your own knowledge plus web search (via
   `ahj-fetch-agent`, see Step 2) to determine which body actually has
   permitting authority at that address. Watch for:
   - Incorporated city vs. unincorporated county land (different AHJ).
   - Special districts (fire, utility, coastal, historic) that may layer
     additional permitting on top of the base city/county AHJ.
3. If more than one plausible AHJ fits and you cannot resolve it confidently,
   **stop and ask the user to confirm** which jurisdiction applies. Do not
   proceed on a guess — a wrong AHJ produces a confidently wrong report.

## Step 1 — Check what's already known

1. Call `find_ahj` for the resolved jurisdiction.
   - **No match** → this is a new AHJ. Go to Step 2.
2. If matched, call `get_structured_permit_data(ahj_id, permit_type)`.
   - **`found: true` and `ahj.last_verified_at` is within the last 180
     days** **and** the categories a permit report needs (fees, timeline, documents,
     submission rules — deadlines may legitimately be empty for some
     permit types) are populated → this is a **fresh cache hit**. Skip
     straight to Step 3.
   - **`found: false`, or stale (`last_verified_at` older than the
     freshness window, or null), or missing more than one core category** →
     treat as a **cache miss**. Go to Step 2.
   - **Partial** (fresh but missing only one minor category, e.g. no
     deadlines on file and the permit type plausibly has none) → use your
     judgment: proceed to Step 3 if what's missing looks like genuine
     absence rather than incomplete research; otherwise treat as a miss.

## Step 2 — Populate the knowledge base (cache miss only)

1. Delegate `ahj-fetch-agent` with the resolved jurisdiction (name, and
   `ahj_id` if it already existed) and the permit type. Wait for it to
   finish and report back the `ahj_id` and the raw documents it saved.
   - If it reports it found **no official online source at all**, do not
     invent one — proceed to Step 3 anyway; `compile-agent` will produce a
     report that honestly states what wasn't found.
2. Once `ahj-fetch-agent` completes, delegate `parsing-agent` and
   `vector-ingest-agent` **in a single parallel batch** — both are given the
   same `ahj_id` and permit type, and both consume the same raw documents
   independently. Do not run them sequentially; there is no dependency
   between them.
3. Wait for **both** to finish before moving on. If either reports it
   couldn't extract a category, note that — it will surface honestly in the
   final report rather than needing to be re-run speculatively.

## Step 3 — Compile the answer

Delegate `compile-agent` with the `ahj_id` and permit type. This step is
**never skipped**, including on a cache hit — it's what guarantees every
response follows the same structure. Return its output to the user as-is
(you may add one short sentence of framing, e.g. noting an inferred permit
type or an AHJ ambiguity you resolved, but do not rewrite or reformat the
report itself).

## Output Contract

Every answer to the user must follow this structure (enforced by
`compile-agent`, but repeated here so you can sanity-check its output before
relaying it):

    # Permit Report: <Permit Type> — <AHJ Name>, <State>

    ## Jurisdiction
    - AHJ, type (city/county/state/special district), official portal link, contact info

    ## Requirements Summary
    - When this permit is required; key conditions/exemptions

    ## Fees
    | Fee | Amount | Basis |

    ## Timeline
    - Standard review time; expedited option if any

    ## Deadlines
    - Submission deadlines, permit expiration/renewal windows

    ## Required Documents & Forms
    | Document/Form | Form # | Required? | Source |

    ## Submission Process
    1. Ordered steps; accepted methods (portal/mail/in-person); portal link

    ## Sources & Freshness
    - URLs + fetch dates; last verified date; note any partial/inferred data

A section with no available data still appears, stating plainly that it
isn't covered by current sources — never silently omit a heading or invent
a plausible-sounding value to fill it.

## When this workflow does NOT apply

A question that isn't about a specific permit tied to a jurisdiction (e.g.
"what does AHJ stand for", general questions about how permitting works,
follow-up questions about a report already given this session) doesn't need
the full fetch → parse/vectorize → compile pipeline — answer directly, or
use `find_ahj` / `get_structured_permit_data` / `vector_search` yourself if
it's a quick lookup against already-known data.
