# Bioequivalence workspace

Formulary separates general FDA research from deterministic test/reference comparison.

## Product modes

### FDA research chat

The chat searches FDA sources, durably ingests large result sets, retrieves bounded evidence,
and answers general regulatory or drug questions with provenance. For quantitative requests it
collects the drug, product, formulation, route, strength, study condition, and intended comparison
before retrieval. It must not predict candidate pharmacokinetic values.

### Exploratory comparison

This mode compares source-linked FDA reference evidence with independently supplied company test
values. It calculates and plots test/reference point ratios, but always returns
`exploratory_only`. It cannot establish bioequivalence because the values may not come from a
matched test/reference study.

### Formal summary check

This mode accepts company-supplied geometric means and the associated 90% confidence interval for
each required endpoint. The deterministic engine calculates the point ratio and checks whether
the complete supplied interval lies inside the configured limits. With the default unscaled
average-BE rule:

```text
test/reference geometric mean ratio = test geometric mean / reference geometric mean × 100
endpoint passes when CI lower ≥ 80.00 and CI upper ≤ 125.00
```

The default required endpoints are `Cmax` and `AUC0-t`. FDA product-specific guidance can require
different endpoints, study conditions, analytes, or statistical approaches. The current engine
therefore describes its result as meeting the configured criteria, not as FDA approval or an
independent validation of the study.

FDA guidance: <https://www.fda.gov/media/192774/download>

## Concentration-time profiles

Reference and test curves are optional. Points must have strictly increasing non-negative times
and non-negative concentrations. The service derives descriptive values using:

- `Cmax`: maximum supplied concentration
- `Tmax`: time of the maximum supplied concentration
- `AUC0-last`: linear trapezoidal area across the supplied points

Curve-derived summaries support visualization. They do not replace subject-level endpoint
analysis or confidence intervals.

## API and MCP boundaries

- `POST /api/v1/bioequivalence/analyze` is the authenticated workspace API.
- `analyze_bioequivalence_summary` exposes the same deterministic service through internal MCP.
- `prepare_bioequivalence_evidence_request` prevents the chatbot from researching a quantitative
  comparison before high-impact context is available.

The API is stateless in the first vertical slice. Durable study projects, file upload, raw
subject-level noncompartmental analysis, and product-specific-rule resolution remain separate
implementation stages.

## Export policy

The first slice enables simulator-neutral JSON export only when all configured required endpoints
meet the supplied formal-summary criteria. The export contains the assessment, context,
provenance fields, caveats, and any supplied concentration profiles. Export is a user-controlled
research handoff and is not a safety, efficacy, or regulatory approval statement.
