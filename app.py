"""RAGForge Streamlit UI: a thin client for the RAGForge FastAPI backend."""
import os
import time
import uuid
from datetime import datetime

import requests
import streamlit as st

API = os.getenv("RAGFORGE_API_URL", "http://localhost:8000/api/v1").rstrip("/")
STATUS_ICON = {"pending": "⏳", "processing": "⚙️", "indexed": "✅", "completed": "✅", "failed": "❌"}

st.set_page_config(page_title="RAGForge", page_icon="🔎", layout="wide")
st.markdown(
    """<style>
    #MainMenu, footer {visibility: hidden;}
    .block-container {padding-top: 2rem; max-width: 1200px;}
    .rf-title {font-size: 2rem; font-weight: 700; margin-bottom: 0;}
    .rf-sub {color: #6b7280; margin-bottom: 1rem;}
    </style>""",
    unsafe_allow_html=True,
)


class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def init_state():
    defaults = {
        "token": None, "email": None, "flash": None, "last_job_id": "",
        "conv_id": None, "conv_doc_id": None, "messages": [], "loaded_conv": None,
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


def logout(message=None):
    for k in ("token", "email", "conv_id", "conv_doc_id", "loaded_conv"):
        st.session_state[k] = None
    st.session_state.messages = []
    if message:
        st.session_state.flash = ("warning", message)


def call(method, path, *, json=None, params=None, files=None, timeout=60, auth=True):
    """Call the API, unwrap the {success, message, data, error} envelope."""
    headers = {}
    if auth and st.session_state.token:
        headers["Authorization"] = f"Bearer {st.session_state.token}"
    try:
        r = requests.request(method, f"{API}{path}", json=json, params=params,
                             files=files, headers=headers, timeout=timeout)
    except requests.RequestException as e:
        raise ApiError(f"Cannot reach the API at {API}. Is it running? ({type(e).__name__})") from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code == 401 and auth:
        logout("Your session expired. Please sign in again.")
        raise ApiError("Session expired. Please sign in again.", 401)
    if not r.ok or body.get("success") is False:
        msg = body.get("message") or body.get("detail") or r.text[:200] or f"HTTP {r.status_code}"
        raise ApiError(str(msg), r.status_code)
    return body.get("data")


def fmt_date(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d %b %Y, %H:%M")
    except Exception:
        return value or ""


def show_sources(sources):
    if not sources:
        return
    with st.expander(f"Sources ({len(sources)})"):
        for s in sources:
            with st.container(border=True):
                st.markdown(f"**[{s['citation']}] Page {s['page_number']}** · chunk {s['chunk_index']}")
                st.caption(s["content"])


def show_answer(result):
    if result["status"] == "insufficient_context":
        st.warning("Insufficient context: the document doesn't contain enough evidence to answer.")
    if result.get("answer"):
        st.markdown(result["answer"])
    show_sources(result.get("sources"))


# ---------- Auth ----------
def auth_sidebar():
    st.sidebar.markdown("### 🔎 RAGForge")
    tab_in, tab_up = st.sidebar.tabs(["Sign in", "Register"])
    with tab_in:
        email = st.text_input("Email", key="li_email")
        pw = st.text_input("Password", type="password", key="li_pw")
        if st.button("Sign in", type="primary", use_container_width=True):
            do_login(email, pw)
    with tab_up:
        email2 = st.text_input("Email", key="rg_email")
        pw2 = st.text_input("Password (12+ characters)", type="password", key="rg_pw")
        if st.button("Create account", use_container_width=True):
            if len(pw2) < 12:
                st.error("Password must be at least 12 characters.")
            else:
                try:
                    call("POST", "/auth/register", json={"email": email2, "password": pw2}, auth=False)
                    do_login(email2, pw2)
                except ApiError as e:
                    st.error(str(e))


def do_login(email, password):
    try:
        data = call("POST", "/auth/login", json={"email": email, "password": password}, auth=False)
        st.session_state.token = data["access_token"]
        st.session_state.email = email.strip().lower()
        st.rerun()
    except ApiError as e:
        st.sidebar.error(str(e))


# ---------- Tabs ----------
def poll_job(job_id, doc_id):
    deadline = time.time() + 300
    with st.status("Indexing document…", expanded=True) as status:
        while time.time() < deadline:
            job = call("GET", f"/ingestion-jobs/{job_id}")
            st.write(f"{STATUS_ICON.get(job['status'], '')} Job **{job['status']}** "
                     f"(attempt {job['attempts']}/{job['max_attempts']})")
            if job["status"] == "completed":
                status.update(label="Document indexed", state="complete")
                return "success", "Document indexed and ready for questions."
            if job["status"] == "failed":
                status.update(label="Indexing failed", state="error")
                return "error", "Indexing failed. Check that the PDF is text-based and within the size/page limits."
            time.sleep(2)
        status.update(label="Still processing", state="running")
    return "info", "Still processing. Refresh the list in a moment."


def documents_tab(docs):
    st.subheader("Upload a PDF")
    f = st.file_uploader("Text-based PDF (max 10 MiB, 100 pages)", type=["pdf"])
    if f and st.button("Upload & index", type="primary"):
        if f.size > 10 * 1024 * 1024:
            st.error("PDF size must not exceed 10 MiB.")
            return
        try:
            res = call("POST", "/uploads/pdf", files={"file": (f.name, f.getvalue(), "application/pdf")}, timeout=120)
            st.session_state.last_job_id = res["job_id"]
            st.session_state.flash = poll_job(res["job_id"], res["document_id"])
            st.rerun()
        except ApiError as e:
            st.error(str(e))

    st.subheader("Your documents")
    if st.button("Refresh"):
        st.rerun()
    if not docs:
        st.info("No documents yet. Upload a PDF to get started.")
    else:
        st.dataframe(
            [{"Name": d["name"], "Status": f"{STATUS_ICON.get(d['status'], '')} {d['status']}",
              "Chunks": d["chunk_count"], "Created": fmt_date(d["created_at"]), "ID": d["id"]} for d in docs],
            use_container_width=True, hide_index=True)

    with st.expander("Check an ingestion job"):
        job_id = st.text_input("Job ID", value=st.session_state.last_job_id)
        if st.button("Check status") and job_id:
            try:
                j = call("GET", f"/ingestion-jobs/{job_id.strip()}")
                st.write(f"{STATUS_ICON.get(j['status'], '')} **{j['status']}** · attempts {j['attempts']}/{j['max_attempts']}"
                         f" · updated {fmt_date(j['updated_at'])}")
            except ApiError as e:
                st.error(str(e))


def load_conversation(conv_id):
    data = call("GET", f"/conversations/{conv_id}/messages")
    st.session_state.conv_id = conv_id
    st.session_state.conv_doc_id = data["document_id"]
    st.session_state.messages = [{"role": m["role"], "content": m["content"]} for m in data["messages"]]
    st.session_state.loaded_conv = conv_id


def chat_tab(docs):
    names = {d["id"]: d["name"] for d in docs}
    indexed = [d for d in docs if d["status"] == "indexed"]
    left, right = st.columns([1, 3])
    with left:
        if st.button("＋ New conversation", use_container_width=True, type="primary"):
            st.session_state.update(conv_id=str(uuid.uuid4()), conv_doc_id=None, messages=[], loaded_conv=None)
            st.rerun()
        try:
            convs = call("GET", "/conversations", params={"limit": 50})
        except ApiError as e:
            convs = []
            st.error(str(e))
        for c in convs:
            label = ("▸ " if c["id"] == st.session_state.conv_id else "") + (c["title"] or "Conversation")[:40]
            if st.button(label, key=f"c_{c['id']}", use_container_width=True, help=names.get(c["document_id"], "")):
                try:
                    load_conversation(c["id"])
                    st.rerun()
                except ApiError as e:
                    st.error(str(e))

    with right:
        if not st.session_state.conv_id:
            st.info("Start a new conversation or pick one on the left.")
            return
        if st.session_state.conv_doc_id is None and not st.session_state.messages:
            if not indexed:
                st.warning("No indexed documents yet. Upload a PDF in the Documents tab.")
                return
            opts = {d["id"]: d["name"] for d in indexed}
            st.session_state.conv_doc_id = st.selectbox("Document", list(opts), format_func=opts.get)
        else:
            st.caption(f"📄 {names.get(st.session_state.conv_doc_id, st.session_state.conv_doc_id)}")

        for m in st.session_state.messages:
            with st.chat_message(m["role"]):
                if m["role"] == "assistant" and "result" in m:
                    if m.get("rewritten") and m["rewritten"] != m.get("question"):
                        st.caption(f"Searched as: {m['rewritten']}")
                    show_answer(m["result"])
                else:
                    st.markdown(m["content"])

        q = st.chat_input("Ask a question about the document")
        if q:
            with st.chat_message("user"):
                st.markdown(q)
            with st.chat_message("assistant"):
                with st.spinner("Thinking…"):
                    try:
                        res = call("POST", "/conversations/ask", timeout=180, json={
                            "document_id": st.session_state.conv_doc_id,
                            "conversation_id": st.session_state.conv_id, "question": q})
                    except ApiError as e:
                        st.error(str(e))
                        return
            st.session_state.messages += [
                {"role": "user", "content": q},
                {"role": "assistant", "content": res["result"]["answer"], "result": res["result"],
                 "rewritten": res["standalone_question"], "question": q}]
            st.rerun()


def search_tab(docs):
    indexed = {d["id"]: d["name"] for d in docs if d["status"] == "indexed"}
    if not indexed:
        st.info("Upload and index a PDF first.")
        return
    doc_id = st.selectbox("Document", list(indexed), format_func=indexed.get, key="sq_doc")
    mode = st.radio("Mode", ["Ask (grounded answer)", "Semantic search"], horizontal=True)
    q = st.text_input("Your question or query", key="sq_q")
    top_k = st.slider("Top K", 1, 20, 7) if mode == "Semantic search" else None
    if st.button("Run", type="primary") and q.strip():
        try:
            with st.spinner("Working…"):
                if top_k is None:
                    show_answer(call("POST", "/qa/ask", json={"document_id": doc_id, "question": q}, timeout=180))
                else:
                    data = call("POST", "/retrieval/search", json={"document_id": doc_id, "query": q, "top_k": top_k})
                    st.caption(f"{data['total']} result(s)")
                    for r in data["results"]:
                        with st.container(border=True):
                            st.markdown(f"**Page {r['page_number']}** · chunk {r['chunk_index']} · score {r['score']:.3f}")
                            st.write(r["content"])
        except ApiError as e:
            st.error(str(e))


def main():
    init_state()
    st.markdown('<div class="rf-title">RAGForge</div>'
                '<div class="rf-sub">Secure multi-user RAG · FastAPI · LangGraph · ChromaDB</div>',
                unsafe_allow_html=True)
    if st.session_state.flash:
        kind, text = st.session_state.flash
        getattr(st, kind)(text)
        st.session_state.flash = None
    if not st.session_state.token:
        auth_sidebar()
        st.info("Sign in or register in the sidebar to upload documents and ask questions.")
        return
    st.sidebar.markdown(f"**{st.session_state.email}**")
    st.sidebar.caption(f"API: {API}")
    if st.sidebar.button("Sign out"):
        logout()
        st.rerun()
    try:
        docs = call("GET", "/documents", params={"limit": 100}) or []
    except ApiError as e:
        st.error(str(e))
        return
    t1, t2, t3 = st.tabs(["📄 Documents", "💬 Chat", "🔎 Search & Q&A"])
    with t1:
        documents_tab(docs)
    with t2:
        chat_tab(docs)
    with t3:
        search_tab(docs)


main()
