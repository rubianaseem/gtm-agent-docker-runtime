import io
import tempfile
import unittest
from pathlib import Path

from runtime import jsonlog
from runtime.config import SecretError, read_secret


class SecretsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.buf = io.StringIO()
        jsonlog.set_stream(self.buf)
        jsonlog.clear_secrets()

    def tearDown(self):
        import sys
        jsonlog.set_stream(sys.stdout)
        jsonlog.clear_secrets()

    def test_file_env_wins_then_secrets_dir_then_env(self):
        f = self.tmp / "tok.txt"
        f.write_text("from-file-value\n")
        (self.tmp / "crm_token").write_text("from-secrets-dir")
        env = {"CRM_TOKEN_FILE": str(f), "SECRETS_DIR": str(self.tmp), "CRM_TOKEN": "from-env-value"}
        self.assertEqual(read_secret("CRM_TOKEN", env=env), "from-file-value")
        del env["CRM_TOKEN_FILE"]
        self.assertEqual(read_secret("CRM_TOKEN", env=env), "from-secrets-dir")
        (self.tmp / "crm_token").unlink()
        self.assertEqual(read_secret("CRM_TOKEN", env=env), "from-env-value")
        self.assertIn("secret_from_env", self.buf.getvalue())

    def test_missing_required_raises_without_value(self):
        with self.assertRaises(SecretError) as cm:
            read_secret("CRM_TOKEN", env={"SECRETS_DIR": str(self.tmp)})
        self.assertIn("CRM_TOKEN", str(cm.exception))

    def test_empty_optional_secret_is_none(self):
        (self.tmp / "anthropic_api_key").write_text("\n")
        self.assertIsNone(read_secret("ANTHROPIC_API_KEY", required=False, env={"SECRETS_DIR": str(self.tmp)}))

    def test_loaded_secret_never_appears_in_logs(self):
        (self.tmp / "crm_token").write_text("s3cr3t-token-abcdef")
        val = read_secret("CRM_TOKEN", env={"SECRETS_DIR": str(self.tmp)})
        jsonlog.log("error", "debug_dump", header=f"Authorization: Bearer {val}", raw=val)
        out = self.buf.getvalue()
        self.assertNotIn(val, out)
        self.assertIn("[REDACTED]", out)

    def test_bearer_and_email_masked_even_if_unregistered(self):
        jsonlog.log("info", "x", h="Bearer abcdefghijk123", who="jane.doe@acme.example")
        out = self.buf.getvalue()
        self.assertNotIn("abcdefghijk123", out)
        self.assertNotIn("jane.doe@acme.example", out)


if __name__ == "__main__":
    unittest.main()
