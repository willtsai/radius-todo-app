#!/usr/bin/env python3
"""
Compare app-graph.json between main (base) and the PR branch (head)
and produce a Markdown PR comment with:
  - Side-by-side Mermaid graphs (main vs PR)
  - A color-coded diff graph (green=added, amber=modified, red=removed)
  - Clickable nodes linking to the PR diff page
  - Resources/connections change table
  - "Powered by Radius" footer

The HEAD graph is generated fresh by `rad app graph` in the PR
workflow and read from disk.  The BASE graph is read from main's
committed .radius/app-graph.json via `git show`.

Usage (CI):
    python scripts/graph_diff.py

Environment variables:
    BASE_SHA     — base commit SHA (PR target branch, i.e. main).
    HEAD_GRAPH   — path to the freshly generated app-graph.json
                   from the PR branch's app.bicep.
    DIFF_OUTPUT  — path to write the Markdown output (default: stdout).
    PR_NUMBER    — PR number for building diff URLs.
    REPO_OWNER   — repository owner (e.g. "nithyatsu").
    REPO_NAME    — repository name (e.g. "prototype").
"""

import hashlib
import json
import os
import re
import subprocess
import sys

# Import detailed-mode helpers from our sibling module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate_architecture import (
    is_detailed_mode,
    make_detailed_label,
    resolve_image_tag,
    _resolve_param_image,
)


# ── helpers ──────────────────────────────────────────────────────────

