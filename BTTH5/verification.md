# Verification

Ngày kiểm tra: 2026-10-04.

## Môi trường

- macOS 27.0 (arm64), uv 0.12.22.
- Python trong `.venv` của mỗi project: 3.12.14 (`requires-python >=3.11`).
- Không có API key thật trong môi trường kiểm tra.

## Dependency pin

`pyproject.toml` giới hạn major version; `uv.lock` riêng của từng project khóa phiên bản chính xác.

| Package | Ràng buộc | Phiên bản trong uv.lock |
|---|---|---|
| langchain | `>=1.4.3,<2` | 1.4.3 |
| langchain-core | (qua langchain) | 1.6.6 |
| langgraph | (qua langchain) | 1.2.12 |
| langchain-openai | `>=1.6.7,<2` | 1.6.7 |
| openai | (qua langchain-openai) | 3.24.0 |
| streamlit | `>=1.65.0,<2` | 1.65.0 |
| python-dotenv | `>=1.2.4,<2` | 1.2.4 |
| pyyaml (stage 02–04) | `>=6.0.3,<7` | 6.0.3 |
| pytest (dev) | `>=9.1.1,<10` | 9.1.1 |

API dùng: `langchain.agents.create_agent`, `AgentMiddleware.wrap_model_call` / `wrap_tool_call`, `ModelCallLimitMiddleware`, `ToolCallLimitMiddleware`, `langgraph.config.get_stream_writer`, `agent.stream(stream_mode=["updates", "custom"])`. Không dùng API agent legacy.

## Tests offline đã chạy

`uv run pytest` trong từng project (mock model, không cần key):

| Project | Kết quả |
|---|---|
| stage-00-chat | 17 passed |
| stage-01-files | 34 passed |
| stage-02-skills | 41 passed |
| stage-03-bash | 48 passed |
| stage-04-script-skill | 61 passed |

Nội dung: observer counters (2 model calls, nhiều tool calls), snapshot trước/sau tool result, snapshot deep copy, skill/resource inventory; agent thật với mock model qua stream; Streamlit AppTest (render, thiếu config, 2 lượt không nhân đôi history, rerun không gọi model, chọn snapshot cũ chỉ đọc, cuộc trò chuyện mới, lỗi runtime rollback history, xem file output không gọi agent, skill load hiển thị và bị xóa khi conversation mới); file tools (missing, traversal, absolute, symlink escape, phạm vi ghi, reset workspace có marker); catalog (metadata, initial prompt chỉ có catalog, YAML lỗi, thiếu metadata, trùng name); bash (cwd, Python của project, exit 0/1, timeout, không lộ credential); `check_csv.py` (thống kê fixture, NaN/Infinity/âm, file thiếu, thiếu cột, parse lỗi exit 1, không sửa input); import chỉ từ stdlib/dependency khai báo/module trong project.

## Kiểm tra độc lập

- Copy riêng từng thư mục `stage-*` (không kèm `.venv`) sang `/tmp/lab-copy/`, chạy `uv sync --locked` và `uv run pytest`: cả 5 project pass (17/34/41/48/61).
- Chạy `uv run --project stage-02-skills streamlit run stage-02-skills/app.py` từ thư mục root: app dùng đúng workspace của project (catalog có 1 skill), hiện cảnh báo thiếu `OPENROUTER_API_KEY, MODEL_NAME`, ô chat bị khóa.

## UI smoke với MOCK endpoint (không phải live)

Dùng một OpenAI-compatible **mock server** local trả tool calls theo kịch bản cố định, qua `ChatOpenAI` thật của app, trong Chromium headless. Kết quả dưới đây là mock, không phản ánh hành vi của model thật.

- stage-00: chat nhiều lượt; trạng thái chuyển `Đang gọi model` trong lúc chờ rồi `Hoàn tất`; tool calls = 0; snapshot có system prompt, messages, usage do mock trả.
- stage-03: `python --version` qua bash; trace hiện command, `exit_code: 0`, stdout `Python 3.12.14`.
- stage-04: luồng csv-quality: một model response yêu cầu song song `read_file` SKILL.md + `bash`; panel hiện `Tool đang chạy: read_file, bash`; 4 model calls, 4 tool calls, 18 event; Skill content đã vào history (1), Tài nguyên đã đọc (1); File đầu ra hiện `output/csv-quality.md`. Luồng weekly-report + follow-up: skill ghi nhận ở message #2, request đầu tiên chứa nội dung là `Lượt 1, model call #2`. Cuộc trò chuyện mới: counters về 0, skill content (0), hiện Context cấu hình (chưa gửi). Tắt mock server: UI hiện `OpenAIConnectionError`, trạng thái Lỗi, history rollback, model call đếm 1 (retry nội bộ của SDK không đếm riêng).
- Trace JSONL: sequence liên tục, tool_started/tool_finished khớp tool_call_id, không chứa API key.

## Cập nhật UI (theme, bố cục, counters, trace)

- `.streamlit/config.toml` ở cả 5 project: headless, theme, tắt usage stats. Chạy `uv run streamlit run app.py`: không còn lời nhắc email.
- Chạy lại `uv run pytest` ở cả 5 project: 17/34/41/48/61 passed.
- UI smoke với MOCK endpoint (stage 00, stage 04): badge trạng thái chuyển Sẵn sàng → Đang gọi model → Hoàn tất trong lúc chạy; badge ok cho từng tool; nhãn counters không bị cắt (kiểm tra bằng DOM); lệnh bash hiện dạng code; mục File đầu ra vẫn hiện.
- Bố cục không cuộn trang (MOCK, Chromium headless): ở viewport cao 700/900/1100px, trang không cuộn (`scrollHeight` bằng chiều cao cửa sổ); hai cột cao 452/652/852px và cuộn riêng; ô chat nằm dưới hai cột. Chưa xác nhận được việc cột chat tự cuộn xuống tin mới, vì browser headless của môi trường test không chạy animation frame.

## Chưa kiểm tra

- **Live với model thật: chưa chạy** (không có API key). Cần chạy theo prompt trong README từng project: read/write, load skill, đọc reference, bash/script, artifact, input lỗi, câu hỏi không cần skill (`2 + 3 bằng bao nhiêu?`), follow-up sau tool call. Các hành vi phụ thuộc model (chọn đúng skill, không load skill vô cớ, không tuyên bố đã đọc file ở stage 00, không bịa thống kê) chỉ được xác nhận khi chạy live.
- Windows/WSL2 và Linux: chưa chạy.
- Screenshot UI: không chụp được (công cụ chụp màn hình của browser headless timeout); UI được kiểm tra bằng nội dung DOM.
