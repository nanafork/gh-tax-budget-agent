# ---------- IMPORTING THE NECESSARY LIBRARIES ----------

import os
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple

from playwright.sync_api import sync_playwright
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import Table, TableStyle, SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet

# ---------- INITIAL CONFIGURATION ----------

TAX_CALC_URL = "https://kessir.github.io/taxcalculatorgh/"
OUTPUT_DIR = "outputs"

# EXACT scenarios required by the PDF spec
SCENARIOS = [
    {"name": "case1", "salary": 4000,  "allowances": 0,    "relief": 0},
    {"name": "case2", "salary": 8000,  "allowances": 1000, "relief": 200},
    {"name": "case3", "salary": 15000, "allowances": 2500, "relief": 500},
]

# Rule-based default allocation (sum ≤ 1.0)
RB_WEIGHTS = {
    "Housing": 0.30,
    "Food": 0.20,
    "Transport": 0.10,
    "Utilities": 0.10,
    "Healthcare": 0.10,
    "Education/Skills": 0.05,
    "Savings/Emergency": 0.10,
    "Discretionary": 0.05,
}

OPENAI_MODEL_DEFAULT = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# ---------- UTILITIES ----------

@dataclass
class BudgetItem:
    category: str
    amount: float
    pct: float  

def ghc(value: float) -> str:
    return f"GHS {value:,.2f}"

