import re
from dataclasses import dataclass
from datetime import date
from html import escape


@dataclass(frozen=True)
class Template:
    slug: str
    title: str
    fields: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]

    @property
    def path(self):
        return "/sample/" + self.slug


TEMPLATES = {
    "inventory": Template("inventory", "Inventory snapshot", ("sku", "product", "stock", "updated"), (
        ("BX-104", "Dispenser brackets", "18", "2026-10-14"),
        ("HX-068", "Intake filter pack", "0", "2026-10-15"),
        ("JL-400", "Retaining clips", "37", "2026-10-16"),
    )),
    "orders": Template("orders", "Purchase order status", ("reference", "supplier", "status", "updated"), (
        ("PO-410", "Leaf Works", "processing", "2026-10-14"),
        ("PO-411", "River Studio", "delivered", "2026-10-15"),
        ("PO-412", "Meridian Supply", "approved", "2026-10-16"),
    )),
}


def plan(instruction: str):
    if re.search(r"https?://|ftp://|file://", instruction, flags=re.IGNORECASE):
        raise ValueError("The job selects a sample template and does not accept destinations.")
    labels = {
        "inventory": r"\b(?:inventory|stock|sku)\b",
        "orders": r"\b(?:order|orders)\b",
    }
    matches = [slug for slug, pattern in labels.items() if re.search(pattern, instruction, flags=re.IGNORECASE)]
    if len(matches) != 1:
        raise ValueError("Choose either an inventory report or a purchase order status report.")
    return TEMPLATES[matches[0]]


def validate_rows(template: Template, rows: list[list[str]], search: str):
    output = []
    keys = set()
    for cells in rows:
        if len(cells) != len(template.fields) or any(not value or len(value) > 120 for value in cells):
            raise ValueError("invalid_cells")
        row = dict(zip(template.fields, cells, strict=True))
        key = cells[0]
        if not re.fullmatch(r"[A-Z]{2}-\d{3}", key) or key in keys:
            raise ValueError("invalid_or_duplicate_identifier")
        keys.add(key)
        date.fromisoformat(row["updated"])
        if template.slug == "inventory":
            if not re.fullmatch(r"\d{1,7}", row["stock"]):
                raise ValueError("invalid_stock")
            row["stock"] = int(row["stock"])
        elif row["status"] not in {"approved", "processing", "delivered"}:
            raise ValueError("invalid_order_status")
        if not search or search.casefold() in " ".join(map(str, row.values())).casefold():
            output.append(row)
    return output


def sample_page(template: Template):
    headers = "".join(f"<th>{escape(field.title())}</th>" for field in template.fields)
    rows = "".join("<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>" for row in template.rows)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{escape(template.title)}</title>
<style>body{{font:18px system-ui;margin:48px;background:#edf5ff;color:#153052}}
main{{max-width:860px;margin:auto}}table{{border-collapse:collapse;width:100%;background:white}}
th,td{{padding:15px;border:1px solid #becfe5;text-align:left}}th{{background:#d9e8fa}}</style></head>
<body><main><h1>{escape(template.title)}</h1><p>Synthetic supply operations portal</p>
<table><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table></main></body></html>"""
