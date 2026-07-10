# SQLite Context Retrieval Example

Good context retrieval starts before ranking. A metadata field earns its place
when it changes which records survive a real retrieval query.

This dependency-free demo compares three ways to retrieve synthetic
Chief-of-Staff-style context from an in-memory SQLite database:

1. **Content only:** rank titles and bodies with FTS5.
2. **Broad metadata:** add generic tags such as `work`, `decision`, and `open`
   to FTS5.
3. **Discriminative metadata:** apply query-aligned structured filters, then
   rank the surviving content with FTS5.

The point is not that more metadata is always better. The point is that
accurate fields such as project, entity, record kind, status, temporal
validity, and provenance can separate records whose wording looks equally
relevant.

## Run It

Requires Python 3 with SQLite FTS5, which is included in standard Python builds
on common current platforms.

```sh
python3 -B metadata_retrieval_demo.py compare
python3 -B metadata_retrieval_demo.py ablate
python3 -B metadata_retrieval_demo.py buckets
python3 -B metadata_retrieval_demo.py query q01_atlas_renewal_decision
```

The comparison reports:

- `Hit@1`: queries whose first result is relevant.
- `Recall@3`: expected records found in the first three results.
- `MRR`: mean reciprocal rank of the first relevant result.
- `Avg candidates`: matching records before the top-three cutoff.
- `Filter FN`: relevant records visible to content search but removed by
  structured filters.

The ablation command removes one filter group at a time. This shows which
fields shrink the candidate set and where incomplete metadata can hurt recall.
The buckets command shows the same core retrieval measures for each recurring
kind of failure, so a good overall average cannot hide one consistently weak
category.

For the included 35 records and 12 queries, the measured comparison is:

| Mode | Hit@1 | Recall@3 | MRR | Average candidates | Filter false negatives |
| --- | ---: | ---: | ---: | ---: | ---: |
| Content only | 0.500 | 0.917 | 0.715 | 12.00 | 0 |
| Broad metadata | 0.667 | 1.000 | 0.833 | 13.08 | 0 |
| Discriminative metadata | 0.917 | 0.917 | 0.917 | 1.00 | 1 |

The discriminative path substantially improves first-result accuracy and
shrinks the candidate set. Its lower Recall@3 is not hidden: it comes from the
deliberately incomplete entity field described below.

## The Deliberate Failure

One synthetic Cedar security record mentions the fictional stakeholder Morgan
Lee in its text but is missing its normalized `entity` value. Content search
can find it. A strict `entity=morgan_lee` filter removes it.

That failure is intentional: discriminative metadata helps only when it is
complete and reliable. Explicit security or workspace boundaries should remain
hard filters; uncertain inferred fields are safer as ranking boosts or fallback
signals. The ablation makes this visible: removing the incomplete entity filter
restores perfect retrieval on these fixtures, while increasing the average
candidate set from 1.00 to 1.25.

## Refine Metadata from Failure Buckets

A useful metadata schema is rarely designed perfectly on the first attempt.
It gets better when real retrieval mistakes are turned into repeatable tests.
This example uses a simple refinement loop:

1. **Capture** the wrong result, missed result, or unnecessarily large result
   set.
2. **Classify** the mistake into a failure bucket that describes what went
   wrong.
3. **Fixture** it as a small synthetic query with known correct records.
4. **Refine** only the metadata or retrieval rule that should separate that
   case.
5. **Regress** the affected bucket and the full suite before keeping the
   change.

Each evaluation query has one primary `failure_bucket`. The included buckets
are deliberately described in user-facing language:

| Failure bucket | What it protects against |
| --- | --- |
| Wrong scope or entity | The right words appear in the wrong workspace, project, or entity. |
| Wrong record type | A proposal, meeting, or task is mistaken for a decision or fact. |
| Wrong time or stale version | An old record or out-of-period event outranks current truth. |
| Wrong lifecycle state | Closed, draft, or completed work is returned instead of open or approved work. |
| Conflicting provenance | A draft or discussion looks as authoritative as the requested source. |
| Missing or inconsistent metadata | A useful filter removes the right record because its value is empty or normalized differently. |
| Multiple correct results | A question needs a complete set, not just one top answer. |

Run `python3 -B metadata_retrieval_demo.py buckets` to see Hit@1, Recall@3,
MRR, average candidate-set size, and filter-induced false negatives for each
category.

### Avoid Overfitting the Buckets

Failure fixtures are regression evidence, not proof that retrieval is solved.
If every refinement is tuned only to the existing examples, the schema can
memorize those examples without learning a reusable distinction. Add fresh or
held-out synthetic cases over time, and keep a metadata change only when it
improves the target bucket without creating unacceptable false negatives in
another bucket or in the full suite.

## Retrieval Boundary

Each evaluation query contains normalized `search_terms` and `filters`. This
keeps the benchmark deterministic and isolates retrieval behavior. Translating
natural language into those filters is a separate system with its own accuracy
and fallback requirements.

All data is synthetic. The demo:

- uses Python's standard library only;
- creates the database in memory;
- makes no model or network calls;
- writes no runtime state; and
- contains no real assistant, employee, customer, or connector data.

## Files

- `metadata_retrieval_demo.py`: schema, retrieval modes, metrics, ablations,
  and CLI.
- `examples/context_items.jsonl`: deliberately confusable synthetic records.
- `examples/eval_queries.jsonl`: questions, primary failure buckets, normalized
  filters, and expected records.
- `tests/test_metadata_retrieval_demo.py`: deterministic behavior and failure
  checks.

## Quality Checks

```sh
python3 -B metadata_retrieval_demo.py --self-test
python3 -B -m unittest discover -s tests -v
python3 -B -m py_compile metadata_retrieval_demo.py tests/test_metadata_retrieval_demo.py
```

## Scope

This is a small retrieval experiment, not a production Chief of Staff, query
parser, vector database, access-control system, or claim that every metadata
filter should be strict. It demonstrates one practical design rule and exposes
its failure mode.
