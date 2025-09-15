import streamlit as st
from PyPDF2 import PdfReader
from sentence_transformers import SentenceTransformer, util
import torch
import re

def extract_text_from_pdf(uploaded_file):
    reader = PdfReader(uploaded_file)
    text = ""
    for page in reader.pages:
        page_text = page.extract_text()
        if page_text:
            text += page_text + "\n"
    return text

def split_into_chunks(text, min_length=30):
    raw_chunks = re.split(r"\n\s*\n", text)
    chunks = [chunk.strip() for chunk in raw_chunks if len(chunk.strip()) > min_length]
    return chunks

def find_changed_chunks(chunks_old, chunks_new, threshold=0.85):
    model = SentenceTransformer("all-MiniLM-L6-v2")
    emb_old = model.encode(chunks_old, convert_to_tensor=True)
    emb_new = model.encode(chunks_new, convert_to_tensor=True)

    changed = []
    for idx, new_chunk in enumerate(chunks_new):
        sim_scores = util.cos_sim(emb_new[idx], emb_old)
        max_sim = torch.max(sim_scores).item()
        if max_sim < threshold:
            changed.append({
                "chunk": new_chunk,
                "similarity": max_sim
            })
    return changed

# Streamlit UI
st.set_page_config(page_title="📄 PDF 버전 비교기", layout="wide")
st.title("📄 PDF 문서 버전 비교기 (드래그 앤 드롭 지원)")

st.markdown("""
드래그 앤 드롭으로 두 PDF 파일을 업로드하면,  
**신버전에서 바뀐 문장/문단만 자동으로 추출**해줍니다.
""")

col1, col2 = st.columns(2)
with col1:
    file_old = st.file_uploader("⬅️ 이전 버전 PDF 업로드", type=["pdf"], key="old")
with col2:
    file_new = st.file_uploader("➡️ 최신 버전 PDF 업로드", type=["pdf"], key="new")

threshold = st.slider("🔧 변경으로 판단할 최대 유사도 (낮을수록 민감)", 0.5, 0.95, 0.85, step=0.01)

if file_old and file_new and st.button("🔍 변경된 문장 비교 시작"):
    with st.spinner("1️⃣ 텍스트 추출 중..."):
        text_old = extract_text_from_pdf(file_old)
        text_new = extract_text_from_pdf(file_new)

    with st.spinner("2️⃣ 문단 분리 중..."):
        chunks_old = split_into_chunks(text_old)
        chunks_new = split_into_chunks(text_new)

    with st.spinner("3️⃣ 변경된 문장 추출 중..."):
        changed = find_changed_chunks(chunks_old, chunks_new, threshold)

    st.subheader("📌 변경된 문장/문단 결과")
    if changed:
        st.markdown(f"총 **{len(changed)}개** 문장이 바뀌었습니다.")
        for i, item in enumerate(changed):
            st.markdown(f"""
                <div style="background-color:#fff9f9;padding:12px;margin-bottom:15px;border-left:6px solid #ff4b4b;">
                <b>[{i+1}] 유사도: {item['similarity']:.2f}</b><br>
                {item['chunk']}
                </div>
            """, unsafe_allow_html=True)
    else:
        st.success("✨ 변경된 문장이 감지되지 않았습니다.")
