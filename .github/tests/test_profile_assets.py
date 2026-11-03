from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
from unittest import TestCase, main
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request
import xml.etree.ElementTree as ET


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / ".github" / "scripts" / "profile_assets.py"
sys.path.insert(0, str(SCRIPT.parent))

import profile_assets as profile  # noqa: E402


NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)


def valid_assets_data() -> dict:
    sample_days = []
    start = (NOW - timedelta(days=364)).date()
    for index in range(365):
        current = start + timedelta(days=index)
        sample_days.append({"date": current.isoformat(), "count": 0 if index % 3 else 4})
    return {
        "login": "r-siddiq",
        "public_nonfork_stars": 17,
        "commits": 31,
        "pull_requests": 5,
        "issues": 3,
        "days": sample_days,
    }


def valid_assets() -> dict[str, bytes]:
    data = valid_assets_data()
    return {
        profile.STATS_PATH: profile.build_stats_svg(data, NOW),
        profile.ACTIVITY_PATH: profile.build_activity_svg(data, NOW),
    }


class FakeGraphQL:
    def __init__(self) -> None:
        self.page = 0
        self.contribution_variables: dict | None = None

    def graphql(self, query: str, variables: dict) -> dict:
        if query == profile.CONTRIBUTIONS_QUERY:
            self.contribution_variables = variables
            data = valid_assets_data()
            weeks = []
            for offset in range(0, len(data["days"]), 7):
                days = data["days"][offset : offset + 7]
                weeks.append({"contributionDays": [{"date": day["date"], "contributionCount": day["count"]} for day in days]})
            return {
                "user": {
                    "login": "r-siddiq",
                    "contributionsCollection": {
                        "totalCommitContributions": 31,
                        "totalIssueContributions": 3,
                        "totalPullRequestContributions": 5,
                        "contributionCalendar": {"weeks": weeks},
                    },
                }
            }
        if query == profile.PUBLIC_REPOSITORIES_QUERY:
            self.page += 1
            if variables.get("after") is None:
                return {
                    "user": {
                        "login": "r-siddiq",
                        "repositories": {
                            "totalCount": 2,
                            "pageInfo": {"hasNextPage": True, "endCursor": "page-2"},
                            "nodes": [{"stargazerCount": 12}],
                        },
                    }
                }
            return {
                "user": {
                    "login": "r-siddiq",
                    "repositories": {
                        "totalCount": 2,
                        "pageInfo": {"hasNextPage": False, "endCursor": "page-2"},
                        "nodes": [{"stargazerCount": 5}],
                    },
                }
            }
        raise AssertionError("unexpected query category")


def blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def tree_sha(entries: dict[str, dict[str, str]]) -> str:
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(b"fake-tree\0" + encoded).hexdigest()


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False

    def read(self) -> bytes:
        return self._payload