def _coerce_num(x) -> float:
    """
    Robust numeric coercion for strings like '', '1,234.56', 'GH₵ 2,000'.
    Returns 0.0 if no number is found.
    """
    s = str(x).replace(",", "").replace("GHS", "").replace("GH₵", "").replace("GH¢", "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except Exception:
        m = re.search(r"(\d+(?:\.\d{1,2})?)", s)
        return float(m.group(1)) if m else 0.0

def _dump_debug(page, scenario_name: str):
    
    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        txt = page.locator("body").inner_text(timeout=4000)
        html = page.content()
        with open(os.path.join(OUTPUT_DIR, f"debug_{scenario_name}.txt"), "w", encoding="utf-8") as f:
            f.write("=== INNER TEXT ===\n")
            f.write(txt)
            f.write("\n\n=== HTML ===\n")
            f.write(html)
    except Exception:
        pass

# ---------- WEB AUTOMATION ----------

def _type_and_fire(page, sel: str, value: float) -> bool:
    loc = page.locator(sel).first
    if loc.count() == 0:
        return False
    loc.wait_for(state="visible", timeout=4000)
    loc.fill("")            
    loc.type(str(value))    
    try:
        page.dispatch_event(sel, "input")
        page.dispatch_event(sel, "change")
        page.dispatch_event(sel, "blur")
    except Exception:
        pass
    page.keyboard.press("Enter")
    page.wait_for_timeout(150)
    return True

def _set_field(page, kind: str, value: float):
    # Keep selectors resilient per PDF tip: target labels/placeholders/testids
    if kind == "salary":
        candidates = [
            'input[placeholder*="Monthly basic income" i]',
            'input[aria-label*="Monthly basic income" i]',
            'input[name="basicIncome"]',
            '[data-testid="basic-income"]',
            'label:has-text("Monthly basic income") >> .. >> input',
            'label:has-text("Monthly Basic Income") >> .. >> input',
        ]
    elif kind == "allowances":
        candidates = [
            'input[placeholder*="Monthly allowances" i]',
            'input[aria-label*="Monthly allowances" i]',
            'input[name="allowances"]',
            '[data-testid="monthly-allowances"]',
            'label:has-text("Monthly allowances") >> .. >> input',
            'label:has-text("Monthly Allowances") >> .. >> input',
        ]
    else:  # relief
        candidates = [
            'input[placeholder*="Tax relief" i]',
            'input[aria-label*="Tax relief" i]',
            'input[name="taxRelief"]',
            '[data-testid="tax-relief"]',
            'label:has-text("Tax relief") >> .. >> input',
            'label:has-text("Tax Relief") >> .. >> input',
        ]
    last_err = None
    for sel in candidates:
        try:
            if _type_and_fire(page, sel, value):
                return
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Could not set field '{kind}'. Last error: {last_err}")

def _extract_amount_from_text(text: str) -> float:
    # Try multiple label variants + currency variants
    patterns = [
        r"Net\s*Income\s*\(take\s*home\)[^\d]*(?:GH\s*[SCc]|GH\s*[₵¢])?\s*([\d,]+(?:\.\d{1,2})?)",
        r"Net\s*Income[^\d]*(?:GH\s*[SCc]|GH\s*[₵¢])?\s*([\d,]+(?:\.\d{1,2})?)",
        r"Net\s*Salary[^\d]*(?:GH\s*[SCc]|GH\s*[₵¢])?\s*([\d,]+(?:\.\d{1,2})?)",
        r"Take\s*home[^\d]*(?:GH\s*[SCc]|GH\s*[₵¢])?\s*([\d,]+(?:\.\d{1,2})?)",
        r"(?:GH\s*[SCc]|GH\s*[₵¢])?\s*([\d,]+(?:\.\d{1,2})?)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m and m.group(1).strip():
            return _coerce_num(m.group(1))
    return 0.0

def _scrape_net_income(page) -> float:
    """
    Aggressive extractor with retries:
    - Prefer DOM containers near 'Net Income (take home)'
    - Fallback to whole-page text
    """
    # Strategy A: DOM scan near likely labels
    labels = ["Net Income (take home)", "Net Income", "Net Salary", "Take home"]
    for lab in labels:
        try:
            section = page.get_by_text(re.compile(re.escape(lab), re.I)).first
            container = section.locator("xpath=ancestor-or-self::*[1]")
            text = container.inner_text(timeout=1500)
            amt = _extract_amount_from_text(text)
            if amt > 0:
                return amt
        except Exception:
            continue

    # Strategy B: whole page text
    try:
        text = page.locator("body").inner_text(timeout=4000)
    except Exception:
        text = page.content()
    amt = _extract_amount_from_text(text)
    return amt

def fill_tax_form_and_get_net_income(page, salary: float, allowances: float, relief: float, scenario_name: str) -> float:
    # Assumes page is already on TAX_CALC_URL; navigation only once in run()
    # Fill
    _set_field(page, "salary", salary)
    _set_field(page, "allowances", allowances)
    _set_field(page, "relief", relief)

    # Click Calculate/Recalculate if present
    try:
        page.get_by_role("button", name=re.compile("calculate", re.I)).first.click(timeout=1500)
    except Exception:
        pass

    # Wait until label & money-like number appear; then scrape
    try:
        page.wait_for_load_state("networkidle")
        page.wait_for_function(
            """() => {
                const t = (document.body.innerText || "").replace(/\\s+/g, " ");
                const hasLabel = /Net\\s*Income|Net\\s*Salary|Take\\s*home/i.test(t);
                const hasMoneyLike = /(GH\\s*[SCc]|GH\\s*[₵¢])?\\s*\\d{1,3}(?:,\\d{3})*(?:\\.\\d{1,2})?/.test(t);
                return hasLabel && hasMoneyLike;
            }""",
            timeout=20000
        )
    except Exception:
        # continue; we'll try scraping anyway
        pass

    # Retry scrape a few times to let UI settle
    for _ in range(4):
        amount = _scrape_net_income(page)
        if amount > 0:
            return amount
        page.wait_for_timeout(350)

    # If still not found, dump and raise
    _dump_debug(page, f"parsefail_{scenario_name}_{int(time.time())}")
    raise RuntimeError("Could not parse a numeric Net Income from the page.")

# ---------- BUDGET GENERATION ----------

def generate_budget_with_llm(net_income: float) -> Tuple[Dict[str, float], str]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("No OPENAI_API_KEY in environment.")

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        prompt = f"""
Create a Ghana-appropriate monthly budget for a net income of GHS {net_income:,.2f}.
Use only these categories: Housing, Food, Transport, Utilities, Healthcare, Education/Skills, Savings/Emergency, Discretionary.
Return a STRICT JSON object with two keys:
- "items": a map of category -> amount (GHS), amounts must be non-negative and sum to <= {net_income:.2f}
- "note": one concise sentence with a Ghana context budgeting tip.
Do not any extra text.
""".strip()

        resp = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", OPENAI_MODEL_DEFAULT),
            messages=[
                {"role": "system", "content": "You are a helpful financial planning assistant."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )
        content = resp.choices[0].message.content.strip()

        # Tolerate fenced JSON
        c = content.strip()
        if c.startswith("```"):
            c = c.strip("`")
            jm = re.search(r"\{.*\}", c, re.S)
            if jm:
                c = jm.group(0)

        import json
        try:
            data = json.loads(c)
        except Exception:
            jm = re.search(r"\{.*\}", c, re.S)
            if not jm:
                raise RuntimeError("LLM did not return valid JSON.")
            data = json.loads(jm.group(0))

        items = data.get("items", {})
        note = data.get("note", "").strip()

        # Sanitize: coerce numbers and scale if needed
        total = sum(max(0.0, _coerce_num(v)) for v in items.values())
        if total > net_income and total > 0:
            factor = net_income / total
        else:
            factor = 1.0
        items = {k: round(max(0.0, _coerce_num(v)) * factor, 2) for k, v in items.items()}

        # Ensure required categories exist
        for ckey in ["Housing", "Food", "Transport", "Utilities", "Healthcare", "Education/Skills", "Savings/Emergency", "Discretionary"]:
            items.setdefault(ckey, 0.0)

        return items, (note or "LLM-generated budget.")
    except Exception as e:
        raise RuntimeError(f"LLM call failed: {e}")

def generate_budget_rule_based(net_income: float) -> Tuple[Dict[str, float], str]:
    items = {cat: round(net_income * w, 2) for cat, w in RB_WEIGHTS.items()}
    total = sum(items.values())
    # If rounding pushed us over, trim Discretionary
    if total > net_income and "Discretionary" in items:
        excess = round(total - net_income, 2)
        items["Discretionary"] = max(0.0, round(items["Discretionary"] - excess, 2))
    note = "Rule-based allocation (fallback)."
    return items, note

def produce_budget(net_income: float) -> Tuple[List[BudgetItem], str]:
    # Try LLM first; fallback to rule-based
    try:
        items_map, note = generate_budget_with_llm(net_income)
    except Exception:
        items_map, note = generate_budget_rule_based(net_income)

    items: List[BudgetItem] = []
    for cat, amt in items_map.items():
        pct = (amt / net_income) if net_income > 0 else 0.0
        items.append(BudgetItem(cat, float(amt), pct))
    items.sort(key=lambda x: x.amount, reverse=True)
    return items, note

# ---------- PDF GENERATION PROCESS ----------

def save_budget_pdf(filename: str, scenario: Dict, net_income: float, items: List[BudgetItem], note: str):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, filename)

    data = [["Category", "Amount (GHS)", "% of Net Income"]]
    for bi in items:
        data.append([bi.category, f"{bi.amount:,.2f}", f"{bi.pct*100:.1f}%"])

    total_amt = sum(b.amount for b in items)
    total_pct = sum(b.pct for b in items) * 100
    data.append(["Total", f"{total_amt:,.2f}", f"{total_pct:.1f}%"])

    doc = SimpleDocTemplate(path, pagesize=A4, rightMargin=24, leftMargin=24, topMargin=24, bottomMargin=24)
    styles = getSampleStyleSheet()
    story = []

    title = Paragraph(f"<b>Monthly Budget Report — {scenario['name'].capitalize()}</b>", styles["Title"])
    story.append(title)
    story.append(Spacer(1, 8))

    meta = Paragraph(
        f"Inputs: Salary = <b>{ghc(scenario['salary'])}</b>, "
        f"Allowances = <b>{ghc(scenario['allowances'])}</b>, "
        f"Tax relief = <b>{ghc(scenario['relief'])}</b><br/>"
        f"Net Income (take home): <b>{ghc(net_income)}</b>",
        styles["BodyText"]
    )
    story.append(meta)
    story.append(Spacer(1, 10))

    table = Table(data, colWidths=[None, 90, 120])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f0f0f0")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.black),
        ("ALIGN", (1,1), (-1,-2), "RIGHT"),
        ("ALIGN", (2,1), (-1,-2), "RIGHT"),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
        ("BACKGROUND", (0,-1), (-1,-1), colors.HexColor("#f7f7f7")),
        ("FONTNAME", (0,-1), (-1,-1), "Helvetica-Bold"),
    ]))
    story.append(table)
    story.append(Spacer(1, 10))

    note_para = Paragraph(f"<i>Notes:</i> {note}", styles["BodyText"])
    story.append(note_para)

    doc.build(story)
    return path

