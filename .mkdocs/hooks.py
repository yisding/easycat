"""MkDocs build hook for the teaching-ladder site.

Two jobs:

1. **README.md as directory index.** GitHub renders `README.md` automatically
   when you browse a folder, so we keep the filename. MkDocs by default would
   serve it at `/00-hello-audio/README/`; we rewrite the URL/dest so it lives
   at `/00-hello-audio/` instead. The top-level `docs/teaching/README.md` is
   served at `/`.

2. **Cross-link rewriting.** The source markdown links to sibling chapters
   with `[..](../04-vad-preroll/)` (folder-style, works on GitHub). MkDocs
   needs an explicit file reference. We rewrite those at build time to
   `[..](../04-vad-preroll/README.md)`, which — combined with job 1 — lands
   at the correct `/04-vad-preroll/` URL.

No source files are modified.
"""

from __future__ import annotations

import re
from pathlib import Path

_CROSS_LINK = re.compile(r"\((\.{1,2}(?:/\d{2}-[^/)]+)?)/\)")


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


def on_page_markdown(markdown, **kwargs):
    return _CROSS_LINK.sub(r"(\1/README.md)", markdown)
