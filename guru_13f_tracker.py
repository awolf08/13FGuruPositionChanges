#!/usr/bin/env python3
"""Track quarter-over-quarter SEC 13F holding changes for famous investors."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession}"
DEFAULT_USER_AGENT = "guru-13f-tracker/1.0 contact@example.com"
DEFAULT_SUBMISSIONS_CACHE_TTL_HOURS = 24.0


@dataclass(frozen=True)
class Filing:
    cik: str
    accession: str
    form: str
    filing_date: str
    report_date: str


@dataclass
class Holding:
    cusip: str
    issuer: str
    title: str
    value_usd: int
    shares: int
    put_call: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.cusip, self.put_call)


def cik10(cik: str) -> str:
    return str(cik).strip().lstrip("0").zfill(10)


def cik_int(cik: str) -> str:
    return str(int(cik10(cik)))


def accession_nodash(accession: str) -> str:
    return accession.replace("-", "")


def request_text(
    url: str,
    user_agent: str,
    cache_path: Path | None = None,
    sleep: float = 0.12,
    cache_ttl: timedelta | None = None,
    refresh: bool = False,
) -> str:
    if cache_path and cache_path.exists() and not refresh:
        if cache_ttl is None:
            return cache_path.read_text(encoding="utf-8")
        cache_age = datetime.now(timezone.utc) - datetime.fromtimestamp(cache_path.stat().st_mtime, timezone.utc)
        if cache_age <= cache_ttl:
            return cache_path.read_text(encoding="utf-8")

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept-Encoding": "identity",
            "Accept": "application/json, application/xml, text/xml, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if cache_path and cache_path.exists():
            print(f"  warning: SEC request failed ({exc.code}); using cached {cache_path}", file=sys.stderr)
            return cache_path.read_text(encoding="utf-8")
        raise RuntimeError(f"SEC request failed ({exc.code}) for {url}") from exc
    except urllib.error.URLError as exc:
        if cache_path and cache_path.exists():
            print(f"  warning: SEC request failed ({exc.reason}); using cached {cache_path}", file=sys.stderr)
            return cache_path.read_text(encoding="utf-8")
        raise RuntimeError(f"SEC request failed ({exc.reason}) for {url}") from exc

    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(body, encoding="utf-8")
    time.sleep(sleep)
    return body

def recent_13f_filings(
    cik: str,
    user_agent: str,
    raw_dir: Path,
    submissions_cache_ttl: timedelta,
    refresh_submissions: bool = False,
) -> list[Filing]:
    cik = cik10(cik)
    url = SEC_SUBMISSIONS_URL.format(cik=cik)
    data = json.loads(
        request_text(
            url,
            user_agent,
            raw_dir / f"CIK{cik}.json",
            cache_ttl=submissions_cache_ttl,
            refresh=refresh_submissions,
        )
    )
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])

    filings: list[Filing] = []
    for form, accession, filing_date, report_date in zip(forms, accessions, filing_dates, report_dates):
        if form in {"13F-HR", "13F-HR/A"}:
            filings.append(Filing(cik, accession, form, filing_date, report_date))

    filings.sort(key=lambda filing: (filing.report_date, filing.filing_date, filing.accession), reverse=True)
    latest_by_report_date: dict[str, Filing] = {}
    for filing in filings:
        latest_by_report_date.setdefault(filing.report_date, filing)
    return list(latest_by_report_date.values())


def filing_index(cik: str, accession: str, user_agent: str, raw_dir: Path) -> dict:
    base = SEC_ARCHIVES_URL.format(cik_int=cik_int(cik), accession=accession_nodash(accession))
    index_url = f"{base}/index.json"
    cache_path = raw_dir / cik10(cik) / accession_nodash(accession) / "index.json"
    return json.loads(request_text(index_url, user_agent, cache_path))


def find_information_table(index_json: dict) -> str:
    items = index_json.get("directory", {}).get("item", [])
    names = [item.get("name", "") for item in items]

    xml_names = [name for name in names if name.lower().endswith(".xml")]
    ranked_patterns = [
        lambda name: "infotable" in name.lower(),
        lambda name: "form13finfo" in name.lower(),
        lambda name: name.lower().startswith("form13f_"),
        lambda name: name.lower() != "primary_doc.xml",
        lambda name: name.lower() == "primary_doc.xml",
    ]
    for pattern in ranked_patterns:
        matches = [name for name in xml_names if pattern(name)]
        if matches:
            return matches[0]

    if not xml_names:
        raise RuntimeError("No XML information table found in filing index")
    return xml_names[0]


def strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def child_text(element: ET.Element, names: Iterable[str], default: str = "") -> str:
    wanted = set(names)
    for child in list(element):
        if strip_ns(child.tag) in wanted:
            return (child.text or "").strip()
    return default


def parse_int(text: str) -> int:
    cleaned = text.replace(",", "").strip()
    if not cleaned:
        return 0
    try:
        return int(float(cleaned))
    except ValueError:
        return 0


def parse_value_usd(text: str) -> int:
    return parse_int(text) * 1000


def parse_holdings(xml_text: str) -> list[Holding]:
    root = ET.fromstring(xml_text)
    info_tables = [element for element in root.iter() if strip_ns(element.tag) == "infoTable"]
    holdings: list[Holding] = []

    for row in info_tables:
        shares_node = next((child for child in list(row) if strip_ns(child.tag) == "shrsOrPrnAmt"), None)
        shares = 0
        if shares_node is not None:
            shares = parse_int(child_text(shares_node, ["sshPrnamt"]))

        holdings.append(
            Holding(
                cusip=child_text(row, ["cusip"]).upper(),
                issuer=child_text(row, ["nameOfIssuer"]),
                title=child_text(row, ["titleOfClass"]),
                value_usd=parse_value_usd(child_text(row, ["value"])),
                shares=shares,
                put_call=child_text(row, ["putCall"]).upper(),
            )
        )

    return holdings


def holdings_for_filing(filing: Filing, user_agent: str, raw_dir: Path) -> list[Holding]:
    index_json = filing_index(filing.cik, filing.accession, user_agent, raw_dir)
    xml_name = find_information_table(index_json)
    base = SEC_ARCHIVES_URL.format(cik_int=cik_int(filing.cik), accession=accession_nodash(filing.accession))
    xml_url = f"{base}/{xml_name}"
    cache_path = raw_dir / cik10(filing.cik) / accession_nodash(filing.accession) / xml_name
    return parse_holdings(request_text(xml_url, user_agent, cache_path))


def aggregate_holdings(holdings: list[Holding]) -> list[Holding]:
    aggregated: dict[tuple[str, str], Holding] = {}
    for holding in holdings:
        key = holding.key
        if key not in aggregated:
            aggregated[key] = Holding(
                cusip=holding.cusip,
                issuer=holding.issuer,
                title=holding.title,
                value_usd=holding.value_usd,
                shares=holding.shares,
                put_call=holding.put_call,
            )
            continue
        aggregated[key].value_usd += holding.value_usd
        aggregated[key].shares += holding.shares
    return list(aggregated.values())


def compare_holdings(current: list[Holding], previous: list[Holding]) -> list[dict[str, object]]:
    current_by_key = {holding.key: holding for holding in aggregate_holdings(current)}
    previous_by_key = {holding.key: holding for holding in aggregate_holdings(previous)}
    all_keys = sorted(set(current_by_key) | set(previous_by_key))
    rows: list[dict[str, object]] = []

    for key in all_keys:
        cur = current_by_key.get(key)
        prev = previous_by_key.get(key)
        sample = cur or prev
        if sample is None:
            continue

        cur_shares = cur.shares if cur else 0
        prev_shares = prev.shares if prev else 0
        delta = cur_shares - prev_shares

        if prev is None:
            action = "NEW"
        elif cur is None:
            action = "EXIT"
        elif delta > 0:
            action = "ADD"
        elif delta < 0:
            action = "REDUCE"
        else:
            action = "UNCHANGED"

        if action == "UNCHANGED":
            continue

        pct_change = ""
        if prev_shares:
            pct_change = round(delta / prev_shares * 100, 2)

        rows.append(
            {
                "action": action,
                "issuer": sample.issuer,
                "title": sample.title,
                "cusip": sample.cusip,
                "put_call": sample.put_call,
                "previous_shares": prev_shares,
                "current_shares": cur_shares,
                "share_change": delta,
                "pct_change": pct_change,
                "previous_value_usd": prev.value_usd if prev else 0,
                "current_value_usd": cur.value_usd if cur else 0,
            }
        )

    order = {"NEW": 0, "EXIT": 1, "ADD": 2, "REDUCE": 3}
    rows.sort(key=lambda row: (order.get(str(row["action"]), 9), str(row["issuer"])))
    return rows


def portfolio_rows(current: list[Holding], previous: list[Holding], changes: list[dict[str, object]]) -> list[dict[str, object]]:
    current_by_key = {holding.key: holding for holding in aggregate_holdings(current)}
    previous_by_key = {holding.key: holding for holding in aggregate_holdings(previous)}
    change_by_key = {(str(row["cusip"]), str(row["put_call"])): str(row["action"]) for row in changes}
    rows: list[dict[str, object]] = []

    for key in sorted(set(current_by_key) | set(previous_by_key)):
        cur = current_by_key.get(key)
        prev = previous_by_key.get(key)
        sample = cur or prev
        if sample is None:
            continue
        rows.append(
            {
                "action": change_by_key.get(key, "HOLD"),
                "issuer": sample.issuer,
                "title": sample.title,
                "cusip": sample.cusip,
                "put_call": sample.put_call,
                "previous_value_usd": prev.value_usd if prev else 0,
                "current_value_usd": cur.value_usd if cur else 0,
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "investor",
        "manager",
        "cik",
        "current_report_date",
        "current_filing_date",
        "previous_report_date",
        "previous_filing_date",
        "action",
        "issuer",
        "title",
        "cusip",
        "put_call",
        "previous_shares",
        "current_shares",
        "share_change",
        "pct_change",
        "previous_value_usd",
        "current_value_usd",
        "previous_portfolio_pct",
        "current_portfolio_pct",
        "current_accession",
        "previous_accession",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def format_num(value: object) -> str:
    if value == "":
        return ""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def days_since(date_text: str) -> int | None:
    try:
        report_date = datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (datetime.now(timezone.utc).date() - report_date).days


def write_markdown(path: Path, groups: list[dict[str, object]]) -> None:
    lines = [
        "# Latest 13F Guru Changes",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "Note: 13F reports are delayed disclosures and usually omit shorts, many non-US holdings, cash, bonds, and some derivatives.",
        "",
    ]

    for group in groups:
        investor = group["investor"]
        manager = group["manager"]
        current = group["current"]
        previous = group["previous"]
        changes = group["changes"]
        error = group.get("error")

        lines.append(f"## {investor} - {manager}")
        if error:
            lines.extend(["", f"Error: {error}", ""])
            continue

        lines.extend(
            [
                "",
                f"Current: {current.report_date} filed {current.filing_date} ({current.form}, {current.accession})",
                f"Previous: {previous.report_date} filed {previous.filing_date} ({previous.form}, {previous.accession})",
                "",
            ]
        )
        age_days = days_since(current.report_date)
        if age_days is not None and age_days > 180:
            lines.extend(
                [
                    f"Warning: latest 13F report date is {age_days} days old. This manager may have stopped filing, changed entity, or delayed filings.",
                    "",
                ]
            )

        if not changes:
            lines.extend(["No share-count changes found.", ""])
            continue

        lines.extend(
            [
                "| Action | Issuer | Security | Shares Change | Previous | Current | % Change | Value ($) |",
                "|---|---|---|---:|---:|---:|---:|---:|",
            ]
        )
        for row in changes[:80]:
            pct = row["pct_change"]
            pct_text = "" if pct == "" else f"{pct}%"
            security = str(row["title"])
            if row["put_call"]:
                security = f"{security} {row['put_call']}"
            lines.append(
                "| {action} | {issuer} | {security} | {delta} | {prev} | {cur} | {pct} | {value} |".format(
                    action=row["action"],
                    issuer=str(row["issuer"]).replace("|", "\\|"),
                    security=security.replace("|", "\\|"),
                    delta=format_num(row["share_change"]),
                    prev=format_num(row["previous_shares"]),
                    cur=format_num(row["current_shares"]),
                    pct=pct_text,
                    value=format_num(row["current_value_usd"]),
                )
            )
        if len(changes) > 80:
            lines.append(f"\nShowing first 80 changes of {len(changes)}. See CSV for all rows.")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def pct(value: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(value / total * 100, 4)


def fmt_pct(value: object) -> str:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "0.00%"


def pct_point_text(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f} pp"


def write_html_chart(path: Path, groups: list[dict[str, object]]) -> None:
    min_portfolio_pct = 1.0
    action_colors = {
        "NEW": "#1f9d55",
        "ADD": "#2563eb",
        "REDUCE": "#d97706",
        "EXIT": "#dc2626",
        "HOLD": "#64748b",
    }
    lines = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>13F Guru Position Changes</title>",
        "<style>",
        ":root { color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f6f7f9; color: #1f2937; }",
        "body { margin: 0; padding: 28px; }",
        "main { max-width: 1280px; margin: 0 auto; }",
        "h1 { margin: 0 0 8px; font-size: 28px; }",
        ".note { margin: 0 0 22px; color: #5b6472; font-size: 14px; }",
        ".legend { display: flex; flex-wrap: wrap; gap: 10px 18px; margin: 0 0 22px; font-size: 13px; color: #374151; }",
        ".legend span { display: inline-flex; align-items: center; gap: 7px; }",
        ".swatch { width: 13px; height: 13px; border-radius: 3px; display: inline-block; }",
        "section { background: #fff; border: 1px solid #dde2ea; border-radius: 8px; padding: 18px; margin: 0 0 18px; }",
        "h2 { margin: 0; font-size: 18px; }",
        ".meta { margin: 6px 0 14px; font-size: 13px; color: #6b7280; }",
        ".pies { display: grid; grid-template-columns: repeat(2, minmax(260px, 1fr)); gap: 18px; align-items: start; }",
        ".piebox { display: grid; grid-template-columns: 220px 1fr; gap: 16px; align-items: center; min-width: 0; }",
        ".pie-title { margin: 0 0 10px; font-size: 14px; font-weight: 700; color: #374151; }",
        ".pie { width: 220px; aspect-ratio: 1; border-radius: 50%; border: 1px solid #d7dce5; box-shadow: inset 0 0 0 34px #fff; }",
        ".entries { display: grid; gap: 7px; min-width: 0; }",
        ".entry { display: grid; grid-template-columns: 13px minmax(0, 1fr) 56px; gap: 8px; align-items: center; font-size: 12px; }",
        ".entry-name { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }",
        ".entry-value { text-align: right; color: #374151; font-variant-numeric: tabular-nums; }",
        ".movers { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 0 0 16px; }",
        ".mover { border: 1px solid #e2e6ee; border-radius: 6px; padding: 10px; min-width: 0; }",
        ".mover-label { margin: 0 0 4px; font-size: 11px; color: #6b7280; text-transform: uppercase; }",
        ".mover-name { font-size: 13px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }",
        ".mover-value { margin-top: 3px; font-size: 12px; color: #374151; font-variant-numeric: tabular-nums; }",
        ".summary-head { display: flex; justify-content: space-between; gap: 16px; align-items: end; margin-bottom: 14px; }",
        ".summary-head p { margin: 4px 0 0; color: #6b7280; font-size: 13px; }",
        ".top-list { display: grid; gap: 10px; }",
        ".top-row { display: grid; grid-template-columns: 44px minmax(0, 1fr) 92px 82px; gap: 12px; align-items: center; border: 1px solid #e2e6ee; border-radius: 6px; padding: 10px 12px; }",
        ".rank { color: #6b7280; font-size: 12px; font-weight: 800; font-variant-numeric: tabular-nums; }",
        ".top-name { font-size: 13px; font-weight: 800; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }",
        ".top-meta { margin-top: 4px; color: #6b7280; font-size: 11px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }",
        ".bar-track { height: 9px; margin-top: 7px; background: #e8ecf2; border-radius: 999px; overflow: hidden; }",
        ".bar-fill { height: 100%; background: linear-gradient(90deg, #1f9d55, #2563eb); border-radius: 999px; }",
        ".top-pct, .top-holders { text-align: right; font-variant-numeric: tabular-nums; }",
        ".top-pct { font-size: 13px; font-weight: 800; }",
        ".top-holders { color: #6b7280; font-size: 12px; }",
        ".empty { color: #6b7280; font-size: 13px; }",
        ".warning { color: #9a3412; font-size: 13px; margin: 8px 0 0; }",
        "@media (max-width: 960px) { .pies { grid-template-columns: 1fr; } }",
        "@media (max-width: 760px) { .movers { grid-template-columns: repeat(2, minmax(0, 1fr)); } }",
        "@media (max-width: 640px) { body { padding: 14px; } .piebox { grid-template-columns: 1fr; } .pie { width: min(100%, 260px); } .movers { grid-template-columns: 1fr; } .summary-head { display: block; } .top-row { grid-template-columns: 34px minmax(0, 1fr); } .top-pct, .top-holders { text-align: left; grid-column: 2; } }",
        "</style>",
        "</head>",
        "<body><main>",
        "<h1>13F Guru Position Changes</h1>",
        f'<p class="note">Generated {html.escape(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))}. Pie slices are total 13F portfolio weight. Positions below {min_portfolio_pct:.0f}% are grouped as Other.</p>',
        '<div class="legend">',
    ]
    for action, color in action_colors.items():
        lines.append(f'<span><i class="swatch" style="background:{color}"></i>{action}</span>')
    lines.extend(['<span><i class="swatch" style="background:#e5e7eb"></i>OTHER</span>', "</div>"])

    def pie_gradient(slices: list[tuple[str, float]]) -> str:
        cursor = 0.0
        parts = []
        for color, value in slices:
            if value <= 0:
                continue
            start = cursor * 3.6
            cursor += value
            end = cursor * 3.6
            parts.append(f"{color} {start:.2f}deg {end:.2f}deg")
        if cursor < 100:
            parts.append(f"#e5e7eb {cursor * 3.6:.2f}deg 360deg")
        return "conic-gradient(" + ", ".join(parts or ["#e5e7eb 0deg 360deg"]) + ")"

    def pie_entries(rows: list[dict[str, object]], field: str) -> list[tuple[str, str, str, float]]:
        entries = []
        for row in rows:
            value = float(row.get(field, 0))
            if value <= 0:
                continue
            action = str(row["action"])
            color = action_colors.get(action, "#4b5563")
            issuer = str(row["issuer"])
            label = f"{issuer} · {action}"
            entries.append((color, issuer, label, value))
        entries.sort(key=lambda item: item[3], reverse=True)
        total = sum(item[3] for item in entries)
        if total < 100:
            entries.append(("#e5e7eb", "Other", "Other / <1%", max(0.0, 100 - total)))
        return entries

    def top_mover(rows: list[dict[str, object]], actions: set[str], largest_positive: bool) -> dict[str, object] | None:
        candidates = [row for row in rows if str(row["action"]) in actions]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda row: (
                float(row.get("current_portfolio_pct", 0)) - float(row.get("previous_portfolio_pct", 0))
                if largest_positive
                else float(row.get("previous_portfolio_pct", 0)) - float(row.get("current_portfolio_pct", 0))
            ),
        )

    def render_mover(label: str, row: dict[str, object] | None) -> str:
        if row is None:
            return f'<div class="mover"><p class="mover-label">{html.escape(label)}</p><div class="mover-name">None</div><div class="mover-value">-</div></div>'
        prev_pct = float(row.get("previous_portfolio_pct", 0))
        cur_pct = float(row.get("current_portfolio_pct", 0))
        delta = cur_pct - prev_pct
        action = str(row["action"])
        color = action_colors.get(action, "#4b5563")
        issuer = html.escape(str(row["issuer"]))
        return (
            f'<div class="mover">'
            f'<p class="mover-label">{html.escape(label)}</p>'
            f'<div class="mover-name" title="{issuer}">{issuer}</div>'
            f'<div class="mover-value"><i class="swatch" style="background:{color}"></i> {html.escape(action)} · {fmt_pct(prev_pct)} → {fmt_pct(cur_pct)} ({pct_point_text(delta)})</div>'
            f'</div>'
        )

    def top_combined_current_positions() -> list[dict[str, object]]:
        positions: dict[str, dict[str, object]] = {}
        for group in groups:
            if group.get("error"):
                continue
            investor = str(group["investor"])
            for row in group["portfolio_rows"]:
                issuer = str(row["issuer"])
                current_pct = float(row.get("current_portfolio_pct", 0))
                current_value = int(row.get("current_value_usd", 0))
                if not issuer or current_pct <= 0:
                    continue
                record = positions.setdefault(
                    issuer,
                    {
                        "issuer": issuer,
                        "current_portfolio_pct": 0.0,
                        "current_value_usd": 0,
                        "holders": set(),
                    },
                )
                record["current_portfolio_pct"] = float(record["current_portfolio_pct"]) + current_pct
                record["current_value_usd"] = int(record["current_value_usd"]) + current_value
                record["holders"].add(investor)

        return sorted(positions.values(), key=lambda row: float(row["current_portfolio_pct"]), reverse=True)[:10]

    for group in groups:
        investor = html.escape(str(group["investor"]))
        manager = html.escape(str(group["manager"]))
        error = group.get("error")
        lines.append("<section>")
        lines.append(f"<h2>{investor} - {manager}</h2>")
        if error:
            lines.append(f'<p class="warning">Error: {html.escape(str(error))}</p></section>')
            continue

        current = group["current"]
        previous = group["previous"]
        rows = [
            row
            for row in group["portfolio_rows"]
            if max(float(row.get("previous_portfolio_pct", 0)), float(row.get("current_portfolio_pct", 0))) >= min_portfolio_pct
        ]
        lines.append(
            f'<p class="meta">Current {html.escape(current.report_date)} filed {html.escape(current.filing_date)}; previous {html.escape(previous.report_date)} filed {html.escape(previous.filing_date)}.</p>'
        )
        age_days = days_since(current.report_date)
        if age_days is not None and age_days > 180:
            lines.append(f'<p class="warning">Latest 13F report date is {age_days} days old.</p>')

        if not rows:
            lines.append(f'<p class="empty">No positions at or above {min_portfolio_pct:.0f}% portfolio weight.</p></section>')
            continue

        biggest_add = top_mover(rows, {"ADD"}, True)
        biggest_reduce = top_mover(rows, {"REDUCE"}, False)
        biggest_new = top_mover(rows, {"NEW"}, True)
        biggest_exit = top_mover(rows, {"EXIT"}, False)
        lines.append('<div class="movers">')
        lines.append(render_mover("最大加仓", biggest_add))
        lines.append(render_mover("最大减仓", biggest_reduce))
        lines.append(render_mover("最大新进", biggest_new))
        lines.append(render_mover("最大清仓", biggest_exit))
        lines.append("</div>")

        visible = sorted(
            rows,
            key=lambda row: max(float(row.get("previous_portfolio_pct", 0)), float(row.get("current_portfolio_pct", 0))),
            reverse=True,
        )[:80]

        previous_entries = pie_entries(visible, "previous_portfolio_pct")
        current_entries = pie_entries(visible, "current_portfolio_pct")

        lines.append('<div class="pies">')
        for title, entries in [("原先", previous_entries), ("现在", current_entries)]:
            slices = [(color, value) for color, _issuer, _label, value in entries]
            lines.extend(
                [
                    "<div>",
                    f'<p class="pie-title">{title}</p>',
                    '<div class="piebox">',
                    f'<div class="pie" style="background:{html.escape(pie_gradient(slices))}"></div>',
                    '<div class="entries">',
                ]
            )
            for color, issuer, label, value in entries[:16]:
                lines.extend(
                    [
                        '<div class="entry">',
                        f'<i class="swatch" style="background:{color}"></i>',
                        f'<div class="entry-name" title="{html.escape(label)}">{html.escape(label)}</div>',
                        f'<div class="entry-value">{fmt_pct(value)}</div>',
                        "</div>",
                    ]
                )
            if len(entries) > 16:
                lines.append(f'<p class="meta">Showing 16 legend rows of {len(entries)} slices.</p>')
            lines.extend(
                [
                    "</div>",
                    "</div>",
                    "</div>",
                ],
            )
        lines.append("</div>")
        if len(rows) > len(visible):
            lines.append(f'<p class="meta">Pie uses top {len(visible)} changed positions by portfolio weight. CSV contains all rows.</p>')
        lines.append("</section>")

    top_positions = top_combined_current_positions()
    if top_positions:
        max_pct = max(float(row["current_portfolio_pct"]) for row in top_positions)
        lines.extend(
            [
                '<section class="summary">',
                '<div class="summary-head">',
                "<div>",
                "<h2>Combined Latest Portfolio Weight Top 10</h2>",
                "<p>Each row sums the latest 13F portfolio weight for the same issuer across all tracked investors.</p>",
                "</div>",
                f'<p>{len([group for group in groups if not group.get("error")])} investors included</p>',
                "</div>",
                '<div class="top-list">',
            ]
        )
        for rank, row in enumerate(top_positions, 1):
            issuer = html.escape(str(row["issuer"]))
            current_pct = float(row["current_portfolio_pct"])
            current_value = int(row["current_value_usd"])
            holders = sorted(str(holder) for holder in row["holders"])
            width = 0 if max_pct <= 0 else max(3.0, current_pct / max_pct * 100)
            lines.extend(
                [
                    '<div class="top-row">',
                    f'<div class="rank">#{rank}</div>',
                    "<div>",
                    f'<div class="top-name" title="{issuer}">{issuer}</div>',
                    f'<div class="top-meta" title="{html.escape("; ".join(holders))}">{html.escape("; ".join(holders))}</div>',
                    '<div class="bar-track">',
                    f'<div class="bar-fill" style="width:{width:.2f}%"></div>',
                    "</div>",
                    "</div>",
                    f'<div class="top-pct">{fmt_pct(current_pct)}</div>',
                    f'<div class="top-holders">{len(holders)} holders<br>${format_num(current_value)}</div>',
                    "</div>",
                ]
            )
        lines.extend(["</div>", "</section>"])

    lines.extend(["</main></body>", "</html>"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def load_investors(path: Path) -> list[dict[str, str]]:
    investors = json.loads(path.read_text(encoding="utf-8"))
    for investor in investors:
        missing = {"name", "manager", "cik"} - set(investor)
        if missing:
            raise ValueError(f"Investor entry missing fields: {sorted(missing)}")
    return investors


def run(args: argparse.Namespace) -> int:
    user_agent = args.user_agent or os.environ.get("SEC_USER_AGENT", DEFAULT_USER_AGENT)
    out_dir = Path(args.out)
    raw_dir = out_dir / "raw"
    investors = load_investors(Path(args.investors))
    submissions_cache_ttl = timedelta(hours=max(0.0, args.submissions_cache_ttl_hours))
    all_rows: list[dict[str, object]] = []
    groups: list[dict[str, object]] = []
    success_count = 0
    failure_count = 0

    for investor in investors:
        name = investor["name"]
        manager = investor["manager"]
        cik = cik10(investor["cik"])
        print(f"Fetching {name} / {manager} ({cik})...", file=sys.stderr)

        try:
            filings = recent_13f_filings(
                cik,
                user_agent,
                raw_dir,
                submissions_cache_ttl,
                refresh_submissions=args.refresh_submissions,
            )
            if len(filings) < 2:
                raise RuntimeError("Fewer than two 13F-HR filings found")

            current, previous = filings[0], filings[1]
            current_holdings = holdings_for_filing(current, user_agent, raw_dir)
            previous_holdings = holdings_for_filing(previous, user_agent, raw_dir)
            changes = compare_holdings(current_holdings, previous_holdings)
            current_total = sum(holding.value_usd for holding in current_holdings)
            previous_total = sum(holding.value_usd for holding in previous_holdings)
            portfolio = portfolio_rows(current_holdings, previous_holdings, changes)

            for row in portfolio:
                row["previous_portfolio_pct"] = pct(int(row["previous_value_usd"]), previous_total)
                row["current_portfolio_pct"] = pct(int(row["current_value_usd"]), current_total)

            for row in changes:
                row["previous_portfolio_pct"] = pct(int(row["previous_value_usd"]), previous_total)
                row["current_portfolio_pct"] = pct(int(row["current_value_usd"]), current_total)
                enriched = {
                    "investor": name,
                    "manager": manager,
                    "cik": cik,
                    "current_report_date": current.report_date,
                    "current_filing_date": current.filing_date,
                    "previous_report_date": previous.report_date,
                    "previous_filing_date": previous.filing_date,
                    **row,
                    "current_accession": current.accession,
                    "previous_accession": previous.accession,
                }
                all_rows.append(enriched)

            groups.append(
                {
                    "investor": name,
                    "manager": manager,
                    "current": current,
                    "previous": previous,
                    "changes": changes,
                    "portfolio_rows": portfolio,
                    "current_total": current_total,
                    "previous_total": previous_total,
                }
            )
            success_count += 1
        except Exception as exc:
            failure_count += 1
            groups.append({"investor": name, "manager": manager, "error": str(exc)})
            print(f"  warning: {exc}", file=sys.stderr)

    write_csv(out_dir / "latest_changes.csv", all_rows)
    write_markdown(out_dir / "latest_changes.md", groups)
    write_html_chart(out_dir / "latest_chart.html", groups)
    print(f"Wrote {out_dir / 'latest_changes.md'}")
    print(f"Wrote {out_dir / 'latest_changes.csv'}")
    print(f"Wrote {out_dir / 'latest_chart.html'}")
    if failure_count and (args.strict or success_count == 0):
        print(f"Completed with {failure_count} failure(s) and {success_count} success(es).", file=sys.stderr)
        return 1
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--investors", default="investors.json", help="Path to investor CIK JSON list")
    parser.add_argument("--out", default="reports", help="Output directory")
    parser.add_argument("--user-agent", help="SEC User-Agent, preferably 'name email@example.com'")
    parser.add_argument(
        "--submissions-cache-ttl-hours",
        type=float,
        default=DEFAULT_SUBMISSIONS_CACHE_TTL_HOURS,
        help="Hours to reuse cached SEC submissions indexes before refreshing them",
    )
    parser.add_argument(
        "--refresh-submissions",
        action="store_true",
        help="Ignore cached SEC submissions indexes and fetch fresh filing lists",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return a non-zero exit code if any investor fails",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(run(parse_args(sys.argv[1:])))
