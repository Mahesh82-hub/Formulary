"""The unverified-figure check, pinned to lines from real answers.

Each case is taken from an answer a user received, with the evidence the tools returned for
it (trimmed). The invented figures must be flagged and the genuine ones must not.
"""

from app.services.grounding import ungrounded_numbers, unverified_note

CEFTIOFUR_EVIDENCE = [
    "Pharmacokinetic and pharmacodynamic characterization of ceftiofur crystalline-free acid "
    "following subcutaneous administration in domestic goats. In vitro plasma protein "
    "binding of ceftiofur was 46.6%. " + "Unrelated values: dose 6.6 mg/kg, 0.45 h lag. " * 5,
    "Comparative plasma and interstitial fluid pharmacokinetics and tissue residues of "
    "ceftiofur crystalline-free acid in cattle with induced coliform mastitis. The objectives "
    "were to compare plasma protein binding of CEF between healthy and diseased cows. "
    + "Filler text about study design and sampling times. "
    * 30,
]
CEFTIOFUR_ANSWER = """| Species | % bound | fu,p | Source |
|---|---|---|---|
| Goat (in-vitro plasma) | 46.6 % bound | 53.4 % unbound (1 - 0.466 = 0.534) | PubMed 27943295 |
| Cattle (healthy & mastitic cows) | ≈ 45 %-55 % bound | ≈ 45 %-55 % unbound | PubMed 29971798 |
| Other livestock | ~50 % bound | ≈ 50 % unbound | PubMed 28731206, 26872361 |"""


def test_invented_protein_binding_values_are_flagged_and_real_ones_are_not() -> None:
    flagged = ungrounded_numbers(
        CEFTIOFUR_ANSWER, CEFTIOFUR_EVIDENCE, question="fraction unbound for ceftiofur"
    )

    assert flagged == ["45", "55", "50"]


MESALAMINE_EVIDENCE = [
    "The aqueous solubilities at 25 and 37 degrees C were 0.844 and 1.41 mg/mL, respectively. "
    "Consistent with the observed pKa1 (2.30) and pKa2 (5.69) of mesalamine, the solubility-pH "
    "profile showed minimal solubility between pH 2 and 5.5. " + "Other study text. " * 100,
]
MESALAMINE_ANSWER = """| pH | Solubility trend | Comments |
|---|---|---|
| 1.2 | High, comparable to aqueous solubility (≈ 0.84 mg/mL at 25 °C) | pKa 2.30 |
| 4.5 | Low, well below 0.5 mg/mL | zwitterion between pKa 2.30 and 5.69 |
| 6.8 | Rising | above the second pKa 5.69 |
Overall aqueous solubility: 0.844 mg/mL (25 °C) and 1.41 mg/mL (37 °C); high at pH 6.8-7.4."""


def test_values_invented_to_fill_a_table_are_flagged() -> None:
    flagged = ungrounded_numbers(MESALAMINE_ANSWER, MESALAMINE_EVIDENCE, question="mesalamine")

    # 0.84 is 0.844 rounded; pH values and temperatures are conditions, not data.
    assert flagged == ["0.5"]


def test_an_explicit_calculation_from_grounded_inputs_is_not_flagged() -> None:
    answer = (
        "pKa 12.4. Fraction protonated = 1 / (1 + 10^(7.4 - 12.4)) = 0.99999, "
        "so >99.99 % is protonated at pH 7.4."
    )

    flagged = ungrounded_numbers(
        answer, ["The pKa of metformin is 12.4."], question="metformin at pH 7.4"
    )

    assert flagged == []


def test_unit_conversions_and_rounding_of_source_values_are_accepted() -> None:
    answer = "Aqueous solubility is about 0.060 mg/mL, i.e. 60 µg/mL."

    flagged = ungrounded_numbers(
        answer, ["olaparib aqueous solubility was 0.0601 mg/mL in water"], question="olaparib"
    )

    assert flagged == []


