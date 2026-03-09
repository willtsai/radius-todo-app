#!/usr/bin/env python3
"""
Generate an interactive Mermaid architecture diagram for README.md.

Two modes of operation:
  1. **rad app graph** (primary) — reads structured output from `rad app graph`
     via the RAD_GRAPH_OUTPUT env var. This is used in CI after Radius is
     installed in a Kind cluster.
  2. **Direct Bicep parsing** (fallback) — regex-parses app.bicep directly.
     Used locally or when `rad app graph` is not yet available.

The generated Mermaid diagram uses GitHub's visual style (white background,
rounded-corner nodes, green/amber borders) and has clickable nodes that open
the corresponding line in app.bicep on GitHub.
"""

import json
import re
import os
import sys


def is_detailed_mode() -> bool:
    """Check if detailed mode is enabled via the DETAILED env var."""
    return os.environ.get("DETAILED", "false").lower() in ("true", "1", "yes")


def resolve_image_tag(resource: dict, bicep_path: str | None = None) -> tuple[str, str] | None:
    """Resolve image and tag for a resource.

    Returns (image, tag) or None if the resource isn't a container.

    Priority:
      1. Explicit 'image' property (from rad graph JSON or Bicep parse)
      2. If image references a Bicep parameter, resolve the default value
      3. Fallback: resource name as image, 'latest' as tag
    """
    if resource.get("category") not in ("container",):
        return None

    image_str = resource.get("image")

    # If the image looks like an ARM parameter reference, try resolving from Bicep
    if not image_str or (image_str and image_str.startswith("[")):
        image_str = _resolve_param_image(resource, bicep_path)

    if not image_str:
        # Fallback: use resource name
        name = resource.get("display_name") or resource.get("symbolic_name") or resource.get("name", "unknown")
        return (name, "latest")

    # Split on last colon to separate image from tag
    if ":" in image_str:
        idx = image_str.rfind(":")
        return (image_str[:idx], image_str[idx + 1:])
    else:
        return (image_str, "latest")


def _resolve_param_image(resource: dict, bicep_path: str | None) -> str | None:
    """Try to resolve image from Bicep parameter defaults."""
    if not bicep_path or not os.path.exists(bicep_path):
        return None

    try:
        with open(bicep_path, "r") as f:
            bicep_content = f.read()
    except OSError:
        return None

    # Find all param declarations with string defaults that look like images
    # e.g.: param magpieimage string = 'ghcr.io/image-registry/magpie:latest'
    param_pattern = re.compile(
        r"param\s+(\w+)\s+string\s*=\s*'([^']+)'", re.MULTILINE
    )
    param_defaults = {m.group(1): m.group(2) for m in param_pattern.finditer(bicep_content)}

    # Now check if the resource's Bicep block references one of these params
    res_name = resource.get("symbolic_name") or resource.get("name", "")
    # Search for: resource <name> ... image: <paramName>
    res_block_pattern = re.compile(
        r"resource\s+" + re.escape(res_name) + r"\s+'[^']+'.+?image:\s*(\w+)",
        re.DOTALL,
    )
    m = res_block_pattern.search(bicep_content)
    if m:
        param_name = m.group(1)
        if param_name in param_defaults:
            return param_defaults[param_name]

    # Also try matching by display_name
    display_name = resource.get("display_name", "")
    if display_name and display_name != res_name:
        res_block_pattern2 = re.compile(
            r"name:\s*'" + re.escape(display_name) + r"'.+?image:\s*(\w+)",
            re.DOTALL,
        )
        m2 = res_block_pattern2.search(bicep_content)
        if m2:
            param_name = m2.group(1)
            if param_name in param_defaults:
                return param_defaults[param_name]

    return None


def make_detailed_label(resource: dict, bicep_path: str | None = None) -> str:
    """Build a detailed multi-line Mermaid label: name + image:tag."""
    name = resource.get("display_name") or resource.get("symbolic_name") or resource.get("name", "unknown")
    result = resolve_image_tag(resource, bicep_path)
    if result:
        image, tag = result
        return '<b>{}</b><br/><span style="color:#656d76">{}</span>'.format(
            name, f"{image}:{tag}"
        )
    return f"<b>{name}</b>"


