# -*- coding: utf-8 -*-
"""Web UI.   streamlit run app.py

Configuration (environment variables, or Streamlit secrets when deployed to the cloud):
  ANTHROPIC_API_KEY   required
  APP_PASSWORD        optional; when set, visitors must enter it before running anything
  DAILY_RUN_LIMIT     optional; reviews allowed per 24 hours (default 10) - protects the API budget
  RUNS_PER_VISITOR    optional; runs per browser session when there is no password (default 1)
  MAX_PAPERS          optional; upper limit for papers per review (default 5)
"""
import hmac
import os
import subprocess
import sys
import time

import streamlit as st

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
import glob  # noqa: E402
import importlib  # noqa: E402

from verified_lit_review import scope, service  # noqa: E402

# After a push, Streamlit reruns this file but keeps the imported package in memory: a cloud run
# after a push still used the old report layout and the old output limit. So reload the package when
# its files change, and keep the background runs that are in progress.
_PKG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "verified_lit_review")
_stamp = max(os.path.getmtime(f) for f in glob.glob(os.path.join(_PKG, "*.py")))
if getattr(service, "_CODE_STAMP", None) != _stamp:     # None: loaded before this check existed
    _jobs, _lock = service._JOBS, service._JOBS_LOCK
    for _name in ("numbers", "db", "sources", "fetch", "build_report", "agent", "scope", "service"):
        if f"verified_lit_review.{_name}" in sys.modules:
            importlib.reload(sys.modules[f"verified_lit_review.{_name}"])   # same module object, new code
    service._JOBS, service._JOBS_LOCK = _jobs, _lock
service._CODE_STAMP = _stamp


