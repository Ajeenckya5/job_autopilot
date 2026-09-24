import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from local.llm_proxy import forward, keychain_status, migrate_config, scrub


class Runner:
    def __init__(self):
        self.store = {}

    def __call__(self, args, capture_output=True, text=True):
        account = args[args.index("-a") + 1]
        if "add-generic-password" in args:
            self.store[account] = args[args.index("-w") + 1]

            class Added:
                returncode = 0
                stdout = ""
                stderr = ""

            return Added()
        secret = self.store.get(account, "")

        class Found:
            returncode = 0 if secret else 1
            stdout = f"{secret}\n" if secret else ""
            stderr = ""

        return Found()


class ProxyTests(unittest.TestCase):
    def test_rejects_an_unknown_host_without_using_the_key(self):
        runner = Runner()
        runner.store["openai"] = "sk-secret-value"
        with self.assertRaises(ValueError):
            forward({"provider": "openai", "url": "https://evil.example/v1", "body": {}}, runner)
        self.assertIn("[key]", scrub("Bearer sk-secret-value"))

    def test_migration_returns_provider_names_only(self):
        runner = Runner()
        with TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text("llm_provider: gemini\nllm_api_key: AIza-secret-value\n")
            moved = migrate_config(path, runner)
        self.assertEqual(moved, ["gemini"])
        self.assertTrue(keychain_status(runner)["gemini"])
        self.assertNotIn("AIza-secret-value", " ".join(moved))


if __name__ == "__main__":
    unittest.main()
