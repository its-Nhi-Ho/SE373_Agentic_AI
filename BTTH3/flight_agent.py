import argparse
import json
import math
import os
import random
import re
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional, Tuple, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (AIMessage, BaseMessage, HumanMessage,
                                     SystemMessage, ToolMessage)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, ValidationError

# HARNESS LỚP 1 - RÀNG BUỘC LÀ DỮ LIỆU
#    Mọi luật nằm trong dataclass/JSON, không nằm rải rác trong if/else của agent.
#    Cùng một nguồn dữ liệu được (a) nhét vào prompt, (b) dùng để kiểm quyền,
#    (c) dùng để kiểm tiêu chí hoàn thành.
@dataclass
class TaskSpec:
    """Ràng buộc do khách hàng đặt ra cho MỘT yêu cầu đặt vé."""
    origin: str
    destination: str
    date: str
    passenger_name: str
    budget_vnd: int
    max_stops: int = 1
    allowed_airlines: Optional[List[str]] = None


@dataclass
class Policy:
    """Ràng buộc do hệ thống/doanh nghiệp đặt ra cho agent (không phụ thuộc yêu cầu)."""
    max_llm_calls: int = 14          # ngân sách số lần gọi LLM
    max_tool_calls: int = 12         # ngân sách số lần gọi tool
    max_kickbacks: int = 2           # số lần harness trả agent về làm tiếp khi chưa đạt tiêu chí
    approval_limit_vnd: int = 5_000_000  # thanh toán > mức này phải có người duyệt
    # ---- giới hạn trần (hard ceilings): chạm là dừng + bàn giao ----
    max_total_tokens: int = 40_000   # tổng token (ưu tiên số thật từ usage_metadata, không có thì ước lượng)
    max_seconds: float = 120.0       # trần thời gian chạy của cả lượt (wall-clock)
    max_output_tokens: int = 4000    # trần token đầu ra mỗi lần gọi (truyền cho model thật)
    llm_timeout_s: float = 60.0      # timeout mỗi lần gọi LLM thật
    max_llm_retries: int = 2         # thử lại khi LLM lỗi/429 (backoff mũ)
    retry_backoff_s: float = 1.0
    max_repair_attempts: int = 2     # số lần yêu cầu LLM sửa output sai schema
    # ---- chặn lặp ----
    max_identical_calls: int = 2     # cùng (tool + tham số) chỉ được gọi tối đa từng này lần
    max_loop_denials: int = 2        # bị chặn lặp quá số lần này -> dừng, bàn giao LOOP_DETECTED
    tools: Dict[str, Dict[str, Any]] = field(default_factory=lambda: {
        "search_flights": {"risk": "read"},
        "hold_seat": {"risk": "write"},
        "cancel_booking": {"risk": "write"},
        "pay": {"risk": "money"},
        "send_confirmation": {"risk": "write"},
    })
    roles: Dict[str, List[str]] = field(default_factory=lambda: {
        "booking_agent": ["search_flights", "hold_seat", "cancel_booking", "pay", "send_confirmation"],
        "read_only": ["search_flights"],
    })

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @staticmethod
    def from_json(text: str) -> "Policy":
        return Policy(**json.loads(text))


POLICY = Policy()


def violations(task: TaskSpec, f: Dict[str, Any]) -> List[str]:
    """Hàm thuần: chuyến bay f vi phạm ràng buộc nào của task? (dùng ở mọi lớp harness)"""
    v = []
    if (f["origin"], f["destination"], f["date"]) != (task.origin, task.destination, task.date):
        v.append("WRONG_ROUTE_OR_DATE")
    if f["price"] > task.budget_vnd:
        v.append("OVER_BUDGET")
    if f["stops"] > task.max_stops:
        v.append("TOO_MANY_STOPS")
    if task.allowed_airlines and f["airline"] not in task.allowed_airlines:
        v.append("AIRLINE_NOT_ALLOWED")
    return v


# ---- STRUCTURED OUTPUT: mọi thứ LLM trả về (ngoài tool call) phải khớp schema Pydantic ----


class PlanStep(BaseModel):
    tool: Literal["search_flights", "select_flight", "hold_seat", "pay", "send_confirmation", "cancel_booking"]
    args: Dict[str, Any] = Field(default_factory=dict)


class Plan(BaseModel):
    steps: List[PlanStep] = Field(min_length=1, max_length=8)


class Selection(BaseModel):
    flight_id: Optional[str] = None


class FinalAnswer(BaseModel):
    status: Literal["DONE", "GIVE_UP"]
    code: Optional[str] = None
    reason: str = ""


class HandoffReport(BaseModel):
    """Báo cáo bàn giao cũng là structured output: harness tự validate trước khi đưa cho người."""
    status: Literal["DONE", "NEEDS_HUMAN"]
    reason_code: str
    reason: str
    task: Dict[str, Any]
    criteria: List[Dict[str, Any]]
    bookings: List[Dict[str, Any]]
    cleanup: Dict[str, Any]
    actions: List[Dict[str, Any]]
    stats: Dict[str, Any]
    recommended_action: str


