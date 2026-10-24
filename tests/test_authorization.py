import unittest
from urllib.parse import parse_qs, urlsplit

from tests.support import load

authorization, intent = load("authorization"), load("intent")


class AuthorizationTests(unittest.TestCase):
    def link(self, response):
        return next(
            line
            for line in response.splitlines()
            if line.startswith("https://trello.com/1/authorize?")
        )

    def test_no_key_shows_application_addresses(self):
        response = authorization.authorization_help("  ")
        self.assertIn("https://trello.com/apps/admin", response)
        self.assertIn(authorization.DOCS_URL, response)
        self.assertIn("api_key", response)
        self.assertNotIn("/1/authorize?", response)

    def test_link_contains_only_expected_permissions(self):
        response = authorization.authorization_help("  test-key  ")
        link = urlsplit(self.link(response))
        self.assertEqual(
            (link.scheme, link.netloc, link.path), ("https", "trello.com", "/1/authorize")
        )
        self.assertEqual(
            parse_qs(link.query),
            {
                "key": ["test-key"],
                "expiration": ["30days"],
                "scope": ["read,write"],
                "response_type": ["token"],
                "name": ["AstrBotTrelloConnect"],
            },
        )
        self.assertFalse(link.fragment)
        self.assertIn("不会自动回填 Token", response)
        self.assertIn("不要把 Token 发给 Bot", response)

    def test_key_is_encoded_without_injecting_params(self):
        key = "key&scope=account#token=not-a-real-token"
        link = urlsplit(self.link(authorization.authorization_help(key)))
        query = parse_qs(link.query)
        self.assertEqual(query["key"], [key])
        self.assertEqual(query["scope"], ["read,write"])
        self.assertNotIn("token", query)
        self.assertFalse(link.fragment)

    def test_auth_aliases(self):
        for text in ["/trello 授权", "/trello 登录", "/trello auth", "/TRELLO LOGIN"]:
            with self.subTest(text=text):
                parsed = intent.parse_intent(text)
                self.assertEqual(parsed.action, "authorize")
                self.assertTrue(parsed.explicit)
