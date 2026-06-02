"""
gap_extractor.py — Extract knowledge gaps from PNU (Problem-Need-Use)
templates, score them by Value of Information (VOI), and generate
AI Citation + Boolean query pairs for each gap.

Gaps are rooted in the daylight-and-cognition / built-environment domain
covered by COGS 160. Each gap has a unique query pair derived from its
specific P, N, and U components.

Usage:
    python gap_extractor.py              # writes gap_results.json + query_results.json
    python gap_extractor.py --help
    python gap_extractor.py --dry-run    # print without writing files
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ── PNU templates ─────────────────────────────────────────────────────────────
# Each entry: (P)henomenon, (N)uisance/confound, (U)se context, base_voi_score
PNU_TEMPLATES = [
    (
        "natural daylight exposure",
        "artificial circadian disruption",
        "open-plan office environments",
        92,
        "What are the empirically validated thresholds at which natural daylight exposure "
        "improves sustained attention in open-plan offices, controlling for circadian disruption effects? "
        "What longitudinal evidence exists for causal mechanisms?",
        '"daylight exposure" AND "sustained attention" AND ("open-plan" OR "open plan")',
    ),
    (
        "biophilic design elements",
        "soundscape noise confounds",
        "university classroom settings",
        88,
        "What direct empirical evidence links biophilic design elements to cognitive performance "
        "outcomes in university classrooms, independent of ambient soundscape noise? "
        "Which outcome measures are most sensitive to biophilic interventions?",
        '"biophilic design" AND "cognitive performance" AND ("classroom" OR "learning environment")',
    ),
    (
        "correlated colour temperature (CCT) of lighting",
        "thermal comfort variation",
        "secondary school learning environments",
        85,
        "How does correlated colour temperature independently affect reading comprehension "
        "and working memory in secondary school classrooms when thermal comfort is held constant? "
        "What CCT range produces peak cognitive outcomes?",
        '"colour temperature" AND ("reading comprehension" OR "working memory") AND "classroom"',
    ),
    (
        "window-to-floor ratio",
        "glare and visual discomfort",
        "knowledge-worker office buildings",
        82,
        "What is the empirically supported optimal window-to-floor ratio for knowledge workers "
        "that maximises cognitive alertness while controlling for glare-induced visual discomfort? "
        "Are there interaction effects with façade orientation?",
        '"window-to-floor ratio" AND ("glare" OR "visual discomfort") AND "cognitive" AND "office"',
    ),
    (
        "indoor plant density (biophilia)",
        "maintenance disruption and distraction",
        "corporate workplace wellness programs",
        79,
        "Does empirical evidence support a dose-response relationship between indoor plant density "
        "and self-reported stress or cognitive restoration in corporate workplaces, "
        "independent of maintenance disruption? What plant species or densities are validated?",
        '"indoor plants" AND ("stress reduction" OR "cognitive restoration") AND ("workplace" OR "office")',
    ),
    (
        "dynamic lighting control systems",
        "occupant adaptation and habituation",
        "hospital nursing ward environments",
        76,
        "What rigorous studies examine whether dynamic lighting control reduces nursing staff "
        "cognitive fatigue in hospital wards after controlling for occupant habituation to "
        "lighting changes? Are circadian and task-lighting effects separable?",
        '"dynamic lighting" AND ("cognitive fatigue" OR "alertness") AND ("hospital" OR "healthcare")',
    ),
    (
        "outdoor view access from workspace",
        "content and distance of view",
        "remote knowledge worker home offices",
        73,
        "How does quantified outdoor view access from a home office window affect "
        "attentional restoration and productivity, controlling for the content and "
        "distance of the view? What objective or validated subjective measures are used?",
        '"view from window" AND ("attention restoration" OR "productivity") AND ("home office" OR "remote work")',
    ),
    (
        "thermal preference regulation",
        "seasonal acclimatisation variation",
        "mixed-mode naturally ventilated buildings",
        70,
        "What empirical mechanisms explain why thermal preference self-regulation improves "
        "occupant cognitive comfort in mixed-mode buildings after accounting for "
        "seasonal acclimatisation effects? What are the boundary conditions?",
        '"thermal preference" AND "cognitive comfort" AND ("mixed-mode" OR "natural ventilation")',
    ),
    (
        "acoustic absorption and reverberation time",
        "speech intelligibility masking",
        "elementary school open-plan classrooms",
        67,
        "What is the empirically supported reverberation time target for elementary school "
        "open-plan classrooms that optimises reading and numeracy outcomes while "
        "controlling for background noise and speech intelligibility masking?",
        '"reverberation time" AND ("speech intelligibility" OR "reading") AND "open-plan" AND "school"',
    ),
    (
        "green roof and living wall systems",
        "building thermal load confounds",
        "urban high-density residential buildings",
        63,
        "Does empirical evidence demonstrate that green roof and living wall systems "
        "independently reduce occupant stress and improve cognitive well-being in dense "
        "urban residential settings beyond their documented thermal load reduction effects?",
        '"green roof" OR "living wall" AND ("stress" OR "well-being") AND ("urban" OR "residential")',
    ),
    (
        "daylighting simulation accuracy",
        "occupant behaviour variability",
        "post-occupancy evaluation research",
        60,
        "How accurately do current daylighting simulation tools predict occupant-measured "
        "illuminance and cognitive alertness outcomes in post-occupancy evaluations, "
        "and where does occupant behaviour variability introduce the largest prediction gaps?",
        '"daylighting simulation" AND ("post-occupancy" OR "occupant behaviour") AND ("accuracy" OR "validation")',
    ),
    (
        "personal control over workstation lighting",
        "peer influence and social norms",
        "shared open-plan knowledge work spaces",
        57,
        "What empirical studies isolate the cognitive and affective benefits of personal "
        "control over workstation lighting in shared open-plan offices from confounding "
        "peer influence and social norm effects on lighting settings?",
        '"personal lighting control" AND ("cognition" OR "mood" OR "productivity") AND "open-plan"',
    ),
    (
        "spatial daylight autonomy (sDA) metrics",
        "temporal distribution and occupancy mismatch",
        "educational building certification schemes",
        54,
        "Are spatial daylight autonomy metrics in green building certification schemes "
        "predictive of actual occupant cognitive performance outcomes, or does temporal "
        "distribution mismatch between rated hours and occupancy invalidate the metric?",
        '"spatial daylight autonomy" AND ("cognitive" OR "occupant satisfaction") AND ("certification" OR "LEED" OR "BREEAM")',
    ),
    (
        "melanopsin-mediated non-visual light response",
        "individual chronotype variation",
        "shift-work industrial environments",
        50,
        "What direct evidence quantifies how melanopsin-mediated non-visual light responses "
        "affect shift worker cognitive performance and error rates in industrial settings, "
        "after controlling for individual chronotype variation and sleep debt?",
        '"melanopsin" AND ("non-visual" OR "circadian") AND ("shift work" OR "shift-work") AND "cognitive"',
    ),
]


def build_gaps_and_queries() -> tuple[list[dict], list[dict]]:
    gaps: list[dict] = []
    queries: list[dict] = []

    for i, (p, n, u, base_voi, ai_query, bool_query) in enumerate(PNU_TEMPLATES, 1):
        pnu_id = f"PNU-{i:03d}"
        gap_id = f"GAP-{pnu_id}"
        description = (
            f"Investigating the direct causal linkages and boundary conditions "
            f"missing between {p} and {n} specifically within {u}."
        )
        gaps.append(
            {
                "gap_id": gap_id,
                "source_pnu": pnu_id,
                "phenomenon": p,
                "nuisance_confound": n,
                "use_context": u,
                "voi_score": float(base_voi),
                "description": description,
            }
        )
        queries.append(
            {
                "gap_id": gap_id,
                "ai_citation_query": ai_query,
                "boolean_query": bool_query,
            }
        )

    # Sort gaps by VOI descending
    gaps.sort(key=lambda g: g["voi_score"], reverse=True)
    return gaps, queries


def run_extraction(*, dry_run: bool = False) -> None:
    gaps, queries = build_gaps_and_queries()

    if dry_run:
        print("=== gap_results.json (preview) ===")
        print(json.dumps(gaps[:3], indent=2))
        print(f"\n... {len(gaps)} gaps total")
        print("\n=== query_results.json (preview) ===")
        print(json.dumps(queries[:3], indent=2))
        print(f"\n... {len(queries)} query pairs total")
        return

    Path("gap_results.json").write_text(
        json.dumps(gaps, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    Path("query_results.json").write_text(
        json.dumps(queries, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"gap_extractor: wrote {len(gaps)} gaps to gap_results.json")
    print(f"gap_extractor: wrote {len(queries)} query pairs to query_results.json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Extract knowledge gaps from PNU templates and generate "
            "AI Citation + Boolean query pairs for Google Scholar search."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print a preview without writing output files",
    )
    args = parser.parse_args()
    run_extraction(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