def parse_bicep(bicep_path):
    """Parse a Bicep file and extract resources, connections, and line numbers."""
    with open(bicep_path, "r") as f:
        content = f.read()

    resources = []
    connections = []

    resource_pattern = re.compile(
        r"resource\s+(\w+)\s+'([^']+)'\s*=\s*\{(.*?)\n\}",
        re.DOTALL,
    )

    for match in resource_pattern.finditer(content):
        symbolic_name = match.group(1)
        resource_type = match.group(2)
        body = match.group(3)

        line_number = content[: match.start()].count("\n") + 1

        name_match = re.search(r"name:\s*'([^']+)'", body)
        display_name = name_match.group(1) if name_match else symbolic_name

        image_match = re.search(r"image:\s*'([^']+)'", body)
        image = image_match.group(1) if image_match else None

        port_match = re.search(r"containerPort:\s*(\d+)", body)
        port = port_match.group(1) if port_match else None

        if "containers" in resource_type.lower():
            category = "container"
        elif "rediscaches" in resource_type.lower():
            category = "datastore"
        elif "sqldatabases" in resource_type.lower():
            category = "datastore"
        elif "mongodatabases" in resource_type.lower():
            category = "datastore"
        elif "applications" in resource_type.lower() and "containers" not in resource_type.lower():
            category = "application"
        else:
            category = "other"

        resources.append({
            "symbolic_name": symbolic_name,
            "display_name": display_name,
            "resource_type": resource_type,
            "image": image,
            "port": port,
            "category": category,
            "line_number": line_number,
        })

        conn_pattern = re.compile(r"connections:\s*\{(.*?)\n\s*\}", re.DOTALL)
        conn_match = conn_pattern.search(body)
        if conn_match:
            conn_body = conn_match.group(1)
            # Extract source URLs/refs from connection entries
            source_urls = re.findall(r"source:\s*'([^']+)'", conn_body)
            for source_url in source_urls:
                # source can be a URL like 'http://http-back-ctnr-simple1:3000'
                # or a resource ref like 'backend.id'
                url_match_inner = re.match(r"https?://([^:/]+)", source_url)
                if url_match_inner:
                    target_hostname = url_match_inner.group(1)
                    connections.append({"from": symbolic_name, "to_hostname": target_hostname})
                else:
                    connections.append({"from": symbolic_name, "to": source_url})

        source_refs = re.findall(r"source:\s*(\w+)\.(id|connectionString)", body)
        for ref_name, _ in source_refs:
            conn = {"from": symbolic_name, "to": ref_name}
            if conn not in connections:
                connections.append(conn)

    # Resolve hostname-based connections to symbolic names
    # Build lookup: display_name (resource name) -> symbolic_name
    name_to_symbolic = {r["display_name"]: r["symbolic_name"] for r in resources}

    resolved_connections = []
    for conn in connections:
        if "to_hostname" in conn:
            hostname = conn["to_hostname"]
            # Match hostname against resource display names
            target_sym = name_to_symbolic.get(hostname)
            if target_sym:
                resolved_connections.append({"from": conn["from"], "to": target_sym})
            else:
                # Hostname didn't match any display name exactly; skip
                print(f"  Warning: could not resolve connection target hostname '{hostname}'")
        else:
            resolved_connections.append(conn)

    return resources, resolved_connections


