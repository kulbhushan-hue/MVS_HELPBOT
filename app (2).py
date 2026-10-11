"""
MVS Helper Chatbot - Streamlit UI + local Ollama (llama3:latest)

Run:
    pip install -r requirements.txt
    ollama pull llama3:latest        # once
    streamlit run app.py
"""

import html
import re
from datetime import datetime

import ollama
import streamlit as st

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
DEFAULT_MODEL = "llama3:latest"

SYSTEM_PROMPT = """You are an expert z/OS MVS systems programmer assistant.
The user will give you MVS / z/OS / JES2 / JCL / TSO / ISPF / VTAM / RACF /
VSAM / DFSMS issues or error messages (for example IEF450I, IEC030I, S0C7,
S322, S806, S0C4, IEF212I, JCL ERROR, HASP messages).

Rules for every answer:
1. Be exact and brief. No long theory.
2. Use this layout:
   **Meaning:** one or two lines.
   **Likely cause:** short bullet list.
   **Fix / Commands:** exact commands or JCL in fenced code blocks.
   **Verify:** how to confirm it is fixed.
3. For operator/console commands use real syntax, e.g. D A,L  /  D GRS,C  /
   D IPLINFO  /  $DJ  /  $PJ  /  C jobname  /  F jobname,cmd  /  D U,,,addr,1.
4. ALSO show what the user should see on the terminal. Put a simulated
   3270 screen in a fenced block that starts with ```screen  (not ```text).
   Keep it under 20 lines, 80 columns max, and clearly make it an example.
5. Never invent message IDs or commands. If unsure, say so and tell the user
   which manual / command to check (MVS System Messages, MVS System Codes,
   MVS System Commands).
6. If the question is not about mainframe / MVS, politely say you only help
   with MVS queries.
"""

# Small built-in cheat sheet. If the user's text contains one of these codes,
# the facts are injected into the prompt so the model stays accurate.
KNOWN_CODES = {
    "S0C7": "Data exception: packed/zoned decimal field holds invalid data. "
            "Check uninitialised COMP-3 fields, bad input records, wrong copybook/record layout.",
    "S0C4": "Protection exception: program touched storage it does not own. "
            "Check subscripts/table overflow, bad pointers, LINKAGE section/parm mismatch, missing CALL parameters.",
    "S322": "Job exceeded TIME limit (CPU). Check for loop; raise TIME= on JOB/EXEC card or use TIME=1440 / NOLIMIT only if allowed.",
    "S806": "Module not found (LINK/LOAD/XCTL/ATTACH). Check STEPLIB/JOBLIB/LNKLST, module name spelling, member in load library.",
    "S0C1": "Operation exception: invalid instruction executed. Often a missing/bad CALLed module or a branch into data.",
    "S913": "RACF security violation - not authorised for dataset. Check RACF profile / PERMIT.",
    "S222": "Job cancelled by operator or TSO user (no dump requested).",
    "S878": "Insufficient virtual storage in the region. Increase REGION= on JOB/EXEC.",
    "S0CB": "Division by zero (decimal/fixed point). Validate divisor before dividing.",
    "S013": "OPEN error: DCB attributes conflict or member not found in PDS. Check RECFM/LRECL/BLKSIZE/DSORG and member name.",
    "S37": "Out of space on dataset (Sx37 family: B37, D37, E37). Increase PRIMARY/SECONDARY, add volumes, or compress PDS.",
    "B37": "Out of space on volume / extents exhausted. Increase SPACE or add VOL=(,,,n).",
    "D37": "Primary space exhausted and no secondary specified. Add secondary allocation.",
    "E37": "No more space on volume or max extents reached. Add volumes or reallocate larger.",
    "IEF450I": "Abend in a job step. Message gives jobname, stepname, SYSTEM= (Sxxx) or USER= (Uxxxx) completion code and reason code.",
    "IEF212I": "Dataset not found (DD statement). Check DSN spelling, catalog (LISTCAT), and DISP=OLD on a missing dataset.",
    "IEF272I": "Step was not executed (previous step failure / COND). Check COND= and IF/THEN/ELSE logic.",
    "IEF285I": "Dataset kept/deleted/cataloged/uncataloged notification (informational, per DISP).",
    "IEC030I": "I/O error in dataset - B37/D37/E37 style space problem reported as IEC030I (e.g. B37-04 = out of extents).",
    "IEC031I": "D37 space abend message.",
    "IEC032I": "E37 space abend message.",
    "IEC141I": "013-xx OPEN abend message; reason code after the dash tells the exact cause (e.g. 013-18 = member not found).",
    "IGD17054I": "SMS: Allocation failed / dataset class issue. Check storage class, data class, volume availability.",
    "HASP": "JES2 message prefix. Use $D commands (e.g. $DJ, $DSPL, $DSPOOL) for status.",
    "IEF605I": "Unauthorised program/job attempted (not authorised for requested function).",
    "IEA995I": "SYMPTOM DUMP OUTPUT: system completion code and PSW/registers at time of abend. Read SYSTEM COMPLETION CODE and ABEND REASON.",
    "ICH408I": "RACF: user not authorised to access a resource. Message shows userid, resource name, and access needed.",
    "IEE136I": "LOCAL TIME and DATE display (response to D T).",
    "CSV003I": "Requested module not found - check STEPLIB/LNKLST.",
    "CSV028I": "Abend occurred during LOAD/LINK; check STEPLIB / LNKLST concatenation.",
}