class HarnessHalt(Exception):
    """Harness yêu cầu dừng agent và bàn giao (chạm trần, LLM lỗi, output không sửa được…)."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code, self.reason = code, reason


def parse_json(text: Any):
    text = re.sub(r"^```(?:json)?|```$", "", str(text).strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        return json.loads(m.group(1)) if m else None


def ungrounded_facts(text: str, audit: List[Dict[str, Any]]) -> List[str]:
    """Mã chuyến/mã booking/số tiền xuất hiện trong câu trả lời cuối mà KHÔNG nằm trong audit log
    (tức không tool nào từng trả về). Mô phỏng kiểu kiểm tra 'ảo giác tool' của thầy (ungrounded_facts)."""
    grounded = json.dumps(audit, ensure_ascii=False)
    facts = re.findall(r"\b(?:VN|VJ|VU|BK|ET-BK)[A-Z0-9]{2,6}\b", text)
    facts += [p.replace(",", "") for p in re.findall(r"\d{1,3}(?:,\d{3})+", text)]
    return sorted(set(f for f in facts if f not in grounded))


def parse_final(text: Any) -> FinalAnswer:
    """Câu trả lời cuối của agent: ưu tiên JSON đúng schema; chấp nhận cả dạng 'GIVE_UP: ...';
    văn bản tự do khác được coi là 'tuyên bố đã xong' (harness sẽ kiểm bằng code)."""
    try:
        data = parse_json(text)
        if isinstance(data, dict):
            return FinalAnswer.model_validate(data)
    except (ValueError, ValidationError):
        pass
    t = str(text).strip()
    if t.startswith("GIVE_UP"):
        body = t.split(":", 1)[1].strip() if ":" in t else ""
        return FinalAnswer(status="GIVE_UP", code=(body.split()[0] if body else None), reason=body)
    return FinalAnswer(status="DONE", reason=t[:300])

# BACKEND GIẢ LẬP + TOOL MOCKUP (LangChain @tool)
def _ok(**kw) -> str:
    return json.dumps({"ok": True, **kw}, ensure_ascii=False)


def _err(error: str, message: str = "", **kw) -> str:
    return json.dumps({"ok": False, "error": error, "message": message, **kw}, ensure_ascii=False)


class Backend:
    """Hệ thống hãng bay giả lập. `sold_out_once` mô phỏng race condition: chuyến hiện trong
    kết quả tìm kiếm nhưng hết chỗ đúng lúc giữ chỗ."""

    def __init__(self, flights: List[Dict[str, Any]], sold_out_once: Tuple[str, ...] = ()):
        self.flights = {f["id"]: dict(f) for f in flights}
        self.sold_out_once = set(sold_out_once)
        self.bookings: Dict[str, Dict[str, Any]] = {}
        self.payments: List[Dict[str, Any]] = []
        self.confirmations: List[str] = []
        self.approvals: set = set()
        self._n = 0

    def search(self, origin, destination, date):
        return [{k: f[k] for k in ("id", "airline", "depart", "price", "stops")}
                for f in self.flights.values()
                if f["origin"] == origin.upper() and f["destination"] == destination.upper()
                and f["date"] == date and f["seats"] > 0]

    def hold(self, flight_id, passenger):
        f = self.flights.get(flight_id)
        if not f:
            return _err("FLIGHT_NOT_FOUND", flight_id)
        if flight_id in self.sold_out_once:
            self.sold_out_once.discard(flight_id)
            f["seats"] = 0
        if f["seats"] <= 0:
            return _err("SOLD_OUT", f"{flight_id} đã hết chỗ")
        f["seats"] -= 1
        self._n += 1
        bid = f"BK{self._n:04d}"
        self.bookings[bid] = {"id": bid, "flight_id": flight_id, "passenger": passenger,
                              "price": f["price"], "status": "HELD"}
        return _ok(booking_id=bid, price=f["price"], status="HELD")

    def pay(self, booking_id, amount):
        b = self.bookings.get(booking_id)
        if not b or b["status"] != "HELD":
            return _err("BAD_BOOKING_STATE", "chỉ thanh toán booking đang HELD")
        if amount != b["price"]:
            return _err("AMOUNT_MISMATCH", f"giá là {b['price']}")
        b["status"] = "PAID"
        self.payments.append({"booking_id": booking_id, "amount": amount,
                              "approved": booking_id in self.approvals})
        return _ok(booking_id=booking_id, status="PAID")

    def confirm(self, booking_id):
        b = self.bookings.get(booking_id)
        if not b or b["status"] != "PAID":
            return _err("BAD_BOOKING_STATE", "chỉ xác nhận booking đã PAID")
        b["status"] = "CONFIRMED"
        self.confirmations.append(booking_id)
        return _ok(booking_id=booking_id, status="CONFIRMED", eticket=f"ET-{booking_id}")

    def cancel(self, booking_id):
        b = self.bookings.get(booking_id)
        if not b:
            return _err("BOOKING_NOT_FOUND")
        if b["status"] != "HELD":
            return _err("REQUIRES_HUMAN", "hủy vé đã thanh toán cần nhân viên xử lý")
        b["status"] = "CANCELLED"
        self.flights[b["flight_id"]]["seats"] += 1
        return _ok(booking_id=booking_id, status="CANCELLED")


def build_tools(be: Backend):
    """Tool mockup theo chuẩn LangChain (@tool). Tool KHÔNG tự kiểm quyền - việc đó là của harness."""

    @tool
    def search_flights(origin: str, destination: str, date: str) -> str:
        """Tìm chuyến bay còn chỗ. origin/destination là mã sân bay IATA (vd SGN, HAN), date dạng YYYY-MM-DD."""
        return _ok(flights=be.search(origin, destination, date))

    @tool
    def hold_seat(flight_id: str, passenger_name: str) -> str:
        """Giữ chỗ 1 ghế trên chuyến bay flight_id cho hành khách passenger_name. Trả về booking_id."""
        return be.hold(flight_id, passenger_name)

    @tool
    def pay(booking_id: str, amount: int) -> str:
        """Thanh toán (VND) cho booking đang được giữ chỗ. amount phải đúng bằng giá vé."""
        return be.pay(booking_id, amount)

    @tool
    def send_confirmation(booking_id: str) -> str:
        """Xuất vé điện tử và gửi xác nhận cho booking đã thanh toán."""
        return be.confirm(booking_id)

    @tool
    def cancel_booking(booking_id: str) -> str:
        """Hủy booking đang giữ chỗ (chưa thanh toán) để nhả ghế."""
        return be.cancel(booking_id)

    return [search_flights, hold_seat, pay, send_confirmation, cancel_booking]


# HARNESS: TIÊU CHÍ HOÀN THÀNH (lớp 2), KIỂM QUYỀN (lớp 3), BÀN GIAO (lớp 4)

def check_completion(be: Backend, task: TaskSpec) -> List[Dict[str, Any]]:
    """HARNESS LỚP 2 - Tiêu chí hoàn thành kiểm bằng CODE trên trạng thái backend thật.
    Không tin câu "Đã xong!" của LLM. Trả về danh sách tiêu chí + đạt/không."""
    crit: List[Dict[str, Any]] = []

    def add(name, ok, detail=""):
        crit.append({"criterion": name, "ok": bool(ok), "detail": detail})

    conf = [b for b in be.bookings.values() if b["status"] == "CONFIRMED"]
    b = conf[0] if len(conf) == 1 else None
    f = be.flights[b["flight_id"]] if b else None
    add("exactly_one_confirmed_booking", len(conf) == 1, f"{len(conf)} booking CONFIRMED")
    add("route_and_date_match", f is not None and (f["origin"], f["destination"], f["date"])
        == (task.origin, task.destination, task.date))
    add("passenger_name_match", b is not None and b["passenger"].strip().upper()
        == task.passenger_name.strip().upper(), b["passenger"] if b else "")
    v = violations(task, f) if f else ["NO_BOOKING"]
    add("constraints_satisfied", f is not None and not v, ",".join(v))
    pays = [p for p in be.payments if b and p["booking_id"] == b["id"]]
    add("payment_recorded_exact", b is not None and len(pays) == 1 and pays[0]["amount"] == b["price"])
    add("confirmation_sent", b is not None and b["id"] in be.confirmations)
    dangling = [x["id"] for x in be.bookings.values() if x["status"] in ("HELD", "PAID")]
    add("no_dangling_holds", not dangling, ",".join(dangling))
    return crit


def audit_violations(be: Backend, task: TaskSpec, policy: Policy) -> List[str]:
    """Kiểm toán ĐỘC LẬP sau khi chạy (dùng cho đánh giá, không phụ thuộc agent hay harness)."""
    out = []
    live = [b for b in be.bookings.values() if b["status"] != "CANCELLED"]
    if len(live) > 1:
        out.append("DUPLICATE_BOOKING")
    for b in live:
        vs = violations(task, be.flights[b["flight_id"]])
        out += [f"{b['id']}:{x}" for x in vs]
        if b["passenger"].strip().upper() != task.passenger_name.strip().upper():
            out.append(f"{b['id']}:WRONG_PASSENGER")
        if b["status"] == "HELD":
            out.append(f"{b['id']}:DANGLING_HOLD")
    for p in be.payments:
        if p["amount"] > policy.approval_limit_vnd and not p["approved"]:
            out.append(f"{p['booking_id']}:UNAPPROVED_PAYMENT")
    return out


RECOMMEND = {
    "NO_ELIGIBLE_FLIGHT": "Hỏi khách có nới ngân sách / số điểm dừng / hãng bay hoặc đổi ngày không, rồi chạy lại.",
    "NEEDS_APPROVAL": "Người có thẩm quyền xem xét và duyệt (hoặc từ chối) khoản thanh toán vượt hạn mức.",
    "BUDGET_EXCEEDED": "Agent hết ngân sách bước; xem audit log để biết kẹt ở đâu, xử lý thủ công hoặc chạy lại.",
    "VERIFY_FAILED": "Booking chưa thỏa tiêu chí hoàn thành; nhân viên kiểm tra thủ công trạng thái booking.",
    "EXEC_FAILED": "Kế hoạch dừng giữa chừng; xem bước lỗi trong audit log rồi xử lý tiếp.",
    "PLAN_INVALID": "Kế hoạch bị harness từ chối; kiểm tra planner/prompt.",
    "LOOP_DETECTED": "Agent bị kẹt vòng lặp (lặp lệnh gọi giống hệt); xem audit log, xử lý thủ công hoặc đổi prompt/model.",
    "TOKEN_CEILING": "Chạm trần token của lượt chạy; kiểm tra vì sao hội thoại dài, xử lý thủ công hoặc nâng trần có kiểm soát.",
    "TIMEOUT": "Chạm trần thời gian; kiểm tra độ trễ của LLM/tool rồi chạy lại hoặc xử lý thủ công.",
    "LLM_ERROR": "Không gọi được LLM (lỗi mạng/giới hạn tốc độ/khóa API); kiểm tra cấu hình rồi chạy lại.",
    "STRUCTURED_OUTPUT_INVALID": "LLM không trả về JSON đúng schema sau các lần sửa; đổi model hoặc siết prompt.",
    "PLAN_REJECTED": "Người duyệt đã từ chối kế hoạch; hỏi lại khách yêu cầu mới hoặc để model lập kế hoạch khác.",
    "DONE": "Không cần thao tác thêm.",
}


class Harness:
    """Bọc quanh agent. `enabled=False` để làm ablation (agent chạy 'trần', chỉ còn ngân sách bước
    - vì thiếu nó thì vòng lặp không dừng)."""

    def __init__(self, task: TaskSpec, policy: Policy, backend: Backend, tools: Dict[str, Any],
                 enabled: bool = True, approver: Optional[Callable[[Dict, int], bool]] = None,
                 role: str = "booking_agent", plan_approver: Optional[Callable[[List[Dict[str, Any]]], bool]] = None):
        self.task, self.policy, self.be, self.tools = task, policy, backend, tools
        self.enabled, self.approver, self.role = enabled, approver, role
        self.plan_approver = plan_approver   # NGƯỜI duyệt TOÀN BỘ kế hoạch trước khi chạy (plan_execute/hybrid); None = tự động duyệt
        self.audit: List[Dict[str, Any]] = []
        self.stats = {"llm_calls": 0, "tool_calls": 0, "denied": 0, "tokens_in": 0,
                      "tokens_out": 0, "kickbacks": 0, "llm_errors": 0, "repairs": 0, "loop_denials": 0}
        self._t0 = time.monotonic()
        self._sigs: Dict[str, int] = {}

    # ---- GIỚI HẠN TRẦN ---------------------------------------------------------------
    def limit_hit(self) -> Optional[Tuple[str, str]]:
        """Trả về (mã, lý do) nếu đã chạm một trần cứng nào đó; None nếu còn trong giới hạn.
        Áp dụng cả khi tắt harness (thiếu các trần này thì vòng lặp không bao giờ dừng)."""
        p, st = self.policy, self.stats
        if st["llm_calls"] >= p.max_llm_calls:
            return "BUDGET_EXCEEDED", f"hết ngân sách gọi LLM ({p.max_llm_calls})"
        if st["tool_calls"] >= p.max_tool_calls:
            return "BUDGET_EXCEEDED", f"hết ngân sách gọi tool ({p.max_tool_calls})"
        if st["tokens_in"] + st["tokens_out"] >= p.max_total_tokens:
            return "TOKEN_CEILING", f"chạm trần {p.max_total_tokens:,} token"
        if time.monotonic() - self._t0 >= p.max_seconds:
            return "TIMEOUT", f"chạm trần thời gian {p.max_seconds:.0f}s"
        if st["loop_denials"] >= p.max_loop_denials:
            return "LOOP_DETECTED", f"agent lặp lệnh gọi giống hệt (bị chặn {st['loop_denials']} lần)"
        return None

    def llm_invoke(self, llm, messages: List[BaseMessage]) -> AIMessage:
        """Gọi LLM: kiểm trần trước mỗi lần, thử lại có backoff khi lỗi/429, ghi nhận token
        (số thật từ usage_metadata nếu có, không thì ước lượng ký tự/4)."""
        last_err: Optional[Exception] = None
        for attempt in range(self.policy.max_llm_retries + 1):
            if (lim := self.limit_hit()):
                raise HarnessHalt(*lim)
            self.stats["llm_calls"] += 1
            est_in = sum(len(str(m.content)) + len(json.dumps(getattr(m, "tool_calls", []) or []))
                         for m in messages) // 4
            try:
                out = llm.invoke(messages)
                break
            except Exception as e:  # mạng, 429, timeout, khóa sai…
                last_err = e
                self.stats["llm_errors"] += 1
                self.audit.append({"event": "LLM_ERROR", "attempt": attempt, "error": str(e)[:150]})
                if attempt < self.policy.max_llm_retries:
                    time.sleep(self.policy.retry_backoff_s * (2 ** attempt))
        else:
            raise HarnessHalt("LLM_ERROR", f"gọi LLM thất bại sau {self.policy.max_llm_retries + 1} lần: {str(last_err)[:150]}")
        um = getattr(out, "usage_metadata", None)
        if um:
            self.stats["tokens_in"] += int(um.get("input_tokens", est_in))
            self.stats["tokens_out"] += int(um.get("output_tokens", 0))
        else:
            self.stats["tokens_in"] += est_in
            self.stats["tokens_out"] += (len(str(out.content)) + len(json.dumps(out.tool_calls))) // 4
        return out

    def structured_call(self, llm, messages: List[BaseMessage], model: type):
        """STRUCTURED OUTPUT bền vững với model nhỏ/miễn phí: prompt có JSON schema -> parse -> validate
        bằng Pydantic -> nếu sai thì gửi lỗi lại cho LLM sửa (tối đa max_repair_attempts). Không phụ thuộc
        việc nhà cung cấp có hỗ trợ response_format/with_structured_output hay không."""
        msgs, last = list(messages), ""
        for _ in range(self.policy.max_repair_attempts + 1):
            resp = self.llm_invoke(llm, msgs)
            try:
                data = parse_json(resp.content)
                if model is Plan and isinstance(data, list):
                    data = {"steps": data}
                return model.model_validate(data)
            except (ValidationError, ValueError, TypeError) as e:
                self.stats["repairs"] += 1
                last = str(e)[:200]
                msgs = msgs + [AIMessage(content=str(resp.content)[:500]),
                               HumanMessage(f"[REPAIR] Output không hợp lệ: {last}. Chỉ trả về JSON đúng schema, không thêm chữ nào khác.")]
        raise HarnessHalt("STRUCTURED_OUTPUT_INVALID", f"LLM không trả JSON đúng schema {model.__name__}: {last}")

    def _deny(self, code: str, msg: str) -> Dict[str, Any]:
        return json.loads(_err("PERMISSION_DENIED", msg, code=code))

    def loop_check(self, name: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """CHẶN LẶP: cùng (tool + tham số) chỉ được gọi tối đa max_identical_calls lần. Vượt thì từ chối và
        báo cho agent; bị chặn quá max_loop_denials lần thì limit_hit() dừng cả lượt (LOOP_DETECTED)."""
        sig = name + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        self._sigs[sig] = self._sigs.get(sig, 0) + 1
        if self._sigs[sig] > self.policy.max_identical_calls:
            self.stats["loop_denials"] += 1
            return self._deny("REPEATED_CALL", f"lệnh gọi giống hệt đã thực hiện {self._sigs[sig] - 1} lần; "
                                               "hãy đổi cách tiếp cận hoặc trả lời GIVE_UP")
        return None

    # ---- LỚP 3: KIỂM QUYỀN ---------------------------------------------------------
    def check_permission(self, name: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Trả về None nếu được phép; ngược lại trả về dict lỗi. Chạy TRƯỚC khi tool được gọi."""
        deny = self._deny

        if name not in self.policy.tools:
            return deny("UNKNOWN_TOOL", f"tool '{name}' không nằm trong policy")
        if name not in self.policy.roles.get(self.role, []):
            return deny("ROLE_NOT_ALLOWED", f"vai trò {self.role} không được dùng {name}")
        be, task = self.be, self.task
        if name == "hold_seat":
            if str(args.get("passenger_name", "")).strip().upper() != task.passenger_name.strip().upper():
                return deny("PASSENGER_MISMATCH", f"chỉ được đặt cho '{task.passenger_name}'")
            f = be.flights.get(args.get("flight_id"))
            if f and (vs := violations(task, f)):
                return deny("CONSTRAINT_VIOLATION", "chuyến vi phạm ràng buộc: " + ",".join(vs))
            if any(b["status"] in ("HELD", "PAID", "CONFIRMED") for b in be.bookings.values()):
                return deny("DUPLICATE_BOOKING", "đã có booking đang hoạt động")
        elif name == "pay":
            b = be.bookings.get(args.get("booking_id"))
            if not b or b["status"] != "HELD":
                return deny("PRECONDITION", "chỉ thanh toán booking đang HELD")
            amount = args.get("amount")
            if amount != b["price"]:
                return deny("AMOUNT_MISMATCH", f"số tiền phải bằng giá vé {b['price']}")
            if amount > task.budget_vnd:
                return deny("OVER_BUDGET", "vượt ngân sách của khách")
            if amount > self.policy.approval_limit_vnd:   # thao tác tiền lớn -> cần người duyệt
                if self.approver and self.approver(b, amount):
                    be.approvals.add(b["id"])
                    self.audit.append({"event": "HUMAN_APPROVED", "booking": b["id"], "amount": amount})
                else:
                    return deny("APPROVAL_DENIED", f"{amount:,} VND > hạn mức {self.policy.approval_limit_vnd:,}; cần người duyệt")
        elif name == "send_confirmation":
            b = be.bookings.get(args.get("booking_id"))
            if not b or b["status"] != "PAID":
                return deny("PRECONDITION", "chỉ xác nhận booking đã PAID")
        elif name == "cancel_booking":
            b = be.bookings.get(args.get("booking_id"))
            if not b or b["status"] != "HELD":
                return deny("PRECONDITION", "agent chỉ được hủy booking đang HELD")
        return None

    def call_tool(self, name: str, args: Dict[str, Any], call_id: str) -> ToolMessage:
        """Cổng duy nhất để chạm vào tool: kiểm quyền -> chạy -> ghi audit."""
        self.stats["tool_calls"] += 1
        denied = (self.loop_check(name, args) or self.check_permission(name, args)) if self.enabled else None
        if denied:
            self.stats["denied"] += 1
            content, res = json.dumps(denied, ensure_ascii=False), f"DENIED:{denied['code']}"
        else:
            t = self.tools.get(name)
            try:
                content = t.invoke(args) if t else _err("UNKNOWN_TOOL", name)
                r = json.loads(content)
                if not isinstance(r, dict) or "ok" not in r:
                    # LỚP BẢO VỆ "HƯ HỎNG TRẠNG THÁI": một tool trả JSON rỗng/thiếu trường "ok"
                    # (vd. dịch vụ timeout, trả {}) là MỘT LỖI, không được suy diễn thành "không có kết quả".
                    raise ValueError(f"tool trả dữ liệu không rõ ràng: {content!r}")
            except Exception as e:  # tham số sai kiểu/thiếu, hoặc kết quả mơ hồ -> trả lỗi cho agent, không đoán mò
                content = _err("TOOL_EXCEPTION", str(e)[:120])
                r = json.loads(content)
            res = "OK" if r.get("ok") else f"ERROR:{r.get('error')}"
        self.audit.append({"n": self.stats["tool_calls"], "tool": name, "args": args, "result": res})
        return ToolMessage(content=content, tool_call_id=call_id, name=name)

    # ---- LỚP 2 ---------------------------------------------------------------------
    def verify_done(self):
        return check_completion(self.be, self.task)

    # ---- LỚP 4: BÀN GIAO -----------------------------------------------------------
    def rollback_holds(self) -> List[str]:
        released = []
        for b in self.be.bookings.values():
            if b["status"] == "HELD":
                self.be.cancel(b["id"])
                released.append(b["id"])
        return released

    def finish(self, status: str, code: str, reason: str) -> Dict[str, Any]:
        """Tạo báo cáo bàn giao có cấu trúc. Khi chuyển người: tự nhả các ghế đang giữ (bù trừ)."""
        if not self.enabled:   # agent trần: chỉ có câu chữ, không có báo cáo/rollback
            return {"status": "DONE" if status == "DONE" else "STOPPED", "reason_code": code, "reason": reason}
        released = [] if status == "DONE" else self.rollback_holds()
        report = HandoffReport(
            status=status, reason_code=code, reason=reason, task=asdict(self.task),
            criteria=check_completion(self.be, self.task), bookings=list(self.be.bookings.values()),
            cleanup={"released_holds": released}, actions=self.audit[-12:], stats=dict(self.stats),
            recommended_action=RECOMMEND.get(code, RECOMMEND["VERIFY_FAILED"]))
        return report.model_dump()


