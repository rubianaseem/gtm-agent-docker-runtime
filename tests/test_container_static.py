import re
import shutil
import tempfile
import unittest
from pathlib import Path

from tools import miniyaml
from tools.container_lint import (build_context, is_ignored, lint_compose, lint_dockerfile, lint_dockerignore,
                                  load_ignore, parse_dockerfile)

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "evals" / "fixtures"


class DockerfileTest(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "Dockerfile").read_text()
        self.ins = parse_dockerfile(self.text)

    def test_repo_dockerfile_has_no_findings(self):
        self.assertEqual(lint_dockerfile(self.text), [])

    def test_every_from_pinned_to_same_digest(self):
        froms = [i["args"].split()[0] for i in self.ins if i["op"] == "FROM"]
        self.assertGreaterEqual(len(froms), 2)
        for f in froms:
            self.assertRegex(f, r"^python:3\.12\.\d+-slim-bookworm@sha256:[0-9a-f]{64}$")
        self.assertEqual(len(set(froms)), 1)

    def test_final_stage_non_root_exec_form_healthcheck(self):
        final = max(i["stage"] for i in self.ins)
        fin = [i for i in self.ins if i["stage"] == final]
        user = [i for i in fin if i["op"] == "USER"][-1]["args"]
        self.assertNotIn(user.split(":")[0], ("0", "root"))
        self.assertTrue(any(i["op"] == "HEALTHCHECK" and "CMD [" in i["args"] for i in fin))
        self.assertTrue(any(i["op"] == "ENTRYPOINT" and i["args"].startswith("[") for i in fin))

    def test_no_secret_in_arg_env(self):
        for i in self.ins:
            if i["op"] in ("ARG", "ENV"):
                self.assertNotRegex(i["args"], r"(?i)(token|secret|password|api_?key)\s*=")

    def test_naive_fixture_is_flagged(self):
        ids = {f["id"] for f in lint_dockerfile((FIX / "Dockerfile.naive").read_text()) if f["severity"] == "error"}
        for want in ("D01", "D02", "D03", "D04", "D05", "D06", "D07"):
            self.assertIn(want, ids)


class IgnoreAndContextTest(unittest.TestCase):
    def test_dockerignore_rules(self):
        rules = load_ignore((ROOT / ".dockerignore").read_text())
        for p in (".env", ".env.local", "secrets/crm_token.txt", "state/state.db", ".git/config",
                  "runtime/__pycache__/x.pyc", "tests/test_x.py"):
            self.assertTrue(is_ignored(p, rules), p)
        for p in ("runtime/worker.py", "config/reps.json", "requirements.txt", "sample_leads.csv"):
            self.assertFalse(is_ignored(p, rules), p)
        self.assertEqual(lint_dockerignore((ROOT / ".dockerignore").read_text()), [])

    def test_planted_canary_never_in_build_context(self):
        tmp = Path(tempfile.mkdtemp()) / "repo"
        shutil.copytree(ROOT, tmp, ignore=shutil.ignore_patterns(".git", "state", "__pycache__", ".venv"))
        canary = "canary-unit-test-7f3a"
        for rel in ("secrets/crm_token.txt", ".env", "state/state.db"):
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_text(canary)
        files = build_context(tmp, (ROOT / ".dockerignore").read_text())
        self.assertTrue(files)
        self.assertFalse([f for f in files if canary in (tmp / f).read_text(errors="ignore")])
        naive_files = build_context(tmp, (FIX / "dockerignore.naive").read_text())
        self.assertTrue([f for f in naive_files if canary in (tmp / f).read_text(errors="ignore")])


class ComposeTest(unittest.TestCase):
    def setUp(self):
        self.doc = miniyaml.loads((ROOT / "compose.yaml").read_text())

    def test_repo_compose_clean(self):
        self.assertEqual(lint_compose((ROOT / "compose.yaml").read_text()), [])

    def test_worker_hardening(self):
        w = self.doc["services"]["worker"]
        self.assertIs(w["read_only"], True)
        self.assertEqual(w["cap_drop"], ["ALL"])
        self.assertIn("agent-state:/data", w["volumes"])
        self.assertEqual(sorted(w["secrets"]), ["anthropic_api_key", "crm_token"])
        self.assertFalse([k for k in w["environment"] if re.search(r"(?i)token|key|secret", k)])
        self.assertEqual(w["depends_on"]["mock-crm"]["condition"], "service_healthy")
        self.assertIs(self.doc["networks"]["gtm-internal"]["internal"], True)

    def test_secrets_are_files(self):
        for name, spec in self.doc["secrets"].items():
            self.assertTrue(spec["file"].startswith("./secrets/"), name)

    def test_naive_compose_flagged(self):
        ids = {f["id"] for f in lint_compose((FIX / "compose.naive.yaml").read_text()) if f["severity"] == "error"}
        for want in ("C01", "C02", "C03", "C04", "C05", "C09"):
            self.assertIn(want, ids)

    def test_miniyaml_rejects_unsupported(self):
        with self.assertRaises(ValueError):
            miniyaml.loads("a:\n  - b: c\n")
        with self.assertRaises(ValueError):
            miniyaml.loads("a: &anchor x\n")

    def test_matches_pyyaml_if_installed(self):
        try:
            import yaml  # type: ignore
        except ImportError:
            self.skipTest("PyYAML not installed (optional cross-check)")
        self.assertEqual(yaml.safe_load((ROOT / "compose.yaml").read_text()), self.doc)


if __name__ == "__main__":
    unittest.main()
