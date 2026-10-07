---
name: refund-policy
description: Tra cứu và áp dụng chính sách hoàn tiền theo ngày mua từ tài liệu trong workspace. Dùng khi khách hỏi có được hoàn tiền, thời hạn hoàn hoặc phí hoàn tiền.
---

# Tra cứu chính sách hoàn tiền

1. Kiểm tra câu hỏi có ngày mua, ngày yêu cầu hoàn và trạng thái kích hoạt. Nếu thiếu bất kỳ thông tin nào, hỏi lại thông tin còn thiếu trước khi kết luận.
2. Gọi `list_files` với `data/policies/` để tìm các tài liệu hiện có. Không đoán tên file từ ví dụ hoặc lịch sử cũ.
3. Dùng `read_file` đọc **tất cả** file chính sách do `list_files` trả về trước khi đưa ra kết luận. Không dừng sau file đầu tiên, kể cả khi file đó không áp dụng. Nếu chưa đọc được tất cả, tool báo lỗi hoặc không có tài liệu phù hợp, nói rõ chưa đủ căn cứ; không suy đoán quy định của file chưa đọc.
4. Đối chiếu ngày mua với phạm vi hiệu lực trong **từng file đã đọc**. Chỉ lấy thời hạn và phí từ file có phạm vi bao gồm ngày mua. Không lấy thời hạn của một file để áp dụng cho phạm vi của file khác; không chọn theo ngày yêu cầu hoàn. Nếu có nhiều chính sách cùng áp dụng hoặc phạm vi không rõ, hỏi lại hoặc báo chưa đủ căn cứ.
5. Tính số ngày lịch từ ngày mua đến ngày yêu cầu hoàn. Dùng ngày yêu cầu trong câu hỏi, không dùng ngày hiện tại. Số ngày bằng đúng giới hạn vẫn đủ điều kiện thời gian. Áp dụng thêm điều kiện kích hoạt trong tài liệu.
6. Đọc `skills/refund-policy/references/answer-template.md` bằng `read_file` và trả lời theo cấu trúc đó. Chỉ nêu phí khi đủ điều kiện; nếu không đủ điều kiện thì bỏ hẳn dòng phí. Trích đường dẫn của chính file có phạm vi áp dụng làm căn cứ.