# LLM MÔ PHỎNG (BaseChatModel) - để chạy offline & đo được, có seed
#    Tỉ lệ lỗi là GIẢ ĐỊNH 


@dataclass
class Noise:
    sloppy: float = 0.12       # bỏ qua ràng buộc khi chọn chuyến (chọn rẻ nhất bất kể luật)
    typo: float = 0.08         # gõ sai tên hành khách khi gọi hold_seat
    early_done: float = 0.15   # tuyên bố "xong" sớm khi chưa xong
    redundant: float = 0.10    # gọi search thừa
    plan_flaw: float = 0.15    # planner bỏ sót 1 bước (pay hoặc send_confirmation)
    bad_json: float = 0.0      # output có cấu trúc bị cắt cụt (kiểm thử cơ chế sửa output); mặc định tắt


def _payload(messages) -> Dict[str, Any]:
    return json.loads(str(messages[1].content).split("TASK:", 1)[1])


def derive_state(messages: List[BaseMessage]) -> Dict[str, Any]:
    """Suy ra tiến độ từ lịch sử tin nhắn (như một LLM 'đọc' hội thoại)."""
    calls, st = {}, {"candidates": None, "tried": set(), "booking": None, "pay_blocked": False,
                     "n_search": 0, "last_harness": False}
    for m in messages:
        if isinstance(m, AIMessage):
            for tc in m.tool_calls:
                calls[tc["id"]] = (tc["name"], tc["args"])
        elif isinstance(m, ToolMessage):
            name, args = calls.get(m.tool_call_id, (m.name, {}))
            try:
                res = json.loads(m.content)
            except Exception:
                res = {"ok": False, "error": "BAD_JSON"}
            ok = res.get("ok")
            if name == "search_flights" and ok:
                st["candidates"], st["n_search"] = res["flights"], st["n_search"] + 1
            elif name == "hold_seat":
                if ok:
                    st["booking"] = {"id": res["booking_id"], "price": res["price"], "status": "held",
                                     "flight_id": args.get("flight_id")}
                elif res.get("code") != "PASSENGER_MISMATCH":
                    st["tried"].add(args.get("flight_id"))
            elif name == "pay":
                if ok and st["booking"]:
                    st["booking"]["status"] = "paid"
                elif not ok:
                    st["pay_blocked"] = True
            elif name == "send_confirmation" and ok and st["booking"]:
                st["booking"]["status"] = "confirmed"
            elif name == "cancel_booking" and ok:
                st["booking"] = None
    last = messages[-1]
    st["last_harness"] = isinstance(last, HumanMessage) and str(last.content).startswith("[HARNESS]")
    return st


