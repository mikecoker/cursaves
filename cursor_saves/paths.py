"""Platform detection and Cursor storage path resolution."""

import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

_WIN_DRIVE_RE = re.compile(r"^/?([A-Za-z]):(/.*)?$")


def get_cursor_user_dir() -> Path:
    """Return the Cursor User data directory for the current platform.

    macOS:   ~/Library/Application Support/Cursor/User
    Linux:   ~/.config/Cursor/User
    Windows: %APPDATA%/Cursor/User
    """
    system = platform.system()
    if system == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "Cursor" / "User"
    elif system == "Linux":
        base = Path.home() / ".config" / "Cursor" / "User"
    elif system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            base = Path(appdata) / "Cursor" / "User"
        else:
            base = Path.home() / "AppData" / "Roaming" / "Cursor" / "User"
    else:
        print(
            f"Error: Unsupported platform '{system}'.\n"
            f"cursaves supports macOS, Linux, and Windows.\n"
            f"On macOS, Cursor data is at ~/Library/Application Support/Cursor/User/\n"
            f"On Linux, Cursor data is at ~/.config/Cursor/User/\n"
            f"On Windows, Cursor data is at %APPDATA%\\Cursor\\User\\",
            file=sys.stderr,
        )
        sys.exit(1)

    if not base.exists():
        print(
            f"Error: Cursor data directory not found at:\n"
            f"  {base}\n\n"
            f"This usually means:\n"
            f"  - Cursor is not installed on this machine, or\n"
            f"  - Cursor has never been opened (no data created yet), or\n"
            f"  - Cursor stores data at a non-standard location\n\n"
            f"Expected path for {system}: {base}",
            file=sys.stderr,
        )
        sys.exit(1)

    return base


def get_global_db_path() -> Path:
    """Return the path to Cursor's global state.vscdb."""
    return get_cursor_user_dir() / "globalStorage" / "state.vscdb"


def get_workspace_storage_dir() -> Path:
    """Return the path to Cursor's workspace storage directory."""
    return get_cursor_user_dir() / "workspaceStorage"


def get_cursor_projects_dir() -> Path:
    """Return the path to ~/.cursor/projects/ (agent transcripts, etc.)."""
    return Path.home() / ".cursor" / "projects"


def file_uri_to_path(uri: str) -> str:
    """Decode a file:// URI to a filesystem path.

    Windows Cursor stores folders as ``file:///d%3A/projects/foo``.
    """
    if not uri.startswith("file:"):
        return uri.replace("%20", " ")
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if _WIN_DRIVE_RE.match(path):
        path = path.lstrip("/")
    return path


def path_to_file_uri(path: str) -> str:
    """Convert a filesystem path to a file:// URI."""
    return Path(path).expanduser().absolute().as_uri()


def path_basename(path: str) -> str:
    """Basename that treats both / and \\ as separators."""
    p = path.replace("\\", "/").rstrip("/")
    if not p:
        return ""
    return p.rsplit("/", 1)[-1]


def _split_drive_prefix(prefix: str) -> tuple[Optional[str], str]:
    """Return (drive_letter or None, posix-style absolute tail).

    ``D:\\projects\\foo`` → ``('D', '/projects/foo')``
    ``/d:/projects/foo`` → ``('d', '/projects/foo')``
    ``/Users/foo`` → ``(None, '/Users/foo')``
    """
    p = prefix.strip()
    if p.startswith("file:"):
        p = file_uri_to_path(p)
    p = p.replace("\\", "/")
    m = _WIN_DRIVE_RE.match(p)
    if m:
        rest = m.group(2) or "/"
        if not rest.startswith("/"):
            rest = "/" + rest
        return m.group(1), rest
    if not p.startswith("/"):
        p = "/" + p
    return None, p


def canonical_fs_path(path: str) -> str:
    """Stable, comparable path string (lowercase drive, forward slashes)."""
    drive, rest = _split_drive_prefix(path)
    rest = "/" + rest.strip("/")
    if rest != "/":
        rest = rest.rstrip("/")
    if drive:
        return f"{drive.lower()}:{rest}"
    return rest or "/"


