# Block 1: Tra cứu chính sách đúng phiên bản

## Kết quả kiểm tra tool

Đã gọi trực tiếp `_list` ở `stage-01-files` và `stage-02-skills` bằng Python chuẩn. Sau khi cài dependency từ `uv.lock`, bộ test đầy đủ cũng chạy thành công: stage 01 có 36/36 test qua; stage 02 có 44/44 test qua. Cả hai stage trả cùng kết quả:

| Đầu vào | Kết quả |
| --- | --- |
| `data/policies` | `ok=true`, hai file chính sách, sắp xếp theo tên, mỗi mục có `name`, `path`, `type` |
| `data/weekly_notes.md` | `NOT_A_DIRECTORY` |
| `data/missing` | `DIRECTORY_NOT_FOUND` |
| `../outside`, `/tmp` | `PATH_OUTSIDE_WORKSPACE` |

Đã thêm test cho symlink dẫn ra ngoài workspace và đổi tên hai file chính sách. `list_files` lấy tên từ thư mục hiện tại, không phụ thuộc tên cũ.

Test tích hợp `stage-02-skills/tests/test_agent.py::test_refund_skill_discovers_renamed_policies_and_reference` dùng agent LangChain thật với model giả định sẵn các tool call. Test xác nhận skill vào model history, `list_files` trả tên mới, agent đọc hai tài liệu đã đổi tên và reference. Test này kiểm tra luồng tool, không đánh giá khả năng suy luận độc lập của model.

## Các tình huống hội thoại

Đã chạy qua Streamlit app và agent LangChain thật với OpenRouter (`openrouter/free`). Mỗi lượt dùng cuộc trò chuyện và workspace tạm mới; hai lần đổi tên không sửa workspace chính. Các file JSONL dưới đây được app ghi trực tiếp, không phải trace của mock model.

| Trường hợp | Kết quả thực tế | Trace và vị trí bằng chứng |
| --- | --- | --- |
| A: mua 28/09/2026, hoàn 06/10/2026, chưa kích hoạt | Chính sách cũ, 8 ngày, không đủ điều kiện vì quá 7 ngày; không nêu phí; dẫn `data/policies/policy-before-oct.md` | [Trace A](stage-02-skills/traces/20261007-104018_d41b02ad_turn01_c4e0462c.jsonl): sự kiện 4-5 đọc skill, 8-9 liệt kê thư mục, các sự kiện `tool_finished` sau đó chứa chính sách |
| B: mua 02/10/2026, hoàn 12/10/2026, chưa kích hoạt | Chính sách mới, 10 ngày, đủ điều kiện, không phí; dẫn `data/policies/policy-from-oct.md` | [Trace B](stage-02-skills/traces/20261007-103615_4a3bbe40_turn01_aec97732.jsonl): sự kiện 4-5 đọc skill, 8-11 liệt kê và đọc reference song song, 14-17 đọc hai chính sách |
| A sau đổi tên hai file | Vẫn là 8 ngày, không đủ điều kiện; dẫn `data/policies/old-rules.md` | [Trace A đổi tên](stage-02-skills/traces/20261007-103808_40b66db9_turn01_cb6a373c.jsonl): sự kiện 8-9 liệt kê tên mới, 12-15 đọc `new-rules.md` và `old-rules.md`, 18-19 đọc reference |
| B sau đổi tên hai file | Vẫn là 10 ngày, đủ điều kiện, không phí; dẫn `data/policies/new-rules.md` | [Trace B đổi tên](stage-02-skills/traces/20261007-103848_9b2a0b0f_turn01_8a37236d.jsonl): sự kiện 8-9 liệt kê tên mới, 12-13 yêu cầu đọc cả hai file mới |
| Thiếu trạng thái kích hoạt | Hỏi trạng thái kích hoạt; chỉ xác nhận đủ thời gian, chưa kết luận đủ điều kiện hoàn | [Trace thiếu thông tin](stage-02-skills/traces/20261007-103906_c21bbb0d_turn01_31f4fe9a.jsonl): sự kiện 4-5 đọc skill, 12-14 yêu cầu đọc hai chính sách và reference |

Stage 00 cũng đã nhận câu hỏi A với cùng cấu hình model. Agent có `tools=[]`, không có tool result và trả lời rằng không có thông tin chính sách để kết luận. Đây là giới hạn cần ghi nhận trước khi thêm tool.

Lần thử A đầu tiên sau đổi tên đã **sai**: model chỉ đọc `new-rules.md` rồi áp dụng nhầm hạn 14 ngày cho ngày mua trước tháng 10; xem [trace lỗi ban đầu](stage-02-skills/traces/20261007-103642_50b595e2_turn01_6855382d.jsonl), sự kiện 12-16. Đã sửa skill để bắt buộc đọc tất cả file liệt kê được và chỉ dùng thời hạn/phí từ file có phạm vi bao gồm ngày mua. Lần chạy lại A sau đổi tên đạt kết quả đúng như bảng trên.

Trace JSONL của app chứa tool calls, tool results và context snapshot; sự kiện `run_completed` chỉ ghi độ dài câu trả lời, không ghi nguyên văn câu trả lời cuối. Kết quả trả lời đã được đối chiếu trực tiếp từ phiên AppTest và tóm tắt ở bảng. Không đưa `.env` hoặc API key vào bài nộp.

## Câu hỏi cuối bài

Tool `list_files` cho agent biết những tài liệu thực sự đang có; `read_file` cung cấp nội dung; skill chỉ dẫn cách chọn chính sách từ phạm vi hiệu lực theo ngày mua và cách trả lời. Prompt đơn thuần không thể quan sát tên file mới sau khi đổi tên, nên không thể bảo đảm tra cứu đúng tài liệu nếu thiếu tool tìm file.