def test_a_compact_tool_result_needs_no_matching_context_words() -> None:
    assert (
        ungrounded_numbers(
            "2500 micrograms equals 2.5 milligrams.",
            ['{"value": 2.5, "unit": "mg"}'],
            question="Convert 2500 mcg to mg",
        )
        == []
    )


def test_identifiers_years_and_listed_sources_are_ignored() -> None:
    answer = (
        "Approved in 2021 under NDA 209637 (PMID 12345678, NCT01234567).\n\n"
        "**Sources**\n1. [A 2019 study of 3,400 patients](https://example.test/1)"
    )

    assert ungrounded_numbers(answer, [], question="") == []


def test_the_note_names_the_unverified_figures() -> None:
    note = unverified_note(["45", "55"])

    assert "Unverified figures:** 45, 55" in note
    assert unverified_note([]) == ""


def test_invisible_characters_and_implied_drug_names_do_not_cause_false_alarms() -> None:
    """A live answer wrote "pK​a₂" and never repeated the drug name in its table row."""
    evidence = [
        "The aqueous solubility of metformin (pKa: 2.8 and 11.5) in the pH range of 1.2-6.8 is "
        "300 mg/mL. " + "Unrelated filler about formulation and manufacturing. " * 40
    ]
    answer = "| **pK​a₂** (second basic centre) | 11.5 |\n| pKa₁ (first basic centre) | 2.8 |"

    assert (
        ungrounded_numbers(answer, evidence, question="What is the pKa of metformin at pH 7.4?")
        == []
    )


def test_dye_names_ph_lists_and_restated_calculations_are_not_flagged() -> None:
    evidence = ['{"inactive_ingredients": ["corn starch", "FD&C red no. 40 aluminum lake"]}']
    assert ungrounded_numbers("Contains FD&C Red No. 40 aluminum lake.", evidence) == []

    listed = "Not reported at pH 1, 6.4, 7.2 or other points such as pH 4.5, 5.5 and 8.0."
    assert ungrounded_numbers(listed, ["filler"], question="mesalamine") == []

    answer = (
        "Fraction protonated = 1 / (1 + 10^(7.4 - 12.4)) = 0.99999\n"
        "So about 99.999 % of metformin is protonated."
    )
    assert ungrounded_numbers(answer, ["The pKa of metformin is 12.4."], question="pH 7.4") == []


def test_an_invented_value_cannot_launder_itself_through_an_equation() -> None:
    answer = "fu = 0.45 (estimated)\nThe fraction unbound is 0.45."

    assert ungrounded_numbers(answer, ["Filler with no numbers."], question="ceftiofur") == ["0.45"]


def test_a_value_is_judged_where_it_is_introduced_not_on_every_restatement() -> None:
    """A live olaparib answer stated 0.060 mg/mL twice and a wrong 0.14 uM (it is 0.14 mM)."""
    evidence = [
        "Olaparib (OLA), a poorly water-soluble anticancer drug (0.0601 mg/mL) with limited oral "
        "bioavailability. " + "Unrelated: class IV drugs showed 14 % variability. " * 30
    ]
    answer = (
        "- Olaparib's measured water solubility is 0.060 mg/mL (about 0.14 uM).\n"
        "- Consistent with peer-reviewed literature on BCS class and low permeability, "
        "the 0.060 mg/mL and 0.14 uM figures support class IV."
    )

    assert ungrounded_numbers(answer, evidence, question="olaparib solubility and BCS") == ["0.14"]


def test_latex_working_counts_as_a_calculation() -> None:
    answer = (
        "The pKa of metformin is 12.4.\n"
        "\\\\text{Fraction BH+} \\\\approx \\\\frac{1}{1 + 1\\\\times10^{-5}} \\\\approx 0.99999"
    )

    assert ungrounded_numbers(answer, ["The pKa of metformin is 12.4."], question="pH 7.4") == []
