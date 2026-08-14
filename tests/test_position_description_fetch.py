"""Regression checks for finding a position description link on a job ad.

The PD carries the selection criteria the ad usually paraphrases, so attaching
it changes the analysis. Discovery is the fragile half: PD links are named
inconsistently and the href is often an opaque handler with no filename, so the
scoring has to survive real-world markup rather than a tidy `href="pd.pdf"`.
"""
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import documents  # noqa: E402


PAGEUP_AD = """
<html><body>
  <a href="/en/listing/">Back to search results</a>
  <a href="https://www.example.edu/privacy">Privacy statement</a>
  <p>For a copy of the position description, please see below:</p>
  <p><a href="https://secure.dc2.pageuppeople.com/apply/TransferRichTextFile.ashx?sData=abc123">
     PD - Solutions Architect.pdf</a></p>
  <a href="#apply-now">Apply now</a>
  <a href="mailto:p.cheng@example.edu">Email the hiring manager</a>
</body></html>
"""


class PositionDescriptionLinkTests(unittest.TestCase):
    def test_opaque_handler_link_wins_on_anchor_text(self):
        """The winning href has no .pdf in it — only the anchor text says PD."""
        candidates = documents._pd_link_candidates(PAGEUP_AD, "https://careers.example.edu/job/1")
        self.assertTrue(candidates)
        self.assertEqual(candidates[0]["text"], "PD - Solutions Architect.pdf")
        self.assertIn("TransferRichTextFile", candidates[0]["url"])

    def test_navigation_and_boilerplate_links_are_not_candidates(self):
        candidates = documents._pd_link_candidates(PAGEUP_AD, "https://careers.example.edu/job/1")
        urls = [item["url"] for item in candidates]
        self.assertNotIn("https://www.example.edu/privacy", urls)
        self.assertFalse([url for url in urls if url.endswith("/en/listing/")])
        self.assertFalse([url for url in urls if url.startswith("mailto:")])

    def test_relative_document_links_resolve_against_the_ad(self):
        html = '<a href="../files/Position Description.pdf">Download the role description</a>'
        candidates = documents._pd_link_candidates(html, "https://careers.example.gov.au/jobs/1234")
        self.assertEqual(
            candidates[0]["url"],
            "https://careers.example.gov.au/files/Position Description.pdf",
        )

    def test_a_bare_pdf_link_still_scores(self):
        """Not every ad labels the link; a document href alone is enough to try."""
        html = '<a href="https://jobs.example.com/assets/role.pdf">More information</a>'
        candidates = documents._pd_link_candidates(html, "https://jobs.example.com/1")
        self.assertTrue(candidates)

    def test_named_pd_outranks_an_unlabelled_pdf(self):
        html = (
            '<a href="https://jobs.example.com/assets/benefits.pdf">Our benefits</a>'
            '<a href="https://jobs.example.com/get?id=9">Position Description</a>'
        )
        candidates = documents._pd_link_candidates(html, "https://jobs.example.com/1")
        self.assertEqual(candidates[0]["text"], "Position Description")


class DispositionFilenameTests(unittest.TestCase):
    def test_quoted_filename(self):
        self.assertEqual(
            documents._pd_filename_from_disposition('attachment; filename="PD - Analyst.pdf"'),
            "PD - Analyst.pdf",
        )

    def test_rfc5987_filename(self):
        self.assertEqual(
            documents._pd_filename_from_disposition("attachment; filename*=UTF-8''PD%20Analyst.pdf"),
            "PD Analyst.pdf",
        )

    def test_missing_header(self):
        self.assertEqual(documents._pd_filename_from_disposition(""), "")


if __name__ == "__main__":
    unittest.main()
