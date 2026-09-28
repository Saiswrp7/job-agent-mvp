"""Four made-up resumes in shapes that break parsers. Fictional people,
example.com emails, +91-90000 phone numbers.

    python3 evals/make_synthetic.py      # writes evals/fixtures/resumes/synthetic_*.pdf

- synthetic_fresher.pdf      Indian placement style: education first, CGPA,
                             internships, projects, positions of responsibility
- synthetic_two_column.pdf   sidebar with Skills + Education (reproduces bug 3)
- synthetic_senior.pdf       10 years, two pages, four employers
- synthetic_sales.pdf        non-tech: FMCG area sales manager
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (BaseDocTemplate, Frame, FrameBreak,
                                PageTemplate, Paragraph, SimpleDocTemplate,
                                Spacer)

OUT = Path(__file__).resolve().parent / "fixtures" / "resumes"

# Georgia, not the built-in Helvetica: Helvetica has no rupee sign, and most of
# these resumes quote money in rupees, as Indian resumes do.
from reportlab.lib.fonts import addMapping
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

_F = "/System/Library/Fonts/Supplemental/"
pdfmetrics.registerFont(TTFont("Body", _F + "Georgia.ttf"))
pdfmetrics.registerFont(TTFont("Body-Bold", _F + "Georgia Bold.ttf"))
addMapping("Body", 0, 0, "Body")
addMapping("Body", 1, 0, "Body-Bold")
addMapping("Body", 0, 1, "Body")
addMapping("Body", 1, 1, "Body-Bold")

NAME = ParagraphStyle("name", fontName="Body-Bold", fontSize=17, leading=21)
CONTACT = ParagraphStyle("contact", fontName="Body", fontSize=9, leading=12)
H = ParagraphStyle("h", fontName="Body-Bold", fontSize=10.5, leading=14,
                   spaceBefore=7, spaceAfter=2, textColor=HexColor("#1F3A5F"))
ROLE = ParagraphStyle("role", fontName="Body-Bold", fontSize=9.5, leading=12,
                      spaceBefore=3)
BODY = ParagraphStyle("body", fontName="Body", fontSize=9, leading=11.5)
BODY_L = ParagraphStyle("bodyl", fontName="Body", fontSize=10.5, leading=14)
BUL = ParagraphStyle("bul", parent=BODY, leftIndent=10, bulletIndent=2)
SIDE_H = ParagraphStyle("sh", parent=H, textColor=HexColor("#FFFFFF"))
SIDE = ParagraphStyle("side", parent=BODY, textColor=HexColor("#FFFFFF"))


BUL_L = ParagraphStyle("bull", parent=BUL, fontSize=10.5, leading=14.5)
_big = False


def bullets(items):
    return [Paragraph(t, BUL_L if _big else BUL, bulletText="•") for t in items]


def role(company, title, dates, items):
    return [Paragraph(f"{title}, {company} &nbsp;&nbsp;|&nbsp;&nbsp; {dates}", ROLE)] \
        + bullets(items)


def fresher():
    s = [Paragraph("ANANYA RAO", NAME),
         Paragraph("B.Tech, Electronics and Communication | Manipal Institute of "
                   "Technology | ananya.rao@example.com | +91-90000-00001 | Manipal, "
                   "Karnataka", CONTACT),
         Paragraph("EDUCATION", H),
         Paragraph("<b>Manipal Institute of Technology</b>, B.Tech ECE, CGPA 8.42/10 "
                   "&nbsp;|&nbsp; 2021 - 2025", BODY),
         Paragraph("<b>Delhi Public School, Bengaluru</b>, Class XII (CBSE), 92.4% "
                   "&nbsp;|&nbsp; 2021", BODY),
         Paragraph("<b>Delhi Public School, Bengaluru</b>, Class X (CBSE), 95.2% "
                   "&nbsp;|&nbsp; 2019", BODY),
         Paragraph("INTERNSHIPS", H)]
    s += role("Zeptonow Retail (fictional)", "Product Intern", "May 2024 - Jul 2024", [
        "Mapped the reorder flow for 40 dark-store SKUs and wrote a PRD for a "
        "one-tap reorder button, shipped to 5% of users in a test.",
        "Ran 12 user interviews with first-time buyers and summarised drop-off "
        "reasons for the checkout team.",
        "Built a SQL dashboard in Metabase tracking daily reorder rate by city."])
    s += role("Kaveri Analytics (fictional)", "Data Analyst Intern", "Dec 2023 - Jan 2024", [
        "Cleaned 1.2 lakh rows of sales data in Python (pandas) and cut report "
        "prep time from 2 days to 3 hours.",
        "Presented a churn cohort analysis to the founders."])
    s += [Paragraph("ACADEMIC PROJECTS", H)] + bullets([
        "<b>Campus Canteen App</b>: React Native app for pre-ordering food; 800 "
        "students used it in the first month.",
        "<b>Crop Price Predictor</b>: regression model on Agmarknet data, 11% "
        "mean error on onion prices."])
    s += [Paragraph("POSITIONS OF RESPONSIBILITY", H)] + bullets([
        "Head, Product Club, MIT Manipal (2023 - 2024): ran 6 case-study "
        "workshops for 150+ students.",
        "Placement Coordinator, ECE batch (2024 - 2025)."])
    s += [Paragraph("ACHIEVEMENTS", H)] + bullets([
        "Finalist, Flipkart GRiD 5.0 product case challenge (top 50 of 9,000 teams).",
        "JEE Main 2021: 98.1 percentile."])
    s += [Paragraph("SKILLS", H),
          Paragraph("SQL, Python (pandas), Excel, Figma, Metabase, Google Analytics, "
                    "user interviews, PRD writing", BODY)]
    SimpleDocTemplate(str(OUT / "synthetic_fresher.pdf"), pagesize=A4,
                      leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                      bottomMargin=14 * mm, title="Ananya Rao Resume").build(s)


def two_column():
    """Left sidebar (dark) with contact, skills, education, languages; right
    column with summary and experience. The classic Canva shape."""
    path = OUT / "synthetic_two_column.pdf"
    W, Hh = A4
    side_w = 62 * mm
    doc = BaseDocTemplate(str(path), pagesize=A4, title="Rohan Mehta Resume")
    left = Frame(0, 0, side_w, Hh, leftPadding=8 * mm, rightPadding=5 * mm,
                 topPadding=14 * mm, id="left")
    right = Frame(side_w, 0, W - side_w, Hh, leftPadding=7 * mm,
                  rightPadding=12 * mm, topPadding=14 * mm, id="right")

    def paint(canvas, _doc):
        canvas.setFillColor(HexColor("#1F3A5F"))
        canvas.rect(0, 0, side_w, Hh, stroke=0, fill=1)

    doc.addPageTemplates([PageTemplate(frames=[left, right], onPage=paint)])
    s = [Paragraph("CONTACT", SIDE_H),
         Paragraph("rohan.mehta@example.com", SIDE),
         Paragraph("+91-90000-00002", SIDE),
         Paragraph("Pune, Maharashtra", SIDE),
         Paragraph("SKILLS", SIDE_H)]
    s += [Paragraph(t, SIDE) for t in [
        "Performance marketing", "Google Ads, Meta Ads", "SQL, BigQuery",
        "Mixpanel, AppsFlyer", "A/B testing", "CleverTap, MoEngage",
        "Excel, Looker Studio", "Lifecycle marketing"]]
    s += [Paragraph("EDUCATION", SIDE_H),
          Paragraph("<b>MBA, Marketing</b>", SIDE),
          Paragraph("Symbiosis Institute of Business Management, Pune", SIDE),
          Paragraph("2019 - 2021", SIDE), Spacer(1, 4),
          Paragraph("<b>B.Com</b>", SIDE),
          Paragraph("Narsee Monjee College, Mumbai", SIDE),
          Paragraph("2016 - 2019", SIDE),
          Paragraph("LANGUAGES", SIDE_H),
          Paragraph("English, Hindi, Marathi", SIDE),
          FrameBreak(),
          Paragraph("ROHAN MEHTA", NAME),
          Paragraph("Growth Marketing Manager", CONTACT),
          Paragraph("SUMMARY", H),
          Paragraph("Growth marketer with 4 years in consumer fintech and "
                    "edtech apps. Runs paid acquisition and CRM together, and "
                    "judges both on 90-day retained users, not installs.", BODY),
          Paragraph("EXPERIENCE", H)]
    s += role("PayNest (fictional)", "Growth Marketing Manager", "Aug 2023 - Present", [
        "Own a ₹1.8 Cr monthly paid budget across Google and Meta; brought "
        "cost per KYC-complete user down 31% in two quarters.",
        "Set up the first holdout test for push and WhatsApp campaigns, which "
        "showed 40% of attributed CRM revenue was not incremental.",
        "Rebuilt the onboarding journey in CleverTap; day-7 activation rose "
        "from 22% to 29%.",
        "Work with product and data on a weekly growth review of 14 funnel metrics."])
    s += role("LearnLoop (fictional)", "Performance Marketing Associate", "Jun 2021 - Jul 2023", [
        "Ran app-install campaigns for a test-prep app, scaling from 20k to "
        "90k installs a month at flat CPI.",
        "Built creative testing cadence of 10 new ads a week; winning ads cut "
        "CPI by 18%.",
        "Moved attribution from Firebase to AppsFlyer and cleaned up "
        "duplicate install counting."])
    doc.build(s)


def senior():
    global _big
    _big = True         # a two-page senior resume, set in a larger size
    s = [Paragraph("KAVITA IYER", NAME),
         Paragraph("Director of Product | Bengaluru | kavita.iyer@example.com | "
                   "+91-90000-00003", CONTACT),
         Paragraph("SUMMARY", H),
         Paragraph("Product leader with 11 years across payments, lending and "
                   "marketplaces. Has built and run product teams of up to 14 PMs, "
                   "taken two products from zero to national scale, and owned a "
                   "P&amp;L of ₹240 Cr.", BODY),
         Paragraph("EXPERIENCE", H)]
    s += role("Vittam Finance (fictional)", "Director of Product, Lending", "Mar 2022 - Present", [
        "Lead 14 PMs and 3 designers across personal loans, credit line and "
        "collections; own the lending P&amp;L of ₹240 Cr.",
        "Launched a pre-approved credit line to 3.2 million users; it became "
        "38% of new disbursals within a year.",
        "Cut loan approval time from 26 hours to 7 minutes by moving "
        "underwriting to a rules engine with bureau pulls.",
        "Brought 90+ day delinquency on new loans down from 4.1% to 2.6% with "
        "the risk team.",
        "Set the quarterly product planning process used by 6 business units.",
        "Partnered with 4 NBFC and bank lenders to add co-lending, taking "
        "monthly disbursals from ₹60 Cr to ₹190 Cr.",
        "Built the collections product (digital reminders, field app, "
        "settlement offers), lifting cure rate on 30-day buckets by 9 points.",
        "Took the lending app through two RBI digital lending audits with no "
        "major findings."])
    s += role("ShopKart Marketplace (fictional)", "Senior Product Manager, Payments",
              "Jan 2019 - Feb 2022", [
        "Owned checkout and payments for 40 million monthly buyers; raised "
        "payment success rate from 91.2% to 95.8%.",
        "Launched UPI Autopay for subscriptions, adopted by 1.1 million "
        "subscribers in 9 months.",
        "Led the move from two payment gateways to four with smart routing, "
        "saving ₹11 Cr a year in fees.",
        "Hired and managed a team of 4 PMs.",
        "Designed cash-on-delivery to prepaid nudges that moved 7% of COD "
        "orders to prepaid, cutting RTO losses.",
        "Shipped saved cards and one-click EMI at checkout with the bank "
        "partnerships team."])
    s += role("QuickServe Logistics (fictional)", "Product Manager", "Jul 2016 - Dec 2018", [
        "Built the driver app from zero to 25,000 daily active drivers in 5 cities.",
        "Designed dynamic batching of deliveries, cutting cost per order by 17%.",
        "Ran weekly field visits and 60+ driver interviews to set the roadmap."])
    s += role("Infotech Solutions (fictional)", "Business Analyst", "Jun 2014 - Jun 2016", [
        "Wrote requirements for a core banking upgrade used by 300 branches.",
        "Built SQL reports for branch operations, replacing 20 manual Excel "
        "sheets."])
    s += [Paragraph("EDUCATION", H),
          Paragraph("<b>IIM Lucknow</b>, PGP (MBA) &nbsp;|&nbsp; 2012 - 2014", BODY),
          Paragraph("<b>NIT Trichy</b>, B.Tech Computer Science &nbsp;|&nbsp; 2008 - 2012",
                    BODY),
          Paragraph("SKILLS", H),
          Paragraph("Product strategy, P&amp;L ownership, lending and credit, "
                    "payments, UPI, team building, SQL, experimentation, "
                    "roadmapping, stakeholder management", BODY),
          Paragraph("BOARD AND ADVISORY", H)] + bullets([
        "Product advisor, Seedling Health (fictional, seed stage), 2023 - present.",
        "Mentor, NASSCOM product circle, 2021 - 2024: 20 early PMs mentored."]) + [
          Paragraph("AWARDS", H)] + bullets([
        "Vittam Finance CEO Award for the credit line launch, 2023.",
        "Speaker, Global Fintech Fest 2024, on underwriting at scale."])
    SimpleDocTemplate(str(OUT / "synthetic_senior.pdf"), pagesize=A4,
                      leftMargin=24 * mm, rightMargin=24 * mm, topMargin=22 * mm,
                      bottomMargin=14 * mm, title="Kavita Iyer Resume").build(s)
    _big = False


def sales():
    s = [Paragraph("IMRAN SHAIKH", NAME),
         Paragraph("Area Sales Manager | Lucknow, Uttar Pradesh | "
                   "imran.shaikh@example.com | +91-90000-00004", CONTACT),
         Paragraph("PROFILE", H),
         Paragraph("Sales manager with 7 years in FMCG general trade and modern "
                   "trade across Uttar Pradesh. Manages distributors, a field team "
                   "of 18 and a ₹48 Cr annual target.", BODY),
         Paragraph("WORK EXPERIENCE", H)]
    s += role("Himalaya Foods (fictional)", "Area Sales Manager", "Apr 2022 - Present", [
        "Manage 11 distributors and 18 sales officers across 9 districts of "
        "eastern UP, with a ₹48 Cr annual target.",
        "Grew secondary sales 23% in FY25 by adding 2,100 new retail outlets.",
        "Cut distributor stock-outs from 14% to 5% with weekly stock reviews.",
        "Launched a new snacks range in 3,000 outlets in 60 days."])
    s += role("Bharat Consumer Products (fictional)", "Sales Officer", "Jun 2018 - Mar 2022", [
        "Handled 420 outlets in Kanpur; top sales officer in the zone for "
        "FY20 and FY21.",
        "Opened 6 modern trade accounts, including two regional supermarket "
        "chains.",
        "Trained 5 new sales representatives on beat planning."])
    s += [Paragraph("EDUCATION", H),
          Paragraph("<b>Amity University, Lucknow</b>, MBA (Sales and Marketing) "
                    "&nbsp;|&nbsp; 2016 - 2018", BODY),
          Paragraph("<b>Lucknow University</b>, B.Sc &nbsp;|&nbsp; 2013 - 2016", BODY),
          Paragraph("SKILLS", H),
          Paragraph("Distributor management, general trade, modern trade, beat "
                    "planning, trade schemes, team handling, Excel, SAP SD, "
                    "Salesforce", BODY),
          Paragraph("CERTIFICATIONS", H)] + bullets([
        "Certified Sales Professional, Indian Institute of Sales (2021)."])
    SimpleDocTemplate(str(OUT / "synthetic_sales.pdf"), pagesize=A4,
                      leftMargin=16 * mm, rightMargin=16 * mm, topMargin=14 * mm,
                      bottomMargin=14 * mm, title="Imran Shaikh Resume").build(s)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    fresher()
    two_column()
    senior()
    sales()
    for p in sorted(OUT.glob("synthetic_*.pdf")):
        print(p)