def code_version():
    """Short commit id of the deployed code, shown at the bottom of the page."""
    git = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".git")
    try:
        with open(os.path.join(git, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
        if head.startswith("ref: "):
            with open(os.path.join(git, head[5:]), encoding="utf-8") as f:
                head = f.read().strip()
        return head[:7]
    except OSError:
        return ""

# Streamlit Cloud keeps secrets in st.secrets; copy them to the environment the agent reads.
try:
    for _k in ("ANTHROPIC_API_KEY", "APP_PASSWORD", "DAILY_RUN_LIMIT", "MAX_PAPERS", "RUNS_PER_VISITOR",
               "MAX_COST_PER_RUN", "OPENALEX_API_KEY"):
        if _k in st.secrets and not os.environ.get(_k):
            os.environ[_k] = str(st.secrets[_k])
except Exception:  # no secrets file when running locally - environment variables are used instead
    pass
DAILY_RUN_LIMIT = int(os.environ.get("DAILY_RUN_LIMIT", "10"))
MAX_PAPERS = int(os.environ.get("MAX_PAPERS", "5"))
MAX_LOGIN_ATTEMPTS = 5
RUNS_PER_VISITOR = int(os.environ.get("RUNS_PER_VISITOR", "1"))
_ROOT = os.path.dirname(os.path.abspath(__file__))
DEMOS = {"בריאות: מעקב טלפוני אחרי שחרור": os.path.join(_ROOT, "examples", "telephone-follow-up",
                                                          "Research_Summary_Report.md"),
         "חינוך: הערכה מעצבת ומשוב": os.path.join(_ROOT, "examples", "formative-assessment",
                                                    "Research_Summary_Report.md"),
         "רפואה: שיקום לב באי-ספיקת לב (English)": os.path.join(_ROOT, "examples", "heart-failure-rehab",
                                                               "Research_Summary_Report.md")}
# The agent's own error messages are English; visitors get them in Hebrew.
ERRORS_HE = {"question is empty": "השאלה ריקה. כתבו שאלת מחקר.",
             "longer than": "השאלה ארוכה מדי.",
             "n_papers": "מספר המאמרים חייב להיות בין 1 ל-5.",
             "without submitting": "הסוכן לא הצליח להשלים דו\"ח לשאלה הזו. נסו לנסח אותה אחרת.",
             "could be downloaded": "לא הצלחנו להוריד אף אחד מהמאמרים שנבחרו. נסו שאלה אחרת.",
             "cost limit": "הריצה נעצרה כי הגיעה לתקרת העלות לריצה.",
             "declined": "המודל סירב לבקשה הזו.",
             "searchable english keywords": "לא נמצאו בשאלה מילות חיפוש. נסו לכתוב אותה באנגלית."}


def hebrew_error(message):
    return next((he for en, he in ERRORS_HE.items() if en in message.lower()),
                "הריצה נכשלה. נסו שוב או נסחו את השאלה אחרת.")


def service_unavailable(error):
    """Errors caused by the owner's API account (no credit, bad or revoked key, rate limit)."""
    status = getattr(error, "status_code", None)
    text = str(error).lower()
    return status in (401, 403, 429, 529) or "credit balance" in text or "billing" in text


def runs_text(n):
    return "ריצה אחת" if n == 1 else f"{n} ריצות"

MAX_QUESTION_CHECKS = 5    # per browser session; each check is one short model call (about a cent)
AUTHOR = ("נבנה על ידי **שרון וייסברג (Sharon Weissberg)** · "
          "[LinkedIn](https://www.linkedin.com/in/sharon-weissberg-82596911) · "
          "[GitHub](https://github.com/sharon0r/verified-lit-review) · "
          "[sharonor.we@gmail.com](mailto:sharonor.we@gmail.com)")
# One model only: in a review by an AI physician agent, Opus scored 7-9/10 on the same question,
# Haiku 6 and Sonnet 5 (see docs/EVALUATION.md), so visitors are not offered the cheaper models.
MODEL = "claude-opus-5-5"
ICONS = {"search": "🔍", "fetch": "⬇️", "retrieve": "📄", "verify": "✅"}

st.set_page_config(page_title="סקירת ספרות מאומתת", page_icon="📚", layout="centered")
st.markdown("""<style>
  .block-container, section[data-testid="stSidebar"] {direction: rtl; text-align: right;}
  code {direction: ltr; text-align: left;}
  /* an English question in the text box keeps its punctuation in place ("?" at the end) */
  textarea, input {unicode-bidi: plaintext; text-align: start;}
  /* each paragraph takes its direction from its own text: Hebrew right-to-left, an English report
     or an English quote left-to-right */
  .stMarkdown p, .stMarkdown li, .stMarkdown td, .stMarkdown th, .stMarkdown h1, .stMarkdown h2,
  .stMarkdown h3 {unicode-bidi: plaintext; text-align: start;}
  [data-testid="stMetricValue"] {direction: ltr;}
  /* less empty space above the page, so the question box is visible without scrolling */
  .block-container {padding-top: 2rem !important;}
  .app-title {text-align: center; font-size: 1.6rem; font-weight: 700; margin: 0 0 .25rem 0;
               line-height: 1.3;}
</style>""", unsafe_allow_html=True)

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    if st.session_state.get("authorized"):  # other people's questions are never shown to visitors
        st.subheader("ריצות קודמות")
        for r in service.list_runs(8):
            st.caption(f"#{r['run_id']} · {r['verified']}/{r['quotes']} ✅ · {r['question'][:55]}")
    st.divider()
    st.markdown(AUTHOR)

# ------------------------------------------------------------------ main
st.markdown('<div class="app-title">📚 סקירת ספרות עם ציטוטים מאומתים</div>', unsafe_allow_html=True)
st.markdown(AUTHOR)
st.markdown("הזינו שאלת מחקר בכל תחום, וסוכן AI יפיק עבורכם תוך כמה דקות סקירת ספרות ראשונית על סמך עד "
            "חמישה מאמרים אקדמיים בגישה פתוחה. הסוכן מעדיף סקירות שיטתיות ומחקרים בעלי מערך מחקרי חזק, "
            "ומאתר בכל מאמר את הקטעים הרלוונטיים לשאלה באמצעות דירוג לפי שכיחות מונחים (BM25). "
            "**הסקירה נכתבת מתוך קטעים אלה בלבד (RAG), וכל ציטוט בה מאומת אוטומטית מול נוסח המאמר המקורי.**")

question = st.text_area("שאלת מחקר (באנגלית או בעברית)", key="question", height=80,
                        max_chars=service.MAX_QUESTION_CHARS,
                        placeholder="למשל: Does telephone follow-up after hospital discharge reduce readmissions?")
# the settings sit next to the question, not in the sidebar, which phones hide behind a menu
_lang_col, _n_col = st.columns(2)
language = _lang_col.radio("שפת הדו\"ח", ["he", "en"], format_func={"he": "עברית", "en": "English"}.get,
                           horizontal=True)
n_papers = _n_col.slider("מספר מאמרים", 1, MAX_PAPERS, MAX_PAPERS)


def show_demo():
    with st.expander("📄 רוצים לראות קודם? דו\"ח אמיתי שהסוכן כבר הפיק (חינם, מיידי)"):
        demo = st.radio("בחרו דוגמה", list(DEMOS), horizontal=True, label_visibility="collapsed")
        try:
            with open(DEMOS[demo], encoding="utf-8") as f:
                st.markdown(f.read())
        except OSError:
            st.caption("הדוגמה לא נמצאה.")


if not os.environ.get("ANTHROPIC_API_KEY"):
    st.error("ההרצה החיה אינה זמינה כרגע. אפשר לעיין בדוגמה המוכנה.")
    show_demo()
    st.stop()

password = os.environ.get("APP_PASSWORD")
if password and not st.session_state.get("authorized"):
    failures = st.session_state.get("failed_logins", 0)
    if failures >= MAX_LOGIN_ATTEMPTS:
        st.error("יותר מדי ניסיונות שגויים. הגישה ננעלה.")
        st.stop()
    entered = st.text_input("סיסמת גישה להרצה", type="password")
    if entered and hmac.compare_digest(entered, password):
        st.session_state.authorized = True
        st.rerun()
    if entered:
        # every widget change reruns the script; count a password only once, not on each rerun
        if entered != st.session_state.get("last_attempt"):
            st.session_state.last_attempt = entered
            st.session_state.failed_logins = failures + 1
            time.sleep(1.5)                      # slows down guessing
        st.error("סיסמה שגויה")
    st.info("ההרצה משתמשת במפתח API בתשלום, ולכן היא מוגנת בסיסמה. אפשר לעיין בדוגמה ובקוד בגיטהאב.")
    show_demo()
    st.stop()

used = service.runs_since(time.time() - 86400)
# Without a password the app is open, so every visitor gets RUNS_PER_VISITOR runs (default 1);
# the shared daily limit and the prepaid API balance cap the total either way.
per_visitor = None if password else RUNS_PER_VISITOR
my_runs = st.session_state.get("my_runs", 0)
left_today = max(DAILY_RUN_LIMIT - used, 0)
mine_left = None if per_visitor is None else max(per_visitor - my_runs, 0)
can_run = True
if used >= DAILY_RUN_LIMIT:
    st.warning("הריצות של היום נוצלו. אפשר לעיין בדוגמה המוכנה, או לנסות שוב מחר.")
    can_run = False
elif per_visitor is not None and my_runs >= per_visitor:
    st.info("ריצת הניסיון שלכם נוצלה. תודה! אפשר לעיין בדוגמה המוכנה ובקוד בגיטהאב.")
    can_run = False
else:
    st.caption((f"נותרה לכם {runs_text(mine_left)} · " if mine_left else "")
               + f"היום נותרו באתר {runs_text(left_today)} מתוך {DAILY_RUN_LIMIT}")

def follow(job):
    """Show the background run's progress until it ends. Returns (succeeded, counts_as_a_run)."""
    with st.status("הסוכן עובד… (אפשר לרענן את הדף, הריצה ממשיכה)", expanded=True) as status:
        shown, clock, last_event = 0, st.empty(), time.time()
        while True:
            # updating the clock also lets the page notice a button press (e.g. stop) without delay
            quiet = time.time() - last_event
            clock.caption(f"⏱️ {int(time.time() - job['started'])} שניות"
                          + (" · ✍️ הסוכן כותב או חושב, השלב הזה יכול לקחת כדקה…" if quiet > 15 else ""))
            done = job["done"]                           # read first: every event is in before done
            for kind, msg in job["events"][shown:]:
                st.write(f"{ICONS.get(kind, '•')} {msg}")
            if len(job["events"]) > shown:
                last_event = time.time()
            shown = len(job["events"])
            if done:
                break
            time.sleep(0.5)
        e = job["error"]
        if e is None:
            status.update(label="הסקירה הושלמה", state="complete", expanded=False)
            return True, True
        if isinstance(e, service.Cancelled):             # the visitor pressed stop: keep their trial run
            status.update(label="הריצה נעצרה", state="error", expanded=False)
            st.info("הריצה נעצרה. אפשר לתקן את השאלה ולהריץ שוב.")
            return False, False
        if isinstance(e, ValueError):                    # the question was refused before any model call
            status.update(label="הריצה לא התחילה", state="error")
            st.error(hebrew_error(str(e)))
            return False, False
        status.update(label="הריצה נכשלה", state="error")
        if isinstance(e, RuntimeError):                  # the run started (and cost money), then failed
            st.error(hebrew_error(str(e)))
            return False, True
        if service_unavailable(e):                       # our account, not the visitor: keep their trial run
            st.error("השירות אינו זמין כרגע. הריצה שלכם לא נוצלה, ואפשר לנסות שוב מאוחר יותר.")
            return False, False
        st.error("אירעה שגיאה בלתי צפויה. נסו שוב בעוד דקה.")   # no internal details on a public page
        return False, True


# The run lives in a background thread (service.start_job) and its token in the URL, so a phone that
# locks its screen, or a page refresh, finds the same run again instead of losing a paid report.
token = st.query_params.get("job")
job = service.get_job(token) if token else None
if token and job is None:
    st.query_params.pop("job", None)
    if not st.session_state.get("result"):
        st.info("הריצה הקודמת נקטעה כי האפליקציה הופעלה מחדש. אפשר להריץ שוב.")
run_failed = False


def begin(q):
    """Start the paid review in the background and remember its token in the URL."""
    if service.runs_since(time.time() - 86400) >= DAILY_RUN_LIMIT:   # re-check right before spending
        st.warning("הריצות של היום נוצלו.")
        st.stop()
    st.session_state.pop("scope", None)
    t = service.start_job(q, n_papers, language, model=MODEL)
    st.query_params["job"] = t
    return t


if (job is None or job["done"]) and can_run:
    sc = st.session_state.get("scope")
    if sc and sc["question"] == question:
        # the question was too broad: offer focused versions, or the user's own question as it is
        st.info(f"🔎 **השאלה רחבה מדי לסקירה של כמה מאמרים.** {sc['reason']}")
        own = f"השאלה שלי, כפי שהיא: {question}"
        choice = st.radio("בחרו שאלה:", sc["options"] + [own])
        if st.button("▶️ התחילו סקירה עם השאלה שנבחרה", type="primary"):
            token = begin(question if choice == own else choice)
            job = service.get_job(token)
    elif st.button("▶️ התחילו סקירה", type="primary"):
        checked = None
        if question.strip() and st.session_state.get("checks", 0) < MAX_QUESTION_CHECKS:
            st.session_state.checks = st.session_state.get("checks", 0) + 1
            try:
                with st.spinner("בודקים שהשאלה ממוקדת…"):
                    checked = scope.check_question(question)
            except Exception:  # noqa: BLE001 - the check helps; it never blocks a review
                checked = None
        if checked and not checked["focused"]:
            st.session_state.scope = {**checked, "question": question}
            st.rerun()
        token = begin(question)
        job = service.get_job(token)
if job is not None and st.session_state.get("result_job") != token:
    # pressing the button reruns the page; the run itself stops before its next model call
    if not job["done"] and st.button("⏹️ עצרו את הריצה"):
        service.cancel_job(token)
    if job["cancel"] and not job["done"]:
        st.caption("עוצרים… הריצה תיעצר לפני השלב הבא.")
    ok, counts = follow(job)
    if counts and st.session_state.get("counted_job") != token:     # each run is counted once
        st.session_state.counted_job = token
        st.session_state.my_runs = my_runs + 1
    if ok:
        st.session_state.result, st.session_state.result_job = job["result"], token
        st.rerun()                                       # refresh the counters above for the new state
    run_failed = True
    st.query_params.pop("job", None)                     # a failed run is shown once, not on every rerun

r = st.session_state.get("result")
if not r and not can_run:      # the ready example only when the visitor cannot run a review
    show_demo()
if r:
    total = r["verified"] + len(r["rejected"])
    # one line instead of five tiles, which stack into five tall rows on a phone
    st.caption(f"📄 {r['papers']} מאמרים · ✅ {r['verified']}/{total} ציטוטים אומתו מול ה-PDF · "
               f"⏱️ {r['seconds'] / 60:.1f} דק' · 💲{r['cost_usd']:.2f}")

    tab_report, tab_audit, tab_how = st.tabs(["📝 הדו\"ח", "🔎 תיעוד אימות (Audit trail)", "⚙️ איך זה עובד"])
    with tab_report:
        st.markdown(r["report_markdown"])
        d1, d2 = st.columns(2)
        d1.download_button("הורדה כ-Markdown", r["report_markdown"], "Research_Summary_Report.md")
        docx = os.path.splitext(r["report"])[0] + ".docx"
        if not os.path.exists(docx):
            conv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src", "verified_lit_review", "md_to_docx.py")
            subprocess.run([sys.executable, "-I", conv, r["report"], docx], capture_output=True)
        if os.path.exists(docx):
            with open(docx, "rb") as f:
                d2.download_button("הורדה כ-Word", f.read(), "Research_Summary_Report.docx")
    with tab_audit:
        run = service.get_run(r["run_id"])
        if run is None:                         # the database was reset (e.g. the app restarted)
            st.info("תיעוד הריצה כבר לא זמין, כי האפליקציה הופעלה מחדש. הדו\"ח עצמו נשמר בלשונית הראשונה.")
            st.stop()
        st.caption(f"ריצה #{r['run_id']} · מודל {run['model']} · "
                   f"{time.strftime('%d/%m/%Y %H:%M', time.localtime(run['started_at']))} · נשמר ב-SQLite")
        st.dataframe([{"✅": "✅" if q["verified"] else "❌", "עמוד": q["claimed_page"], "ציטוט": q["text"],
                       "מאמר": q["title"], "הערה": q["reason"] or ""} for q in run["quote_checks"]],
                     use_container_width=True, hide_index=True)
        if r["rejected"]:
            st.warning("ציטוטים שנדחו בבדיקה הסופית מול ה-PDF:")
            st.json(r["rejected"])
    with tab_how:
        st.markdown("""
1. **סוכן (Claude Opus 5.5):** מנסח את השאלה כ-PICO ומחפש בשלושה מאגרים: PubMed (סקירות שיטתיות ומטה-אנליזות עדכניות), Europe PMC (רפואה ומדעי החיים) ו-OpenAlex (כל התחומים). הוא בוחר מאמרים לפי התקציר.
2. **הורדה:** PDF בגישה פתוחה מ-PMC (AWS), מהקישור שבתוצאת החיפוש או דרך OpenAlex. כל עמוד נשמר ב-SQLite (FTS5).
3. **RAG:** הסוכן שולף רק את העמודים הרלוונטיים וכותב ממצאים וציטוטים על בסיסם.
4. **בקרה:** כל ציטוט נבדק מילה במילה מול העמוד. כל מספר בממצאים נבדק מול המספרים שבמאמר, וכל מספר במסקנה חייב להופיע בממצא או בציטוט מאומת. מה שלא עבר חוזר לסוכן לתיקון, ובדו"ח נשאר רק מה שאומת.
5. **הגבלות:** תקרת עלות לכל ריצה, מגבלה יומית לאתר כולו, ומספר מוגבל של שלבים לסוכן.
6. **תיעוד:** כל בדיקה נשמרת במסד הנתונים עם המודל, הזמן והעלות.
""")

if code_version():
    st.caption(f"גרסה {code_version()}")