def paths_match(a: str, b: str) -> bool:
    """True if two paths refer to the same location, ignoring slash/drive case."""
    ca, cb = canonical_fs_path(a), canonical_fs_path(b)
    if os.name == "nt":
        return ca.casefold() == cb.casefold()
    return ca == cb


def native_fs_path(path: str) -> str:
    """Path using this OS's separators when it is a local-style path."""
    drive, rest = _split_drive_prefix(path)
    if os.name == "nt" and drive:
        return os.path.normpath(f"{drive}:{rest.replace('/', os.sep)}")
    if drive:
        return f"{drive.lower()}:{rest}"
    posix = path.replace("\\", "/")
    if os.name == "nt" and posix.startswith("/"):
        return "/" + posix.strip("/") if posix != "/" else "/"
    return os.path.normpath(path) if path else path


def _path_forms(prefix: str) -> dict[str, str]:
    """Named spellings of a project prefix (slash, backslash, file URI, …)."""
    drive, rest = _split_drive_prefix(prefix)
    rest = "/" + rest.strip("/")
    if rest == "/":
        rest = ""
    forms: dict[str, str] = {}
    if drive:
        for letter in (drive.lower(), drive.upper()):
            slash = f"{letter}:{rest}"
            forms[f"slash_{letter}"] = slash
            forms[f"bslash_{letter}"] = slash.replace("/", "\\")
            forms[f"vscode_{letter}"] = f"/{letter}:{rest}"
            forms[f"uri_{letter}"] = f"file:///{letter}:{rest}"
            forms[f"urienc_{letter}"] = f"file:///{letter}%3A{rest}"
    else:
        slash = rest if rest.startswith("/") else "/" + rest
        forms["slash"] = slash
        forms["bslash"] = slash.replace("/", "\\")
        forms["vscode"] = slash
        forms["uri"] = "file://" + slash
        forms["uri_space"] = "file://" + slash.replace(" ", "%20")
    stripped = prefix.rstrip("/\\")
    if stripped:
        forms["original"] = stripped
    return forms


def prefix_rewrite_pairs(old_prefix: str, new_prefix: str) -> list[tuple[str, str]]:
    """(old, new) string pairs covering Windows/POSIX/URI spellings of a prefix."""
    if not old_prefix or not new_prefix or old_prefix == new_prefix:
        return []
    old_forms = _path_forms(old_prefix)
    new_forms = _path_forms(new_prefix)
    new_slash = (
        new_forms.get("slash")
        or next((v for k, v in new_forms.items() if k.startswith("slash_")), None)
        or canonical_fs_path(new_prefix)
    )
    new_uri = (
        new_forms.get("uri")
        or next(
            (
                v
                for k, v in new_forms.items()
                if k.startswith("uri_") and "%3A" not in v
            ),
            None,
        )
        or (
            "file://" + new_slash
            if new_slash.startswith("/")
            else f"file:///{new_slash}"
        )
    )

    pairs: list[tuple[str, str]] = []
    for key, old_v in old_forms.items():
        if not old_v:
            continue
        if key.startswith("urienc") or "%3A" in old_v:
            new_v = new_uri
        elif key.startswith("uri") or old_v.startswith("file://"):
            new_v = new_uri
        elif "\\" in old_v:
            new_v = new_slash
        else:
            new_v = new_slash
        if old_v != new_v:
            pairs.append((old_v, new_v))

    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for pair in sorted(pairs, key=lambda item: len(item[0]), reverse=True):
        if pair not in seen:
            seen.add(pair)
            ordered.append(pair)
    return ordered


def rewrite_path_string(value: str, old_prefix: str, new_prefix: str) -> str:
    """Replace project-path prefixes in a single string, including Windows variants."""
    if not old_prefix or old_prefix == new_prefix:
        if old_prefix and old_prefix in value:
            return value.replace(old_prefix, new_prefix)
        return value

    result = value
    replaced_backslash = False
    for old_v, new_v in prefix_rewrite_pairs(old_prefix, new_prefix):
        if old_v and old_v in result:
            if "\\" in old_v:
                replaced_backslash = True
            result = result.replace(old_v, new_v)

    if replaced_backslash and "\\" in result:
        new_slash = canonical_fs_path(new_prefix)
        if new_slash.startswith("/"):
            result = _slashify_after_prefix(result, new_slash)
    return result


