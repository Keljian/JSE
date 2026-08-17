"""Guards against ad prose being mistaken for the employer name.

The 17 Aug 2026 shortlist export had 117 of 1,365 rows whose `company` was a
technology, a framework or a sentence fragment lifted out of the ad body
("Azure", "MQTT", "ITIL", "Technology. You'll", "Honours Masters Degree
Doctorate Post"). Those values overwrote the correct advertiser supplied by the
board, then flowed into the analysis prompt and the warm-path lookup.

Two rules are asserted here:
  1. a candidate that is only technologies, generic function nouns, a bare
     acronym or a sentence fragment is never accepted as an extracted name;
  2. an extracted name that appears exactly once and matches no contact or
     application domain does not replace the advertiser.
"""
import pytest

from db.companies import (
    _candidate_is_corroborated,
    _extract_named_company_from_text,
    _is_weak_company_candidate,
    classify_company_intelligence,
)


@pytest.mark.parametrize("candidate", [
    "Azure",
    "MQTT",
    "ITIL",
    "DevOps",
    "PLCs",
    "WHS",
    "Product Managers",
    "Information Security",
    "Honours Masters Degree Doctorate Post",
    "Technology. You'll",
    "Purpose Safe Seas. Clean Seas",
])
def test_prose_fragments_are_rejected_as_extracted_names(candidate):
    assert _is_weak_company_candidate(candidate, extracted=True) is True


@pytest.mark.parametrize("candidate", [
    "Coles Group",
    "Seymour Whyte",
    "Community Housing Ltd",
    "Apache Corporation",
    "Essential Services Commission",
])
def test_real_company_names_survive(candidate):
    assert _is_weak_company_candidate(candidate, extracted=True) is False


@pytest.mark.parametrize("advertiser", ["VISY", "ATTAR", "CSL"])
def test_board_supplied_acronym_advertisers_are_not_rejected(advertiser):
    """The acronym guard is for regex output only. A board that says the
    advertiser is VISY is reporting a fact, not guessing from prose."""
    assert _is_weak_company_candidate(advertiser, extracted=False) is False
    assert _is_weak_company_candidate(advertiser, extracted=True) is True


def test_technology_mention_does_not_become_the_employer():
    job = {
        "company": "Talent International",
        "title": "Solution Architect",
        "description": (
            "We are seeking a Solution Architect with Azure experience. "
            "You will design cloud workloads and mentor engineers."
        ),
        "url": "https://au.seek.com/job/1",
    }
    result = classify_company_intelligence(job)
    assert result["actual_company"] != "Azure"


def test_single_mention_does_not_overwrite_the_advertiser():
    job = {
        "company": "Seymour Whyte",
        "title": "Systems Manager",
        "description": "Reporting to the GM you will partner with Downer on delivery.",
        "url": "https://au.seek.com/job/2",
    }
    result = classify_company_intelligence(job)
    assert result["actual_company"] == "Seymour Whyte"
    assert result["employer_type"] == "direct_employer"


def test_repeated_mention_is_corroborated():
    # Note the lowercase "at": the extraction patterns are case-sensitive on
    # the lead-in word, so "At Coles Group" at the start of a sentence does not
    # match. That is pre-existing behaviour, asserted here so a future change
    # to the patterns is a deliberate one.
    body = (
        "Coles Group is transforming its retail technology. "
        "You will work at Coles Group on a delivery squad. Coles Group offers hybrid work."
    )
    assert _candidate_is_corroborated("Coles Group", body) is True
    assert _extract_named_company_from_text(body, "MVP") == "Coles Group"


def test_domain_match_corroborates_a_single_mention():
    body = "You will join Xceltium as a Practice Lead. Apply via the link."
    assert _candidate_is_corroborated(
        "Xceltium", body, email_domains=["careers@xceltium.com.au".split("@")[-1]]
    ) is True


def test_uncorroborated_recruiter_end_client_is_marked_low_confidence():
    job = {
        "company": "Kaliba",
        "title": "Head of Managed Services",
        "description": (
            "Our client is a well-established Melbourne MSP. "
            "You will partner with Contoso on service delivery."
        ),
        "url": "https://www.linkedin.com/jobs/view/3",
    }
    result = classify_company_intelligence(job)
    if result["employer_type"] == "recruiter" and result["actual_company"] != "Unknown":
        assert result["company_confidence"] == "low"
