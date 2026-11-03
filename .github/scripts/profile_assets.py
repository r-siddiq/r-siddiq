#!/usr/bin/env python3
"""Generate public profile SVGs and publish only owned assets to profile-assets.

The generator reads GitHub's public profile contribution summary and public,
non-fork repositories. It stores no response or token and never logs API bodies.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


EXPECTED_REPOSITORY = "r-siddiq/r-siddiq"
EXPECTED_LOGIN = "r-siddiq"
ASSET_BRANCH = "profile-assets"
STATS_PATH = "profile-stats.svg"
ACTIVITY_PATH = "activity-graph.svg"
MARKER_PATH = ".profile-assets-owned.json"
MARKER_CONTENT = (
    b'{"managed_by":"https://github.com/r-siddiq/r-siddiq/.github/workflows/profile-assets.yml",'
    b'"assets":["profile-stats.svg","activity-graph.svg"],"format":1}\n'
)
API_VERSION = "2022-11-28"
USER_AGENT = "r-siddiq-profile-assets/1.0"
MAX_GRAPHQL_PAGES = 100
MAX_PUBLIC_REPOSITORIES = MAX_GRAPHQL_PAGES * 100
MAX_CONTRIBUTIONS_PER_DAY = 10000
MAX_REPOSITORY_STARS = 10000000
SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)


class ProfileAssetError(Exception):
    """A safe-to-report operational error without response bodies or tokens."""


def _safe_int(value: Any, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProfileAssetError("GitHub returned a non-integer statistic")
    if value < 0 or value > maximum:
        raise ProfileAssetError("GitHub returned a statistic outside the accepted range")
    return value


def _utc_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _svg_root(width: int, height: int, title: str, description: str) -> ET.Element:
    root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "viewBox": f"0 0 {width} {height}",
            "width": str(width),
            "height": str(height),
            "role": "img",
            "aria-labelledby": "title description",
            "preserveAspectRatio": "xMidYMid meet",
        },
    )
    ET.SubElement(root, f"{{{SVG_NS}}}title", {"id": "title"}).text = title
    ET.SubElement(root, f"{{{SVG_NS}}}desc", {"id": "description"}).text = description
    return root


def _append_text(
    root: ET.Element,
    text: str,
    x: float,
    y: float,
    *,
    size: int = 14,
    fill: str = "#b9b9b9",
    weight: str = "400",
    anchor: str = "start",
    letter_spacing: str | None = None,
) -> None:
    attributes = {
        "x": f"{x:.2f}",
        "y": f"{y:.2f}",
        "fill": fill,
        "font-family": "-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif",
        "font-size": str(size),
        "font-weight": weight,
        "text-anchor": anchor,
    }
    if letter_spacing:
        attributes["letter-spacing"] = letter_spacing
    ET.SubElement(root, f"{{{SVG_NS}}}text", attributes).text = text


def _append_rect(root: ET.Element, x: float, y: float, width: float, height: float, fill: str, **extra: str) -> None:
    attributes = {
        "x": f"{x:.2f}",
        "y": f"{y:.2f}",
        "width": f"{width:.2f}",
        "height": f"{height:.2f}",
        "fill": fill,
        **extra,
    }
    ET.SubElement(root, f"{{{SVG_NS}}}rect", attributes)


def _append_line(root: ET.Element, x1: float, y1: float, x2: float, y2: float, stroke: str, width: float = 1.0) -> None:
    ET.SubElement(
        root,
        f"{{{SVG_NS}}}line",
        {
            "x1": f"{x1:.2f}",
            "y1": f"{y1:.2f}",
            "x2": f"{x2:.2f}",
            "y2": f"{y2:.2f}",
            "stroke": stroke,
            "stroke-width": f"{width:.2f}",
        },
    )


def _serialize_svg(root: ET.Element) -> bytes:
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    validate_svg(data)
    return data


def validate_svg(data: bytes) -> None:
    if not isinstance(data, bytes) or not data.startswith(b"<?xml"):
        raise ProfileAssetError("Generated image is not an XML SVG")
    if len(data) > 1_000_000:
        raise ProfileAssetError("Generated SVG exceeded the size limit")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ProfileAssetError("Generated SVG is invalid XML") from exc
    if root.tag != f"{{{SVG_NS}}}svg":
        raise ProfileAssetError("Generated image does not have an SVG root")
    forbidden_tags = {"script", "foreignObject", "image", "iframe", "object", "embed"}
    forbidden_attributes = {"href", "src", "onload", "onclick", "onerror"}
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1].lower()
        if local_name in {name.lower() for name in forbidden_tags}:
            raise ProfileAssetError("Generated SVG contains an active or remote element")
        for key, value in element.attrib.items():
            attr_name = key.rsplit("}", 1)[-1].lower()
            if attr_name in forbidden_attributes or "url(" in value.lower():
                raise ProfileAssetError("Generated SVG contains an external or active reference")
            if any(ord(char) < 0x20 and char not in "\t\n\r" for char in value):
                raise ProfileAssetError("Generated SVG contains invalid control characters")


def build_stats_svg(data: dict[str, Any], generated_at: datetime) -> bytes:
    login = data.get("login")
    stars = _safe_int(data.get("public_nonfork_stars"), maximum=MAX_REPOSITORY_STARS * 1000)
    commits = _safe_int(data.get("commits"), maximum=1_000_000)
    pull_requests = _safe_int(data.get("pull_requests"), maximum=1_000_000)
    issues = _safe_int(data.get("issues"), maximum=1_000_000)
    if login != EXPECTED_LOGIN:
        raise ProfileAssetError("GitHub returned a different profile identity")

    root = _svg_root(
        520,
        356,
        "Public GitHub profile statistics for r-siddiq",
        "Stars across public non-fork repositories and contribution counts from GitHub's visible contribution calendar for the past 12 months.",
    )
    _append_rect(root, 2, 2, 516, 352, "#151515", rx="16", stroke="#30303a", **{"stroke-width": "2"})
    _append_text(root, "PROFILE STATS", 20, 29, size=15, fill="#fe428e", weight="700", letter_spacing="1.5")
    _append_text(root, "Public repositories and visible contributions", 20, 51, size=14, fill="#b9b9b9")

    values = [
        ("PUBLIC STARS", stars, "non-fork repositories"),
        ("COMMITS", commits, "past 12 months"),
        ("PULL REQUESTS OPENED", pull_requests, "past 12 months"),
        ("ISSUES OPENED", issues, "past 12 months"),
    ]
    left, gap, width = 14.0, 12.0, 240.0
    top, row_gap, height = 69.0, 124.0, 110.0
    for index, (label, value, detail) in enumerate(values):
        x = left + (index % 2) * (width + gap)
        y = top + (index // 2) * row_gap
        _append_rect(root, x, y, width, height, "#1d1d27", rx="12", stroke="#3a3542", **{"stroke-width": "1"})
        _append_text(root, label, x + 16, y + 25, size=14, fill="#a8a8b3", weight="700", letter_spacing=".8")
        value_text = f"{value:,}"
        value_size = max(20, int(32 - max(0, len(value_text) - 9) * 1.5))
        _append_text(root, value_text, x + 16, y + 64, size=value_size, fill="#f0f0f0", weight="700")
        _append_text(root, detail, x + 16, y + 88, size=13, fill="#8f8f9d")

    updated = generated_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
    _append_text(root, f"PUBLIC DATA · UPDATED {updated} UTC", 20, 339, size=12, fill="#8f8f9d", weight="600", letter_spacing=".6")
    return _serialize_svg(root)


def _day_key(value: Any) -> str:
    if not isinstance(value, str):
        raise ProfileAssetError("GitHub returned an invalid contribution date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ProfileAssetError("GitHub returned an invalid contribution date") from exc
    return parsed.isoformat()


def _month_marks(days: list[tuple[str, int]], x0: float, plot_width: float) -> list[tuple[float, str]]:
    result: list[tuple[float, str]] = []
    if not days:
        return result
    first_date = date.fromisoformat(days[0][0])
    last_date = date.fromisoformat(days[-1][0])
    cursor = date(first_date.year, first_date.month, 1)
    total_days = max(1, (last_date - first_date).days)
    while cursor <= last_date:
        if cursor >= first_date:
            x = x0 + ((cursor - first_date).days / total_days) * plot_width
            result.append((x, cursor.strftime("%b")))
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    # Bi-monthly labels stay readable when the card is rendered at phone width.
    return result[::2]


def _aggregate_7day_periods(
    days: list[tuple[str, int]], current_utc_date: date
) -> list[tuple[str, str, int]]:
    expected_start = current_utc_date - timedelta(days=364)
    if len(days) != 365:
        raise ProfileAssetError("GitHub did not return exactly 365 daily contribution records")
    if days[0][0] != expected_start.isoformat() or days[-1][0] != current_utc_date.isoformat():
        raise ProfileAssetError("GitHub returned a contribution calendar outside the requested UTC dates")

    complete_days = days[:-1]
    if len(complete_days) != 364 or complete_days[-1][0] != (current_utc_date - timedelta(days=1)).isoformat():
        raise ProfileAssetError("The latest complete UTC contribution day is missing")

    periods: list[tuple[str, str, int]] = []
    for offset in range(0, len(complete_days), 7):
        window = complete_days[offset : offset + 7]
        if len(window) != 7:
            raise ProfileAssetError("GitHub returned an incomplete 7-day contribution period")
        start = date.fromisoformat(window[0][0])
        end = date.fromisoformat(window[-1][0])
        if (end - start).days != 6:
            raise ProfileAssetError("GitHub returned a contribution period with a date gap")
        total = sum(count for _, count in window)
        if total > MAX_CONTRIBUTIONS_PER_DAY * 7:
            raise ProfileAssetError("GitHub returned a weekly contribution total outside the accepted range")
        periods.append((start.isoformat(), end.isoformat(), total))

    if len(periods) != 52:
        raise ProfileAssetError("GitHub did not return 52 complete 7-day contribution periods")
    return periods


def build_activity_svg(data: dict[str, Any], generated_at: datetime) -> bytes:
    login = data.get("login")
    raw_days = data.get("days")
    if login != EXPECTED_LOGIN or not isinstance(raw_days, list) or not raw_days:
        raise ProfileAssetError("GitHub returned incomplete public activity data")

    normalized: list[tuple[str, int]] = []
    previous = ""
    for entry in raw_days:
        if not isinstance(entry, dict):
            raise ProfileAssetError("GitHub returned an invalid contribution day")
        key = _day_key(entry.get("date"))
        if key <= previous:
            raise ProfileAssetError("GitHub returned repeated or unordered contribution days")
        count = _safe_int(entry.get("count"), maximum=MAX_CONTRIBUTIONS_PER_DAY)
        if previous:
            previous_date = date.fromisoformat(previous)
            current_date = date.fromisoformat(key)
            if (current_date - previous_date).days != 1:
                raise ProfileAssetError("GitHub returned a contribution calendar with a gap")
        normalized.append((key, count))
        previous = key
    if len(normalized) != 365:
        raise ProfileAssetError("GitHub returned an unexpected contribution window")

    current_utc_date = generated_at.astimezone(timezone.utc).date()
    periods = _aggregate_7day_periods(normalized, current_utc_date)
    period_points = [(end, total) for _, end, total in periods]
    window_start, window_end = periods[0][0], periods[-1][1]
    root = _svg_root(
        600,
        360,
        "Public GitHub contribution totals by 7-day period for r-siddiq",
        f"Each point sums seven actual daily counts in one complete 7-day period. The 52 consecutive periods cover {window_start} through {window_end} UTC; today's partial contribution count is excluded.",
    )
    _append_rect(root, 2, 2, 596, 356, "#151515", rx="16", stroke="#30303a", **{"stroke-width": "2"})
    _append_text(root, "CONTRIBUTION ACTIVITY", 22, 34, size=17, fill="#fe428e", weight="700", letter_spacing="1.5")
    _append_text(root, "Visible public contributions per 7-day period · past 52 weeks", 22, 57, size=14, fill="#b9b9b9")

    x0, y0, plot_width, plot_height = 64.0, 88.0, 512.0, 178.0
    max_count = max(1, max(count for _, count in period_points))
    axis_max = max(4, ((max_count + 3) // 4) * 4)
    _append_rect(root, x0, y0, plot_width, plot_height, "#181820", rx="6")
    for step in range(5):
        value = axis_max - step * axis_max / 4
        y = y0 + step * plot_height / 4
        _append_line(root, x0, y, x0 + plot_width, y, "#30303a", 1)
        _append_text(root, f"{value:.0f}", x0 - 10, y + 5, size=15, fill="#a3a3af", anchor="end")

    point_count = len(period_points)
    points: list[tuple[float, float]] = []
    for index, (_, value) in enumerate(period_points):
        x = x0 + (index / max(1, point_count - 1)) * plot_width
        y = y0 + plot_height - (value / axis_max) * plot_height
        points.append((x, y))

    line_points = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    polygon_points = f"{x0:.2f},{y0 + plot_height:.2f} " + line_points + f" {x0 + plot_width:.2f},{y0 + plot_height:.2f}"
    ET.SubElement(
        root,
        f"{{{SVG_NS}}}polygon",
        {"points": polygon_points, "fill": "#fe428e", "fill-opacity": "0.12"},
    )
    ET.SubElement(
        root,
        f"{{{SVG_NS}}}polyline",
        {
            "points": line_points,
            "fill": "none",
            "stroke": "#fe428e",
            "stroke-width": "3",
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
        },
    )
    for x, label in _month_marks(period_points, x0, plot_width):
        _append_line(root, x, y0, x, y0 + plot_height, "#252530", 1)
        _append_text(root, label, x, y0 + plot_height + 22, size=14, fill="#a3a3af", anchor="middle")

    total = sum(value for _, value in period_points)
    _append_text(root, f"{total:,} contributions · {window_start} to {window_end} UTC", 22, 338, size=14, fill="#a3a3af", weight="600")
    return _serialize_svg(root)


CONTRIBUTIONS_QUERY = """
query ProfileContributions($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    login
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      totalIssueContributions
      totalPullRequestContributions
      contributionCalendar {
        weeks {
          contributionDays { date contributionCount }
        }
      }
    }
  }
}
"""


PUBLIC_REPOSITORIES_QUERY = """
query PublicRepositories($login: String!, $after: String) {
  user(login: $login) {
    login
    repositories(
      first: 100
      after: $after
      ownerAffiliations: [OWNER]
      privacy: PUBLIC
      isFork: false
      orderBy: { field: STARGAZERS, direction: DESC }
    ) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { stargazerCount }
    }
  }
}
"""


def fetch_public_profile(api: Any, generated_at: datetime) -> dict[str, Any]:
    login = EXPECTED_LOGIN
    end = generated_at.astimezone(timezone.utc)
    current_utc_date = end.date()
    start_date = current_utc_date - timedelta(days=364)
    start = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
    contribution_data = api.graphql(
        CONTRIBUTIONS_QUERY,
        {"login": login, "from": _utc_datetime(start), "to": _utc_datetime(end)},
    )
    user = contribution_data.get("user")
    returned_login = user.get("login") if isinstance(user, dict) else None
    if not isinstance(returned_login, str) or returned_login.casefold() != login.casefold():
        raise ProfileAssetError("GitHub did not return the expected public profile")
    collection = user.get("contributionsCollection")
    if not isinstance(collection, dict):
        raise ProfileAssetError("GitHub returned an incomplete contribution calendar")
    calendar = collection.get("contributionCalendar")
    if not isinstance(calendar, dict):
        raise ProfileAssetError("GitHub returned an incomplete contribution calendar")
    weeks = calendar.get("weeks")
    if not isinstance(weeks, list):
        raise ProfileAssetError("GitHub returned an invalid contribution calendar")
    days: list[dict[str, Any]] = []
    for week in weeks:
        if not isinstance(week, dict) or not isinstance(week.get("contributionDays"), list):
            raise ProfileAssetError("GitHub returned an invalid contribution week")
        for entry in week["contributionDays"]:
            if not isinstance(entry, dict):
                raise ProfileAssetError("GitHub returned an invalid contribution day")
            key = _day_key(entry.get("date"))
            entry_date = date.fromisoformat(key)
            if start_date <= entry_date <= current_utc_date:
                days.append({"date": key, "count": entry.get("contributionCount")})

    stars = 0
    cursor: str | None = None
    expected_total: int | None = None
    repositories_seen = 0
    seen_cursors: set[str] = set()
    for _ in range(MAX_GRAPHQL_PAGES):
        repos_data = api.graphql(PUBLIC_REPOSITORIES_QUERY, {"login": login, "after": cursor})
        repos_user = repos_data.get("user")
        returned_repo_login = repos_user.get("login") if isinstance(repos_user, dict) else None
        if not isinstance(returned_repo_login, str) or returned_repo_login.casefold() != login.casefold():
            raise ProfileAssetError("GitHub did not return the expected public repositories")
        connection = repos_user.get("repositories")
        if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
            raise ProfileAssetError("GitHub returned an invalid public repository page")
        page_info = connection.get("pageInfo")
        if not isinstance(page_info, dict):
            raise ProfileAssetError("GitHub returned invalid public repository pagination")
        page_total = _safe_int(connection.get("totalCount"), maximum=MAX_PUBLIC_REPOSITORIES)
        if expected_total is None:
            expected_total = page_total
        elif page_total != expected_total:
            raise ProfileAssetError("GitHub's public repository list changed during pagination")
        if len(connection["nodes"]) > 100:
            raise ProfileAssetError("GitHub returned more repositories than requested per page")
        for repo in connection["nodes"]:
            if not isinstance(repo, dict):
                raise ProfileAssetError("GitHub returned an invalid repository")
            stars += _safe_int(repo.get("stargazerCount"), maximum=MAX_REPOSITORY_STARS)
        repositories_seen += len(connection["nodes"])
        if repositories_seen > expected_total:
            raise ProfileAssetError("GitHub returned more public repositories than its reported total")
        if stars > MAX_REPOSITORY_STARS * max(1, expected_total):
            raise ProfileAssetError("GitHub returned a star count outside the accepted range")
        has_next = page_info.get("hasNextPage")
        if not isinstance(has_next, bool):
            raise ProfileAssetError("GitHub returned invalid public repository pagination")
        if not has_next:
            break
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
            raise ProfileAssetError("GitHub returned a repeated repository page")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise ProfileAssetError("GitHub public repository pagination exceeded the safety limit")

    if repositories_seen != expected_total:
        raise ProfileAssetError("GitHub returned an incomplete public repository list")

    commit_count = _safe_int(collection.get("totalCommitContributions"), maximum=1_000_000)
    issue_count = _safe_int(collection.get("totalIssueContributions"), maximum=1_000_000)
    pull_request_count = _safe_int(collection.get("totalPullRequestContributions"), maximum=1_000_000)
    total_visible = sum(_safe_int(day.get("count"), maximum=MAX_CONTRIBUTIONS_PER_DAY) for day in days)

    return {
        "login": login,
        "public_nonfork_stars": stars,
        "commits": commit_count,
        "pull_requests": pull_request_count,
        "issues": issue_count,
        "days": days,
        "visible_total": total_visible,
    }


def generate_assets(api: Any, generated_at: datetime) -> dict[str, bytes]:
    data = fetch_public_profile(api, generated_at)
    stats = build_stats_svg(data, generated_at)
    activity = build_activity_svg(data, generated_at)
    # Both assets must be valid before the caller can attempt to publish either.
    validate_svg(stats)
    validate_svg(activity)
    return {STATS_PATH: stats, ACTIVITY_PATH: activity}


def _git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def _request_json(api_base: str, token: str, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = api_base.rstrip("/") + path
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            if response.status == 204:
                return {}
            body = response.read()
            if not body:
                return {}
            result = json.loads(body)
    except HTTPError as exc:
        raise ProfileAssetError(f"GitHub asset publication failed with HTTP {exc.code}; existing assets were not intentionally replaced") from None
    except URLError:
        raise ProfileAssetError("GitHub asset publication failed to connect") from None
    except (TimeoutError, json.JSONDecodeError):
        raise ProfileAssetError("GitHub asset publication timed out or returned invalid JSON") from None
    if not isinstance(result, dict):
        raise ProfileAssetError("GitHub asset publication returned invalid data")
    return result


def _read_managed_branch(api_base: str, token: str, repo: str) -> tuple[str | None, str | None, dict[str, str]]:
    ref_path = f"/repos/{repo}/git/ref/heads/{quote(ASSET_BRANCH, safe='')}"
    try:
        ref = _request_json(api_base, token, "GET", ref_path)
    except ProfileAssetError as exc:
        if "HTTP 404" not in str(exc):
            raise
        # Verify the repository and token before treating a missing ref as first creation.
        repository = _request_json(api_base, token, "GET", f"/repos/{repo}")
        returned_repo = repository.get("full_name")
        if (
            not isinstance(returned_repo, str)
            or returned_repo.casefold() != repo.casefold()
            or repository.get("private") is True
            or repository.get("fork") is True
            or repository.get("archived") is True
            or repository.get("default_branch") != "main"
        ):
            raise ProfileAssetError("GitHub repository identity could not be verified") from None
        return None, None, {}

    object_data = ref.get("object")
    if not isinstance(object_data, dict) or not isinstance(object_data.get("sha"), str):
        raise ProfileAssetError("GitHub returned an invalid profile asset reference")
    head_sha = object_data["sha"]
    commit = _request_json(api_base, token, "GET", f"/repos/{repo}/git/commits/{quote(head_sha, safe='')}")
    tree = commit.get("tree")
    if not isinstance(tree, dict) or not isinstance(tree.get("sha"), str):
        raise ProfileAssetError("GitHub returned an invalid profile asset tree")
    tree_data = _request_json(api_base, token, "GET", f"/repos/{repo}/git/trees/{quote(tree['sha'], safe='')}")
    entries = tree_data.get("tree")
    if not isinstance(entries, list):
        raise ProfileAssetError("GitHub returned an invalid profile asset tree")
    root_entries: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and isinstance(entry.get("sha"), str):
            root_entries[entry["path"]] = entry["sha"]
    marker_sha = root_entries.get(MARKER_PATH)
    if marker_sha is None:
        raise ProfileAssetError("The profile-assets branch exists without the expected ownership marker; left untouched")
    marker = _request_json(api_base, token, "GET", f"/repos/{repo}/git/blobs/{quote(marker_sha, safe='')}")
    if marker.get("encoding") != "base64" or not isinstance(marker.get("content"), str):
        raise ProfileAssetError("The profile-assets ownership marker could not be validated; left untouched")
    try:
        marker_bytes = base64.b64decode(marker["content"], validate=False)
    except (ValueError, TypeError):
        raise ProfileAssetError("The profile-assets ownership marker could not be validated; left untouched") from None
    if marker_bytes.strip() != MARKER_CONTENT.strip():
        raise ProfileAssetError("The profile-assets branch is not owned by this workflow; left untouched")
    return head_sha, tree["sha"], root_entries


def publish_assets(api_base: str, token: str, repo: str, assets: dict[str, bytes]) -> str:
    if repo.casefold() != EXPECTED_REPOSITORY.casefold():
        raise ProfileAssetError("Refusing to publish assets outside the verified profile repository")
    if set(assets) != {STATS_PATH, ACTIVITY_PATH}:
        raise ProfileAssetError("Refusing to publish an unexpected set of files")
    for content in assets.values():
        validate_svg(content)

    parent_sha, base_tree_sha, existing_entries = _read_managed_branch(api_base, token, repo)
    expected_blob_shas = {path: _git_blob_sha(content) for path, content in assets.items()}
    if (
        parent_sha is not None
        and existing_entries.get(MARKER_PATH) is not None
        and all(existing_entries.get(path) == expected_blob_shas[path] for path in assets)
    ):
        # No API writes or new dangling blobs are needed when both SVGs already match.
        return parent_sha

    blob_shas: dict[str, str] = {}
    for path, content in assets.items():
        result = _request_json(
            api_base,
            token,
            "POST",
            f"/repos/{repo}/git/blobs",
            {"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"},
        )
        blob_sha = result.get("sha")
        if not isinstance(blob_sha, str):
            raise ProfileAssetError("GitHub did not return an asset identifier")
        blob_shas[path] = blob_sha

    marker_sha = existing_entries.get(MARKER_PATH)
    if parent_sha is None:
        marker_blob = _request_json(
            api_base,
            token,
            "POST",
            f"/repos/{repo}/git/blobs",
            {"content": base64.b64encode(MARKER_CONTENT).decode("ascii"), "encoding": "base64"},
        )
        marker_sha = marker_blob.get("sha")
        if not isinstance(marker_sha, str):
            raise ProfileAssetError("GitHub did not return the ownership marker identifier")

    changes = [
        {"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}
        for path, blob_sha in blob_shas.items()
    ]
    if parent_sha is None:
        changes.append({"path": MARKER_PATH, "mode": "100644", "type": "blob", "sha": marker_sha})

    tree_payload: dict[str, Any] = {"tree": changes}
    if base_tree_sha is not None:
        tree_payload["base_tree"] = base_tree_sha
    new_tree = _request_json(api_base, token, "POST", f"/repos/{repo}/git/trees", tree_payload)
    tree_sha = new_tree.get("sha")
    if not isinstance(tree_sha, str):
        raise ProfileAssetError("GitHub did not return a profile asset tree")
    if base_tree_sha is not None and tree_sha == base_tree_sha:
        if parent_sha is None:
            raise ProfileAssetError("Unexpected empty profile asset tree")
        return parent_sha

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    actor = {"name": "github-actions[bot]", "email": "41898282+github-actions[bot]@users.noreply.github.com", "date": now}
    commit_payload: dict[str, Any] = {
        "message": "Refresh generated profile SVG assets",
        "tree": tree_sha,
        "author": actor,
        "committer": actor,
    }
    if parent_sha is not None:
        commit_payload["parents"] = [parent_sha]
    else:
        commit_payload["parents"] = []
    new_commit = _request_json(api_base, token, "POST", f"/repos/{repo}/git/commits", commit_payload)
    commit_sha = new_commit.get("sha")
    if not isinstance(commit_sha, str):
        raise ProfileAssetError("GitHub did not return a profile asset commit")

    encoded_ref = quote(f"refs/heads/{ASSET_BRANCH}", safe="/")
    if parent_sha is None:
        _request_json(api_base, token, "POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{ASSET_BRANCH}", "sha": commit_sha})
    else:
        _request_json(api_base, token, "PATCH", f"/repos/{repo}/git/{encoded_ref}", {"sha": commit_sha, "force": False})
    return commit_sha


def _generate_command(args: argparse.Namespace) -> None:
    token = os.environ.get("PROFILE_WORKFLOW_TOKEN", "")
    if not token:
        raise ProfileAssetError("The workflow token is not available")
    repo = os.environ.get("PROFILE_REPOSITORY", "")
    if repo.casefold() != EXPECTED_REPOSITORY.casefold():
        raise ProfileAssetError("The workflow is not running in the verified profile repository")
    if os.environ.get("PROFILE_REF") != "refs/heads/main":
        raise ProfileAssetError("Profile assets may be generated from main only")

    generated_at = datetime.now(timezone.utc)
    api = GraphQLClient(token)
    assets = generate_assets(api, generated_at)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name, content in assets.items():
        path = output / name
        path.write_bytes(content)
    # Print paths and sizes only; do not print GraphQL responses or token values.
    for name, content in sorted(assets.items()):
        print(f"Generated {name} ({len(content)} bytes)")


def _publish_command(args: argparse.Namespace) -> None:
    token = os.environ.get("PROFILE_WORKFLOW_TOKEN", "")
    if not token:
        raise ProfileAssetError("The workflow token is not available")
    repo = os.environ.get("PROFILE_REPOSITORY", "")
    if repo.casefold() != EXPECTED_REPOSITORY.casefold():
        raise ProfileAssetError("Profile assets may be published from the verified profile repository only")
    if os.environ.get("PROFILE_REF") != "refs/heads/main":
        raise ProfileAssetError("Profile assets may be published from main only")
    assets: dict[str, bytes] = {}
    for name in (STATS_PATH, ACTIVITY_PATH):
        path = (Path(args.input_dir) / name).resolve()
        if not path.is_file():
            raise ProfileAssetError("Both generated SVG assets must exist before publication")
        assets[name] = path.read_bytes()
        validate_svg(assets[name])
    api_base = os.environ.get("PROFILE_API_URL", "https://api.github.com")
    parsed_api_url = urlsplit(api_base)
    if (
        parsed_api_url.scheme != "https"
        or parsed_api_url.netloc.casefold() != "api.github.com"
        or parsed_api_url.path not in ("", "/")
    ):
        raise ProfileAssetError("The GitHub API URL is not the expected public API")
    sha = publish_assets(api_base, token, repo, assets)
    print(f"Published generated profile assets at commit {sha}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate and publish public profile SVGs using GitHub Actions")
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate")
    generate.add_argument("--output", required=True)
    generate.set_defaults(handler=_generate_command)
    publish = subparsers.add_parser("publish")
    publish.add_argument("--input-dir", required=True)
    publish.set_defaults(handler=_publish_command)
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except ProfileAssetError as exc:
        print(f"Profile assets were not updated: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"Profile assets were not updated ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


class GraphQLClient:
    """Small token-aware GraphQL client with injectable request transport for tests."""

    def __init__(self, token: str, opener: Any = urlopen) -> None:
        self._token = token
        self._opener = opener

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables}, separators=(",", ":")).encode("utf-8")
        request = Request(
            "https://api.github.com/graphql",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with self._opener(request, timeout=20) as response:
                if response.status != 200:
                    raise ProfileAssetError(f"GitHub GraphQL returned HTTP {response.status}")
                payload = json.loads(response.read())
        except HTTPError as exc:
            raise ProfileAssetError(f"GitHub GraphQL returned HTTP {exc.code}") from None
        except URLError:
            raise ProfileAssetError("GitHub GraphQL could not be reached") from None
        except TimeoutError:
            raise ProfileAssetError("GitHub GraphQL timed out") from None
        except json.JSONDecodeError:
            raise ProfileAssetError("GitHub GraphQL returned invalid JSON") from None
        except OSError as exc:
            raise ProfileAssetError(f"GitHub GraphQL request failed ({type(exc).__name__})") from None

        if not isinstance(payload, dict) or payload.get("errors"):
            raise ProfileAssetError("GitHub GraphQL returned an error; no SVGs were created")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ProfileAssetError("GitHub GraphQL returned incomplete data")
        return data


if __name__ == "__main__":
    raise SystemExit(main())
