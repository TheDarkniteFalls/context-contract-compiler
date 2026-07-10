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
- `examples/eval_queries.jsonl`: questions, normalized filters, and expected
  records.
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