def _slashify_after_prefix(value: str, posix_prefix: str) -> str:
    """Turn leftover backslashes into slashes after a POSIX prefix rewrite."""
    if posix_prefix not in value or "\\" not in value:
        return value
    parts = value.split(posix_prefix)
    out = [parts[0]]
    for rest in parts[1:]:
        match = re.match(r"^([^\s\"']*)(.*)$", rest, re.DOTALL)
        if match:
            path_part = match.group(1).replace("\\", "/")
            out.append(posix_prefix + path_part + match.group(2))
        else:
            out.append(posix_prefix + rest.replace("\\", "/"))
    return "".join(out)


def sanitize_project_path(project_path: str) -> str:
    """Convert a project path to Cursor's sanitized directory name format.

    /Users/callum/Desktop/Projects/myrepo -> Users-callum-Desktop-Projects-myrepo
    D:\\projects\\cursaves -> d-projects-cursaves
    """
    p = project_path.replace("\\", "/").strip("/")
    m = _WIN_DRIVE_RE.match(p)
    if m:
        rest = (m.group(2) or "").lstrip("/")
        p = m.group(1).lower() + (("/" + rest) if rest else "")
    return p.replace("/", "-")


def _decode_ssh_host(host: str) -> str:
    """Decode an SSH host identifier.

    Cursor encodes SSH hosts as hex-encoded JSON, e.g.:
    7b22686f73744e616d65223a22636f7265227d -> {"hostName":"core"} -> core
    """
    try:
        # Try to decode as hex
        decoded = bytes.fromhex(host).decode("utf-8")
        # Try to parse as JSON
        data = json.loads(decoded)
        if isinstance(data, dict) and "hostName" in data:
            return data["hostName"]
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return host


def find_workspace_dirs_for_project(project_path: str) -> list[Path]:
    """Find all workspace directories that map to a given project path.

    Scans workspace.json files in workspaceStorage/ to find matches.
    Returns list of workspace directory paths, newest first.
    """
    ws_storage = get_workspace_storage_dir()
    if not ws_storage.exists():
        return []

    target = os.path.normpath(os.path.expanduser(project_path))

    matches = []
    for ws_dir in ws_storage.iterdir():
        if not ws_dir.is_dir():
            continue
        ws_json = ws_dir / "workspace.json"
        if not ws_json.exists():
            continue
        try:
            data = json.loads(ws_json.read_text())
            folder_uri = data.get("folder", "")
            if folder_uri.startswith("file://"):
                folder_path = file_uri_to_path(folder_uri)
            elif folder_uri.startswith("vscode-remote://"):
                # SSH remote workspace - extract the path portion
                # Format: vscode-remote://ssh-remote%2B<host>/<path>
                parts = folder_uri.split("/", 3)
                if len(parts) >= 4:
                    folder_path = "/" + parts[3]
                else:
                    continue
            else:
                continue

            if paths_match(folder_path, target):
                matches.append(ws_dir)
        except (json.JSONDecodeError, OSError):
            continue

    # Sort by modification time, newest first
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches


def find_transcript_dir(project_path: str) -> Optional[Path]:
    """Find the agent-transcripts directory for a project."""
    projects_dir = get_cursor_projects_dir()
    if not projects_dir.exists():
        return None

    sanitized = sanitize_project_path(project_path)
    transcript_dir = projects_dir / sanitized / "agent-transcripts"
    if transcript_dir.exists():
        return transcript_dir

    return None


def get_project_path() -> str:
    """Get the current project path (current working directory)."""
    return os.getcwd()


