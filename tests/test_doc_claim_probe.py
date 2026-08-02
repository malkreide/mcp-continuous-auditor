#!/usr/bin/env python3
"""Tests for scripts/doc_claim_probe.py — do cited identifiers exist?

Every test builds a small repository in a temp dir: a document that makes
claims and a code tree that either backs them or does not. Nothing is mocked —
the probe is file reads and an ``ast`` walk, and there is no seam worth
inserting.

Half of these tests are about what the probe must NOT report. That ratio is
deliberate: the check is only useful if a red run means something, and a check
that flags `Requires-Dist`, a PEP number or a worked example teaches its
readers to skim past it.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import doc_claim_probe as dc  # noqa: E402


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.root / "scripts").mkdir()

    def code(self, name: str, text: str) -> None:
        (self.root / "scripts" / name).write_text(text, encoding="utf-8")

    def doc(self, text: str, name: str = "README.md") -> None:
        (self.root / name).write_text(text, encoding="utf-8")

    def probe(self, **kwargs) -> dc.Report:
        return dc.run(self.root, **kwargs)

    def codes(self, report: dc.Report) -> list[str]:
        return [f.code for f in report.findings]


class ResolutionTest(Case):
    def test_a_cited_constant_that_exists_resolves(self) -> None:
        self.code("gate.py", 'FINDING = "LOCK_DRIFT"\n')
        self.doc("The gate raises `LOCK_DRIFT` when the lock is behind.\n")
        report = self.probe()
        self.assertEqual(report.findings, [])
        self.assertEqual(report.exit_code(), dc.EXIT_GREEN)

    def test_a_cited_constant_that_does_not_exist_is_a_finding(self) -> None:
        self.code("gate.py", 'FINDING = "LOCK_DRIFT"\n')
        self.doc("The gate raises `LOCK_INVENTED` when the lock is behind.\n")
        report = self.probe()
        self.assertEqual(self.codes(report), ["UNRESOLVED_CLAIM"])
        self.assertEqual(report.findings[0].token, "LOCK_INVENTED")
        self.assertEqual(report.exit_code(), dc.EXIT_FINDINGS)

    def test_a_rubric_defined_in_yaml_resolves(self) -> None:
        """The resolution index is not a Python symbol table, and must not be.

        A promptfoo rubric lives in YAML and a check name in a shell script. A
        Python-only index would report every one of them as missing — a probe
        wrong about the very incident it was written for.
        """
        (self.root / "rubrics.yaml").write_text("- id: FID-003\n", encoding="utf-8")
        self.code("gate.py", "x = 1\n")
        self.doc("Fidelity is graded by `FID-003`.\n")
        self.assertEqual(self.probe().findings, [])

    def test_one_finding_per_identifier_however_often_it_is_repeated(self) -> None:
        self.code("gate.py", "x = 1\n")
        self.doc("`GHOST_CODE` here.\n\n`GHOST_CODE` again.\n\nAnd `GHOST_CODE`.\n")
        self.assertEqual(len(self.probe().findings), 1)


class NoiseTest(Case):
    """What the probe must stay silent about."""

    def setUp(self) -> None:
        super().setUp()
        self.code("gate.py", "x = 1\n")

    def test_packaging_and_http_vocabulary_is_not_an_identifier_claim(self) -> None:
        self.doc("It reads `Requires-Dist` and sets a `User-Agent` on every request.\n")
        self.assertEqual(self.probe().findings, [])

    def test_standards_citations_are_exempt(self) -> None:
        self.doc("Metadata is read over `PEP-658`; see also `RFC-6749` and `CVE-2024-3`.\n")
        self.assertEqual(self.probe().findings, [])

    def test_prose_outside_backticks_is_not_a_claim(self) -> None:
        """Only what the author marked as code counts.

        Without this the probe would be grepping English for capital letters,
        and every acronym in every sentence would become a finding.
        """
        self.doc("The NOT_A_REAL_CODE mentioned in passing is prose, not a citation.\n")
        self.assertEqual(self.probe().findings, [])

    def test_sample_output_in_a_fenced_block_is_illustration(self) -> None:
        self.doc(
            "Run it:\n\n"
            "```\n"
            "$ python scripts/gate.py\n"
            "GATE_RESULT_EXAMPLE: nothing found\n"
            "```\n")
        self.assertEqual(self.probe().findings, [])

    def test_an_identifier_beside_a_link_to_another_repo_is_reported_not_flagged(self) -> None:
        """`OPS-005` belongs to another repository. Listed, never resolved.

        Dropping it silently would be a blind spot; flagging it would be wrong.
        The report says the exemption was applied and to what.
        """
        self.doc(
            "| after the build | [audit-skill](https://github.com/o/audit-skill) | "
            "Its `OPS-005` (pipeline honesty) is the relevant rubric |\n")
        report = self.probe()
        self.assertEqual(report.findings, [])
        self.assertIn("README.md: OPS-005", report.external)
        self.assertIn("not resolved here", " ".join(report.notes))


class PathTest(Case):
    def test_a_path_in_a_command_example_must_exist(self) -> None:
        self.code("gate.py", "x = 1\n")
        self.doc("```bash\npython scripts/gate.py --target .\n```\n")
        self.assertEqual(self.probe().findings, [])

    def test_a_renamed_file_is_caught(self) -> None:
        self.code("gate.py", "x = 1\n")
        self.doc("```bash\npython scripts/old_name.py --target .\n```\n")
        report = self.probe()
        self.assertEqual(self.codes(report), ["UNRESOLVED_PATH"])
        self.assertEqual(report.findings[0].token, "scripts/old_name.py")

    def test_a_dotfile_path_keeps_its_leading_dot(self) -> None:
        """`.github/workflows/tests.yml`, not `github/workflows/tests.yml`.

        The first version of the token pattern dropped the dot and reported the
        truncated remainder as a dead path — a probe inventing its own finding.
        """
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "tests.yml").write_text("name: tests\n", encoding="utf-8")
        self.code("gate.py", "x = 1\n")
        self.doc("Run by `.github/workflows/tests.yml` on every push.\n")
        self.assertEqual(self.probe().findings, [])

    def test_a_path_inside_a_url_is_not_a_local_path(self) -> None:
        self.code("gate.py", "x = 1\n")
        self.doc("See https://github.com/o/r/blob/main/scripts/elsewhere.py for it.\n")
        self.assertEqual(self.probe().findings, [])


class MembershipTest(Case):
    """The ARCH-003 case: codes cited beside a collection they are not in."""

    GREEN = (
        "GREEN_RUBRICS = {\n"
        '    "ARCH-001",\n'
        '    "ARCH-002",\n'
        '    "ARCH-010",\n'
        "}\n"
        'OTHER = "ARCH-003"\n'
    )

    def test_a_code_cited_beside_the_collection_but_not_in_it(self) -> None:
        self.code("rubrics.py", self.GREEN)
        self.doc("The finding was graded green under `GREEN_RUBRICS`: `ARCH-003`.\n")
        report = self.probe()
        self.assertEqual(self.codes(report), ["NOT_A_MEMBER"])
        finding = report.findings[0]
        self.assertEqual(finding.token, "ARCH-003")
        # The members are printed: the reader must not have to open the file to
        # find out what the collection actually contains.
        self.assertIn("ARCH-001", finding.detail)
        # And it distinguishes "exists elsewhere" from "does not exist at all",
        # because those are two different corrections.
        self.assertIn("does exist elsewhere", finding.detail)

    def test_members_are_not_flagged(self) -> None:
        self.code("rubrics.py", self.GREEN)
        self.doc("Green under `GREEN_RUBRICS`: `ARCH-001` and `ARCH-002`.\n")
        self.assertEqual(self.probe().findings, [])

    def test_only_tokens_shaped_like_members_are_judged(self) -> None:
        """A paragraph may legitimately mention an unrelated constant.

        Membership is enforced against tokens that look like members; without
        that restraint every constant near a collection name would be a finding
        and the check would be muted within the week.
        """
        self.code("rubrics.py", self.GREEN + 'EXIT_GREEN = "EXIT_GREEN"\n')
        self.doc("`GREEN_RUBRICS` decides the grade; the process returns `EXIT_GREEN`.\n")
        self.assertEqual(self.probe().findings, [])

    def test_the_check_only_runs_where_the_collection_is_actually_named(self) -> None:
        self.code("rubrics.py", self.GREEN)
        self.doc("The finding was graded against `ARCH-003`.\n")
        self.assertEqual(self.probe().findings, [])

    def test_a_multiline_collection_is_read_whole(self) -> None:
        """`ast`, not a line reader.

        A line-based reader finds the first two members of a wrapped literal and
        reports the rest as non-members.
        """
        self.code("rubrics.py", self.GREEN)
        collections = dc.find_collections(
            [self.root / "scripts" / "rubrics.py"], self.root)
        self.assertEqual(collections["GREEN_RUBRICS"].members,
                         frozenset({"ARCH-001", "ARCH-002", "ARCH-010"}))

    def test_frozenset_and_tuple_spellings_are_seen_through(self) -> None:
        self.code("rubrics.py",
                  'A = frozenset({"X-001", "X-002"})\n'
                  'B = ("Y-001", "Y-002")\n')
        collections = dc.find_collections([self.root / "scripts" / "rubrics.py"], self.root)
        self.assertIn("A", collections)
        self.assertIn("B", collections)


class NotMeasuredTest(Case):
    def test_no_documentation_is_not_a_pass(self) -> None:
        self.code("gate.py", "x = 1\n")
        report = self.probe()
        self.assertEqual(report.findings, [])
        self.assertEqual(report.exit_code(), dc.EXIT_NOT_MEASURED)
        self.assertIn("nothing was measured", " ".join(report.notes))

    def test_no_code_is_a_harness_failure_not_a_wall_of_findings(self) -> None:
        """Every citation would be "unresolved", and none of it would be true.

        A report like that says something about the run, not about the docs.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("`SOME_CODE`\n", encoding="utf-8")
            report = dc.run(root)
            self.assertEqual(report.exit_code(), dc.EXIT_CANNOT_RUN)
            self.assertIn("no code files", report.harness_error)

    def test_ignore_takes_an_identifier_or_its_prefix(self) -> None:
        self.code("gate.py", "x = 1\n")
        self.doc("Rubrics `OPS-005` and `FID-003` come from the audit catalogue.\n")
        self.assertEqual(len(self.probe().findings), 2)
        self.assertEqual(self.probe(ignore=("OPS", "FID-003")).findings, [])


class OwnDocumentationTest(unittest.TestCase):
    """This repository's own READMEs, held to the check it ships.

    Both files cite a lot of identifiers — exit codes, finding codes, script
    paths — and every one of them is a claim that can go stale in a rename.
    """

    def test_every_identifier_and_path_the_readmes_cite_resolves(self) -> None:
        root = Path(__file__).resolve().parents[1]
        report = dc.run(root)
        self.assertTrue(report.docs)
        self.assertEqual(
            [f"{f.doc}:{f.line} {f.token} — {f.detail}" for f in report.findings], [],
            "the README cites something that no longer exists — "
            "run `python scripts/doc_claim_probe.py --target .`")


if __name__ == "__main__":
    unittest.main()
