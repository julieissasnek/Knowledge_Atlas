# VOI Comparison: Track 2 Heuristic vs Article Eater / BN Machinery

**Student:** Julie Issasnek  
**Course:** COGS 160 — Track 2, Task 2/3  
**Date:** 2026-06-04

---

## What This Document Does

The instructor's review noted that the Track 2 VOI score is a useful first-pass heuristic but does not represent the richer VOI machinery already in Article Eater and the BN code.  This document explains the difference honestly, states what the Track 2 score is and is not, and shows what a richer future extension would look like.

---

## 1. Track 2 VOI — What It Actually Is

The VOI scores in `gap_extractor.py` are **domain-expert pre-rankings** embedded directly in the PNU template list.

```python
# From gap_extractor.py — each PNU template carries a fixed priority score
PNU_TEMPLATES = [
    ("natural daylight exposure", "artificial circadian disruption",
     "open-plan office environments",  92,  ...),   # ← this is the VOI score
    ("biophilic design elements", "soundscape noise confounds",
     "university classroom settings",  88,  ...),
    ...
    ("melanopsin-mediated non-visual response", "chronotype variation",
     "shift-work industrial environments",  50,  ...),
]
```

**What this captures:**
- Domain salience: how central the mechanism is to the daylight-and-cognition literature
- Implicit evidence sparsity: higher-VOI gaps have less prior coverage

**What it does NOT capture:**
- Evidence contestation (conflicting findings)
- Network centrality in a causal graph
- Downstream impact (how many other claims depend on this one)
- Study feasibility
- Structural VOI vs epistemic VOI distinction
- Expected posterior change after acquiring a paper

**Correct description:** *A domain-expert retrieval-priority ranking. Useful for ordering search targets. Not a full decision-theoretic VOI calculation.*

---

## 2. Article Eater VOI — Structural + Epistemic Split

File reviewed by instructor: `Article_Eater_PostQuinean_v1/src/services/voi_search.py`

AE separates two orthogonal components:

| Component | What it measures |
|-----------|-----------------|
| `structural_voi` | Value from filling a structural gap: centrality + sparsity of supporting evidence |
| `epistemic_voi` | Value from reducing uncertainty: weighted by belief importance in the causal network |
| `gap_type` | Direction / validation / mechanism / boundary — each gets different priority weights |
| `combined_voi` | Alpha-weighted sum; alpha depends on gap type |

**Why the split matters:**  A gap can be high structural VOI (central node in the network, little evidence) but low epistemic VOI (high current confidence anyway).  The Track 2 scalar collapses both into one number.

---

## 3. BN Graphical Opportunity Scorer — Five Dimensions

File reviewed by instructor: `BN_graphical/src/literature_integration/opportunity_scorer.py`

```python
# Default weights (from instructor review notes)
gap_severity:      0.25   # how little evidence exists
contestation:      0.20   # how conflicting the evidence is
centrality:        0.25   # importance of the edge in the causal network
downstream_impact: 0.20   # how many downstream nodes depend on this edge
feasibility:       0.10   # how practical the study is
```

**Cases the Track 2 scalar cannot distinguish:**

| Scenario | Track 2 behaviour | BN scorer behaviour |
|----------|------------------|---------------------|
| Uncertain but peripheral gap | Ranked by domain-expert score | Low centrality → low priority |
| Central, already well-supported | Ranked high | Low gap_severity → lower priority |
| Contested (conflicting studies) | Treated same as sparse gap | High contestation → high priority |
| Large downstream consequences | No signal | High downstream_impact → bumped up |
| Important but hard to study | No signal | Low feasibility → discounted |

---

## 4. Article Eater Active Learning Coordinator

File reviewed by instructor: `Article_Eater_PostQuinean_v1/src/services/active_learning_coordinator.py`

Creates `ActiveLearningGap` records with fields the Track 2 pipeline does not produce:

```
bn_uncertainty       — current uncertainty of the BN edge
bn_estimate          — current point estimate
credible_interval    — posterior credible interval
voi_score            — combined VOI from voi_search.py
structural_voi       — separate structural component
epistemic_voi        — separate epistemic component
priority             — final priority including feasibility
search_terms         — auto-generated search terms from gap
n_supporting_papers  — number of papers currently supporting the edge
```

---

## 5. What the Bayesian VOI Service Adds (Decision-Theoretic Level)

File reviewed by instructor: `Article_Eater_PostQuinean_v1/docs/BAYESIAN_VOI_SERVICE_IMPLEMENTATION.md`

The full Bayesian VOI frames search value as:

```
prior credence           → uncertainty before acquiring the paper
likelihood by design     → RCT vs observational vs meta-analysis
expected information gain → KL divergence between prior and posterior
expected utility gain    → downstream impact weighted by decision stakes
expected posterior change → how much the BN edge estimate would shift
```

This is the correct conceptual target for an autonomous literature-search system.  It is beyond the scope of Track 2.

---

## 6. Honest Position of Track 2 VOI

| Claim | Accurate? |
|-------|-----------|
| "Useful first-pass ranking for which gaps to search first" | ✅ Yes |
| "Represents the full Article Eater / BN VOI" | ❌ No |
| "Captures evidence contestation" | ❌ No |
| "Separates structural from epistemic value" | ❌ No |
| "Can drive enterprise article-acquisition priorities without manual review" | ❌ No — requires review |

The Track 2 VOI should be used as **Stage 1 filter: which gaps to search at all**.  It should not be used as the final prioritisation rule for which accepted papers to ingest into the BN.

---

## 7. Future Extension: `voi_breakdown` Object

The instructor's review proposes emitting a `voi_breakdown` per gap alongside the scalar `voi_score`.  This is the correct next step — it does not require implementing the full BN scorer, but it makes the gap between Track 2 and the full model explicit and machine-readable.

**Target structure (not yet implemented):**

```json
{
  "gap_id": "GAP-PNU-001",
  "voi_score": 92,
  "voi_breakdown": {
    "local_confidence_gap": 0.92,
    "evidence_sparsity": "high — <5 empirical RCTs found in harvest",
    "network_centrality": "not computed — would require BN graph access",
    "downstream_impact": "not computed — would require BN graph access",
    "contestation": "not computed — would require labeled finding set",
    "feasibility": "not computed",
    "structural_voi": "not computed — requires AE voi_search.py",
    "epistemic_voi": "not computed — requires AE active_learning_coordinator.py",
    "note": "Track 2 voi_score is a domain-expert priority rank. Fields marked 'not computed' require Article Eater / BN integration."
  }
}
```

To implement this properly: call `Article_Eater_PostQuinean_v1/src/services/voi_search.py` after harvest, pass the discovered paper set per gap, and populate the structural and epistemic fields from the AE VOI classes.

---

## 8. Implementation Roadmap (not required for Track 2 A)

| Sprint | Work | Prerequisite |
|--------|------|-------------|
| Done | Domain-expert priority ranking (this submission) | None |
| Next | Emit `voi_breakdown` with honest `not_computed` placeholders | None |
| Future | Gap-type-aware query selection (replication for contested gaps, primary for mechanism gaps) | Labeled finding set |
| Future | Structural + epistemic VOI from AE classes | AE repo access |
| Future | Full BN opportunity scoring | BN graph access |