def list_all_workspaces() -> list[dict]:
    """List all Cursor workspaces with metadata.

    Returns a list of dicts with:
      - folder_uri: raw URI from workspace.json
      - path: extracted filesystem path (for workspace, path to the .code-workspace file)
      - type: 'local', 'ssh', or 'workspace'
      - host: SSH hostname (for ssh type, None otherwise)
      - workspace_dir: Path to the workspace directory
      - mtime: modification time of the workspace DB
    """
    ws_storage = get_workspace_storage_dir()
    if not ws_storage.exists():
        return []

    workspaces = []
    for ws_dir in ws_storage.iterdir():
        if not ws_dir.is_dir():
            continue
        ws_json = ws_dir / "workspace.json"
        if not ws_json.exists():
            continue
        try:
            data = json.loads(ws_json.read_text())

            ws_type = "local"
            host = None
            folder_path = ""
            folder_uri = ""

            # workspace .code-workspace: uses "workspace" key instead of "folder"
            if "workspace" in data and not data.get("folder"):
                ws_uri = data["workspace"]
                if ws_uri.startswith("file://"):
                    folder_uri = ws_uri
                    folder_path = file_uri_to_path(ws_uri)
                    ws_type = "workspace"
                else:
                    continue
            else:
                folder_uri = data.get("folder", "")
                if not folder_uri:
                    continue

                if folder_uri.startswith("file://"):
                    folder_path = file_uri_to_path(folder_uri)
                elif folder_uri.startswith("vscode-remote://"):
                    ws_type = "ssh"
                    # Format: vscode-remote://ssh-remote%2B<host>/<path>
                    authority = folder_uri.split("/")[2]  # ssh-remote%2B<host>
                    if "%2B" in authority:
                        host = authority.split("%2B", 1)[1]
                    elif "+" in authority:
                        host = authority.split("+", 1)[1]
                    # Decode the host if it's hex-encoded JSON (e.g. {"hostName":"core"})
                    if host:
                        host = _decode_ssh_host(host)
                    parts = folder_uri.split("/", 3)
                    if len(parts) >= 4:
                        folder_path = "/" + parts[3]
                    else:
                        continue
                else:
                    continue

            # Get DB modification time
            db_path = ws_dir / "state.vscdb"
            mtime = db_path.stat().st_mtime if db_path.exists() else 0

            workspaces.append(
                {
                    "folder_uri": folder_uri,
                    "path": native_fs_path(folder_path),
                    "type": ws_type,
                    "host": host,
                    "workspace_dir": ws_dir,
                    "mtime": mtime,
                }
            )
        except (json.JSONDecodeError, OSError):
            continue

    # Sort by modification time, newest first
    workspaces.sort(key=lambda w: w["mtime"], reverse=True)
    return workspaces


def get_global_composer_headers() -> list[dict]:
    """Read the central composer.composerHeaders from the global DB.

    Returns the allComposers list from composer.composerHeaders in the
    global DB's ItemTable. In Cursor 3.0+ this is the authoritative
    index of all chats, each tagged with a workspaceIdentifier.

    Returns an empty list if not present (pre-3.0 Cursor).
    """
    from . import db

    global_db = get_global_db_path()
    if not global_db.exists():
        return []
    try:
        with db.CursorDB(global_db) as cdb:
            headers = cdb.get_json("composer.composerHeaders", table="ItemTable")
            if headers and isinstance(headers, dict):
                return headers.get("allComposers", [])
    except Exception:
        pass
    return []


_global_headers_cache: Optional[dict[str, list[dict]]] = None


def _build_global_headers_map() -> dict[str, list[dict]]:
    """Build a workspace-hash → [composer header entries] map from the global index.

    Returns a dict keyed by workspace directory hash (workspaceIdentifier.id).
    Each value is a list of composer header dicts for that workspace.
    Cached for the lifetime of the process.
    """
    global _global_headers_cache
    if _global_headers_cache is not None:
        return _global_headers_cache

    result: dict[str, list[dict]] = {}
    for entry in get_global_composer_headers():
        wi = entry.get("workspaceIdentifier", {})
        ws_id = wi.get("id", "")
        if ws_id:
            result.setdefault(ws_id, []).append(entry)
    _global_headers_cache = result
    return result


def invalidate_headers_cache():
    """Clear the cached global headers map (call after writing to the global DB)."""
    global _global_headers_cache
    _global_headers_cache = None