def parse_rad_graph_output(output_path):
    """Parse the output of `rad app graph` and extract resources and connections.

    Expected format (JSON) from `rad app graph <file.bicep>`:
    {
      "metadata": { "sourceFiles": ["app.bicep"], ... },
      "resources": [
        {
          "id": "/planes/radius/local/resourceGroups/.../containers/frontend",
          "name": "frontend",
          "type": "Applications.Core/containers",
          "sourceLocation": { "file": "app.bicep", "line": 18 },
          "properties": {
            "container": { "image": "nginx:alpine", "ports": { "web": { "containerPort": 80 } } },
            "connections": { "backend": { "source": "http://backend:3000" } }
          }
        },
        ...
      ],
      "connections": [
        { "sourceId": "/planes/.../containers/frontend", "targetId": "http://backend:3000", "type": "connection" },
        { "sourceId": "/planes/.../containers/frontend", "targetId": "app", "type": "dependsOn" }
      ]
    }

    If the output is not valid JSON, fall back to line-based parsing.
    """
    with open(output_path, "r") as f:
        raw = f.read().strip()

    # rad app graph may print status lines (e.g. "Building ...") before the JSON.
    # Strip everything before the first '{' to get clean JSON.
    json_start = raw.find("{")
    if json_start > 0:
        print(f"Skipping {json_start} bytes of non-JSON prefix")
        raw = raw[json_start:]

    resources = []
    connections = []

    try:
        data = json.loads(raw)

        # Dynamically infer the bicep filename from metadata or first resource
        bicep_filename = None
        metadata = data.get("metadata", {})
        source_files = metadata.get("sourceFiles", [])
        if source_files:
            bicep_filename = source_files[0]

        # Build a lookup: resource id -> resource name
        id_to_name = {}

        for res in data.get("resources", []):
            name = res.get("name", "unknown")
            res_type = res.get("type", "")
            res_id = res.get("id", "")
            props = res.get("properties", {})

            # sourceLocation is nested: { "file": "app.bicep", "line": 23 }
            source_loc = res.get("sourceLocation", {})
            line_number = source_loc.get("line", 0)
            source_file = source_loc.get("file", "")

            # Use the first resource's source file if metadata didn't have it
            if not bicep_filename and source_file:
                bicep_filename = source_file

            # Extract image from nested properties.container.image
            container = props.get("container", {})
            image = container.get("image")
            # Skip ARM template expressions like [parameters('magpieimage')]
            if image and image.startswith("["):
                image = None

            # Extract port from nested properties.container.ports.*.containerPort
            port = None
            ports = container.get("ports", {})
            for port_entry in ports.values():
                if isinstance(port_entry, dict):
                    cp = port_entry.get("containerPort")
                    if cp and not str(cp).startswith("["):
                        port = str(cp)
                        break

            # Categorize
            if "containers" in res_type.lower():
                category = "container"
            elif "rediscaches" in res_type.lower() or "sqldatabases" in res_type.lower() or "mongodatabases" in res_type.lower():
                category = "datastore"
            elif "applications" in res_type.lower() and "containers" not in res_type.lower():
                category = "application"
            else:
                category = "other"

            resources.append({
                "symbolic_name": name,
                "display_name": name,
                "resource_type": res_type,
                "image": image,
                "port": port,
                "category": category,
                "line_number": line_number,
                "source_file": source_file,
            })

            if res_id:
                id_to_name[res_id] = name

        # Parse top-level connections array (new format from rad app graph)
        for conn in data.get("connections", []):
            source_id = conn.get("sourceId", "")
            target_id = conn.get("targetId", "")
            conn_type = conn.get("type", "")

            # Skip dependsOn connections to the application resource
            if conn_type == "dependsOn":
                continue

            # Resolve sourceId to resource name
            source_name = id_to_name.get(source_id, "")
            if not source_name:
                # Try matching by the last segment of the id
                source_last = source_id.rstrip("/").rsplit("/", 1)[-1] if "/" in source_id else source_id
                for rid, rname in id_to_name.items():
                    if rid.endswith("/" + source_last):
                        source_name = rname
                        break

            # Resolve targetId to resource name
            target_name = id_to_name.get(target_id, "")
            if not target_name:
                # ARM expression like [reference('database').id] — extract the
                # symbolic name from the reference() call and match it to a
                # known resource.
                arm_ref_match = re.match(r"\[reference\('(\w+)'\)", target_id)
                if arm_ref_match:
                    ref_sym = arm_ref_match.group(1)
                    # Match the symbolic name to any known resource name
                    for rname in id_to_name.values():
                        if rname == ref_sym:
                            target_name = rname
                            break

                # targetId might be a URL like "http://backend:3000"
                if not target_name:
                    url_match = re.match(r"https?://([^:/]+)", target_id)
                    if url_match:
                        hostname = url_match.group(1)
                        # Match hostname to any resource name (may contain hostname as substring)
                        for rname in id_to_name.values():
                            if hostname in rname or rname in hostname:
                                target_name = rname
                                break
                        if not target_name:
                            # Use hostname itself as the target name
                            target_name = hostname

                # Plain name — try direct match
                if not target_name:
                    target_last = target_id.rstrip("/").rsplit("/", 1)[-1] if "/" in target_id else target_id
                    for rid, rname in id_to_name.items():
                        if rid.endswith("/" + target_last) or rname == target_last:
                            target_name = rname
                            break
                    if not target_name:
                        target_name = target_last

            if source_name and target_name and source_name != target_name:
                conn_entry = {"from": source_name, "to": target_name}
                if conn_entry not in connections:
                    connections.append(conn_entry)

        print(f"Parsed rad app graph output: {len(resources)} resources, {len(connections)} connections")
        print(f"Inferred bicep filename: {bicep_filename}")

    except (json.JSONDecodeError, KeyError) as e:
        print(f"Warning: Could not parse rad app graph output as JSON ({e})")
        print("Raw output:")
        print(raw[:500])
        print("\nFalling back to direct Bicep parsing...")
        return None, None, None

    return resources, connections, bicep_filename


