"""International to US drug-name normalisation.

FDA data uses United States Adopted Names (USAN). Where the international non-proprietary name
(INN) differs, a search for the INN finds nothing at all: on 24 September 2026,
``openfda.generic_name:"PARACETAMOL"`` matched 0 labels while ``"ACETAMINOPHEN"`` matched
3,474. The model then reported that FDA had no data, which was false.

The table covers the well-known INN/USAN divergences. Both names are always searched, so an
entry can only widen a search, never lose a match.
"""

from __future__ import annotations

INN_TO_USAN: dict[str, str] = {
    "paracetamol": "acetaminophen",
    "salbutamol": "albuterol",
    "adrenaline": "epinephrine",
    "noradrenaline": "norepinephrine",
    "lignocaine": "lidocaine",
    "frusemide": "furosemide",
    "glibenclamide": "glyburide",
    "pethidine": "meperidine",
    "ciclosporin": "cyclosporine",
    "rifampicin": "rifampin",
    "aciclovir": "acyclovir",
    "valaciclovir": "valacyclovir",
    "amoxycillin": "amoxicillin",
    "cefalexin": "cephalexin",
    "colecalciferol": "cholecalciferol",
    "dexamfetamine": "dextroamphetamine",
    "methylthioninium chloride": "methylene blue",
    "orciprenaline": "metaproterenol",
    "isoprenaline": "isoproterenol",
    "chlorphenamine": "chlorpheniramine",
    "dicycloverine": "dicyclomine",
    "hyoscine": "scopolamine",
    "mesalazine": "mesalamine",
    "beclometasone": "beclomethasone",
    "oxybuprocaine": "benoxinate",
    "procaine benzylpenicillin": "penicillin g procaine",
    "benzylpenicillin": "penicillin g",
    "phenoxymethylpenicillin": "penicillin v",
    "sodium cromoglicate": "cromolyn sodium",
    "cromoglicate": "cromolyn",
    "clomifene": "clomiphene",
    "oestradiol": "estradiol",
    "ethinyloestradiol": "ethinyl estradiol",
    "thyroxine": "levothyroxine",
}
USAN_TO_INN: dict[str, str] = {usan: inn for inn, usan in INN_TO_USAN.items()}


def us_name(term: str) -> str | None:
    """The US adopted name for an international name, if they differ."""
    return INN_TO_USAN.get(" ".join(term.casefold().split()))


def search_names(term: str) -> list[str]:
    """Every name worth searching for a drug: the term as given, plus its US or INN twin."""
    cleaned = " ".join(term.split())
    if not cleaned:
        return []
    names = [cleaned]
    key = cleaned.casefold()
    for twin in (INN_TO_USAN.get(key), USAN_TO_INN.get(key)):
        if twin and twin.casefold() not in {name.casefold() for name in names}:
            names.append(twin)
    return names


def rewrite_words(text: str) -> str:
    """Replace international names inside free text with their US names."""
    words = text.split()
    rewritten = [INN_TO_USAN.get(word.casefold(), word) for word in words]
    return " ".join(rewritten)