def get_workspace_composer_ids(ws_db_path: Path) -> list[str]:
    """Extract all composer IDs associated with a workspace.

    Combines multiple sources for maximum coverage:
    1. Global composer.composerHeaders index (Cursor 3.0+, most authoritative
       but only contains recently-active chats)
    2. Workspace DB selectedComposerIds + composerChatViewPane entries
       (catches chats opened before the 3.0 migration that aren't yet
       in the global index)
    3. Workspace DB allComposers (Cursor 2.x fallback)

    Returns deduplicated IDs.
    """
    from . import db

    ids: set[str] = set()
    ws_hash = ws_db_path.parent.name

    # Source 1: global headers index (Cursor 3.0+)
    headers_map = _build_global_headers_map()
    for entry in headers_map.get(ws_hash, []):
        cid = entry.get("composerId")
        if cid:
            ids.add(cid)

    # Source 2+3: workspace DB
    try:
        with db.CursorDB(ws_db_path) as cdb:
            data = cdb.get_json("composer.composerData", table="ItemTable")
            if not data:
                return list(ids)

            # Cursor 2.x: allComposers (complete list for old workspaces)
            for c in data.get("allComposers", []):
                cid = c.get("composerId")
                if cid:
                    ids.add(cid)

            # Cursor 3.0+: supplementary sources for chats not in global index
            for cid in data.get("selectedComposerIds", []):
                if cid:
                    ids.add(cid)
            for cid in data.get("lastFocusedComposerIds", []):
                if cid:
                    ids.add(cid)

            for key in cdb.list_keys(
                "workbench.panel.composerChatViewPane.", table="ItemTable"
            ):
                pane = cdb.get_json(key, table="ItemTable")
                if isinstance(pane, dict):
                    for view_key in pane:
                        if ".view." in view_key:
                            cid = view_key.rsplit(".", 1)[-1]
                            if cid:
                                ids.add(cid)
    except Exception:
        pass

    return list(ids)


def list_workspaces_with_conversations() -> list[dict]:
    """List workspaces that have at least one conversation.

    Returns the same dicts as list_all_workspaces(), plus a
    'conversations' key with the count.
    """
    result = []
    for ws in list_all_workspaces():
        db_path = ws["workspace_dir"] / "state.vscdb"
        if not db_path.exists():
            continue
        composer_ids = get_workspace_composer_ids(db_path)
        if composer_ids:
            ws["conversations"] = len(composer_ids)
            result.append(ws)
    return result


def resolve_workspace(selector: str) -> Optional[dict]:
    """Resolve a workspace selector to a workspace dict.

    The selector can be:
      - A number (1-based index from list_workspaces_with_conversations)
      - A workspace hash (directory name under workspaceStorage/)
      - A path substring (matched against workspace paths)
    """
    workspaces = list_workspaces_with_conversations()

    # Try as index
    try:
        idx = int(selector)
        if workspaces and 1 <= idx <= len(workspaces):
            return workspaces[idx - 1]
        return None
    except ValueError:
        pass

    # Try as workspace hash (exact match, or prefix match when selector is 8 chars (short hash))
    # Allow the short hash because that's what's displayed in the workspaces list,
    # so user can just copy-paste the short hash, e.g. `cursaves push -w 497e8ab0`
    for ws in workspaces:
        name = ws["workspace_dir"].name
        if len(selector) == 8:
            # Short hash match (8 chars) - allow prefix match
            if name.startswith(selector):
                return ws
        else:
            # Exact match
            if name == selector:
                return ws

    # Try as path substring
    for ws in workspaces:
        if selector in ws["path"]:
            return ws

    return None


def get_sync_dir() -> Path:
    """Return the cursaves sync directory (~/.cursaves/).

    This is the git repo that holds snapshots and is synced between machines.
    """
    return Path.home() / ".cursaves"


def get_snapshots_dir() -> Path:
    """Return the snapshots directory (~/.cursaves/snapshots/)."""
    snapshots = get_sync_dir() / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    return snapshots


def is_sync_repo_initialized() -> bool:
    """Check if a sync backend has been configured (git repo or cloud)."""
    sync_dir = get_sync_dir()
    if (sync_dir / ".git").exists():
        return True
    # Check for non-git backend config
    config_path = Path.home() / ".config" / "cursaves" / "config.json"
    if config_path.exists():
        try:
            import json

            cfg = json.loads(config_path.read_text())
            return cfg.get("backend") in ("s3", "azure")
        except Exception:
            pass
    return False


