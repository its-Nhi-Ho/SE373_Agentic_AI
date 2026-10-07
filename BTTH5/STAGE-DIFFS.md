# Stage diffs

File và hàm thêm/đổi giữa hai project liên tiếp. Không liệt kê `uv.lock`, `README.md`. File không nhắc đến giống hệt project trước (`observer.py`, `trace.py` giống nhau ở cả 5 project).

Xem diff đầy đủ:

```bash
diff -ru -x .venv -x uv.lock -x __pycache__ stage-00-chat stage-01-files
```

## stage-00-chat → stage-01-files

| File | Thay đổi |
|---|---|
| `tools/__init__.py` | Mới. Export `read_file`, `write_file`. |
| `tools/files.py` | Mới. `_resolve`, `_read`, `_write`, tool `read_file(path)`, tool `write_file(path, content)`, `MAX_READ_BYTES`. |
| `reset_workspace.py` | Mới. `ensure_workspace()`, `reset_workspace()`, `WorkspaceError`, marker `.lab-workspace`. |
| `fixtures/data/weekly_notes.md` | Mới. 5 ghi chú giả. |
| `workspace/` | Mới. `data/`, `output/`, marker. |
| `paths.py` | Thêm `FIXTURES_DIR`, `WORKSPACE_DIR`, `OUTPUT_SUBDIR`, `WORKSPACE_MARKER`. |
| `config.py` | Đổi `PROJECT_NAME`, `APP_TITLE`, `CAPABILITY_TEXT`. Thêm `TOOL_CALL_LIMIT`. |
| `prompts.py` | `CAPABILITY_PROMPT` mô tả workspace, `read_file`, `write_file`. |
| `agent.py` | `TOOLS = [read_file, write_file]`. Thêm `ToolCallLimitMiddleware`. |
| `app.py` | Thêm `output_files()`, `render_output_files()` (mục File đầu ra). `main()` gọi `ensure_workspace()`. |
| `pyproject.toml`, `.gitignore` | Tên project; bỏ qua `workspace/output/*`. |
| `tests/test_files.py` | Mới. Đọc/ghi, missing file, traversal, absolute path, symlink escape, reset workspace. |
| `tests/conftest.py` | `lab_dirs` tạo workspace tạm từ fixtures. |
| `tests/test_agent.py`, `tests/test_app.py`, `tests/test_project.py` | Kiểm tra tool trace read/write, preview file không gọi lại agent, cấu trúc project mới. |

## stage-01-files → stage-02-skills

| File | Thay đổi |
|---|---|
| `skill_catalog.py` | Mới. `SkillMeta`, `Catalog`, `parse_frontmatter()`, `scan_skills()`, `render_catalog()`. |
| `fixtures/skills/weekly-report/` | Mới. `SKILL.md`, `references/report-template.md`. Copy vào `workspace/skills/`. |
| `prompts.py` | Thêm `SKILLS_PROMPT`. `build_system_prompt(catalog_block)` nhận khối catalog. |
| `agent.py` | Thêm `load_catalog()`. `system_prompt()` render catalog; `capabilities()` trả metadata skill và diagnostics. |
| `config.py` | Đổi `PROJECT_NAME`, `APP_TITLE`, `CAPABILITY_TEXT`. |
| `pyproject.toml` | Thêm `pyyaml`. |
| `tests/test_skill_catalog.py` | Mới. Metadata, initial prompt không có body/reference, YAML lỗi, thiếu metadata, trùng name. |
| `tests/test_agent.py`, `tests/test_app.py`, `tests/test_project.py` | Skill load qua `read_file` vào snapshot kế tiếp và lượt follow-up; conversation mới không giữ skill. |

## stage-02-skills → stage-03-bash

| File | Thay đổi |
|---|---|
| `tools/bash.py` | Mới. `minimal_env()`, `_clip()`, `_run_bash()`, tool `bash(command)`. |
| `tools/__init__.py` | Export thêm `bash`. |
| `agent.py` | `TOOLS = [read_file, write_file, bash]`. |
| `prompts.py` | `CAPABILITY_PROMPT` mô tả `bash`: cwd, timeout, đọc `exit_code`/`stdout`/`stderr`. |
| `config.py`, `pyproject.toml` | Tên project, tiêu đề, mô tả khả năng. |
| `tests/test_bash.py` | Mới. cwd, Python của project, exit 0/1, timeout, không lộ credential. |
| `tests/test_agent.py`, `tests/test_app.py`, `tests/test_project.py` | Trace bash có command/stdout/stderr/exit code; 3 tools. |

## stage-03-bash → stage-04-script-skill

| File | Thay đổi |
|---|---|
| `fixtures/skills/csv-quality/` | Mới. `SKILL.md`, `references/report-template.md`, `scripts/check_csv.py`. Copy vào `workspace/skills/`. |
| `fixtures/data/tasks.csv` | Mới. Copy vào `workspace/data/`. |
| `config.py`, `pyproject.toml` | Tên project, tiêu đề, mô tả khả năng. |
| `agent.py` | Chỉ đổi docstring. Tool list giữ nguyên; catalog tự phát hiện `csv-quality`. |
| `tests/test_check_csv.py` | Mới. Thống kê fixture, NaN/Infinity/âm, file thiếu, thiếu cột, parse lỗi, không sửa input. |
| `tests/test_agent.py`, `tests/test_app.py`, `tests/test_project.py`, `tests/test_skill_catalog.py` | Luồng skill chạy script; catalog có 2 skill; source script không bị tính là đã đọc. |