def get_github_file_url(repo_owner, repo_name, branch, file_path, line):
    """Build a GitHub URL that highlights a specific line."""
    return f"https://github.com/{repo_owner}/{repo_name}/blob/{branch}/{file_path}#L{line}"


def generate_mermaid(resources, connections, repo_owner, repo_name, branch, bicep_file,
                     detailed=False, bicep_path=None):
    """Generate a Mermaid diagram string with clickable nodes and GitHub-like styling.

    When detailed=True, container nodes show image:tag metadata.
    """

    lines = ["graph LR"]

    # --- GitHub light theme styling ---
    # Matches GitHub's own dependency/action graph look:
    # white background, light gray borders, clean rounded-corner boxes
    lines.insert(0, "%%{ init: { 'theme': 'base', 'themeVariables': { "
                     "'primaryColor': '#ffffff', "
                     "'primaryTextColor': '#1f2328', "
                     "'primaryBorderColor': '#d1d9e0', "
                     "'lineColor': '#2da44e', "
                     "'secondaryColor': '#f6f8fa', "
                     "'tertiaryColor': '#ffffff', "
                     "'background': '#ffffff', "
                     "'mainBkg': '#ffffff', "
                     "'nodeBorder': '#d1d9e0', "
                     "'clusterBkg': '#f6f8fa', "
                     "'clusterBorder': '#d1d9e0', "
                     "'fontSize': '14px', "
                     "'fontFamily': '-apple-system, BlinkMacSystemFont, Segoe UI, Noto Sans, Helvetica, Arial, sans-serif'"
                     " } } }%%")

    # Class definitions — GitHub light palette with rounded corners
    # Container: blue accent (like GitHub's blue links/actions)
    lines.append("    classDef container fill:#ffffff,stroke:#2da44e,stroke-width:1.5px,color:#1f2328,rx:6,ry:6")
    # Datastore: orange accent (like GitHub's warning/merge colors)
    lines.append("    classDef datastore fill:#ffffff,stroke:#d4a72c,stroke-width:1.5px,color:#1f2328,rx:6,ry:6")
    # Other: neutral gray
    lines.append("    classDef other fill:#ffffff,stroke:#d1d9e0,stroke-width:1.5px,color:#1f2328,rx:6,ry:6")

    resource_map = {r["symbolic_name"]: r for r in resources}

    # Add nodes (skip the top-level application resource)
    # Use regular box nodes [" "] — rounded corners come from rx/ry in classDef
    for res in resources:
        if res["category"] == "application":
            continue

        if detailed:
            label = make_detailed_label(res, bicep_path)
        else:
            # Standard label — clean, no line numbers (those go in tooltip only)
            label_parts = ["<b>" + res["display_name"] + "</b>"]
            if res["image"]:
                label_parts.append(res["image"])
            if res["port"]:
                label_parts.append(":" + res["port"])
            label = "<br/>".join(label_parts)

        lines.append('    {}["{}"]:::{}'.format(res["symbolic_name"], label, res["category"]))

    # Add edges — clean arrow style
    for conn in connections:
        if conn["from"] in resource_map and conn["to"] in resource_map:
            from_res = resource_map[conn["from"]]
            to_res = resource_map[conn["to"]]
            if from_res["category"] == "application" or to_res["category"] == "application":
                continue
            lines.append("    {} --> {}".format(conn["from"], conn["to"]))

    # Add click directives — tooltip shows source file:line, click opens GitHub
    for res in resources:
        if res["category"] == "application":
            continue
        # Use per-resource source_file if available, otherwise fall back to bicep_file
        res_file = res.get("source_file") or bicep_file
        url = get_github_file_url(repo_owner, repo_name, branch, res_file, res["line_number"])
        tooltip = "{}:{}" .format(res_file, res["line_number"])
        lines.append('    click {} href "{}" "{}" _blank'.format(res["symbolic_name"], url, tooltip))

    # Link style — GitHub gray, clean
    edge_count = 0
    for conn in connections:
        if conn["from"] in resource_map and conn["to"] in resource_map:
            from_res = resource_map[conn["from"]]
            to_res = resource_map[conn["to"]]
            if from_res["category"] == "application" or to_res["category"] == "application":
                continue
            lines.append("    linkStyle {} stroke:#2da44e,stroke-width:1.5px".format(edge_count))
            edge_count += 1

    return "\n".join(lines)