class FakeGitHub:
    """In-memory Git Data API model for safe branch creation/update tests."""

    def __init__(self, existing: dict[str, bytes] | None = None, marker: bytes | None = None) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, dict[str, str]]] = {}
        self.commits: dict[str, dict] = {}
        self.branch_sha: str | None = None
        if existing is not None:
            tree_entries: dict[str, dict[str, str]] = {}
            for path, content in existing.items():
                sha = blob_sha(content)
                self.blobs[sha] = content
                tree_entries[path] = {"sha": sha, "mode": "100644", "type": "blob"}
            if marker is not None:
                sha = blob_sha(marker)
                self.blobs[sha] = marker
                tree_entries[profile.MARKER_PATH] = {"sha": sha, "mode": "100644", "type": "blob"}
            base = tree_sha(tree_entries)
            self.trees[base] = tree_entries
            commit = "old-commit"
            self.commits[commit] = {"tree": {"sha": base}}
            self.branch_sha = commit

    def open(self, request: Request, timeout: int = 20):
        del timeout
        method = request.get_method()
        path = request.full_url.removeprefix("https://api.github.com")
        payload = json.loads(request.data) if request.data else None
        self.calls.append((method, path, payload))

        if method == "GET" and path == "/repos/r-siddiq/r-siddiq/git/ref/heads/profile-assets":
            if self.branch_sha is None:
                raise HTTPError(request.full_url, 404, "Not Found", None, io.BytesIO(b""))
            return FakeResponse({"object": {"sha": self.branch_sha}})
        if method == "GET" and path == "/repos/r-siddiq/r-siddiq":
            return FakeResponse({"full_name": "r-siddiq/r-siddiq", "fork": False, "archived": False, "default_branch": "main"})
        if method == "GET" and path.startswith("/repos/r-siddiq/r-siddiq/git/commits/"):
            sha = path.rsplit("/", 1)[-1]
            return FakeResponse(self.commits[sha])
        if method == "GET" and path.startswith("/repos/r-siddiq/r-siddiq/git/trees/"):
            sha = path.rsplit("/", 1)[-1]
            entries = self.trees[sha]
            return FakeResponse({"tree": [{"path": name, **entry} for name, entry in entries.items()]})
        if method == "GET" and path.startswith("/repos/r-siddiq/r-siddiq/git/blobs/"):
            sha = path.rsplit("/", 1)[-1]
            return FakeResponse({"encoding": "base64", "content": base64.b64encode(self.blobs[sha]).decode("ascii")})
        if method == "POST" and path == "/repos/r-siddiq/r-siddiq/git/blobs":
            content = base64.b64decode(payload["content"])
            sha = blob_sha(content)
            self.blobs[sha] = content
            return FakeResponse({"sha": sha})
        if method == "POST" and path == "/repos/r-siddiq/r-siddiq/git/trees":
            entries = dict(self.trees.get(payload.get("base_tree"), {}))
            for item in payload["tree"]:
                entries[item["path"]] = {"sha": item["sha"], "mode": item["mode"], "type": item["type"]}
            sha = tree_sha(entries)
            self.trees[sha] = entries
            return FakeResponse({"sha": sha})
        if method == "POST" and path == "/repos/r-siddiq/r-siddiq/git/commits":
            commit = "commit-" + str(len(self.commits) + 1)
            self.commits[commit] = {"tree": {"sha": payload["tree"]}, "parents": payload["parents"]}
            return FakeResponse({"sha": commit})
        if method == "POST" and path == "/repos/r-siddiq/r-siddiq/git/refs":
            if self.branch_sha is not None:
                raise HTTPError(request.full_url, 422, "Conflict", None, io.BytesIO(b""))
            self.branch_sha = payload["sha"]
            return FakeResponse({"object": {"sha": self.branch_sha}})
        if method == "PATCH" and path == "/repos/r-siddiq/r-siddiq/git/refs/heads/profile-assets":
            if payload.get("force") is not False:
                raise AssertionError("publisher attempted a force update")
            parent = self.commits[payload["sha"]]["parents"]
            if parent != [self.branch_sha]:
                raise HTTPError(request.full_url, 409, "Stale branch", None, io.BytesIO(b""))
            self.branch_sha = payload["sha"]
            return FakeResponse({"object": {"sha": self.branch_sha}})
        raise AssertionError("unexpected REST request category")


