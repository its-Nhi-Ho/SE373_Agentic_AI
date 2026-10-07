---
name: csv-quality
description: Kiểm tra chất lượng file CSV danh sách công việc (cột task_id, owner, hours) bằng script có sẵn, rồi ghi báo cáo Markdown dưới output/. Dùng khi người dùng yêu cầu kiểm tra, rà soát hoặc đánh giá chất lượng dữ liệu CSV công việc.
---

# CSV quality

Kiểm tra chất lượng CSV công việc bằng script, không tự đếm bằng mắt.

## Chạy script

Dùng tool `bash` (cwd là workspace). Lệnh đầy đủ:

```
python skills/csv-quality/scripts/check_csv.py --input <đường dẫn CSV>
```

Ví dụ: `python skills/csv-quality/scripts/check_csv.py --input data/tasks.csv`

Không cần đọc source script để chạy. Chỉ đọc `scripts/check_csv.py` khi cần hiểu một hành vi mà phần dưới không mô tả.

## Kiểm tra kết quả

- `exit_code` 0: phân tích thành công. `stdout` là JSON gồm `row_count`, `missing_owner_count`, `invalid_hours_count`, `duplicate_id_count`, `duplicate_ids`, `issues`. Dữ liệu có lỗi chất lượng vẫn là exit 0.
- `exit_code` 1: **lỗi thực thi** (file không tồn tại, thiếu cột bắt buộc, lỗi parse). Đọc `stderr`, báo lỗi cho người dùng. Không bịa thống kê, không ghi báo cáo như thể đã phân tích.
- `timed_out` true hoặc `ok` false: lệnh không chạy xong; báo lỗi, không suy đoán kết quả.
- Phân biệt rõ **lỗi dữ liệu** (nằm trong `issues`, script vẫn chạy thành công) với **lỗi thực thi** (exit khác 0).

## Viết báo cáo

1. Đọc template `references/report-template.md` trong thư mục skill này, tức `skills/csv-quality/references/report-template.md`.
2. Lấy mọi con số từ JSON của script. Mỗi issue ghi line number (header là line 1), cột và mô tả.
3. Không sửa file CSV khi người dùng chỉ yêu cầu kiểm tra. Có thể đề xuất cách sửa trong mục khuyến nghị.
4. Không tính tổng giờ hay KPI khi còn dữ liệu lỗi; ghi rõ lý do.
5. Ghi báo cáo bằng `write_file` vào đường dẫn người dùng yêu cầu (mặc định `output/csv-quality.md`), rồi trả lời đường dẫn và tóm tắt ngắn.
