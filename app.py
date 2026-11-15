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
from verified_lit_review import service  # noqa: E402

# Streamlit Cloud keeps secrets in st.secrets; copy them to the environment the agent reads.
try:
    for _k in ("ANTHROPIC_API_KEY", "APP_PASSWORD", "DAILY_RUN_LIMIT", "MAX_PAPERS"):
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
                                                    "Research_Summary_Report.md")}

EXAMPLES = {  # short Hebrew label -> the question sent to the agent
    "🏥 מעקב טלפוני אחרי שחרור": "Does telephone follow-up after hospital discharge reduce readmissions?",
    "🎓 משוב לתלמידים": "Does formative assessment with feedback improve student achievement?",
    "💼 עבודה מרחוק": "Does remote work affect employee productivity?",
}
AUTHOR = ("נבנה על ידי **שרון וייסברג (Sharon Weissberg)** · "
          "[LinkedIn](https://www.linkedin.com/in/sharon-weissberg-82596911) · "
          "[GitHub](https://github.com/sharon0r/verified-lit-review) · "
          "[sharonor.we@gmail.com](mailto:sharonor.we@gmail.com)")
MODELS = {"Claude Opus 5.5 (המדויק ביותר)": "claude-opus-5-5", "Claude Sonnet 5.5": "claude-sonnet-5-5",
          "Claude Haiku 5.5 (הזול ביותר)": "claude-haiku-5-5"}
ICONS = {"search": "🔍", "fetch": "⬇️", "retrieve": "📄", "verify": "✅"}

st.set_page_config(page_title="סקירת ספרות מאומתת", page_icon="📚", layout="wide")
st.markdown("""<style>
  .block-container, section[data-testid="stSidebar"] {direction: rtl; text-align: right;}
  blockquote, code {direction: ltr; text-align: left;}
  [data-testid="stMetricValue"] {direction: ltr;}
</style>""", unsafe_allow_html=True)

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.header("הגדרות")
    n_papers = st.slider("מספר מאמרים", 1, MAX_PAPERS, MAX_PAPERS)
    language = st.radio("שפת הדו\"ח", ["he", "en"], format_func={"he": "עברית", "en": "English"}.get, horizontal=True)
    model = MODELS[st.selectbox("מודל (אפשר להשאיר כברירת מחדל)", list(MODELS))]
    if st.session_state.get("authorized"):  # other people's questions are never shown to visitors
        st.divider()
        st.subheader("ריצות קודמות")
        for r in service.list_runs(8):
            st.caption(f"#{r['run_id']} · {r['verified']}/{r['quotes']} ✅ · {r['question'][:55]}")
    st.divider()
    st.markdown(AUTHOR)

# ------------------------------------------------------------------ main
st.title("📚 סקירת ספרות עם ציטוטים מאומתים")
st.markdown(AUTHOR)
st.markdown("כתבו שאלת מחקר בכל תחום, ותקבלו תוך דקה-שתיים סיכום של עד 5 מאמרים אקדמיים שעברו שיפוט עמיתים "
            "וזמינים לקריאה חופשית. **כל ציטוט בדו\"ח נבדק אוטומטית, מילה במילה, מול העמוד המקורי ב-PDF**, "
            "וציטוט שלא נמצא נמחק.")

if "question" not in st.session_state:
    st.session_state.question = next(iter(EXAMPLES.values()))
st.caption("או בחרו שאלה לדוגמה:")
cols = st.columns(len(EXAMPLES))
for col, (label, ex) in zip(cols, EXAMPLES.items()):
    if col.button(label, use_container_width=True):
        st.session_state.question = ex
question = st.text_area("שאלת מחקר (באנגלית או בעברית)", key="question", height=80,
                        max_chars=service.MAX_QUESTION_CHARS)


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
        st.session_state.failed_logins = failures + 1
        time.sleep(1.5)                          # slows down guessing
        st.error("סיסמה שגויה")
    st.info("ההרצה משתמשת במפתח API בתשלום, ולכן היא מוגנת בסיסמה. אפשר לעיין בדוגמה ובקוד בגיטהאב.")
    st.stop()

used = service.runs_since(time.time() - 86400)
# Without a password the app is open, so every visitor gets RUNS_PER_VISITOR runs (default 1);
# the shared daily limit and the prepaid API balance cap the total either way.
per_visitor = None if password else RUNS_PER_VISITOR
my_runs = st.session_state.get("my_runs", 0)
left_today = max(DAILY_RUN_LIMIT - used, 0)
st.caption((f"יש לכם ריצת ניסיון אחת · " if per_visitor == 1 and my_runs == 0 else "")
           + f"היום נותרו באתר {left_today} ריצות מתוך {DAILY_RUN_LIMIT}")
can_run = True
if used >= DAILY_RUN_LIMIT:
    st.warning("הריצות של היום נוצלו. אפשר לעיין בדוגמה המוכנה, או לנסות שוב מחר.")
    can_run = False
elif per_visitor is not None and my_runs >= per_visitor:
    st.info("ריצת הניסיון שלכם נוצלה. תודה! אפשר לעיין בדוגמה המוכנה ובקוד בגיטהאב.")
    can_run = False

if can_run and st.button("▶️ התחילו סקירה", type="primary"):
    st.session_state.my_runs = my_runs + 1
    if service.runs_since(time.time() - 86400) >= DAILY_RUN_LIMIT:   # re-check right before spending
        st.warning("הריצות של היום נוצלו.")
        st.stop()
    with st.status("הסוכן עובד…", expanded=True) as status:
        def on_event(kind, msg):
            st.write(f"{ICONS.get(kind, '•')} {msg}")
        try:
            st.session_state.result = service.run_review(question, n_papers, language, model=model, on_event=on_event)
            status.update(label="הסקירה הושלמה", state="complete", expanded=False)
        except (ValueError, RuntimeError) as e:          # our own, user-readable messages
            status.update(label="הריצה נכשלה", state="error")
            st.error(str(e))
        except Exception:  # noqa: BLE001 - no internal details on a public page
            status.update(label="הריצה נכשלה", state="error")
            st.error("אירעה שגיאה בלתי צפויה. נסו שוב בעוד דקה.")

show_demo()

r = st.session_state.get("result")
if r:
    total = r["verified"] + len(r["rejected"])
    c = st.columns(5)
    c[0].metric("מאמרים", r["papers"])
    c[1].metric("ציטוטים שאומתו", f"{r['verified']}/{total}")
    c[2].metric("נדחו", len(r["rejected"]))
    c[3].metric("עלות", f"${r['cost_usd']:.2f}")
    c[4].metric("זמן", f"{r['seconds']:.0f}s")

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
1. **סוכן (Claude):** מפרק את השאלה, מחפש ב-Europe PMC (רפואה ומדעי החיים) או ב-OpenAlex (כל התחומים) ובוחר מאמרים לפי התקציר.
2. **הורדה:** PDF בגישה פתוחה מ-PMC (AWS) או OpenAlex; כל עמוד נשמר ב-SQLite (FTS5).
3. **RAG:** הסוכן שולף רק את העמודים הרלוונטיים וכותב ממצאים וציטוטים על בסיסם.
4. **בקרה:** כל ציטוט נבדק מילה במילה ומספר במספר מול העמוד. ציטוט שגוי חוזר לסוכן לתיקון, ובבדיקה הסופית נשאר רק מה שאומת.
5. **תיעוד:** כל בדיקה נשמרת במסד הנתונים עם המודל, הזמן והעלות.
""")
