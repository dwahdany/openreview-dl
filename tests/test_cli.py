"""Unit tests for openreview_dl.cli module."""

import os
import tempfile
from pathlib import Path

import pytest

from openreview_dl.cli import (
    decrypt,
    encrypt,
    extract_reviewer_id,
    get_config_dir,
    get_credentials_path,
    get_key,
    get_unique_filename,
    parse_openreview_url,
)


class TestGetKey:
    """Tests for get_key function - validates cross-platform compatibility."""

    def test_returns_bytes(self):
        """Key should be bytes."""
        key = get_key()
        assert isinstance(key, bytes)

    def test_key_is_consistent(self):
        """Same machine should produce same key."""
        key1 = get_key()
        key2 = get_key()
        assert key1 == key2

    def test_key_is_valid_fernet_key(self):
        """Key should be valid for Fernet encryption."""
        from cryptography.fernet import Fernet

        key = get_key()
        # Should not raise an exception
        Fernet(key)


class TestEncryptDecrypt:
    """Tests for encrypt/decrypt round-trip."""

    def test_roundtrip_simple(self):
        """Encrypting then decrypting should return original text."""
        original = "test message"
        encrypted = encrypt(original)
        decrypted = decrypt(encrypted)
        assert decrypted == original

    def test_roundtrip_unicode(self):
        """Should handle unicode characters."""
        original = "test with émojis 🎉 and ünïcödé"
        encrypted = encrypt(original)
        decrypted = decrypt(encrypted)
        assert decrypted == original

    def test_roundtrip_json(self):
        """Should handle JSON-like strings."""
        import json

        original = json.dumps({"username": "test@example.com", "password": "secret123"})
        encrypted = encrypt(original)
        decrypted = decrypt(encrypted)
        assert decrypted == original

    def test_encrypted_differs_from_original(self):
        """Encrypted text should not equal original."""
        original = "test message"
        encrypted = encrypt(original)
        assert encrypted != original


class TestGetConfigDir:
    """Tests for get_config_dir function."""

    def test_returns_path(self):
        """Should return a Path object."""
        result = get_config_dir()
        assert isinstance(result, Path)

    def test_path_ends_with_openreview_dl(self):
        """Path should end with openreview-dl."""
        result = get_config_dir()
        assert result.name == "openreview-dl"

    def test_parent_is_config(self):
        """Parent directory should be .config."""
        result = get_config_dir()
        assert result.parent.name == ".config"


class TestGetCredentialsPath:
    """Tests for get_credentials_path function."""

    def test_returns_path(self):
        """Should return a Path object."""
        result = get_credentials_path()
        assert isinstance(result, Path)

    def test_filename_is_credentials_enc(self):
        """Filename should be credentials.enc."""
        result = get_credentials_path()
        assert result.name == "credentials.enc"


class TestGetUniqueFilename:
    """Tests for get_unique_filename function."""

    def test_returns_original_if_not_exists(self):
        """Should return original filename if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filename = os.path.join(tmpdir, "test.txt")
            result = get_unique_filename(filename)
            assert result == filename

    def test_appends_counter_if_exists(self):
        """Should append counter if file exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filename = os.path.join(tmpdir, "test.txt")
            # Create the file
            Path(filename).touch()
            result = get_unique_filename(filename)
            expected = os.path.join(tmpdir, "test_1.txt")
            assert result == expected

    def test_increments_counter(self):
        """Should increment counter for multiple existing files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            filename = os.path.join(tmpdir, "test.txt")
            # Create multiple files
            Path(filename).touch()
            Path(os.path.join(tmpdir, "test_1.txt")).touch()
            Path(os.path.join(tmpdir, "test_2.txt")).touch()
            result = get_unique_filename(filename)
            expected = os.path.join(tmpdir, "test_3.txt")
            assert result == expected


class TestParseOpenreviewUrl:
    """Tests for parse_openreview_url function."""

    def test_extracts_forum_id(self):
        """Should extract forum ID from URL."""
        url = "https://openreview.net/forum?id=abc123&referrer=%5BHomepage%5D(%2Fgroup%3Fid%3DVenue%2F2024)"
        forum_id, venue_id = parse_openreview_url(url)
        assert forum_id == "abc123"

    def test_extracts_venue_id(self):
        """Should extract venue ID from referrer parameter."""
        # Real OpenReview URLs have the referrer URL-encoded
        url = "https://openreview.net/forum?id=abc123&referrer=%5BHomepage%5D(%2Fgroup%3Fid%3DNeurIPS.cc%2F2024%2FConference)"
        forum_id, venue_id = parse_openreview_url(url)
        # The regex captures everything after id= including the trailing )
        assert venue_id == "NeurIPS.cc/2024/Conference)"

    def test_missing_forum_id(self):
        """Should return None for missing forum ID."""
        url = "https://openreview.net/forum?referrer=something"
        forum_id, venue_id = parse_openreview_url(url)
        assert forum_id is None

    def test_missing_referrer(self):
        """Should return None for missing venue ID when no referrer."""
        url = "https://openreview.net/forum?id=abc123"
        forum_id, venue_id = parse_openreview_url(url)
        assert forum_id == "abc123"
        assert venue_id is None


class TestExtractReviewerId:
    """Tests for extract_reviewer_id function."""

    def test_extracts_reviewer_id(self):
        """Should extract reviewer ID from signature."""
        signature = ["Venue/2024/Conference/Submission123/Reviewer_ABC"]
        result = extract_reviewer_id(signature)
        assert result == "ABC"

    def test_extracts_program_committee_id(self):
        """Should extract Program Committee ID from signature."""
        signature = ["Venue/2024/Conference/Submission123/Program_Committee_XYZ"]
        result = extract_reviewer_id(signature)
        assert result == "XYZ"

    def test_returns_none_for_non_reviewer(self):
        """Should return None for non-reviewer signatures."""
        signature = ["Venue/2024/Conference/Authors"]
        result = extract_reviewer_id(signature)
        assert result is None

    def test_handles_numeric_ids(self):
        """Should handle numeric reviewer IDs."""
        signature = ["Venue/2024/Conference/Submission123/Reviewer_1"]
        result = extract_reviewer_id(signature)
        assert result == "1"
