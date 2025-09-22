# Ghana Tax Calculator → Budget PDFs

This project is a small automation agent that connects Ghana’s online tax calculator with personalized budget reports. This project tests my understanding of web automation, data extraction, LLM integration, and PDF generation.

## Overview

The agent does the following:

1. Opens the [Ghana Tax Calculator](https://kessir.github.io/taxcalculatorgh/) in a headless browser.
2. Fills in exactly three required scenarios:


   Case 1: Salary = GHS 4,000, Allowances = GHS 0, Tax relief = GHS 0
   Case 2: Salary = GHS 8,000, Allowances = GHS 1,000, Tax relief = GHS 200
   Case 3: Salary = GHS 15,000, Allowances = GHS 2,500, Tax relief = GHS 500

3. Scrapes the computed **Net Income (take-home)** from the site.
4. Sends the net income to an **LLM** to generate a Ghana-appropriate monthly budget.

   * Note that If no API key is set, a **rule-based fallback** allocation is used.

5. Generates a **single-page PDF report per scenario**, containing:

   * Input values (salary, allowances, relief)
   * Net income scraped from the calculator
   * Budget table (category, amount, % of income)
   * A short contextual note from the LLM (or fallback)

Outputs are saved in the `outputs/` directory.

---

## Setup Process

1. Clone the repository or extract the files.

   ```bash
   git clone <repo_url>
   cd <repo_name>
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. (Optional) Set environment variables:

   * `OPENAI_API_KEY` → API key for LLM-based budget generation
   * `OPENAI_MODEL` → model name (defaults to `gpt-4o-mini`)
   * `HEADFUL=1` → run browser in non-headless mode for debugging

---

## Running

Run the agent with:

```bash
python agent.py
```

The script will:

* Open the tax calculator site
* Process all three scenarios
* Produce three PDFs in the `outputs/` folder:

  * `budget_case1.pdf`
  * `budget_case2.pdf`
  * `budget_case3.pdf`

Console logs will also show the extracted net incomes and confirm each PDF creation.

---

## Deliverables

* **agent.py** → main script
* **requirements.txt** → dependencies
* **README.md** → this file
* **outputs/** → generated PDFs (case1, case2, case3)

---

##  Notes

* Browser automation is done with **Playwright**.
* PDFs are generated using **reportlab**.
* The agent is resilient to missing API keys and will always generate a working budget.
* Debug files (`debug_*.txt`) are written to `outputs/` if parsing fails, making it easier to troubleshoot selectors.

---

