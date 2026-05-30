import os
import re
from pathlib import Path

from rank_bm25 import BM25Plus


def _is_text_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" not in chunk
    except OSError:
        return False


def search_codebase(search_dirs: list[str], column_names: list[str]) -> list[dict]:
    """Search directories recursively for files mentioning column names, ranked by BM25."""
    if not column_names:
        return []

    file_paths = []
    file_contents = []

    for search_dir in search_dirs:
        for root, dirs, files in os.walk(search_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in sorted(files):
                fpath = Path(root) / fname
                if not _is_text_file(fpath):
                    continue
                try:
                    content = fpath.read_text(errors="ignore")
                except OSError:
                    continue
                file_paths.append(str(fpath))
                file_contents.append(content)

    if not file_contents:
        return []

    def tokenize(text: str) -> list[str]:
        return re.findall(r"\w+", text.lower())

    tokenized_corpus = [tokenize(c) for c in file_contents]
    bm25 = BM25Plus(tokenized_corpus)

    query_tokens = [col.lower() for col in column_names]
    scores = bm25.get_scores(query_tokens)

    ranked = sorted(
        [
            {"path": file_paths[i], "content": file_contents[i], "score": float(scores[i])}
            for i in range(len(file_paths))
            if scores[i] > 0
        ],
        key=lambda x: x["score"],
        reverse=True,
    )

    return ranked
