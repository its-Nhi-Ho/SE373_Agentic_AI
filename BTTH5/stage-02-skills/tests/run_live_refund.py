"""Run one refund scenario through the real Streamlit app and write its normal trace."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from streamlit.testing.v1 import AppTest

import paths
from config import load_settings
from reset_workspace import ensure_workspace


CASES = {
    "A": "Tôi mua ngày 28/09/2026, yêu cầu hoàn ngày 06/10/2026, chưa kích hoạt. Tôi có được hoàn không?",
    "B": "Tôi mua ngày 02/10/2026, yêu cầu hoàn ngày 12/10/2026, chưa kích hoạt. Tôi có được hoàn không?",
    "missing": "Tôi mua ngày 02/10/2026, muốn hoàn ngày 12/10/2026.",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=CASES)
    parser.add_argument("--renamed", action="store_true")
    args = parser.parse_args()
    settings, missing = load_settings()
    if settings is None:
        raise SystemExit(f"Thiếu cấu hình: {', '.join(missing)}")

    with tempfile.TemporaryDirectory(prefix="refund-live-") as temp_dir:
        workspace = Path(temp_dir) / "workspace"
        ensure_workspace(workspace, paths.FIXTURES_DIR)
        if args.renamed:
            policies = workspace / "data" / "policies"
            (policies / "policy-before-oct.md").rename(policies / "old-rules.md")
            (policies / "policy-from-oct.md").rename(policies / "new-rules.md")
        paths.WORKSPACE_DIR = workspace
        before = set(paths.TRACES_DIR.glob("*.jsonl"))
        app = AppTest.from_file(str(paths.PROJECT_ROOT / "app.py"), default_timeout=180).run()
        if app.exception:
            raise RuntimeError(f"App failed before chat: {app.exception}")
        app.chat_input[0].set_value(CASES[args.case]).run()
        traces = sorted(set(paths.TRACES_DIR.glob("*.jsonl")) - before)
        turn = app.session_state["ui_history"][-1]
        print(json.dumps({
            "case": args.case,
            "renamed": args.renamed,
            "answer": turn["answer"],
            "error": turn["error"],
            "trace": str(traces[0]) if len(traces) == 1 else None,
            "trace_count": len(traces),
        }, ensure_ascii=False))
        if app.exception or turn["error"] or len(traces) != 1:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