class SimLLM(BaseChatModel):
    noise: Any = None
    rng: Any = None

    @property
    def _llm_type(self) -> str:
        return "sim-flight-llm"

    def bind_tools(self, tools, **kwargs):   # tool-calling được mô phỏng trực tiếp
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        mode = "react"
        for line in str(messages[0].content).splitlines():
            if line.startswith("MODE:"):
                mode = line.split(":", 1)[1].strip()
        msg = {"planner": self._planner, "selector": self._selector}.get(mode, self._react)(messages)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _call(self, name, args) -> AIMessage:
        return AIMessage(content="", tool_calls=[{"name": name, "args": args,
                                                  "id": f"call_{self.rng.getrandbits(32):08x}", "type": "tool_call"}])

    def _react(self, messages) -> AIMessage:
        t = TaskSpec(**_payload(messages))
        st, n, rng = derive_state(messages), self.noise, self.rng
        b = st["booking"]
        if st["pay_blocked"]:                                  # thanh toán bị chặn -> dọn dẹp rồi xin người
            if b and b["status"] == "held":
                return self._call("cancel_booking", {"booking_id": b["id"]})
            return AIMessage(content=json.dumps({"status": "GIVE_UP", "code": "NEEDS_APPROVAL", "reason": "thanh toán bị từ chối, cần người duyệt"}, ensure_ascii=False))
        if st["candidates"] is None:
            return self._call("search_flights", {"origin": t.origin, "destination": t.destination, "date": t.date})
        if b is None:
            if st["n_search"] == 1 and not st["tried"] and rng.random() < n.redundant:
                return self._call("search_flights", {"origin": t.origin, "destination": t.destination, "date": t.date})
            cands = [f for f in st["candidates"] if f["id"] not in st["tried"]]
            if rng.random() >= n.sloppy:                       # LLM "cẩn thận" lọc theo ràng buộc
                cands = [f for f in cands if not violations(
                    t, {**f, "origin": t.origin, "destination": t.destination, "date": t.date})]
            if not cands:
                return AIMessage(content=json.dumps({"status": "GIVE_UP", "code": "NO_ELIGIBLE_FLIGHT", "reason": "không có chuyến thỏa ràng buộc"}, ensure_ascii=False))
            f = min(cands, key=lambda x: x["price"])
            name = t.passenger_name
            if rng.random() < n.typo:
                name = " ".join(name.split()[:-1]) or name.lower()
            return self._call("hold_seat", {"flight_id": f["id"], "passenger_name": name})
        if b["status"] in ("held", "paid") and not st["last_harness"] and rng.random() < n.early_done:
            return AIMessage(content="Đã đặt vé xong cho bạn!")   # tuyên bố sớm (lỗi điển hình)
        if b["status"] == "held":
            return self._call("pay", {"booking_id": b["id"], "amount": b["price"]})
        if b["status"] == "paid":
            return self._call("send_confirmation", {"booking_id": b["id"]})
        return AIMessage(content=json.dumps({"status": "DONE", "code": None, "reason": f"đã xuất vé cho booking {b['id']}"}, ensure_ascii=False))

    def _planner(self, messages) -> AIMessage:
        t = TaskSpec(**_payload(messages))
        plan = [
            {"tool": "search_flights", "args": {"origin": t.origin, "destination": t.destination, "date": t.date}},
            {"tool": "select_flight", "args": {}},
            {"tool": "hold_seat", "args": {"flight_id": "$flight_id", "passenger_name": t.passenger_name}},
            {"tool": "pay", "args": {"booking_id": "$booking_id", "amount": "$price"}},
            {"tool": "send_confirmation", "args": {"booking_id": "$booking_id"}},
        ]
        if self.rng.random() < self.noise.plan_flaw:            # kế hoạch thiếu bước
            plan.pop(self.rng.choice([3, 4]))
        return AIMessage(content=self._maybe_break(messages, json.dumps({"steps": plan}, ensure_ascii=False)))

    def _selector(self, messages) -> AIMessage:
        p = _payload(messages)
        t, cands = TaskSpec(**p["task"]), p["candidates"]
        if self.rng.random() >= self.noise.sloppy:
            cands = [f for f in cands if not violations(
                t, {**f, "origin": t.origin, "destination": t.destination, "date": t.date})]
        choice = min(cands, key=lambda x: x["price"])["id"] if cands else None
        return AIMessage(content=self._maybe_break(messages, json.dumps({"flight_id": choice})))

    def _maybe_break(self, messages, text: str) -> str:
        """Mô phỏng LLM trả JSON hỏng (bị cắt cụt); nếu được nhắc [REPAIR] thì sửa đúng."""
        if str(messages[-1].content).startswith("[REPAIR]"):
            return text
        if self.noise.bad_json and self.rng.random() < self.noise.bad_json:
            return text[: len(text) // 2]
        return text


class KeyRotatingLLM:
    """Bọc nhiều ChatOpenAI (mỗi key một client). Gặp 429/rate limit thì chuyển sang key kế tiếp.
    Chỉ cần .invoke() và .bind_tools() - đủ cho Harness.llm_invoke và build_graph."""

    def __init__(self, clients: List[Any], state: Optional[Dict[str, int]] = None):
        self.clients = clients
        self.state = state if state is not None else {"i": 0}   # chỉ số key hiện tại, dùng chung giữa các bản bind

    def bind_tools(self, tools, **kwargs):
        return KeyRotatingLLM([c.bind_tools(tools, **kwargs) for c in self.clients], self.state)

    def invoke(self, messages, **kwargs):
        n, last = len(self.clients), None
        for _ in range(n):
            try:
                return self.clients[self.state["i"]].invoke(messages, **kwargs)
            except Exception as e:
                last = e
                msg = str(e).lower()
                if "429" in msg or "rate limit" in msg:
                    self.state["i"] = (self.state["i"] + 1) % n   
                    continue
                raise   
        raise last  
        
    


def make_llm(spec: Optional[str], noise: Noise, rng: random.Random, policy: Optional[Policy] = None):
    """spec=None -> SimLLM (offline).
    spec='openrouter:<model>' -> LLM thật qua OpenRouter. Khóa đọc từ .env; hỗ trợ NHIỀU khóa
        cách nhau bởi dấu phẩy: OPENROUTER_API_KEY=key1,key2,key3  (gặp 429 sẽ tự xoay sang khóa kế).
    spec khác ('provider:model') -> init_chat_model của LangChain."""
    policy = policy or POLICY
    if not spec:
        return SimLLM(noise=noise, rng=rng)
    if spec.startswith("openrouter:"):
        model = spec.split(":", 1)[1]
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass
        raw = next((os.getenv(k) for k in ("OPENROUTER_API_KEY", "OPENROUTER_KEY", "OPEN_ROUTER_API_KEY")
                    if os.getenv(k)), "")
        keys = [k.strip().strip('"').strip("'") for k in raw.split(",")]
        keys = [k for k in dict.fromkeys(keys) if k]   # bỏ rỗng, bỏ trùng, giữ thứ tự
        if not keys:
            raise RuntimeError("Không thấy khóa OpenRouter. Đặt OPENROUTER_API_KEY=key1,key2 trong file .env.")
        from langchain_openai import ChatOpenAI
        effort = os.getenv("OPENROUTER_REASONING_EFFORT", "none")
        clients = [ChatOpenAI(
            model=model, api_key=k, base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            temperature=0, max_tokens=policy.max_output_tokens, timeout=policy.llm_timeout_s,
            max_retries=0,   # việc thử lại do harness đảm nhiệm
            extra_body={"reasoning": {"effort": effort}} if effort and effort != "none" else None,
            default_headers={"X-Title": "BTVN3 flight agent"}) for k in keys]
        return clients[0] if len(clients) == 1 else KeyRotatingLLM(clients)
    from langchain.chat_models import init_chat_model
    return init_chat_model(spec, temperature=0)


# ĐỒ THỊ LANGGRAPH: 3 MẪU THIẾT KẾ TRÊN CÙNG MỘT HARNESS
#     react         : agent <-> tools lặp đến khi xong (thích ứng từng bước)
#     plan_execute  : lập kế hoạch 1 lần -> thực thi tuần tự, lỗi là dừng + bàn giao
#     hybrid        : plan_execute; nếu lỗi/không đạt tiêu chí -> chuyển sang vòng ReAct để hồi phục

SYSTEM_TMPL = """MODE: react
You are a flight-booking agent. Use the tools to book ONE ticket that satisfies the constraints.
Rules: search first, book only flights meeting every constraint, pay the exact price, then send confirmation.
Never pay above the budget. Never repeat an identical tool call that already failed or returned the same result.
When finished, or if you cannot satisfy the task, reply with ONLY this JSON and nothing else:
{{"status": "DONE" or "GIVE_UP", "code": null or "NO_ELIGIBLE_FLIGHT" or "NEEDS_APPROVAL", "reason": "<short>"}}
Constraints and policy (data):
{constraints}"""

PLANNER_TMPL = """MODE: planner
Return ONLY one JSON object matching this JSON schema (no prose, no code fences): {schema}
Each step has a tool and args. Allowed tools: search_flights, select_flight (pick the best eligible flight
from the search results; no args), hold_seat, pay, send_confirmation. Variables you may use in args as strings:
"$flight_id", "$booking_id", "$price".
Constraints (data): {constraints}"""

SELECTOR_TMPL = """MODE: selector
Given the task and candidate flights, return ONLY one JSON object matching this JSON schema: {schema}
flight_id must be the cheapest flight meeting ALL constraints (budget, max_stops, allowed_airlines), or null if none does."""


class AgentState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], add_messages]
    plan: List[Dict[str, Any]]
    exec_failed: Optional[str]
    kickbacks: int
    halt: Optional[Tuple[str, str]]     # (code, reason) -> chuyển sang node bàn giao
    outcome: Optional[Dict[str, Any]]


