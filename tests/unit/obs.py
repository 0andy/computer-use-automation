"""Hand-built Observations for pure unit tests (no browser)."""

from __future__ import annotations

from cua.models import BBox, Control, Observation

MEMBER_ID = "12345"
BALANCE = "$1,234.56"
BASE = "http://localhost:8000"


def control(ref: str, role: str, frame_path: str = "top/main", **kw) -> Control:
    kw.setdefault("bbox", BBox(x=0, y=0, width=10, height=10))
    return Control(ref=ref, role=role, frame_path=frame_path, **kw)


def members_page() -> Observation:
    """MockBank shell + Members frame, as the surface would observe it."""
    return Observation(
        url=f"{BASE}/",
        frames={"top": f"{BASE}/", "top/main": f"{BASE}/members"},
        controls=[
            control("1:0:0", "cell", "top", name="MockBank Online", text="MockBank Online"),
            control("1:0:1", "link", "top", name="Members", text="Members", href=f"{BASE}/members"),
            control("1:1:0", "cell", name="Members", text="Members", table_index=0, row_index=0, col_index=0),
            control("1:1:1", "cell", name="Member ID", text="Member ID", table_index=1, row_index=0, col_index=0),
            control(
                "1:1:2",
                "textbox",
                label="Member ID",
                input_value="",
                attrs={"name": "member_id", "type": "text"},
                ancestor_roles=["table", "row", "cell", "form", "table", "row", "cell"],
                table_index=1,
                row_index=0,
                col_index=1,
            ),
            control("1:1:3", "button", name="Search", attrs={"type": "submit"}, table_index=1, row_index=1, col_index=1),
            control("1:1:4", "paragraph", name="Enter a Member ID and press Search.", text="Enter a Member ID and press Search."),
        ],
    )


def member_detail_page(member_id: str = MEMBER_ID, balance: str = BALANCE) -> Observation:
    def cell(ref: str, text: str, row: int, col: int) -> Control:
        return control(ref, "cell", name=text, text=text, table_index=1, row_index=row, col_index=col)

    return Observation(
        url=f"{BASE}/",
        frames={"top": f"{BASE}/", "top/main": f"{BASE}/member"},
        controls=[
            control("2:0:0", "link", "top", name="Members", text="Members", href=f"{BASE}/members"),
            control("2:1:0", "cell", name="Member Detail", text="Member Detail", table_index=0, row_index=0, col_index=0),
            cell("2:1:1", "Member ID", 0, 0),
            cell("2:1:2", member_id, 0, 1),
            cell("2:1:3", "Name", 1, 0),
            cell("2:1:4", "Alex Sample", 1, 1),
            cell("2:1:5", "Status", 2, 0),
            cell("2:1:6", "Active", 2, 1),
            cell("2:1:7", "Savings", 3, 0),
            cell("2:1:8", balance, 3, 1),
            control("2:1:9", "button", name="Close Account", text="Close Account", attrs={"type": "button"}),
            control("2:1:10", "link", name="New search", text="New search", href=f"{BASE}/members"),
        ],
    )
