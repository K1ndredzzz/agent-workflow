"""Public contracts for secret scanning and output redaction."""

import json
import os
import unittest
from unittest import mock

import agent_dispatch.secrets as secrets


class TestSecrets(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, clear=True))

    def test_secret_scan_allow_exempts_only_heuristics_on_the_marked_line(self) -> None:
        marker = " # secret-scan:allow"
        finding = ["Potential secret token in source"]
        for prefix, value, suffix in (
            ("X-API-Key: ", "synthetic.opaque.value", ""),
            ("Authorization: ", "*key", ""),
            ("X-API-Key: ", "*key", ""),
            ('{"Authorization": "Token ', "synthetic.opaque.value", '"}'),
            ('{"X-API-Key": "', "synthetic.opaque.value", '"}'),
            ("Bearer ", "synthetic.opaque.value", ""),
            ("Basic ", "dXNlcjpmaXh0dXJl", ""),
        ):
            with self.subTest(prefix=prefix, value=value):
                line = f"{prefix}{value}{suffix}"
                marked = line + marker
                self.assertEqual(secrets.scan_text_for_secrets(line, "source"), finding)
                self.assertEqual(secrets.scan_text_for_secrets(marked, "source"), [])
                self.assertEqual(secrets.scan_text_for_secrets(f"{marked}\n{line}", "source"), finding)
                redacted = secrets.redact_secrets(marked)
                self.assertIn("<REDACTED_SECRET>", redacted)
                self.assertNotIn(value, redacted)
                self.assertEqual(secrets.redact_data({"message": [marked]}), {"message": [redacted]})
                self.assertEqual(json.loads(secrets.safe_json({"message": marked})), {"message": redacted})

    def test_api_key_headers_are_redacted_in_text_and_data(self) -> None:
        token = "opaque" + "-synthetic!value"
        for header in ("X-API-Key", "x-goog-api-key", "X-Api-kEy"):
            event = {"request": {"headers": {header: token}}, "status": "ok"}
            plain = f"Request header: {header}: {token}"
            self.assertTrue(secrets.scan_text_for_secrets(plain, "test"))
            self.assertNotIn(token, secrets.redact_secrets(json.dumps(event)))
            self.assertEqual(json.loads(secrets.safe_json(event))["request"]["headers"][header], "<REDACTED_SECRET>")
        self.assertEqual(secrets.redact_secrets("Document X-API-Key header support."), "Document X-API-Key header support.")
        for benign_code in (
            'headers = {"x-api-key": api_key}',
            'headers = {"x-api-key": my_api_key}',
            'headers = {"x-api-key": self.api_key}',
            'headers = {"x-api-key": os.environ["API_KEY"]}',
            'headers = {"x-api-key": process.env.API_KEY}',
            'headers = {"x-api-key": ""}',
            'interface Headers { "x-api-key": string; }',
            'x-api-key: <your-api-key>',
            'X-API-Key: ${API_KEY}',
        ):
            self.assertEqual(secrets.redact_secrets(benign_code), benign_code, f"False positive on code: {benign_code}")
            self.assertEqual(secrets.scan_text_for_secrets(benign_code, "code"), [], f"False block on code: {benign_code}")

    def test_raw_http_header_templates_and_prose_pass_text_scanning(self) -> None:
        for source in (
            "Authorization: Bearer ${ACCESS_TOKEN}",
            "  Authorization: process.env.ACCESS_TOKEN,",
            "  Authorization: `Bearer ${token}`,",
            "- X-API-Key: header used for client authentication",
            "- Authorization: Bearer token validation rules",
            "Authorization: Basic authentication is required for this endpoint.",
            "  x-api-key: ${{ secrets.API_KEY }}",
            "  x-api-key: {{ api_key }}",
            "  x-api-key: string # API key header",
            '  x-api-key: ""',
        ):
            self.assertEqual(secrets.redact_secrets(source), source, f"False positive on: {source}")
            self.assertFalse(secrets.scan_text_for_secrets(source, "template"), f"False block on: {source}")

    def test_header_prose_passes_text_scanning_while_literals_are_flagged(self) -> None:
        self.assertFalse(secrets.scan_text_for_secrets("Document X-API-Key header support", "title"))
        header = "X-API-Key"
        for literal in ("clientKey", "Identifies", "*gateway_key"):
            self.assertNotIn(literal, secrets.redact_secrets(f"{header}: {literal}"))
        for source in (
            f'headers: {{"{header}": clientKey}}',
            f'headers = {{"{header}": "clientKey"}}',
            f"key: &gateway_key clientKey\nheaders:\n  {header}: *gateway_key",
            f"- {header}: Identifies",
        ):
            self.assertTrue(secrets.scan_text_for_secrets(source, "literal"))

    def test_json_quoted_authorization_credentials_are_detected_and_redacted(self) -> None:
        token = "synthetic_pd_tok_987654321"
        fixture_entries = [
            ("Authorization", f"Token token={token}"),
            ("Authorization", f'Token token="{token}"'),
            ("Authorization", f"Token {token}"),
            ("Authorization", f"ApiKey user:{token}"),
            ("Authorization", f'OAuth oauth_consumer_key="ck_{token}", oauth_token="{token}"'),
            ("Authorization", f"Bearer {token}"),
            ("Authorization", "Basic " + "dXNlcjpzeW50aGV0aWM="),
            ("Proxy-Authorization", f"Token token={token}"),
            ("Proxy-Authorization", f"ApiKey {token}"),
            ("Proxy-Authorization", f"OAuth {token}"),
            ("Proxy-Authorization", f"Bearer {token}"),
            ("Proxy-Authorization", "Basic " + "cHJveHk6c3ludGhldGlj"),
        ]
        settings_json = json.dumps(
            {
                "pagerduty": {"headers": {"Authorization": f"Token token={token}"}},
                "proxy": {"headers": {"Proxy-Authorization": f"ApiKey {token}"}},
            },
            indent=2,
        ) + "\n"
        self.assertTrue(secrets.scan_text_for_secrets(settings_json, "settings.json"))
        redacted_json = secrets.redact_secrets(settings_json)
        self.assertNotIn(token, redacted_json)
        self.assertIn("<REDACTED_SECRET>", redacted_json)
        self.assertEqual(secrets.redact_secrets(redacted_json), redacted_json)

        for header, value in fixture_entries:
            for sample in (
                json.dumps({header: value}),
                f"{{'{header}': '{value}'}}",
                f"{header}: {json.dumps(value)}",
            ):
                with self.subTest(header=header, sample=sample):
                    self.assertTrue(secrets.scan_text_for_secrets(sample, "config"), f"Missed secret in {sample!r}")
                    redacted = secrets.redact_secrets(sample)
                    self.assertNotIn(token, redacted)
                    self.assertNotIn("dXNlcjpzeW50aGV0aWM=", redacted)
                    self.assertNotIn("cHJveHk6c3ludGhldGlj", redacted)
                    self.assertEqual(secrets.redact_secrets(redacted), redacted)

    def test_authorization_placeholders_and_prose_pass_text_scanning(self) -> None:
        # Zero false positives on placeholders, benign type words, concatenation prefixes, and English prose.
        for benign in (
            '{"Authorization": "Token token=${PAGERDUTY_TOKEN}"}',
            '{"Authorization": "Token token=\\"${PAGERDUTY_TOKEN}\\""}',
            '{"Authorization": "Token token=<your-token>"}',
            '{"Authorization": "Token token={{ api_token }}"}',
            '{"Authorization": "Token token=$PAGERDUTY_TOKEN"}',
            '{"Authorization": "Bearer ${ACCESS_TOKEN}"}',
            '{"Authorization": "ApiKey ${{ secrets.API_KEY }}"}',
            '{"Authorization": "OAuth {{ .Values.oauthToken }}"}',
            '{"Proxy-Authorization": "Basic <base64-creds>"}',
            '{"Authorization": "Token string"}',
            '{"Authorization": "ApiKey optional"}',
            '{"Authorization": "Bearer required"}',
            '{"Authorization": "Token token=string"}',
            '{"Authorization": "OAuth oauth_token=\\"${OAUTH_TOKEN}\\", oauth_version=\\"1.0\\", realm=\\"api\\""}',
            'headers = {"Authorization": "Bearer " + token}',
            'headers = {"Authorization": "Token token=" + api_token}',
            '{"Authorization": "Bearer token validation rules"}',
            '{"Authorization": "Basic authentication is required for this endpoint."}',
            '{"Authorization": "OAuth 2.0 access token"}',
            'Authorization: Token token=${PAGERDUTY_TOKEN}',
        ):
            with self.subTest(benign=benign):
                self.assertEqual(secrets.redact_secrets(benign), benign, f"False positive on {benign!r}")
                self.assertEqual(secrets.scan_text_for_secrets(benign, "benign"), [], f"False block on {benign!r}")

    def test_secret_scan_allow_preserves_non_bypassable_text_checks(self) -> None:
        marker = " # secret-scan:allow"
        token = "lin_api_" + "syntheticvalue123456"
        live_value = "synthetic process credential"
        key_kind = "PRIVATE KEY"
        pem = f"-----BEGIN {key_kind}-----{marker}\nsynthetic-body\n-----END {key_kind}-----"
        with mock.patch.dict(os.environ, {"UNIT_API_KEY": live_value}):
            for content, credential in (
                (f"note{marker} {token}", token),
                (pem, "synthetic-body"),
                (f"note{marker} {live_value}", live_value),
            ):
                with self.subTest(credential=credential):
                    self.assertEqual(secrets.scan_text_for_secrets(content, "source"), [
                        "Potential secret token in source",
                    ])
                    self.assertNotIn(credential, secrets.redact_secrets(content))

    def test_yaml_alias_credentials_fail_closed_in_text(self) -> None:
        for name, value, flow_mapping in (
            ("X-API-Key", "*gateway_key", False),
            ("Authorization", "Token *gateway_key", False),
            ("Authorization", "*gateway_key", False),
            ("X-API-Key", "*k", True),
            ("Authorization", "*k", True),
        ):
            header = f'  headers: {{"{name}": {value}}}' if flow_mapping else f"  {name}: {value}"
            with self.subTest(header=header):
                source = "key: &gateway_key ${GATEWAY_KEY}\nshort: &k <PLACEHOLDER>\n" + header + "\n"
                self.assertEqual(secrets.scan_text_for_secrets(source, "yaml"), ["Potential secret token in yaml"])

    def test_secret_patterns_no_false_positives_on_kebab_or_env_example(self) -> None:
        # Must NOT false-positive on kebab-case identifiers or .env.example!
        self.assertEqual(secrets.redact_secrets("task-list-container-component"), "task-list-container-component")
        self.assertFalse(secrets.is_secret_filename(".env.example"))
        self.assertFalse(secrets.is_secret_filename("config/.env.sample"))
        self.assertTrue(secrets.is_secret_filename(".env"))
        self.assertTrue(secrets.is_secret_filename(".env.local"))
        self.assertTrue(secrets.is_secret_filename("目录/.env"))
        # Must catch expanded real token families
        for sample in (
            "sk-proj-" + "1234567890abcdefghij1234",
            "github_pat_" + "11ABCDEFG01234567890_abcdefghijklmnop",
            "sk_live_" + "1234567890abcdef12",
            "xoxb-" + "1234567890-abcdef",
            "AIza" + "X" * 35,
            "glpat-" + "1234567890abcdefghij",
        ):
            self.assertEqual(secrets.redact_secrets(f"key={sample}"), "key=<REDACTED_SECRET>", f"Missed redaction for {sample}")
            self.assertTrue(secrets.scan_text_for_secrets(f"Fix {sample}", "title"), f"Missed title secret for {sample}")

    def test_auth_redaction_preserves_english_prose_while_redacting_real_credentials(self) -> None:
        benign_phrases = (
            "Add basic validation to login form",
            "Implement basic authentication flow",
            "Add basic end-to-end tests",
            "Basic configuration helper",
            "Support bearer authentication in API",
            "Support Bearer Authorization header",
            "Support basic Base64URL encoding",
        )
        for phrase in benign_phrases:
            self.assertEqual(secrets.redact_secrets(phrase), phrase, f"False positive on: {phrase}")
            self.assertEqual(secrets.scan_text_for_secrets(phrase, "title"), [], f"False block on: {phrase}")

        secret_cases = (
            ("Authorization: Basic " + "dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("Authorization: Basic " + "simpleword", "simpleword"),
            ("Authorization: Bearer " + "simpleword", "simpleword"),
            ('{"authorization": "Bearer ' + 'simpleword"}', "simpleword"),
            ("Basic " + "dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("Basic " + "YWRtaW46c2VjcmV0", "YWRtaW46c2VjcmV0"),
            ("Bearer " + "invented-only-example", "invented-only-example"),
            ("Bearer " + "eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGciOiJIUzI1NiJ9"),
        )
        for text, secret in secret_cases:
            redacted = secrets.redact_secrets(text)
            self.assertIn("<REDACTED_SECRET>", redacted, f"Missed redaction for: {text}")
            self.assertNotIn(secret, redacted, f"Secret leaked in: {text}")
            self.assertTrue(secrets.scan_text_for_secrets(text, "header"), f"Missed scan hit for: {text}")
