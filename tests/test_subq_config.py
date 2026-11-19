"""Unit tests for SubQ config validation; all tests are offline and secret-free."""
import tomllib
import unittest
from pathlib import Path

from subq_config import SubQConfigError, parse_subq_config


class SubQConfigTests(unittest.TestCase):
    def test_default_is_disabled_and_has_no_credentials(self):
        cfg = parse_subq_config(None)
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.credential_ref, "")
        self.assertEqual(cfg.protocol, "openai_chat_completions")

    def test_repository_toml_subq_example_parses_disabled(self):
        root = Path(__file__).resolve().parents[1]
        data = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
        cfg = parse_subq_config(data["subq"])
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.base_url, "")

    def test_valid_enabled_https_config(self):
        cfg = parse_subq_config({
            "enabled": True, "provider": "example", "base_url": "https://api.example/v1/",
            "model": "decision-model", "credential_ref": "SUBQ_API_KEY",
            "timeout_seconds": 30, "max_retries": 1,
            "daily_request_budget": 40, "daily_token_budget": 20000,
            "daily_cost_budget": 1.25,
        })
        self.assertEqual(cfg.base_url, "https://api.example/v1")
        self.assertEqual(cfg.max_retries, 1)
        self.assertEqual(cfg.daily_request_budget, 40)

    def test_http_loopback_allowed(self):
        cfg = parse_subq_config({
            "enabled": True, "provider": "local", "base_url": "http://localhost:8080/v1",
            "model": "local-model", "credential_ref": "LOCAL_KEY",
        })
        self.assertTrue(cfg.enabled)

    def test_enabled_requires_provider_model_url_and_ref(self):
        base = {"enabled": True, "provider": "p", "base_url": "https://api.example/v1",
                "model": "m", "credential_ref": "KEY"}
        for key in ("provider", "base_url", "model", "credential_ref"):
            with self.subTest(missing=key):
                bad = dict(base); bad.pop(key)
                with self.assertRaises(SubQConfigError):
                    parse_subq_config(bad)

    def test_rejects_non_loopback_http_and_url_credentials(self):
        base = {"enabled": True, "provider": "p", "base_url": "https://api.example/v1",
                "model": "m", "credential_ref": "KEY"}
        for url in ("http://api.example/v1", "https://user:pass@example.com/v1",
                    "https://api.example/v1?key=secret", "ftp://api.example",
                    "https://api.example/bad path", "https://api.example/\npath"):
            with self.subTest(url=url):
                bad = dict(base, base_url=url)
                with self.assertRaises(SubQConfigError):
                    parse_subq_config(bad)

    def test_credential_ref_is_env_var_name_not_secret(self):
        base = {"enabled": True, "provider": "p", "base_url": "https://api.example/v1",
                "model": "m", "credential_ref": "KEY"}
        for ref in ("sk-live-secret", "actual-secret-value", "A=B", "key-name"):
            with self.subTest(ref=ref):
                with self.assertRaises(SubQConfigError):
                    parse_subq_config(dict(base, credential_ref=ref))
        cfg = parse_subq_config(dict(base, credential_ref="SUBQ_API_KEY_1"))
        self.assertEqual(cfg.credential_ref, "SUBQ_API_KEY_1")

    def test_rejects_unknown_protocol_and_retry_above_one(self):
        base = {"enabled": True, "provider": "p", "base_url": "https://api.example/v1",
                "model": "m", "credential_ref": "KEY"}
        for extra in ({"protocol": "vendor_json"}, {"max_retries": 2}):
            with self.subTest(extra=extra):
                with self.assertRaises(SubQConfigError):
                    parse_subq_config(dict(base, **extra))

    def test_rejects_invalid_numeric_budgets_and_boolean_numbers(self):
        base = {"enabled": False}
        for extra in ({"timeout_seconds": 0}, {"timeout_seconds": float("nan")},
                      {"daily_request_budget": -1}, {"daily_token_budget": 1.5},
                      {"max_retries": True}, {"daily_cost_budget": float("inf")}):
            with self.subTest(extra=extra):
                with self.assertRaises(SubQConfigError):
                    parse_subq_config(dict(base, **extra))

    def test_errors_do_not_echo_secret_values(self):
        marker = "DO_NOT_LEAK_SECRET"
        with self.assertRaises(SubQConfigError) as ctx:
            parse_subq_config({"enabled": True, "provider": "p", "base_url": "https://api.example",
                               "model": "m", "credential_ref": marker, "timeout_seconds": marker})
        self.assertNotIn(marker, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
