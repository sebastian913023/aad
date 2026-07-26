"""Tests for backend/auth.py — token signing/verification and password hashing."""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import auth  # noqa: E402


class TokenTests(unittest.TestCase):
    def test_round_trip_preserves_claims(self):
        token = auth.encode_token({"sub": "alice", "role": "admin"}, secret="s3cret")
        payload = auth.decode_token(token, secret="s3cret")
        self.assertEqual(payload["sub"], "alice")
        self.assertEqual(payload["role"], "admin")

    def test_iat_and_exp_are_populated(self):
        before = int(time.time())
        payload = auth.decode_token(
            auth.encode_token({"sub": "alice"}, secret="k", exp_seconds=60), secret="k"
        )
        self.assertGreaterEqual(payload["iat"], before)
        self.assertAlmostEqual(payload["exp"] - payload["iat"], 60, delta=1)

    def test_token_has_three_unpadded_segments(self):
        token = auth.encode_token({"sub": "alice"}, secret="k")
        segments = token.split(".")
        self.assertEqual(len(segments), 3)
        for seg in segments:
            self.assertNotIn("=", seg, "base64url segments must be unpadded")

    def test_wrong_secret_is_rejected(self):
        token = auth.encode_token({"sub": "alice"}, secret="right")
        with self.assertRaises(auth.AuthError):
            auth.decode_token(token, secret="wrong")

    def test_tampered_payload_is_rejected(self):
        # Re-sign nothing: swap the payload for a forged one, keep the signature.
        token = auth.encode_token({"sub": "alice"}, secret="k")
        header_seg, _, signature_seg = token.split(".")
        forged_payload = auth._b64url_encode(b'{"sub":"admin","exp":9999999999}')
        forged = f"{header_seg}.{forged_payload}.{signature_seg}"
        with self.assertRaises(auth.AuthError):
            auth.decode_token(forged, secret="k")

    def test_expired_token_is_rejected(self):
        token = auth.encode_token({"sub": "alice", "exp": int(time.time()) - 1}, secret="k")
        with self.assertRaises(auth.AuthError):
            auth.decode_token(token, secret="k")

    def test_malformed_tokens_are_rejected(self):
        for bad in ["", "not-a-token", "only.two", "a.b.c.d"]:
            with self.subTest(token=bad), self.assertRaises(auth.AuthError):
                auth.decode_token(bad, secret="k")

    def test_unsigned_alg_none_style_token_is_rejected(self):
        """A token with an empty signature must not validate."""
        header_seg = auth._b64url_encode(b'{"alg":"none","typ":"JWT"}')
        payload_seg = auth._b64url_encode(b'{"sub":"admin","exp":9999999999}')
        with self.assertRaises(auth.AuthError):
            auth.decode_token(f"{header_seg}.{payload_seg}.", secret="k")


class PasswordTests(unittest.TestCase):
    def test_verify_accepts_correct_password(self):
        stored = auth.hash_password("hunter2")
        self.assertTrue(auth.verify_password("hunter2", stored))

    def test_verify_rejects_wrong_password(self):
        stored = auth.hash_password("hunter2")
        self.assertFalse(auth.verify_password("hunter3", stored))

    def test_hash_is_salted_so_repeats_differ(self):
        self.assertNotEqual(auth.hash_password("same"), auth.hash_password("same"))

    def test_hash_is_deterministic_for_a_fixed_salt(self):
        self.assertEqual(
            auth.hash_password("pw", salt="fixedsalt"),
            auth.hash_password("pw", salt="fixedsalt"),
        )

    def test_plaintext_never_appears_in_hash(self):
        self.assertNotIn("hunter2", auth.hash_password("hunter2"))

    def test_verify_rejects_malformed_stored_value(self):
        self.assertFalse(auth.verify_password("pw", "no-dollar-separator"))


if __name__ == "__main__":
    unittest.main()