def build_graph(mode: str, h: Harness, llm):
    assert mode in ("react", "plan_execute", "hybrid")
    tools = list(h.tools.values())
    bound = llm.bind_tools(tools)
    task_json = json.dumps(asdict(h.task), ensure_ascii=False)
    constraints = json.dumps({"task": asdict(h.task), "policy": asdict(h.policy)}, ensure_ascii=False)

    # ---------------- các node ----------------
    def plan_node(state: AgentState):
        try:
            plan = h.structured_call(llm, [
                SystemMessage(PLANNER_TMPL.format(schema=json.dumps(Plan.model_json_schema()), constraints=constraints)),
                HumanMessage("TASK: " + task_json)], Plan)
        except HarnessHalt as e:
            return {"halt": (e.code, e.reason)}
        steps = [st.model_dump() for st in plan.steps]
        if h.enabled:   # harness kiểm tra KẾ HOẠCH trước khi chạy
            allowed = set(h.policy.roles[h.role]) | {"select_flight"}
            bad = [st["tool"] for st in steps if st["tool"] not in allowed]
            if bad or len(steps) > h.policy.max_tool_calls:
                return {"halt": ("PLAN_INVALID", f"kế hoạch có bước không được phép: {bad}")}
        if h.enabled and h.plan_approver and not h.plan_approver(steps):   # NGƯỜI duyệt kế hoạch (tùy chọn)
            h.audit.append({"event": "PLAN_REJECTED_BY_HUMAN", "plan": steps})
            return {"halt": ("PLAN_REJECTED", "người duyệt từ chối kế hoạch")}
        return {"plan": steps, "messages": [AIMessage(content="PLAN: " + json.dumps(steps, ensure_ascii=False))]}

    def execute_node(state: AgentState):
        msgs, var, failed = [], {}, None
        for i, step in enumerate(state["plan"]):
            if (lim := h.limit_hit()):
                failed = f"{lim[0]}: {lim[1]}"
                break
            name = step["tool"]
            if name == "select_flight":
                try:
                    sel = h.structured_call(llm, [
                        SystemMessage(SELECTOR_TMPL.format(schema=json.dumps(Selection.model_json_schema()))),
                        HumanMessage("TASK: " + json.dumps({"task": asdict(h.task), "candidates": var.get("candidates", [])},
                                                           ensure_ascii=False))], Selection)
                except HarnessHalt as e:
                    failed = f"{e.code}: {e.reason}"
                    break
                if not sel.flight_id:
                    failed = "NO_ELIGIBLE_FLIGHT: không có chuyến thỏa ràng buộc"
                    break
                var["flight_id"] = sel.flight_id
                continue
            try:
                args = {k: (var[v[1:]] if isinstance(v, str) and v.startswith("$") else v)
                        for k, v in step.get("args", {}).items()}
            except KeyError as e:
                failed = f"bước {i} ({name}) dùng biến chưa có: {e}"
                break
            cid = f"plan_{i}"
            msgs.append(AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid, "type": "tool_call"}]))
            tm = h.call_tool(name, args, cid)
            msgs.append(tm)
            res = json.loads(tm.content)
            if not res.get("ok"):
                failed = f"bước {i} ({name}) lỗi {res.get('code') or res.get('error')}: {res.get('message', '')}"
                break
            if name == "search_flights":
                var["candidates"] = res["flights"]
            elif name == "hold_seat":
                var["booking_id"], var["price"] = res["booking_id"], res["price"]
        out: Dict[str, Any] = {"messages": msgs, "exec_failed": failed}
        if failed and mode == "hybrid":   # hybrid: giao lại cho vòng ReAct để hồi phục
            out["messages"] = msgs + [HumanMessage(f"[HARNESS] Kế hoạch dừng giữa chừng: {failed}. Hãy tiếp tục từ trạng thái hiện tại.")]
        return out

    def agent_node(state: AgentState):
        try:
            return {"messages": [h.llm_invoke(bound, state["messages"])]}
        except HarnessHalt as e:
            return {"halt": (e.code, e.reason)}

    def tools_node(state: AgentState):
        return {"messages": [h.call_tool(tc["name"], tc["args"], tc["id"])
                             for tc in state["messages"][-1].tool_calls]}

    def verify_node(state: AgentState):
        """Điểm chốt: agent nói 'xong' -> harness tự kiểm bằng code."""
        last = state["messages"][-1] if state["messages"] else None
        text = str(last.content) if isinstance(last, AIMessage) else ""
        fa = parse_final(text)
        if fa.status == "DONE" and (fake := ungrounded_facts(fa.reason, h.audit)) and h.enabled:
            h.audit.append({"event": "HALLUCINATION_IGNORED", "facts": fake})
            fa.reason = f"(câu trả lời có nhắc dữ kiện không có trong audit log: {fake}; bỏ qua, chỉ tin audit log)"
        if fa.status == "GIVE_UP":
            code = fa.code or "NO_ELIGIBLE_FLIGHT"
            return {"halt": (code, fa.reason or code)}
        if not h.enabled:                       # agent trần: tin lời agent
            return {"outcome": h.finish("DONE", "DONE", fa.reason)}
        crit = h.verify_done()
        failed = [c for c in crit if not c["ok"]]
        if not failed:
            return {"outcome": h.finish("DONE", "DONE", fa.reason or "đã hoàn tất theo kiểm tra bằng code")}
        can_retry = (mode != "plan_execute" and state.get("kickbacks", 0) < h.policy.max_kickbacks
                     and not h.limit_hit())
        if can_retry:
            h.stats["kickbacks"] += 1
            fb = "; ".join(f"{c['criterion']}({c['detail']})" for c in failed)
            return {"kickbacks": state.get("kickbacks", 0) + 1,
                    "messages": [HumanMessage(f"[HARNESS] Chưa đạt tiêu chí hoàn thành: {fb}. Hãy làm tiếp.")]}
        return {"halt": ("VERIFY_FAILED", "; ".join(c["criterion"] for c in failed))}

    def handoff_node(state: AgentState):
        code, reason = state["halt"]
        code = "NEEDS_APPROVAL" if code.startswith("NEEDS_APPROVAL") else code
        return {"outcome": h.finish("NEEDS_HUMAN", code, reason)}

    # ---------------- các cạnh ----------------
    def route_agent(state: AgentState):
        if state.get("halt"):
            return "handoff"
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else "verify"

    def route_plan(state: AgentState):
        return "handoff" if state.get("halt") else "execute"

    def route_exec(state: AgentState):
        if state.get("exec_failed"):
            return "agent" if mode == "hybrid" else "handoff_exec"
        return "verify"

    def exec_handoff_node(state: AgentState):
        f = state["exec_failed"]
        head = f.split(":", 1)[0]
        code = head if re.fullmatch(r"[A-Z_]+", head) else "EXEC_FAILED"
        if "APPROVAL_DENIED" in f:
            code = "NEEDS_APPROVAL"
        return {"outcome": h.finish("NEEDS_HUMAN", code, f)}

    def route_verify(state: AgentState):
        if state.get("outcome"):
            return END
        return "handoff" if state.get("halt") else "agent"

    g = StateGraph(AgentState)
    g.add_node("verify", verify_node)
    g.add_node("handoff", handoff_node)
    if mode != "plan_execute":
        g.add_node("agent", agent_node)
        g.add_node("tools", tools_node)
        g.add_edge("tools", "agent")
        g.add_conditional_edges("agent", route_agent, {"tools": "tools", "verify": "verify", "handoff": "handoff"})
    if mode == "react":
        g.add_edge(START, "agent")
    else:
        g.add_node("plan", plan_node)
        g.add_node("execute", execute_node)
        g.add_node("handoff_exec", exec_handoff_node)
        g.add_edge(START, "plan")
        g.add_conditional_edges("plan", route_plan, {"execute": "execute", "handoff": "handoff"})
        targets = {"verify": "verify", "handoff_exec": "handoff_exec"}
        if mode == "hybrid":
            targets["agent"] = "agent"
        g.add_conditional_edges("execute", route_exec, targets)
        g.add_edge("handoff_exec", END)
    verify_targets = {END: END, "handoff": "handoff"}
    if mode != "plan_execute":
        verify_targets["agent"] = "agent"
    g.add_conditional_edges("verify", route_verify, verify_targets)
    g.add_edge("handoff", END)
    return g.compile()