def git_show(sha: str, path: str) -> str | None:
    """Return file contents at a given commit, or None if missing."""
    try:
        result = subprocess.run(
            ["git", "show", f"{sha}:{path}"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError:
        return None


def parse_graph(raw: str | None) -> dict:
    """Parse app-graph JSON into a normalised dict."""
    if not raw:
        return {"resources": {}, "connections": []}
    data = json.loads(raw)
    resources = {}
    for r in data.get("resources", []):
        resources[r.get("id", r.get("name", ""))] = r
    connections = []
    for c in data.get("connections", []):
        if c.get("type") != "dependsOn":
            connections.append((c.get("sourceId", ""), c.get("targetId", "")))
    return {"resources": resources, "connections": connections}


def resolve_name(res_id: str, resources: dict) -> str:
    """Get a short display name from a resource id or target string."""
    if res_id in resources:
        return resources[res_id].get("name", res_id)
    # ARM expression like [reference('database').id]
    arm_match = re.match(r"\[reference\('(\w+)'\)", res_id)
    if arm_match:
        sym = arm_match.group(1)
        for r in resources.values():
            if r.get("name") == sym:
                return sym
        return sym
    # URL like http://backend:3000
    url_match = re.match(r"https?://([^:/]+)", res_id)
    if url_match:
        hostname = url_match.group(1)
        for r in resources.values():
            if r.get("name") == hostname:
                return hostname
        return hostname
    # Last path segment
    return res_id.rstrip("/").rsplit("/", 1)[-1]


def resource_label(res: dict, repo_owner: str = "", repo_name: str = "", pr_number: str = "") -> str:
    """Human-readable one-liner for a resource with linked source location."""
    name = res.get("name", "?")
    rtype = res.get("type", "").rsplit("/", 1)[-1]
    loc = res.get("sourceLocation", {})
    file = loc.get("file", "")
    line = loc.get("line", "")
    parts = [f"**{name}**", f"`{rtype}`"]
    if file:
        display = f"{file}:{line}" if line else file
        if repo_owner and repo_name and pr_number:
            # GitHub diff anchor: filename with / and . replaced by -
            anchor = file.replace("/", "-").replace(".", "-")
            url = f"https://github.com/{repo_owner}/{repo_name}/pull/{pr_number}/files#diff-{anchor}"
            if line:
                url += f"R{line}"
            parts.append(f"[{display}]({url})")
        else:
            parts.append(display)
    return " — ".join(parts)


def categorize(res_type: str) -> str:
    """Categorize a resource type."""
    t = res_type.lower()
    if "containers" in t:
        return "container"
    elif any(ds in t for ds in ["rediscaches", "sqldatabases", "mongodatabases"]):
        return "datastore"
    elif "applications" in t and "containers" not in t:
        return "application"
    return "other"


def safe_node_id(name: str) -> str:
    """Make a Mermaid-safe node id."""
    return name.replace("-", "_").replace(".", "_")


# ── diff logic ───────────────────────────────────────────────────────

def diff_graphs(base: dict, head: dict) -> dict:
    """Compute added / removed / modified resources and connections."""
    base_ids = set(base["resources"])
    head_ids = set(head["resources"])

    added = head_ids - base_ids
    removed = base_ids - head_ids
    common = base_ids & head_ids

    modified = set()
    for rid in common:
        if json.dumps(base["resources"][rid], sort_keys=True) != json.dumps(
            head["resources"][rid], sort_keys=True
        ):
            modified.add(rid)

    base_conns = set(base["connections"])
    head_conns = set(head["connections"])
    added_conns = head_conns - base_conns
    removed_conns = base_conns - head_conns

    return {
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchanged": common - modified,
        "added_conns": added_conns,
        "removed_conns": removed_conns,
    }


# ── mermaid generation ───────────────────────────────────────────────

def file_diff_anchor(file_path: str) -> str:
    """Compute GitHub's PR diff anchor for a file path.

    GitHub uses the SHA-256 hex digest of the file path.
    """
    return hashlib.sha256(file_path.encode()).hexdigest()


def make_mermaid_graph(resources: dict, connections: list,
                      detailed: bool = False, bicep_path: str | None = None) -> str:
    """Generate a simple Mermaid graph from resources and connections."""
    lines = []
    lines.append("%%{ init: { 'theme': 'base', 'themeVariables': { "
                 "'primaryColor': '#ffffff', "
                 "'primaryTextColor': '#1f2328', "
                 "'primaryBorderColor': '#d1d9e0', "
                 "'lineColor': '#2da44e', "
                 "'background': '#ffffff', "
                 "'mainBkg': '#ffffff', "
                 "'fontSize': '13px'"
                 " } } }%%")
    lines.append("graph LR")
    lines.append("    classDef container fill:#ffffff,stroke:#2da44e,stroke-width:1.5px,color:#1f2328,rx:6,ry:6")
    lines.append("    classDef datastore fill:#ffffff,stroke:#d4a72c,stroke-width:1.5px,color:#1f2328,rx:6,ry:6")
    lines.append("    classDef other fill:#ffffff,stroke:#d1d9e0,stroke-width:1.5px,color:#1f2328,rx:6,ry:6")

    for rid, res in resources.items():
        name = res.get("name", "unknown")
        res_type = res.get("type", "")
        cat = categorize(res_type)
        if cat == "application":
            continue
        nid = safe_node_id(name)

        if detailed and cat == "container":
            # Build detailed label with image:tag
            detail_res = {
                "display_name": name,
                "symbolic_name": name,
                "name": name,
                "category": cat,
                "image": res.get("properties", {}).get("container", {}).get("image"),
            }
            label = make_detailed_label(detail_res, bicep_path)
        else:
            label = name

        lines.append('    {}["{}"]:::{}'.format(nid, label, cat))

    for src_id, tgt_id in connections:
        src = resolve_name(src_id, resources)
        tgt = resolve_name(tgt_id, resources)
        src_res = resources.get(src_id, {})
        tgt_res = resources.get(tgt_id, {})
        if categorize(src_res.get("type", "")) == "application":
            continue
        if categorize(tgt_res.get("type", "")) == "application":
            continue
        if src != tgt:
            lines.append("    {} --> {}".format(safe_node_id(src), safe_node_id(tgt)))

    return "\n".join(lines)


def make_diff_mermaid(base: dict, head: dict, diff: dict,
                      repo_owner: str, repo_name: str, pr_number: str,
                      detailed: bool = False, bicep_path: str | None = None) -> str:
    """Generate a color-coded diff Mermaid graph.

    Colors:
      - Green: added
      - Amber: modified
      - Red dashed: removed
      - Gray: unchanged
    Clicking a node opens the PR diff page anchored to the source file.
    """
    lines = []
    lines.append("%%{ init: { 'theme': 'base', 'themeVariables': { "
                 "'primaryColor': '#ffffff', "
                 "'primaryTextColor': '#1f2328', "
                 "'primaryBorderColor': '#d1d9e0', "
                 "'lineColor': '#656d76', "
                 "'background': '#ffffff', "
                 "'mainBkg': '#ffffff', "
                 "'fontSize': '13px'"
                 " } } }%%")
    lines.append("graph LR")

    # Class definitions for diff states
    lines.append("    classDef added fill:#dafbe1,stroke:#1a7f37,stroke-width:2px,color:#1a7f37,rx:6,ry:6")
    lines.append("    classDef modified fill:#fff8c5,stroke:#d4a72c,stroke-width:2px,color:#9a6700,rx:6,ry:6")
    lines.append("    classDef removed fill:#ffebe9,stroke:#d1242f,stroke-width:2px,stroke-dasharray:5 5,color:#d1242f,rx:6,ry:6")
    lines.append("    classDef unchanged fill:#ffffff,stroke:#d1d9e0,stroke-width:1px,color:#656d76,rx:6,ry:6")

    all_resources = {**base["resources"], **head["resources"]}
    diff_url = f"https://github.com/{repo_owner}/{repo_name}/pull/{pr_number}/files"

    all_rids = sorted(set(list(head["resources"].keys()) + list(base["resources"].keys())))
    nodes_added = set()

    for rid in all_rids:
        res = all_resources.get(rid, {})
        name = res.get("name", "unknown")
        res_type = res.get("type", "")

        if categorize(res_type) == "application":
            continue

        if rid in diff["added"]:
            status = "added"
            prefix = "+ "
        elif rid in diff["removed"]:
            status = "removed"
            prefix = "- "
        elif rid in diff["modified"]:
            status = "modified"
            prefix = "~ "
        else:
            status = "unchanged"
            prefix = ""

        cat = categorize(res_type)

        if detailed and cat == "container":
            # Build detailed label with image:tag
            detail_res = {
                "display_name": name,
                "symbolic_name": name,
                "name": name,
                "category": cat,
                "image": res.get("properties", {}).get("container", {}).get("image"),
            }
            base_label = make_detailed_label(detail_res, bicep_path)
            # Prepend the status prefix to the bold name inside the label
            label = base_label.replace(f"<b>{name}</b>", f"<b>{prefix}{name}</b>")
        else:
            label = f"{prefix}{name}"

        nid = safe_node_id(name)
        if nid not in nodes_added:
            lines.append('    {}["{}"]:::{}'.format(nid, label, status))
            nodes_added.add(nid)

    # Edges — union of base and head connections
    all_conns = set(head["connections"]) | set(base["connections"])
    for src_id, tgt_id in sorted(all_conns):
        src = resolve_name(src_id, all_resources)
        tgt = resolve_name(tgt_id, all_resources)
        src_res = all_resources.get(src_id, {})
        tgt_res = all_resources.get(tgt_id, {})
        if categorize(src_res.get("type", "")) == "application":
            continue
        if categorize(tgt_res.get("type", "")) == "application":
            continue
        if src == tgt:
            continue

        s = safe_node_id(src)
        t = safe_node_id(tgt)

        conn_tuple = (src_id, tgt_id)
        if conn_tuple in diff.get("added_conns", set()):
            lines.append("    {} -. new .-> {}".format(s, t))
        elif conn_tuple in diff.get("removed_conns", set()):
            lines.append("    {} -. removed .-> {}".format(s, t))
        else:
            lines.append("    {} --> {}".format(s, t))

    # Click directives — link to PR diff page anchored to the source file + line
    for rid in all_rids:
        res = all_resources.get(rid, {})
        name = res.get("name", "unknown")
        if categorize(res.get("type", "")) == "application":
            continue

        nid = safe_node_id(name)
        source_loc = res.get("sourceLocation", {})
        source_file = source_loc.get("file", "")
        line = source_loc.get("line", "")

        if source_file:
            anchor = file_diff_anchor(source_file)
            # For removed resources, link to the left (base) side of the diff
            if rid in diff["removed"]:
                line_anchor = f"L{line}" if line else ""
            else:
                line_anchor = f"R{line}" if line else ""
            file_url = f"{diff_url}#diff-{anchor}{line_anchor}"
            tooltip = f"{name} \u2014 {source_file} line {line}" if line else f"View diff for {name}"
            lines.append('    click {} href "{}" "{}" _blank'.format(nid, file_url, tooltip))

    return "\n".join(lines)


# ── markdown rendering ──────────────────────────────────────────────

def render_diff_section(app_path: str, base_graph: dict, head_graph: dict, diff: dict,
                        repo_owner: str, repo_name: str, pr_number: str,
                        detailed: bool = False, bicep_path: str | None = None) -> str:
    """Render a full Markdown section for one application's diff."""
    lines = []
    app_label = app_path.replace("/.radius/app-graph.json", "").replace(".radius/app-graph.json", "") or "(root)"
    lines.append(f"### 📦 `{app_label}`\n")

    has_changes = (diff["added"] or diff["removed"] or diff["modified"]
                   or diff["added_conns"] or diff["removed_conns"])

    if not has_changes:
        lines.append("> No resource or connection changes.\n")
        return "\n".join(lines)

    # ── Side-by-side graphs ──
    base_mermaid = make_mermaid_graph(base_graph["resources"], base_graph["connections"],
                                      detailed=detailed, bicep_path=bicep_path)
    head_mermaid = make_mermaid_graph(head_graph["resources"], head_graph["connections"],
                                      detailed=detailed, bicep_path=bicep_path)

    lines.append("<table>")
    lines.append("<tr><th>📌 main</th><th>🔀 This PR</th></tr>")
    lines.append("<tr><td>\n")
    lines.append("```mermaid")
    lines.append(base_mermaid)
    lines.append("```")
    lines.append("\n</td><td>\n")
    lines.append("```mermaid")
    lines.append(head_mermaid)
    lines.append("```")
    lines.append("\n</td></tr>")
    lines.append("</table>\n")

    # ── Diff graph ──
    diff_mermaid = make_diff_mermaid(base_graph, head_graph, diff,
                                     repo_owner, repo_name, pr_number,
                                     detailed=detailed, bicep_path=bicep_path)
    lines.append("#### Diff\n")
    lines.append("🟢 Added  🟡 Modified  🔴 Removed\n")
    lines.append("```mermaid")
    lines.append(diff_mermaid)
    lines.append("```\n")

    # ── Resources table ──
    if diff["added"] or diff["removed"] or diff["modified"]:
        lines.append("#### Resources\n")
        lines.append("| Status | Resource |")
        lines.append("|--------|----------|")
        for rid in sorted(diff["added"]):
            lines.append(f"| 🟢 Added | {resource_label(head_graph['resources'][rid], repo_owner, repo_name, pr_number)} |")
        for rid in sorted(diff["removed"]):
            lines.append(f"| 🔴 Removed | {resource_label(base_graph['resources'][rid], repo_owner, repo_name, pr_number)} |")
        for rid in sorted(diff["modified"]):
            lines.append(f"| 🟡 Modified | {resource_label(head_graph['resources'][rid], repo_owner, repo_name, pr_number)} |")
        lines.append("")

    # ── Connections table ──
    all_resources = {**base_graph["resources"], **head_graph["resources"]}
    if diff["added_conns"] or diff["removed_conns"]:
        lines.append("#### Connections\n")
        lines.append("| Status | Connection |")
        lines.append("|--------|------------|")
        for src, tgt in sorted(diff["added_conns"]):
            lines.append(f"| 🟢 Added | {resolve_name(src, all_resources)} → {resolve_name(tgt, all_resources)} |")
        for src, tgt in sorted(diff["removed_conns"]):
            lines.append(f"| 🔴 Removed | {resolve_name(src, all_resources)} → {resolve_name(tgt, all_resources)} |")
        lines.append("")

    # Summary
    summary = []
    if diff["added"]:
        summary.append(f"+{len(diff['added'])} added")
    if diff["removed"]:
        summary.append(f"-{len(diff['removed'])} removed")
    if diff["modified"]:
        summary.append(f"~{len(diff['modified'])} modified")
    if diff["unchanged"]:
        summary.append(f"{len(diff['unchanged'])} unchanged")
    lines.append(f"*Resources: {', '.join(summary)}*\n")

    return "\n".join(lines)


def render_no_changes() -> str:
    return (
        "## � Architecture Changes\n\n"
        "> No architecture changes detected in this PR.\n\n"
        "---\n"
        "*Powered by [Radius](https://radapp.io/)*\n"
    )


def render_full_comment(sections: list) -> str:
    header = "## � Architecture Changes\n\n"
    footer = "\n---\n*Powered by [Radius](https://radapp.io/)*\n"
    return header + "\n".join(sections) + footer


# ── main ─────────────────────────────────────────────────────────────

def main():
    base_sha = os.environ.get("BASE_SHA", "")
    head_graph_path = os.environ.get("HEAD_GRAPH", "")
    output_path = os.environ.get("DIFF_OUTPUT", "")
    pr_number = os.environ.get("PR_NUMBER", "0")
    repo_owner = os.environ.get("REPO_OWNER", "nithyatsu")
    repo_name = os.environ.get("REPO_NAME", "prototype")

    detailed = is_detailed_mode()
    bicep_path = os.environ.get("BICEP_FILE")
    if detailed:
        print("Detailed mode ENABLED — nodes will show image:tag metadata")

    if not base_sha:
        print("Error: BASE_SHA must be set.", file=sys.stderr)
        sys.exit(1)

    # ── Head graph: read from the freshly generated file on disk ──
    head_raw = None
    if head_graph_path:
        try:
            with open(head_graph_path, "r") as f:
                head_raw = f.read()
        except FileNotFoundError:
            print(f"Warning: HEAD_GRAPH file not found: {head_graph_path}", file=sys.stderr)

    # ── Base graph: read from main's committed .radius/app-graph.json ──
    # Find the graph path in main's tree
    base_graph_path = ".radius/app-graph.json"
    base_raw = git_show(base_sha, base_graph_path)

    if not head_raw and not base_raw:
        result = render_no_changes()
    else:
        base_graph = parse_graph(base_raw)
        head_graph = parse_graph(head_raw)

        diff = diff_graphs(base_graph, head_graph)
        section = render_diff_section(
            base_graph_path, base_graph, head_graph, diff,
            repo_owner, repo_name, pr_number,
            detailed=detailed, bicep_path=bicep_path,
        )
        result = render_full_comment([section])

    # Output
    if output_path:
        with open(output_path, "w") as f:
            f.write(result)
        print(f"Diff written to {output_path}")
    else:
        print(result)


if __name__ == "__main__":
    main()