def get_machine_id() -> str:
    """Return a human-readable machine identifier."""
    import socket

    return socket.gethostname()


# ── Workspace matching for imports ─────────────────────────────────────


def find_all_matching_workspaces(source_path: str) -> list[dict]:
    """Find all workspaces that could receive imports from source_path.

    Matches by:
    1. Exact path match (for SSH workspaces with same remote path)
    2. Same basename (fallback for different directory structures)

    Returns list of workspace dicts with type, host, path, workspace_dir,
    sorted by match quality (exact matches first) then by mtime.
    """
    all_ws = list_all_workspaces()
    source_basename = path_basename(source_path)

    exact_matches = []
    basename_matches = []

    for ws in all_ws:
        ws_path = ws["path"]
        ws_basename = path_basename(ws_path)

        if paths_match(ws_path, source_path):
            exact_matches.append(ws)
        elif ws_basename == source_basename:
            basename_matches.append(ws)

    # Return exact matches first, then basename matches
    return exact_matches + basename_matches


def format_workspace_display(ws: dict, include_path: bool = True) -> str:
    """Format a workspace dict for display.

    Returns a string like "ssh core /mnt/home/.../project", "(local) /home/.../project",
    or "(workspace) /home/.../my-proj.code-workspace"
    """
    if ws["type"] == "ssh":
        host = ws.get("host") or "unknown"
        if include_path:
            path = ws["path"]
            if len(path) > 40:
                path = "..." + path[-37:]
            return f"ssh {host} {path}"
        return f"ssh {host}"
    elif ws["type"] == "workspace":
        if include_path:
            path = ws["path"]
            if len(path) > 45:
                path = "..." + path[-42:]
            return f"(workspace) {path}"
        return "(workspace)"
    else:
        if include_path:
            path = ws["path"]
            if len(path) > 45:
                path = "..." + path[-42:]
            return f"(local) {path}"
        return "(local)"


# ── Project identification ────────────────────────────────────────────


def get_project_identifier(project_path: str) -> str:
    """Get a stable identifier for a project, used as the snapshot subdirectory.

    Uses the git remote origin URL if available (normalized to a filesystem-safe
    string).  Falls back to the directory basename for non-git projects.

    This means:
      - Same repo under different local names (bob/ vs alice/) → same identifier
      - Different repos that happen to share a name → different identifiers
    """
    remote_url = _get_git_remote_url(project_path)
    if remote_url:
        return _normalize_remote_url(remote_url)
    return path_basename(project_path)


def _get_git_remote_url(project_path: str) -> Optional[str]:
    """Get the git remote origin URL for a project, if any."""
    try:
        result = subprocess.run(
            ["git", "-C", project_path, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _normalize_remote_url(url: str) -> str:
    """Normalize a git remote URL to a stable, filesystem-safe directory name.

    git@github.com:user/repo.git     → github.com-user-repo
    https://github.com/user/repo.git → github.com-user-repo
    ssh://git@github.com/user/repo   → github.com-user-repo
    """
    # Strip trailing .git
    url = re.sub(r"\.git$", "", url)

    # SSH shorthand: git@host:user/repo
    m = re.match(r"^[\w.-]+@([\w.-]+):(.*)", url)
    if m:
        host, path = m.group(1), m.group(2)
        return _sanitize_identifier(f"{host}/{path}")

    # HTTPS / SSH URI: https://host/path or ssh://git@host/path
    m = re.match(r"^(?:https?|ssh)://(?:[\w.-]+@)?([\w.-]+)/(.*)", url)
    if m:
        host, path = m.group(1), m.group(2)
        return _sanitize_identifier(f"{host}/{path}")

    # Unknown format -- sanitize whatever we got
    return _sanitize_identifier(url)


def _sanitize_identifier(s: str) -> str:
    """Turn an arbitrary string into a safe directory name.

    Replaces slashes, colons, @, etc. with '-' and collapses runs of dashes.
    """
    s = re.sub(r"[/:@\\]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")