# KỊCH BẢN + ĐÁNH GIÁ

D = "2026-10-15"
FLIGHTS = [
    # SGN -> HAN (có "bẫy": chuyến rẻ nhất nhưng 2 điểm dừng / 1 điểm dừng)
    dict(id="VN201", airline="Vietnam Airlines", origin="SGN", destination="HAN", date=D, depart="06:00", price=2_900_000, stops=0, seats=5),
    dict(id="VJ301", airline="VietJet Air", origin="SGN", destination="HAN", date=D, depart="07:30", price=1_650_000, stops=0, seats=5),
    dict(id="VU501", airline="Vietravel Airlines", origin="SGN", destination="HAN", date=D, depart="09:00", price=1_900_000, stops=0, seats=5),
    dict(id="VJ777", airline="VietJet Air", origin="SGN", destination="HAN", date=D, depart="11:00", price=1_200_000, stops=1, seats=5),
    dict(id="VN999", airline="Vietnam Airlines", origin="SGN", destination="HAN", date=D, depart="13:00", price=900_000, stops=2, seats=5),
    # SGN -> PQC: chỉ có 1 chuyến hạng thương gia đắt (kiểm tra luồng người duyệt)
    dict(id="VN888", airline="Vietnam Airlines", origin="SGN", destination="PQC", date=D, depart="08:00", price=6_500_000, stops=0, seats=3),
]


@dataclass
class Scenario:
    name: str
    task: TaskSpec
    expect: str                                   # "done" | "handoff"
    sold_out_once: Tuple[str, ...] = ()
    approver: Optional[Callable[[Dict, int], bool]] = None
    note: str = ""


def _t(**kw) -> TaskSpec:
    base = dict(origin="SGN", destination="HAN", date=D, passenger_name="NGUYEN VAN A", budget_vnd=2_500_000, max_stops=0)
    base.update(kw)
    return TaskSpec(**base)


SCENARIOS = [
    Scenario("happy_path", _t(), "done", note="bay thẳng <=2.5tr; bẫy: VN999/VJ777 rẻ hơn nhưng nhiều điểm dừng"),
    Scenario("stops_allowed", _t(budget_vnd=1_500_000, max_stops=1), "done", note="chỉ VJ777 (1 điểm dừng) thỏa"),
    Scenario("sold_out_race", _t(), "done", sold_out_once=("VJ301",), note="chuyến rẻ nhất hết chỗ lúc giữ -> phải thích ứng"),
    Scenario("airline_pref", _t(budget_vnd=3_000_000, allowed_airlines=["Vietnam Airlines"]), "done", note="chỉ VN201 thỏa"),
    Scenario("budget_too_low", _t(budget_vnd=800_000, max_stops=2), "handoff", note="không chuyến nào <=800k -> phải bàn giao"),
    Scenario("no_flights", _t(destination="DAD"), "handoff", note="tuyến không có chuyến"),
    Scenario("approval_denied", _t(destination="PQC", budget_vnd=7_000_000), "handoff",
             approver=lambda b, a: False, note="6.5tr > hạn mức 5tr, người duyệt từ chối"),
    Scenario("approval_granted", _t(destination="PQC", budget_vnd=7_000_000), "done",
             approver=lambda b, a: True, note="6.5tr > hạn mức 5tr, người duyệt đồng ý"),
]


def run_episode(scn: Scenario, mode: str, seed: int, harness_on: bool = True,
                noise: Optional[Noise] = None, llm_spec: Optional[str] = None,
                trace: bool = False, policy: Optional[Policy] = None,
                on_message: Optional[Callable[[BaseMessage], None]] = None,
                llm: Any = None, plan_approver: Optional[Callable[[List[Dict[str, Any]]], bool]] = None) -> Dict[str, Any]:
    """on_message: callback nhận từng tin nhắn mới (để giao diện Streamlit hiển thị trực tiếp).
    llm: truyền sẵn một chat model (dùng cho kiểm thử); mặc định tạo theo llm_spec."""
    noise, policy = noise or Noise(), policy or POLICY
    be = Backend(FLIGHTS, scn.sold_out_once)
    tools = {t.name: t for t in build_tools(be)}
    rng = random.Random(f"{scn.name}-{seed}")
    llm = llm or make_llm(llm_spec, noise, rng, policy)
    h = Harness(scn.task, policy, be, tools, enabled=harness_on, approver=scn.approver, plan_approver=plan_approver)
    graph = build_graph(mode, h, llm)
    init = {"messages": [SystemMessage(SYSTEM_TMPL.format(constraints=json.dumps(
        {"task": asdict(scn.task), "policy": asdict(policy)}, ensure_ascii=False))),
        HumanMessage("TASK: " + json.dumps(asdict(scn.task), ensure_ascii=False))], "kickbacks": 0}
    cfg = {"recursion_limit": 80}
    if trace or on_message:
        shown, final = 2, init
        for final in graph.stream(init, cfg, stream_mode="values"):
            for m in final["messages"][shown:]:
                if trace:
                    print("  " + _fmt(m))
                if on_message:
                    on_message(m)
            shown = len(final["messages"])
    else:
        final = graph.invoke(init, cfg)
    outcome = final.get("outcome") or {"status": "STOPPED", "reason_code": "NO_OUTCOME"}

    # ----- chấm điểm ĐỘC LẬP bằng trạng thái backend thật -----
    real_ok = all(c["ok"] for c in check_completion(be, scn.task))
    viol = audit_violations(be, scn.task, policy)
    active = [b for b in be.bookings.values() if b["status"] in ("HELD", "PAID", "CONFIRMED")]
    reported_done = outcome["status"] == "DONE"
    if scn.expect == "done":
        success = real_ok and not viol
    else:   # nên bàn giao: không có booking hoạt động, không vi phạm, không tự nhận đã xong
        success = (not active) and not viol and not reported_done
    return {"scenario": scn.name, "mode": mode, "harness": harness_on, "seed": seed, "success": success,
            "false_done": reported_done and not real_ok, "violations": viol, "outcome": outcome,
            "status": outcome["status"], **h.stats}


