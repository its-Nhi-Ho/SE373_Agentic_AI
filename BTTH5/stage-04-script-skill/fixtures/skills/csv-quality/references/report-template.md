# Báo cáo chất lượng dữ liệu: `{đường dẫn CSV}`

Công cụ: `skills/csv-quality/scripts/check_csv.py` | exit code: {exit_code}

## Tổng quan
| Chỉ số | Giá trị |
|---|---|
| Số dòng dữ liệu (không tính header) | {row_count} |
| Dòng thiếu owner | {missing_owner_count} |
| Dòng hours không hợp lệ | {invalid_hours_count} |
| Số task_id bị lặp (distinct) | {duplicate_id_count} ({duplicate_ids}) |

## Chi tiết lỗi
| Line | Cột | Loại | task_id | Mô tả |
|---|---|---|---|---|
| {line} | {column} | {type} | {task_id} | {message} |

## Đánh giá
- Dữ liệu có dùng được để tính tổng giờ/KPI chưa? Nếu còn lỗi: chưa, nêu lý do.

## Khuyến nghị
- {Cách sửa đề xuất cho từng nhóm lỗi; không tự sửa file nguồn}