def update_readme(readme_path, mermaid_block):
    """Update the Architecture section in README.md with the Mermaid diagram."""
    with open(readme_path, "r") as f:
        content = f.read()

    # Build the new Architecture section body
    new_body = "\n".join([
        "",
        "> *Auto-generated from `app.bicep` \u2014 click any node to jump to its definition in the source.*",
        "",
        "```mermaid",
        mermaid_block,
        "```",
        "",
    ])

    # Replace the Architecture section content
    pattern = re.compile(
        r"(## Architecture\s*\n).*?(\n## |\Z)",
        re.DOTALL,
    )

    if pattern.search(content):
        new_content = pattern.sub(r"\1" + new_body + "\n" + r"\2", content)
    else:
        new_content = content + "\n## Architecture\n" + new_body + "\n"

    with open(readme_path, "w") as f:
        f.write(new_content)

    print("README.md updated")


def main():
    repo_root = os.environ.get(
        "GITHUB_WORKSPACE",
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    bicep_file = "app.bicep"
    bicep_path = os.path.join(repo_root, bicep_file)
    readme_path = os.path.join(repo_root, "README.md")

    # Repository info for building GitHub URLs for clickable nodes
    repo_owner = os.environ.get("REPO_OWNER", "nithyatsu")
    repo_name = os.environ.get("REPO_NAME", "prototype")
    branch = os.environ.get("REPO_BRANCH", "main")

    detailed = is_detailed_mode()
    if detailed:
        print("Detailed mode ENABLED — nodes will show image:tag metadata")

    # --- Try rad app graph output first (primary path in CI) ---
    rad_graph_output = os.environ.get("RAD_GRAPH_OUTPUT")
    resources = None
    connections = None

    if rad_graph_output and os.path.exists(rad_graph_output):
        print(f"Reading rad app graph output from {rad_graph_output}...")
        resources, connections, inferred_bicep = parse_rad_graph_output(rad_graph_output)
        # Use the filename from rad app graph output (dynamically inferred)
        if inferred_bicep:
            bicep_file = inferred_bicep
            print(f"Using bicep filename from rad output: {bicep_file}")

    # --- Fallback: parse Bicep directly ---
    if resources is None:
        if not os.path.exists(bicep_path):
            print(f"Error: {bicep_path} not found")
            sys.exit(1)

        print(f"Parsing {bicep_path} directly (fallback mode)...")
        resources, connections = parse_bicep(bicep_path)

    print(f"Found {len(resources)} resources and {len(connections)} connections")
    for r in resources:
        print("  - {} ({}) @ line {}".format(r["display_name"], r["category"], r["line_number"]))
    for c in connections:
        print("  - {} -> {}".format(c["from"], c["to"]))

    print("\nGenerating Mermaid diagram...")
    mermaid_block = generate_mermaid(
        resources, connections,
        repo_owner, repo_name, branch, bicep_file,
        detailed=detailed,
        bicep_path=bicep_path,
    )

    print("\nMermaid output:")
    print(mermaid_block)

    print("\nUpdating README...")
    update_readme(readme_path, mermaid_block)

    print("Done!")


if __name__ == "__main__":
    main()
