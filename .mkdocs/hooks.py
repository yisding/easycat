"""MkDocs build hook for the EasyCat docs site (Learn + Reference).

Three jobs:

1. **README.md as directory index.** GitHub renders `README.md` automatically
   when you browse a folder, so we keep the filename. MkDocs by default would
   serve it at `/teaching/00-hello-audio/README/`; we rewrite the URL/dest so
   it lives at `/teaching/00-hello-audio/` instead. The top-level
   `docs/README.md` is served at `/`.

2. **Folder-style cross-links.** The source markdown links to sibling pages
   with `[..](../04-vad-preroll/)` or `[..](teaching/)` (folder-style, works
   on GitHub). MkDocs needs an explicit file reference. We rewrite any
   relative folder link that resolves to a docs directory containing a
   `README.md` to point at that file — which, combined with job 1, lands at
   the correct directory URL.

3. **Repo-relative deep links.** Pages link to source files and out-of-docs
   pages with `[..](./main.py#L83-L92)` or `[..](../CLAUDE.md)` so the links
   work on GitHub. MkDocs can't render those, so we rewrite them at build
   time to point at `repo_url`'s blob/tree URL on the configured edit branch.

No source files are modified.
"""

from __future__ import annotations

import re
from pathlib import Path

_MD_LINK = re.compile(r"\]\((?P<target>[^)\s#]+)?(?P<fragment>#[^)\s]*)?\)")
_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "tel:", "/")
_STATIC_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js"}
# `edit_uri` looks like ``edit/<branch>/<path>/`` (or ``blob/<branch>/...``);
# the second path segment is the branch the blob URLs should point at.
_BRANCH_FROM_EDIT_URI = re.compile(r"(?:edit|blob|tree)/([^/]+)/")


def _edit_branch(config) -> str:
    """Derive the blob branch from MkDocs' configured ``edit_uri``."""
    m = _BRANCH_FROM_EDIT_URI.match(config.get("edit_uri") or "")
    return m.group(1) if m else "main"


def on_files(files, config):
    site_dir = Path(config["site_dir"])
    for f in files:
        if not f.src_path.endswith("README.md"):
            continue
        if f.src_path == "README.md":
            f.url = ""
            f.dest_path = "index.html"
        else:
            parent = f.src_path.rsplit("/", 1)[0]
            f.url = f"{parent}/"
            f.dest_path = f"{parent}/index.html"
        f.abs_dest_path = str(site_dir / f.dest_path)
    return files


def _rewrite_links(markdown: str, page, config) -> str:
    """Rewrite folder-style and repo-relative links for the rendered site."""
    repo_url = (config.get("repo_url") or "").rstrip("/")
    docs_root = Path(config["docs_dir"]).resolve()
    repo_root = Path(config["config_file_path"]).resolve().parent
    page_dir = (docs_root / page.file.src_path).parent
    branch = _edit_branch(config)

    def _sub(m: re.Match[str]) -> str:
        target = m.group("target") or ""
        fragment = m.group("fragment") or ""
        if not target or target.startswith(_EXTERNAL_PREFIXES):
            return m.group(0)
        resolved = (page_dir / target).resolve()
        try:
            resolved.relative_to(docs_root)
            inside_docs = True
        except ValueError:
            inside_docs = False

        if inside_docs:
            if resolved.is_dir() and (resolved / "README.md").exists():
                return f"]({target.rstrip('/')}/README.md{fragment})"
            if resolved.suffix in {".md", *_STATIC_SUFFIXES} or not resolved.exists():
                return m.group(0)
            # Non-renderable file inside docs (e.g. a teaching `.py` script
            # with `#L..` line anchors) — deep-link to the repo instead.

        if not repo_url:
            return m.group(0)
        try:
            repo_rel = resolved.relative_to(repo_root)
        except ValueError:
            return m.group(0)
        kind = "tree" if resolved.is_dir() else "blob"
        return f"]({repo_url}/{kind}/{branch}/{repo_rel.as_posix()}{fragment})"

    return _MD_LINK.sub(_sub, markdown)


def on_page_markdown(markdown, page, config, **kwargs):
    return _rewrite_links(markdown, page, config)