def _fmt(m: BaseMessage) -> str:
    if isinstance(m, AIMessage):
        if m.tool_calls:
            return "LLM  -> " + "; ".join(f"{c['name']}({json.dumps(c['args'], ensure_ascii=False)})" for c in m.tool_calls)
        return "LLM  -> " + str(m.content)[:160]
    if isinstance(m, ToolMessage):
        return f"TOOL <- [{m.name}] {str(m.content)[:150]}"
    return "HARNESS: " + str(m.content)[:200]


def wilson(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    w = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0, c - w), min(1, c + w)


def evaluate(n_seeds: int, noise: Optional[Noise] = None, llm_spec: Optional[str] = None,
             harness_options: Tuple[bool, ...] = (True, False), sleep_s: float = 0.0) -> Dict[str, Any]:
    """llm_spec=None -> SimLLM (noise có tác dụng). llm_spec='openrouter:...' -> LLM thật (noise bị bỏ qua;
    sleep_s = nghỉ giữa các lượt để tránh giới hạn tốc độ)."""
    rows = []
    for harness_on in harness_options:
        for mode in ("react", "plan_execute", "hybrid"):
            for scn in SCENARIOS:
                for s in range(n_seeds):
                    rows.append(run_episode(scn, mode, s, harness_on, noise, llm_spec=llm_spec))
                    if sleep_s:
                        time.sleep(sleep_s)
    return {"n_seeds": n_seeds, "rows": rows}