QUICK_ISSUES = [
    "S0C7 abend in COBOL step",
    "S806-04 module not found",
    "B37 / D37 / E37 out of space",
    "IEF212I dataset not found",
    "JCL ERROR - how to find it",
    "Job stuck in input queue (JES2)",
    "ICH408I RACF not authorised",
    "How to cancel a hung job",
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def time_greeting() -> str:
    h = datetime.now().hour
    if 5 <= h < 12:
        return "Good Morning"
    if 12 <= h < 17:
        return "Good Afternoon"
    if 17 <= h < 21:
        return "Good Evening"
    return "Good Night"


def welcome_message() -> str:
    return (
        f"{time_greeting()}! 👋 I'm your **MVS Helper**.\n\n"
        "How can I help you with your **MVS queries** today?\n\n"
        "Paste an error message / abend code (e.g. `IEF450I`, `S0C7`, `S806`, `B37`) "
        "or describe the problem, and I'll give you the exact commands/JCL and "
        "a sample screen to compare with."
    )


def find_known_codes(text: str) -> str:
    """Return cheat-sheet facts for any known codes found in the user text."""
    up = text.upper()
    hits = []
    for code, info in KNOWN_CODES.items():
        if re.search(rf"(?<![A-Z0-9]){re.escape(code)}", up):
            hits.append(f"- {code}: {info}")
    return "\n".join(hits)


def render_reply(container, text: str) -> None:
    """Render markdown, turning ```screen blocks into a green-on-black 3270 panel."""
    parts = re.split(r"```screen[^\n]*\n(.*?)```", text, flags=re.S)
    with container.container():
        for i, part in enumerate(parts):
            if i % 2 == 0:
                if part.strip():
                    st.markdown(part)
            else:
                safe = html.escape(part.rstrip())
                st.markdown(
                    f"""
<div style="background:#000;border:2px solid #333;border-radius:6px;padding:10px 14px;">
  <div style="color:#7a7a7a;font-size:11px;margin-bottom:4px;">
    3270 SCREEN (sample - your system may differ slightly)
  </div>
  <pre style="margin:0;color:#33ff33;font-family:'Courier New',monospace;
              font-size:13px;line-height:1.25;white-space:pre;overflow-x:auto;">{safe}</pre>
</div>
""",
                    unsafe_allow_html=True,
                )


def stream_llm(messages, model: str, temperature: float):
    """Yield text chunks from the local Ollama server."""
    for chunk in ollama.chat(
        model=model,
        messages=messages,
        stream=True,
        options={"temperature": temperature},
    ):
        yield chunk["message"]["content"]


def build_messages(history, user_text: str):
    facts = find_known_codes(user_text)
    system = SYSTEM_PROMPT
    if facts:
        system += "\nVerified reference facts for codes in the user's message:\n" + facts
    msgs = [{"role": "system", "content": system}]
    # keep the last 10 turns for context
    for m in history[-10:]:
        msgs.append({"role": m["role"], "content": m["content"]})
    msgs.append({"role": "user", "content": user_text})
    return msgs


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
st.set_page_config(page_title="MVS Helper Chatbot", page_icon="🖥️", layout="wide")

st.markdown(
    "<h2 style='margin-bottom:0'>🖥️ MVS Helper Chatbot</h2>"
    "<span style='color:gray'>z/OS • JCL • JES2 • abends • console commands — powered by local Ollama</span>",
    unsafe_allow_html=True,
)

# ---- sidebar ----
with st.sidebar:
    st.header("⚙️ Settings")
    model_name = st.text_input("Ollama model", value=DEFAULT_MODEL)
    temperature = st.slider("Temperature (lower = more exact)", 0.0, 1.0, 0.1, 0.05)

    st.divider()
    st.subheader("⚡ Quick issues")
    for q in QUICK_ISSUES:
        if st.button(q, use_container_width=True, key=f"q_{q}"):
            st.session_state["pending"] = q

    st.divider()
    if st.button("🗑️ Clear chat", use_container_width=True):
        st.session_state.pop("messages", None)
        st.session_state.pop("pending", None)
        st.rerun()

    st.caption("Tip: paste the exact message text from SYSLOG / JESMSGLG / SDSF.")

# ---- first load: welcome message ----
if "messages" not in st.session_state:
    st.session_state["messages"] = [{"role": "assistant", "content": welcome_message()}]

# ---- show history ----
for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"], avatar="🖥️" if msg["role"] == "assistant" else "🧑‍💻"):
        render_reply(st.empty(), msg["content"])

# ---- input ----
typed = st.chat_input("Type your MVS issue or paste an error message…")
user_text = typed or st.session_state.pop("pending", None)

if user_text:
    with st.chat_message("user", avatar="🧑‍💻"):
        st.markdown(user_text)

    history = list(st.session_state["messages"])
    st.session_state["messages"].append({"role": "user", "content": user_text})

    with st.chat_message("assistant", avatar="🖥️"):
        placeholder = st.empty()
        full = ""
        try:
            for piece in stream_llm(build_messages(history, user_text), model_name, temperature):
                full += piece
                placeholder.markdown(full + "▌")
            render_reply(placeholder, full)
        except Exception as exc:  # Ollama not running / model missing
            full = (
                f"⚠️ Could not reach Ollama or the model `{model_name}`.\n\n"
                f"`{exc}`\n\n"
                "Check that Ollama is running (`ollama serve`) and that the model is "
                f"installed (`ollama pull {model_name}`)."
            )
            placeholder.error(full)

    st.session_state["messages"].append({"role": "assistant", "content": full})
