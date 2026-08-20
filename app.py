"""
Live Quiz App (Mentimeter/Kahoot-style) built with Streamlit + SQLite.

Run with:
    streamlit run app.py

How it works
------------
- A HOST creates a quiz (title + questions), which generates a short join CODE.
- PARTICIPANTS open the same app (from a laptop, phone browser, etc.), enter
  the join code + their name, and answer questions as the host advances them.
- All state (questions, current question index, every submitted answer) is
  stored in a local SQLite file (quiz.db) so it's shared across every
  browser tab / device hitting this one running app instance.
- The app auto-refreshes every 2 seconds so results and question changes
  propagate without manual reloads.

Deployment note: SQLite works great for a single running instance (e.g. one
`streamlit run` process, or one Streamlit Community Cloud app). If you need
multiple server instances behind a load balancer, swap the DB layer for
Postgres/MySQL — the functions in the "DATA LAYER" section are the only
things you'd need to change.
"""

import sqlite3
import json
import random
import string
import time
from datetime import datetime

import pandas as pd
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

DB_PATH = "quiz.db"

# --------------------------------------------------------------------------
# DATA LAYER
# --------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            code TEXT PRIMARY KEY,
            title TEXT,
            questions_json TEXT,
            current_index INTEGER DEFAULT -1,
            revealed INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            session_code TEXT,
            name TEXT,
            joined_at TEXT,
            PRIMARY KEY (session_code, name)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS responses (
            session_code TEXT,
            question_index INTEGER,
            participant_name TEXT,
            answer_index INTEGER,
            answered_at TEXT,
            PRIMARY KEY (session_code, question_index, participant_name)
        )
    """)
    conn.commit()
    conn.close()


def create_session(title, questions):
    code = "".join(random.choices(string.digits, k=5))
    conn = get_conn()
    conn.execute(
        "INSERT INTO sessions (code, title, questions_json, current_index, revealed, created_at) "
        "VALUES (?, ?, ?, -1, 0, ?)",
        (code, title, json.dumps(questions), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return code


def get_session(code):
    conn = get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE code = ?", (code,)).fetchone()
    conn.close()
    return row


def set_current_index(code, idx, revealed=0):
    conn = get_conn()
    conn.execute(
        "UPDATE sessions SET current_index = ?, revealed = ? WHERE code = ?",
        (idx, revealed, code),
    )
    conn.commit()
    conn.close()


def set_revealed(code, revealed=1):
    conn = get_conn()
    conn.execute("UPDATE sessions SET revealed = ? WHERE code = ?", (revealed, code))
    conn.commit()
    conn.close()


def join_session(code, name):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO participants (session_code, name, joined_at) VALUES (?, ?, ?)",
        (code, name, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_participants(code):
    conn = get_conn()
    rows = conn.execute(
        "SELECT name FROM participants WHERE session_code = ? ORDER BY joined_at", (code,)
    ).fetchall()
    conn.close()
    return [r["name"] for r in rows]


def submit_answer(code, question_index, name, answer_index):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO responses "
        "(session_code, question_index, participant_name, answer_index, answered_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (code, question_index, name, answer_index, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def get_responses(code, question_index=None):
    conn = get_conn()
    if question_index is None:
        rows = conn.execute(
            "SELECT * FROM responses WHERE session_code = ?", (code,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM responses WHERE session_code = ? AND question_index = ?",
            (code, question_index),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_results_df(code):
    session = get_session(code)
    if session is None:
        return pd.DataFrame()
    questions = json.loads(session["questions_json"])
    responses = get_responses(code)
    rows = []
    for r in responses:
        q = questions[r["question_index"]]
        chosen = q["options"][r["answer_index"]] if r["answer_index"] is not None else None
        correct = q["options"][q["correct_index"]]
        rows.append({
            "participant": r["participant_name"],
            "question_no": r["question_index"] + 1,
            "question": q["text"],
            "answer_given": chosen,
            "correct_answer": correct,
            "is_correct": chosen == correct,
            "answered_at": r["answered_at"],
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# UI HELPERS
# --------------------------------------------------------------------------

def autorefresh(interval_ms=2000, key="refresh"):
    if HAS_AUTOREFRESH:
        st_autorefresh(interval=interval_ms, key=key)
    else:
        # Fallback: lightweight manual refresh button if the component isn't installed
        st.button("🔄 Refresh", key=key + "_btn")


def host_create_quiz():
    st.subheader("Create a new quiz")

    if "draft_questions" not in st.session_state:
        st.session_state.draft_questions = []

    title = st.text_input("Quiz title", value=st.session_state.get("draft_title", ""))
    st.session_state.draft_title = title

    with st.form("add_question_form", clear_on_submit=True):
        st.markdown("**Add a question**")
        q_text = st.text_input("Question text")
        opt_a = st.text_input("Option A")
        opt_b = st.text_input("Option B")
        opt_c = st.text_input("Option C (optional)")
        opt_d = st.text_input("Option D (optional)")
        correct = st.selectbox("Correct answer", ["A", "B", "C", "D"])
        add = st.form_submit_button("Add question")
        if add:
            options = [o for o in [opt_a, opt_b, opt_c, opt_d] if o.strip()]
            if not q_text.strip() or len(options) < 2:
                st.error("Give the question text and at least 2 options.")
            else:
                correct_index = "ABCD".index(correct)
                if correct_index >= len(options):
                    st.error(f"Option {correct} is empty — pick a correct answer that has text.")
                else:
                    st.session_state.draft_questions.append({
                        "text": q_text,
                        "options": options,
                        "correct_index": correct_index,
                    })
                    st.success("Question added.")

    if st.session_state.draft_questions:
        st.markdown("**Questions so far:**")
        for i, q in enumerate(st.session_state.draft_questions, 1):
            st.write(f"{i}. {q['text']}  —  ✅ {q['options'][q['correct_index']]}")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🗑️ Clear questions"):
            st.session_state.draft_questions = []
            st.rerun()
    with col2:
        if st.button("🚀 Launch quiz", type="primary", disabled=not (title and st.session_state.draft_questions)):
            code = create_session(title, st.session_state.draft_questions)
            st.session_state.host_code = code
            st.session_state.draft_questions = []
            st.session_state.draft_title = ""
            st.rerun()


def host_control_panel(code):
    session = get_session(code)
    if session is None:
        st.error("Session not found.")
        return

    questions = json.loads(session["questions_json"])
    idx = session["current_index"]
    revealed = session["revealed"]
    total = len(questions)

    st.subheader(f"🎛️ Hosting: {session['title']}")
    st.markdown(f"### Join code: `{code}`")
    st.caption("Share this code with your audience so they can join.")

    participants = get_participants(code)
    st.write(f"👥 **{len(participants)} joined**: " + (", ".join(participants) if participants else "—"))

    st.divider()

    colA, colB, colC = st.columns(3)
    with colA:
        if st.button("⏮️ Start / First question", disabled=(idx >= 0)):
            set_current_index(code, 0, revealed=0)
            st.rerun()
    with colB:
        if st.button("➡️ Next question", disabled=(idx >= total - 1)):
            set_current_index(code, idx + 1, revealed=0)
            st.rerun()
    with colC:
        if st.button("👁️ Reveal correct answer", disabled=(idx < 0)):
            set_revealed(code, 1)
            st.rerun()

    if idx < 0:
        st.info("Quiz hasn't started yet — click **Start / First question**.")
    else:
        q = questions[idx]
        st.markdown(f"#### Question {idx + 1} of {total}: {q['text']}")

        responses = get_responses(code, idx)
        counts = [0] * len(q["options"])
        for r in responses:
            if r["answer_index"] is not None and 0 <= r["answer_index"] < len(counts):
                counts[r["answer_index"]] += 1

        df = pd.DataFrame({"option": q["options"], "votes": counts})
        st.bar_chart(df.set_index("option"))

        st.write(f"📥 {len(responses)} / {len(participants)} responded")

        if revealed:
            st.success(f"✅ Correct answer: **{q['options'][q['correct_index']]}**")

    st.divider()
    all_df = get_all_results_df(code)
    if not all_df.empty:
        st.markdown("#### 📊 Full results")
        st.dataframe(all_df, use_container_width=True)
        st.download_button(
            "⬇️ Download results as CSV",
            data=all_df.to_csv(index=False).encode("utf-8"),
            file_name=f"quiz_{code}_results.csv",
            mime="text/csv",
        )

        # Leaderboard
        scored = all_df.groupby("participant")["is_correct"].sum().sort_values(ascending=False)
        st.markdown("#### 🏆 Leaderboard")
        st.dataframe(scored.reset_index().rename(columns={"is_correct": "correct_answers"}),
                     use_container_width=True)

    autorefresh(2000, key="host_refresh")


def participant_join():
    st.subheader("Join a quiz")
    code = st.text_input("Enter join code", max_chars=5)
    name = st.text_input("Your name")
    if st.button("Join", type="primary", disabled=not (code and name)):
        session = get_session(code)
        if session is None:
            st.error("No quiz found with that code.")
        else:
            join_session(code, name)
            st.session_state.participant_code = code
            st.session_state.participant_name = name
            st.rerun()


def participant_view(code, name):
    session = get_session(code)
    if session is None:
        st.error("Session ended or not found.")
        return

    questions = json.loads(session["questions_json"])
    idx = session["current_index"]
    revealed = session["revealed"]

    st.subheader(f"👋 Hi {name} — {session['title']}")

    if idx < 0:
        st.info("Waiting for the host to start the quiz…")
    elif idx >= len(questions):
        st.success("Quiz complete! Thanks for playing 🎉")
    else:
        q = questions[idx]
        st.markdown(f"### Q{idx + 1}: {q['text']}")

        existing = get_responses(code, idx)
        already_answered = any(r["participant_name"] == name for r in existing)

        if already_answered and not revealed:
            st.info("✅ Answer submitted — waiting for the host to move on.")
        elif revealed:
            my_answer = next((r["answer_index"] for r in existing if r["participant_name"] == name), None)
            correct = q["correct_index"]
            if my_answer == correct:
                st.success(f"You got it! ✅ {q['options'][correct]}")
            else:
                st.error(f"Correct answer: {q['options'][correct]}"
                         + (f" (you picked {q['options'][my_answer]})" if my_answer is not None else " (no answer submitted)"))
        else:
            choice = st.radio("Choose your answer:", q["options"], key=f"choice_{idx}")
            if st.button("Submit answer", type="primary"):
                answer_index = q["options"].index(choice)
                submit_answer(code, idx, name, answer_index)
                st.rerun()

    autorefresh(2000, key="participant_refresh")


# --------------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Live Quiz", page_icon="🧠", layout="centered")
    init_db()

    st.title("🧠 Live Quiz")

    role = st.sidebar.radio("I am a...", ["Host", "Participant"])

    if role == "Host":
        if "host_code" in st.session_state:
            if st.sidebar.button("➕ Create a different quiz"):
                del st.session_state["host_code"]
                st.rerun()
            host_control_panel(st.session_state.host_code)
        else:
            host_create_quiz()
    else:
        if "participant_code" in st.session_state:
            if st.sidebar.button("🚪 Leave quiz"):
                del st.session_state["participant_code"]
                del st.session_state["participant_name"]
                st.rerun()
            participant_view(st.session_state.participant_code, st.session_state.participant_name)
        else:
            participant_join()


if __name__ == "__main__":
    main()
