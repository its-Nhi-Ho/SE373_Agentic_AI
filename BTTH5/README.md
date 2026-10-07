# Agent Tools & Skills Lab

5 stage Python có source độc lập, dùng chung một uv workspace (`.venv` và `uv.lock` ở thư mục lab). Mỗi stage là một LangChain agent với UI chat Streamlit và panel State & Context. Stage sau được tạo bằng cách copy stage trước rồi thêm capability.

| Project | Tools | Skills |
|---|---|---|
| [`stage-00-chat`](stage-00-chat/README.md) | Không có | Không có |
| [`stage-01-files`](stage-01-files/README.md) | `read_file`, `write_file` | Không có |
| [`stage-02-skills`](stage-02-skills/README.md) | `read_file`, `write_file` | `weekly-report` |
| [`stage-03-bash`](stage-03-bash/README.md) | `read_file`, `write_file`, `bash` | `weekly-report` |
| [`stage-04-script-skill`](stage-04-script-skill/README.md) | `read_file`, `write_file`, `bash` | `weekly-report`, `csv-quality` |

Thay đổi giữa các project: [STAGE-DIFFS.md](STAGE-DIFFS.md). Bài tập nhóm: [student-assignment.md](student-assignment.md). Kết quả kiểm tra: [verification.md](verification.md).

## Yêu cầu

- Python 3.11+ và [uv](https://docs.astral.sh/uv/).
- API key của OpenRouter; model cần hỗ trợ tool calling.
- `stage-03-bash`, `stage-04-script-skill` cần môi trường POSIX có `bash`. Trên Windows dùng WSL2.

## Chạy một project

Cài dependency của cả 5 stage một lần, từ thư mục `agent-tools-skills-lab`:

```bash
uv sync --all-packages --locked
```

Sau đó chạy stage mong muốn:

```bash
cd stage-02-skills
# Không cần tạo .venv riêng cho stage
cp -n .env.example .env
# Điền credential và model vào .env
uv run streamlit run app.py
```

Mở **Local URL** in ra terminal (mặc định `http://localhost:8501`). Mỗi project có `.streamlit/config.toml` bật chế độ headless (không hỏi email, không tự mở trình duyệt), đặt theme và tắt gửi thống kê sử dụng.

Chạy các project khác: thay `stage-02-skills` bằng `stage-00-chat`, `stage-01-files`, `stage-03-bash` hoặc `stage-04-script-skill`. `uv run` trong mỗi stage tự tìm workspace và dùng `.venv` chung ở thư mục lab. Mỗi project vẫn có `.env`, `workspace/` (nếu có) và `traces/` riêng. Muốn chạy nhiều project cùng lúc, thêm `--server.port 8502`, `8503`, … cho project thứ hai trở đi.

Tests của từng stage (chạy trong thư mục stage, không cần API key):

```bash
uv run pytest
```

## Cấu hình model

`.env` của từng project:

```dotenv
OPENROUTER_API_KEY=...
MODEL_NAME=...          # model hỗ trợ native tool calling
OPENROUTER_BASE_URL=        # tùy chọn; mặc định https://openrouter.ai/api/v1
```

- Chọn model theo tên OpenRouter, ví dụ `openai/gpt-4o-mini`; model phải hỗ trợ native tool calling.
- `OPENROUTER_BASE_URL` mặc định là `https://openrouter.ai/api/v1`; chỉ đặt biến này khi dùng endpoint OpenAI-compatible khác.
- Source và skill không hard-code model hay credential.
- App không truyền `temperature` hay tham số model khác.
- Nếu override `OPENROUTER_BASE_URL`, endpoint phải hỗ trợ tool calling tương thích OpenAI Chat Completions (`tools`, `tool_calls`, message role `tool`). Endpoint không hỗ trợ sẽ lỗi hoặc không gọi tool.
- Thiếu cấu hình: UI hiện cảnh báo và không gọi model.

## Dùng một project độc lập

Mỗi thư mục `stage-*` tự khai báo dependency trong `pyproject.toml`, chứa config, source, fixtures, workspace và tests; không import code từ project khác hay từ root. Trong lab, các stage dùng chung `uv.lock` ở root. Khi copy một stage ra ngoài workspace, `uv sync` sẽ tạo lockfile và môi trường riêng:

```bash
cp -R stage-04-script-skill ~/my-agent-lab
cd ~/my-agent-lab
uv sync
cp .env.example .env
uv run streamlit run app.py
```