# ---------- ORCHESTRATION ----------
def run():
    headless = os.getenv("HEADFUL") != "1"
    results: List[Tuple[Dict, float]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()

        # Navigate once (optimization)
        page.goto(TAX_CALC_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(600)

        # Dismiss possible cookie banners (best-effort)
        for text in ["Accept", "I Agree", "Got it", "OK"]:
            try:
                page.get_by_role("button", name=re.compile(text, re.I)).first.click(timeout=1000)
                page.wait_for_timeout(150)
            except Exception:
                pass

        for sc in SCENARIOS:
            try:
                net_income = fill_tax_form_and_get_net_income(
                    page,
                    salary=sc["salary"],
                    allowances=sc["allowances"],
                    relief=sc["relief"],
                    scenario_name=sc["name"],
                )
                results.append((sc, net_income))
                print(f"[OK] {sc['name']}: Net Income = {ghc(net_income)}")
            except Exception as e:
                print(f"[ERROR] {sc['name']}: {e}")
                _dump_debug(page, f"error_{sc['name']}_{int(time.time())}")
                results.append((sc, 0.0))

        context.close()
        browser.close()

    # PDFs
    for sc, net in results:
        items, note = produce_budget(net)
        fname = f"budget_{sc['name']}.pdf"
        out = save_budget_pdf(fname, sc, net, items, note)
        print(f"[PDF] Wrote {out}")

if __name__ == "__main__":
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    run()