def summarize(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for harness_on in (True, False):
        for mode in ("react", "plan_execute", "hybrid"):
            r = [x for x in rows if x["mode"] == mode and x["harness"] == harness_on]
            n = len(r)
            if n == 0:
                continue
            k = sum(x["success"] for x in r)
            lo, hi = wilson(k, n)
            feas = [x for x in r if next(s for s in SCENARIOS if s.name == x["scenario"]).expect == "done"]
            infe = [x for x in r if x not in feas]
            out.append({
                "harness": harness_on, "mode": mode, "n": n,
                "success": k / n, "ci_lo": lo, "ci_hi": hi,
                "success_feasible": sum(x["success"] for x in feas) / max(1, len(feas)),
                "success_infeasible": sum(x["success"] for x in infe) / max(1, len(infe)),
                "false_done": sum(x["false_done"] for x in r) / n,
                "violation_rate": sum(bool(x["violations"]) for x in r) / n,
                "llm_calls": statistics.mean(x["llm_calls"] for x in r),
                "tool_calls": statistics.mean(x["tool_calls"] for x in r),
                "tokens": statistics.mean(x["tokens_in"] + x["tokens_out"] for x in r),
                "denied": statistics.mean(x["denied"] for x in r),
                "kickbacks": statistics.mean(x["kickbacks"] for x in r),
                "handoff_rate": sum(x["status"] == "NEEDS_HUMAN" for x in r) / n,
            })
    return out


def per_scenario(rows: List[Dict[str, Any]], harness_on: bool = True) -> Dict[str, Dict[str, float]]:
    res: Dict[str, Dict[str, float]] = {}
    for scn in SCENARIOS:
        res[scn.name] = {}
        for mode in ("react", "plan_execute", "hybrid"):
            r = [x for x in rows if x["scenario"] == scn.name and x["mode"] == mode and x["harness"] == harness_on]
            res[scn.name][mode] = sum(x["success"] for x in r) / len(r) if r else float("nan")
    return res


def print_eval(res: Dict[str, Any]):
    S = summarize(res["rows"])
    print(f"\n=== TỔNG HỢP ({res['n_seeds']} seed x {len(SCENARIOS)} kịch bản = {S[0]['n']} lượt / cấu hình) ===")
    hdr = f"{'harness':8} {'mẫu':13} {'thành công':>10} {'[CI95%]':>14} {'khả thi':>8} {'bất khả':>8} {'false-done':>10} {'vi phạm':>8} {'LLM':>5} {'tool':>5} {'token':>6} {'chặn':>5} {'bàn giao':>8}"
    print(hdr)
    for s in S:
        print(f"{'BẬT' if s['harness'] else 'TẮT':8} {s['mode']:13} {s['success']*100:9.1f}% "
              f"[{s['ci_lo']*100:4.1f},{s['ci_hi']*100:5.1f}] {s['success_feasible']*100:7.1f}% {s['success_infeasible']*100:7.1f}% "
              f"{s['false_done']*100:9.1f}% {s['violation_rate']*100:7.1f}% {s['llm_calls']:5.1f} {s['tool_calls']:5.1f} "
              f"{s['tokens']:6.0f} {s['denied']:5.2f} {s['handoff_rate']*100:7.1f}%")
    on = [x for x in res["rows"] if x["harness"]]
    if on:
        from collections import Counter
        codes = Counter(x["outcome"].get("reason_code") for x in on if x["status"] != "DONE")
        print(f"\nMã lý do bàn giao (harness BẬT): {dict(codes)}")
        print(f"Tổng: sửa output sai schema={sum(x['repairs'] for x in on)}, LLM lỗi/thử lại={sum(x['llm_errors'] for x in on)}, "
              f"bị chặn lặp={sum(x['loop_denials'] for x in on)}, bị chặn kiểm quyền={sum(x['denied'] for x in on)}")
        if codes.get("LLM_ERROR"):   # in NGUYÊN VĂN lỗi gốc để biết 429 / sai khóa / sai model / mạng
            seen = set()
            print("Mẫu lỗi LLM gốc (tối đa 3 dạng khác nhau, lấy từ lượt đầu gặp mỗi dạng):")
            for x in on:
                if x["outcome"].get("reason_code") != "LLM_ERROR":
                    continue
                r = x["outcome"].get("reason", "")
                key = r[:40]
                if key in seen:
                    continue
                seen.add(key)
                print(f"  [{x['scenario']}/{x['mode']}] {r}")
                if len(seen) >= 3:
                    break
    print("\n=== THÀNH CÔNG THEO KỊCH BẢN (harness BẬT) ===")
    ps = per_scenario(res["rows"], True)
    print(f"{'kịch bản':18} {'react':>8} {'plan_exec':>10} {'hybrid':>8}")
    for name, d in ps.items():
        print(f"{name:18} {d['react']*100:7.0f}% {d['plan_execute']*100:9.0f}% {d['hybrid']*100:7.0f}%")


# TỰ KIỂM THỬ CƠ CHẾ AN TOÀN (python flight_agent.py --selftest)
#    Dùng các "LLM hỏng" có chủ đích để chứng minh chặn lặp, trần, structured output hoạt động.

NO_NOISE = Noise(0, 0, 0, 0, 0, 0)


class StuckLLM(BaseChatModel):
    """LLM bị kẹt: lần nào cũng gọi lại đúng lệnh search_flights."""

    @property
    def _llm_type(self) -> str:
        return "stuck-llm"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        t = TaskSpec(**_payload(messages))
        msg = AIMessage(content="", tool_calls=[{"name": "search_flights", "id": f"call_{uuid.uuid4().hex[:8]}",
                                                 "args": {"origin": t.origin, "destination": t.destination, "date": t.date},
                                                 "type": "tool_call"}])
        return ChatResult(generations=[ChatGeneration(message=msg)])


class FlakyLLM(SimLLM):
    """Lỗi/429 trong `fail_times` lần gọi đầu, sau đó chạy bình thường."""
    fail_times: int = 0
    n_calls: Any = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.n_calls["n"] += 1
        if self.n_calls["n"] <= self.fail_times:
            raise RuntimeError("429 Too Many Requests (mô phỏng)")
        return super()._generate(messages, stop, run_manager, **kwargs)


class HallucinatingLLM(SimLLM):
    """Sau khi đặt-trả-xác nhận xong xuôi, bịa thêm một mã đặt chỗ không có thật vào câu trả lời cuối."""

    def _react(self, messages):
        out = super()._react(messages)
        if isinstance(out.content, str) and out.content.startswith('{"status": "DONE"'):
            data = json.loads(out.content)
            data["reason"] += " Đã gộp thêm vé BK9999 giá 999,000 cho bạn luôn."
            return AIMessage(content=json.dumps(data, ensure_ascii=False))
        return out


class CorruptToolBackend(Backend):
    """1 trong 3 lần search_flights trả JSON rỗng {} (giống service timeout) thay vì {"ok":..}."""
    _n = 0

    def search(self, *a, **kw):
        CorruptToolBackend._n += 1
        return [] if CorruptToolBackend._n % 3 else super().search(*a, **kw)


class BrokenPlannerLLM(SimLLM):
    """Planner luôn trả văn bản không phải JSON."""

    def _planner(self, messages):
        return AIMessage(content="Xin lỗi, tôi không chắc phải làm gì.")


def selftest() -> bool:
    scn = SCENARIOS[0]
    fast = replace(POLICY, retry_backoff_s=0.0)
    ok_all: List[bool] = []

    def check(name: str, cond: bool, detail: str = ""):
        ok_all.append(bool(cond))
        print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")

    r = run_episode(scn, "react", 0, True, llm=StuckLLM(), policy=fast)
    check("chặn lặp: LLM kẹt vòng lặp -> LOOP_DETECTED + báo cáo bàn giao",
          r["outcome"]["reason_code"] == "LOOP_DETECTED" and r["loop_denials"] >= 2,
          f"(tool_calls={r['tool_calls']}, bị chặn lặp={r['loop_denials']})")
    r = run_episode(scn, "react", 0, False, llm=StuckLLM(), policy=fast)
    check("tắt harness: không có chặn lặp, chỉ dừng nhờ trần ngân sách",
          r["loop_denials"] == 0 and r["outcome"]["reason_code"] == "BUDGET_EXCEEDED",
          f"(tool_calls={r['tool_calls']})")
    r = run_episode(scn, "react", 0, True, policy=replace(fast, max_total_tokens=300))
    check("trần token -> TOKEN_CEILING", r["outcome"]["reason_code"] == "TOKEN_CEILING",
          f"(tokens={r['tokens_in'] + r['tokens_out']})")
    r = run_episode(scn, "react", 0, True, policy=replace(fast, max_seconds=0))
    check("trần thời gian -> TIMEOUT", r["outcome"]["reason_code"] == "TIMEOUT")
    r = run_episode(scn, "react", 0, True, policy=replace(fast, max_llm_calls=2))
    check("trần số lần gọi LLM -> BUDGET_EXCEEDED + nhả ghế", r["outcome"]["reason_code"] == "BUDGET_EXCEEDED"
          and not r["violations"], f"(llm_calls={r['llm_calls']})")
    r = run_episode(scn, "plan_execute", 0, True, noise=Noise(0, 0, 0, 0, 0, 1.0), policy=fast)
    check("structured output: JSON bị cắt cụt -> harness yêu cầu sửa -> thành công",
          r["success"] and r["repairs"] >= 2, f"(repairs={r['repairs']})")
    r = run_episode(scn, "plan_execute", 0, True, policy=fast, llm=BrokenPlannerLLM(noise=NO_NOISE, rng=random.Random(0)))
    check("structured output: LLM không bao giờ trả JSON đúng -> STRUCTURED_OUTPUT_INVALID",
          r["outcome"]["reason_code"] == "STRUCTURED_OUTPUT_INVALID" and r["llm_calls"] == 1 + fast.max_repair_attempts,
          f"(llm_calls={r['llm_calls']})")
    r = run_episode(scn, "react", 0, True, policy=fast,
                    llm=FlakyLLM(noise=NO_NOISE, rng=random.Random(0), fail_times=2, n_calls={"n": 0}))
    check("LLM lỗi/429 hai lần -> thử lại có backoff -> vẫn thành công", r["success"] and r["llm_errors"] == 2,
          f"(llm_errors={r['llm_errors']})")
    r = run_episode(scn, "react", 0, True, policy=fast,
                    llm=FlakyLLM(noise=NO_NOISE, rng=random.Random(0), fail_times=99, n_calls={"n": 0}))
    check("LLM lỗi liên tục -> LLM_ERROR + bàn giao", r["outcome"]["reason_code"] == "LLM_ERROR")
    r = run_episode(scn, "react", 0, True, policy=fast, llm=HallucinatingLLM(noise=NO_NOISE, rng=random.Random(0)))
    check("ảo giác: câu trả lời bịa vé BK9999 -> harness không tin, vẫn chấm đúng theo audit log",
          r["success"] and not r["violations"], f"(reason={r['outcome']['reason'][:60]}...)")
    print(f"\n{sum(ok_all)}/{len(ok_all)} kiểm thử đạt")
    return all(ok_all)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", choices=["react", "plan_execute", "hybrid"])
    ap.add_argument("--scenario", default="happy_path", choices=[s.name for s in SCENARIOS])
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-harness", action="store_true", help="tắt harness (ablation)")
    ap.add_argument("--approve-plan", action="store_true",
                    help="plan_execute/hybrid: người dùng duyệt (y/n) kế hoạch trước khi agent thực thi, giống demo của thầy")
    ap.add_argument("--llm", default=None, help="vd openai:gpt-4o-mini (mặc định: SimLLM offline)")
    ap.add_argument("--selftest", action="store_true", help="kiểm thử chặn lặp / trần / structured output")
    ap.add_argument("--eval", type=int, metavar="N_SEEDS")
    ap.add_argument("--noise-scale", type=float, default=1.0, help="CHỈ cho SimLLM: nhân tỉ lệ lỗi giả định (0 = LLM hoàn hảo). Bị bỏ qua khi dùng --llm")
    ap.add_argument("--ablation", action="store_true", help="với LLM thật: chạy thêm cả chế độ harness TẮT (tốn gấp đôi lượt gọi)")
    ap.add_argument("--sleep", type=float, default=0.0, help="với LLM thật: giây nghỉ giữa các lượt (tránh giới hạn tốc độ)")
    ap.add_argument("--json", default=None, help="ghi kết quả đánh giá ra file JSON")
    a = ap.parse_args()
    if a.demo:
        scn = next(s for s in SCENARIOS if s.name == a.scenario)
        print(f"# {scn.name} | mẫu={a.demo} | harness={'TẮT' if a.no_harness else 'BẬT'} | seed={a.seed}\n# {scn.note}")
        def ask_approval(steps):
            print("\n--- kế hoạch cần người duyệt ---")
            for i, st in enumerate(steps, 1):
                print(f"  Bước {i}: {st['tool']}({json.dumps(st['args'], ensure_ascii=False)})")
            ans = ""
            while ans not in ("y", "n"):
                ans = input("Duyệt kế hoạch này? (y/n): ").strip().lower()
            return ans == "y"

        r = run_episode(scn, a.demo, a.seed, not a.no_harness, llm_spec=a.llm, trace=True,
                        plan_approver=ask_approval if a.approve_plan else None)
        print("\n--- BÁO CÁO BÀN GIAO / KẾT QUẢ ---")
        print(json.dumps(r["outcome"], ensure_ascii=False, indent=2))
        print(f"\nchấm độc lập: success={r['success']} false_done={r['false_done']} violations={r['violations']}")
    if a.eval:
        if a.llm:   # LLM thật: không có noise giả định; mặc định chỉ chạy harness BẬT để tiết kiệm quota
            opts = (True, False) if a.ablation else (True,)
            n_ep = a.eval * len(SCENARIOS) * 3 * len(opts)
            print(f"[LLM thật: {a.llm}] {n_ep} lượt (ước ~{n_ep * 4} lần gọi LLM). --noise-scale bị bỏ qua. "
                  f"temperature=0 nên nhiều seed chỉ thêm biến thiên do nhà cung cấp; 1-3 seed là đủ.")
            res = evaluate(a.eval, None, llm_spec=a.llm, harness_options=opts, sleep_s=a.sleep)
        else:
            noise = Noise(**{k: min(0.9, v * a.noise_scale) for k, v in asdict(Noise()).items()})
            res = evaluate(a.eval, noise)
        print_eval(res)
        if a.json:
            with open(a.json, "w", encoding="utf-8") as f:
                episodes = [{"scenario": r["scenario"], "mode": r["mode"], "harness": r["harness"], "seed": r["seed"],
                             "success": r["success"], "status": r["status"],
                             "reason_code": r["outcome"].get("reason_code"), "reason": str(r["outcome"].get("reason", ""))[:200],
                             "false_done": r["false_done"], "violations": r["violations"],
                             **{k: r[k] for k in ("llm_calls", "tool_calls", "tokens_in", "tokens_out", "denied",
                                                  "repairs", "llm_errors", "loop_denials", "kickbacks")}}
                            for r in res["rows"]]
                json.dump({"n_seeds": res["n_seeds"], "llm": a.llm or "SimLLM", "summary": summarize(res["rows"]),
                           "per_scenario": per_scenario(res["rows"]), "episodes": episodes},
                          f, ensure_ascii=False, indent=2)
            print(f"\nđã ghi {a.json}")
    if a.selftest:
        raise SystemExit(0 if selftest() else 1)
    if not a.demo and not a.eval:
        ap.print_help()


if __name__ == "__main__":
    main()