class ProfileAssetTests(TestCase):
    def test_graphql_pagination_generates_both_valid_svgs(self) -> None:
        api = FakeGraphQL()
        assets = profile.generate_assets(api, NOW)
        self.assertEqual(set(assets), {profile.STATS_PATH, profile.ACTIVITY_PATH})
        self.assertEqual(api.page, 2)
        self.assertEqual(
            api.contribution_variables,
            {"login": "r-siddiq", "from": "2025-11-04T00:00:00Z", "to": "2026-11-03T12:00:00Z"},
        )
        for data in assets.values():
            profile.validate_svg(data)
            root = ET.fromstring(data)
            self.assertTrue(root.tag.endswith("svg"))
            self.assertNotIn(b"<script", data.lower())
            self.assertNotIn(b"https://", data.lower())
        self.assertIn(b"PUBLIC STARS", assets[profile.STATS_PATH])
        self.assertIn(b'viewBox="0 0 520 356"', assets[profile.STATS_PATH])
        self.assertIn(b'width="520"', assets[profile.STATS_PATH])
        self.assertIn(b'height="356"', assets[profile.STATS_PATH])
        self.assertIn(b"past 12 months", assets[profile.STATS_PATH])
        self.assertIn(b"PULL REQUESTS OPENED", assets[profile.STATS_PATH])
        self.assertIn(b"ISSUES OPENED", assets[profile.STATS_PATH])
        self.assertIn(b"CONTRIBUTION ACTIVITY", assets[profile.ACTIVITY_PATH])
        self.assertIn(b'viewBox="0 0 600 360"', assets[profile.ACTIVITY_PATH])
        self.assertIn(b'width="600"', assets[profile.ACTIVITY_PATH])
        self.assertIn(b'height="360"', assets[profile.ACTIVITY_PATH])
        activity_root = ET.fromstring(assets[profile.ACTIVITY_PATH])
        full_text = " ".join(node.text or "" for node in activity_root.iter())
        self.assertIn("contributions per 7-day period", full_text)
        source_days = valid_assets_data()["days"][:-1]
        source_dates = [day["date"] for day in source_days]
        weekly = profile._aggregate_7day_periods(
            [(day["date"], day["count"]) for day in valid_assets_data()["days"]], NOW.date()
        )
        self.assertEqual(len(weekly), 52)
        plotted_dates = []
        for index, period in enumerate(weekly):
            chunk = source_days[index * 7 : (index + 1) * 7]
            self.assertEqual(len(chunk), 7)
            self.assertEqual(period, (chunk[0]["date"], chunk[-1]["date"], sum(day["count"] for day in chunk)))
            plotted_dates.extend(chunk_day["date"] for chunk_day in chunk)
        self.assertEqual(plotted_dates, source_dates)
        self.assertEqual(len(set(plotted_dates)), 364)
        expected_total = sum(day["count"] for day in source_days)
        self.assertEqual(expected_total, sum(period[2] for period in weekly))
        self.assertIn(f"{expected_total:,} contributions · {weekly[0][0]} to {weekly[-1][1]} UTC", full_text)
        polyline = next(node for node in activity_root.iter() if node.tag.endswith("polyline"))
        self.assertEqual(len(polyline.attrib["points"].split()), 52)
        month_labels = [
            node.text
            for node in activity_root.iter()
            if node.tag.endswith("text") and node.text in {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"}
        ]
        self.assertGreaterEqual(len(month_labels), 5)
        self.assertLessEqual(len(month_labels), 7)

    def test_calendar_rejects_negative_gapped_and_duplicate_data(self) -> None:
        data = valid_assets_data()
        data["days"][20]["count"] = -1
        with self.assertRaises(profile.ProfileAssetError):
            profile.build_activity_svg(data, NOW)

        data = valid_assets_data()
        data["days"].pop(20)
        with self.assertRaises(profile.ProfileAssetError):
            profile.build_activity_svg(data, NOW)

        data = valid_assets_data()
        data["days"].pop()
        with self.assertRaises(profile.ProfileAssetError):
            profile.build_activity_svg(data, NOW)

        data = valid_assets_data()
        data["days"][20]["date"] = data["days"][19]["date"]
        with self.assertRaises(profile.ProfileAssetError):
            profile.build_activity_svg(data, NOW)

    def test_mismatched_profile_identity_is_rejected(self) -> None:
        data = valid_assets_data()
        data["login"] = "some-other-user"
        with self.assertRaises(profile.ProfileAssetError):
            profile.build_stats_svg(data, NOW)

    def test_invalid_second_asset_prevents_all_publication(self) -> None:
        server = FakeGitHub()
        assets = valid_assets()
        assets[profile.ACTIVITY_PATH] = b"not-an-svg"
        with patch.object(profile, "urlopen", server.open):
            with self.assertRaises(profile.ProfileAssetError):
                profile.publish_assets("https://api.github.com", "example-token", "r-siddiq/r-siddiq", assets)
        self.assertEqual(server.calls, [])

    def test_first_publication_creates_only_owned_branch_files(self) -> None:
        server = FakeGitHub()
        with patch.object(profile, "urlopen", server.open):
            sha = profile.publish_assets("https://api.github.com", "example-token", "r-siddiq/r-siddiq", valid_assets())
        tree = server.trees[server.commits[sha]["tree"]["sha"]]
        self.assertEqual(set(tree), {profile.MARKER_PATH, profile.STATS_PATH, profile.ACTIVITY_PATH})
        self.assertEqual(server.commits[sha]["parents"], [])
        ref_calls = [call for call in server.calls if call[0] == "POST" and call[1].endswith("/git/refs")]
        self.assertEqual(len(ref_calls), 1)

    def test_existing_owned_branch_preserves_unrelated_files_and_never_forces(self) -> None:
        assets = valid_assets()
        unrelated = b"Keep this unrelated file.\n"
        server = FakeGitHub(
            existing={"notes.txt": unrelated, profile.STATS_PATH: b"old", profile.ACTIVITY_PATH: b"old"},
            marker=profile.MARKER_CONTENT,
        )
        old_commit = server.branch_sha
        with patch.object(profile, "urlopen", server.open):
            new_commit = profile.publish_assets("https://api.github.com", "example-token", "r-siddiq/r-siddiq", assets)
        tree = server.trees[server.commits[new_commit]["tree"]["sha"]]
        self.assertIn("notes.txt", tree)
        self.assertEqual(server.blobs[tree["notes.txt"]["sha"]], unrelated)
        self.assertEqual(server.commits[new_commit]["parents"], [old_commit])
        patch_calls = [call for call in server.calls if call[0] == "PATCH"]
        self.assertEqual(patch_calls[0][2]["force"], False)

    def test_unmarked_branch_is_never_modified(self) -> None:
        server = FakeGitHub(existing={"keep.txt": b"unrelated"}, marker=None)
        with patch.object(profile, "urlopen", server.open):
            with self.assertRaises(profile.ProfileAssetError):
                profile.publish_assets("https://api.github.com", "example-token", "r-siddiq/r-siddiq", valid_assets())
        writes = [call for call in server.calls if call[0] in {"POST", "PATCH"}]
        self.assertEqual(writes, [])

    def test_remote_http_body_and_token_are_never_in_error(self) -> None:
        body_marker = "synthetic-redaction-check"

        def failing_opener(_request, timeout=20):
            del timeout
            raise HTTPError("https://api.github.com/graphql", 403, "Forbidden", None, io.BytesIO(body_marker.encode()))

        api = profile.GraphQLClient("synthetic-token-value", opener=failing_opener)
        with self.assertRaises(profile.ProfileAssetError) as error:
            api.graphql(profile.CONTRIBUTIONS_QUERY, {"login": "r-siddiq", "from": "2025-11-04T00:00:00Z", "to": "2026-11-03T00:00:00Z"})
        self.assertNotIn(body_marker, str(error.exception))
        self.assertNotIn("synthetic-token-value", str(error.exception))

    def test_graphql_errors_and_partial_responses_fail_before_publish(self) -> None:
        private_error_detail = "synthetic-private-error-detail"
        api = profile.GraphQLClient(
            "synthetic-token-value",
            opener=lambda _request, timeout=20: FakeResponse(
                {"errors": [{"message": private_error_detail}]}
            ),
        )
        with self.assertRaises(profile.ProfileAssetError) as error:
            api.graphql(profile.CONTRIBUTIONS_QUERY, {})
        self.assertNotIn(private_error_detail, str(error.exception))
        self.assertNotIn("synthetic-token-value", str(error.exception))

        class PartialGraphQL:
            def graphql(self, _query: str, _variables: dict) -> dict:
                return {
                    "user": {
                        "login": "r-siddiq",
                        "contributionsCollection": {},
                    }
                }

        with patch.object(profile, "publish_assets") as publisher:
            with self.assertRaises(profile.ProfileAssetError):
                assets = profile.generate_assets(PartialGraphQL(), NOW)
                profile.publish_assets("https://api.github.com", "synthetic-token-value", "r-siddiq/r-siddiq", assets)
        publisher.assert_not_called()


if __name__ == "__main__":
    main(verbosity=2)
